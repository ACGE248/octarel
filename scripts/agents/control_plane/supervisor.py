"""Process supervision: launch a delegated worker task and track its lifetime.

A task is executed by spawning ``python -m scripts.agents.orchestrate run ...``
as a subprocess (ENG-AGENT-01's existing single-shot CLI is imported/reused,
never re-implemented) so the daemon can track a real PID independently of the
worker's own execution, and so a crashing/misbehaving worker can never take the
daemon process down with it. Git-worktree write safety is enforced twice, by
design: ``runner.assert_write_safety``/``write_lock`` are the real enforcement
inside the spawned subprocess (via ``orchestrate.run_delegation``); this module
also runs the same check *before* spawning so an obviously unsafe launch is
recorded as ``BLOCKED`` immediately rather than burning a subprocess.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..redaction import redact_text
from ..registry import Registry, RegistryError
from ..runner import (
    WriteSafetyError,
    assert_write_safety,
    current_branch,
    sanitized_subprocess_env,
)
from .models import (
    KIND_WRITE,
    LAUNCH_SESSION,
    PERMISSION_STANDARD,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Task,
    utc_now_iso,
)
from .project import cp_code_root
from .recovery import pid_is_alive
from .state import State

_AGENT_OUTPUT_DIRNAME = ".agent-output"


class SupervisorError(RuntimeError):
    pass


class Supervisor:
    """Owns the daemon's live process table for tasks it has launched itself.

    Tasks recovered from a prior daemon run (only a PID on disk, no live
    ``Popen`` handle) are reconciled by ``recovery.py`` instead, using PID
    liveness rather than this class's process table.
    """

    def __init__(self, *, registry: Registry, repo_root: Path, state: State) -> None:
        self.registry = registry
        self.repo_root = repo_root
        self.state = state
        self._processes: dict[str, subprocess.Popen] = {}
        self._owned_process_groups: set[int] = set()

    def _spawn(self, argv: list[str], *, cwd: Path) -> subprocess.Popen:
        """Isolated so tests can substitute a fake process without a real CLI.

        ENG-CP-02 (issue #165): this subprocess acts on ``cwd`` -- a task's
        own worktree, never necessarily this daemon process's own canonical
        checkout -- so it must never inherit this process's own
        orchestration-only environment (e.g. the ``OCTAGES_ORCH_CANONICAL_REPO_ROOT``
        override the daemon may be running under, ENG-AGENT-12). Defense in
        depth: ``orchestrate.py`` itself resolves its own ``--repo-root``
        explicitly and does not read that variable today, and the actual
        worker CLI further downstream (``runner.run_worker_process``) already
        uses its own minimal environment allowlist -- but this spawn is the
        one place a future addition to either could otherwise silently
        reintroduce the exact contamination class ENG-AGENT-12/issue #161
        already fixed once for the local gate's own phase subprocesses.
        """

        return subprocess.Popen(  # noqa: S603 - argv is built from validated registry/task data
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=sanitized_subprocess_env(),
            start_new_session=True,
        )

    def _terminate_owned_process(self, process: subprocess.Popen, *, grace_seconds: float = 3.0) -> bool:
        """Terminate one process group proven owned by this Supervisor instance."""

        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return False
        poll = getattr(process, "poll", None)
        wait = getattr(process, "wait", None)
        # Membership is recorded immediately after a successful spawn only
        # when the child is verifiably its own session/process-group leader.
        # It remains proof after the leader exits, when descendants may still
        # hold the group alive but ``getpgid(leader_pid)`` no longer works.
        if pid not in self._owned_process_groups:
            return False
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while callable(poll) and poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if callable(poll) and poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        if callable(wait):
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                wait(timeout=1.0)
        self._owned_process_groups.discard(pid)
        return True

    def terminate_task(self, task_id: str) -> bool:
        """Cancel only a subprocess launched and owned by this instance."""

        process = self._processes.get(task_id)
        if process is None:
            return False
        if not self._terminate_owned_process(process):
            return False
        task = self.state.get_task(task_id)
        if task is not None:
            task.state = TASK_CANCELLED
            task.result = "CANCELLED"
            task.pid = None
            self.state.upsert_task(task)
        self._processes.pop(task_id, None)
        return True

    def shutdown_all(self) -> int:
        """Reap every child this instance owns on shutdown or interruption."""

        task_ids = list(self._processes)
        for task_id in task_ids:
            self.terminate_task(task_id)
        return len(task_ids)

    def _build_argv(self, task: Task, *, dry_run: bool) -> list[str]:
        prompt = [
            c.removeprefix("literal:") if c.startswith(("literal:scope:", "literal:base:")) else c
            for c in task.command
            if not (c.startswith("scope:") or c.startswith("base:"))
        ]
        if task.launch_mode == LAUNCH_SESSION:
            # A runbook session covers the whole worktree by design (issue
            # #93): ``orchestrate run``'s mandatory --scope narrowing does not
            # apply, so this goes through the sibling ``session`` subcommand
            # instead (still the same registry/write-lock/audit machinery).
            argv = [
                sys.executable,
                "-m",
                "scripts.agents.orchestrate",
                "session",
                "--task",
                task.task_ref,
                "--repo-root",
                str(Path(task.worktree) if task.worktree else self.repo_root),
                "--worker",
                task.worker,
                "--role",
                task.role,
                "--why",
                f"orchestrator daemon launched runbook session {task.id} ({task.task_ref}/{task.role})",
            ]
            if task.timeout_seconds:
                argv += ["--timeout", str(task.timeout_seconds)]
            if task.permission_profile and task.permission_profile != PERMISSION_STANDARD:
                argv += ["--permission-profile", task.permission_profile]
            if task.fallback_reason:
                argv += ["--fallback-reason", task.fallback_reason]
            if dry_run:
                argv.append("--dry-run")
            if prompt:
                argv += ["--", *prompt]
            return argv

        argv = [
            sys.executable,
            "-m",
            "scripts.agents.orchestrate",
            "run",
            "--task",
            task.task_ref,
            "--repo-root",
            str(Path(task.worktree) if task.worktree else self.repo_root),
            "--worker",
            task.worker,
            "--role",
            task.role,
            "--why",
            f"orchestrator daemon scheduled task {task.id} ({task.task_ref}/{task.role})",
        ]
        for scope in [c for c in task.command if c.startswith("scope:")]:
            argv += ["--scope", scope.removeprefix("scope:")]
        # `orchestrate run` never accepts --permission-profile (Grok Build review, issue
        # #94): the unattended profile is only reachable through a LAUNCH_SESSION task's
        # `session` argv above. An ordinary delegated task's permission_profile is always
        # PERMISSION_STANDARD by construction, so this is not reached in practice today.
        if task.role == "diff-review":
            # ENG-AGENT-13 (issue #138): ``run_delegation`` itself requires
            # ``--include-diff`` for every diff-review role so the read-only
            # reviewer gets a bounded, redacted, exact-tree scoped diff
            # instead of open worktree write access -- never omit it here.
            argv.append("--include-diff")
            # ENG-AGENT-18 (issue #155): a clean, already-committed candidate
            # has no working-tree/index diff, so ``run_delegation`` needs the
            # configured base ref to build a merge-base diff instead. The
            # acceptance pipeline encodes it as a single ``base:<ref>`` command
            # entry (mirroring ``scope:<path>``); pass it through when present
            # and otherwise let ``orchestrate run``'s own default apply.
            base_entries = [c.removeprefix("base:") for c in task.command if c.startswith("base:")]
            if base_entries:
                argv += ["--diff-base", base_entries[-1]]
        if dry_run:
            argv.append("--dry-run")
        if prompt:
            argv += ["--", *prompt]
        return argv

    def launch_task(self, task: Task, *, dry_run: bool = False) -> Task:
        """Launch ``task`` (or record why it cannot run yet). Mutates and persists it."""

        try:
            worker = self.registry.get(task.worker)
        except RegistryError as exc:
            task.state = TASK_BLOCKED
            task.last_error = f"unknown worker: {exc}"
            self.state.upsert_task(task)
            self.state.record_event(category="supervisor", task_id=task.id, level="error", message=task.last_error)
            return task

        worktree = Path(task.worktree) if task.worktree else self.repo_root
        if worker.is_write_capable:
            try:
                lock_dir = worktree / _AGENT_OUTPUT_DIRNAME
                assert_write_safety(worker, worktree, allow_write=True, lock_dir=lock_dir)
            except WriteSafetyError as exc:
                task.state = TASK_BLOCKED
                task.last_error = f"write-safety pre-check failed: {exc}"
                self.state.upsert_task(task)
                self.state.record_event(
                    category="supervisor", task_id=task.id, level="warning", message=task.last_error
                )
                return task
            except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - git unavailable/odd env
                task.state = TASK_BLOCKED
                task.last_error = f"could not verify worktree safety: {exc}"
                self.state.upsert_task(task)
                return task

        argv = self._build_argv(task, dry_run=dry_run)
        # Execute the wrapper from the Octarel/Control Center code checkout so
        # ``scripts.agents`` exists even when the selected project worktree is
        # a different repository. ``--repo-root`` still targets the task worktree.
        process = self._spawn(argv, cwd=cp_code_root())
        try:
            if os.getpgid(process.pid) == process.pid:
                self._owned_process_groups.add(process.pid)
        except (AttributeError, ProcessLookupError, PermissionError, OSError):
            pass
        self._processes[task.id] = process
        task.pid = process.pid
        task.worktree = str(worktree)
        task.state = TASK_RUNNING
        task.stale_recovered = False
        task.last_error = None
        self.state.upsert_task(task)
        self.state.record_event(
            category="supervisor",
            task_id=task.id,
            level="info",
            message=f"launched pid={process.pid} worker={task.worker} dry_run={dry_run}",
        )
        return task

    def poll_once(self) -> list[Task]:
        """Check every process this instance launched; update+persist any that finished.

        Never raises: a failure while reconciling one task is recorded as an
        event and does not stop the remaining tasks from being checked, so a
        single bad interaction can never kill the daemon loop.
        """

        finished: list[Task] = []
        for task_id, process in list(self._processes.items()):
            try:
                return_code = process.poll()
                if return_code is None:
                    continue
                task = self.state.get_task(task_id)
                if task is None:  # pragma: no cover - task deleted mid-flight
                    del self._processes[task_id]
                    continue
                if task.state == TASK_CANCELLED:
                    task.result = "CANCELLED"
                else:
                    stdout, stderr = self._completed_output(process)
                    task.state = TASK_SUCCEEDED if return_code == 0 else TASK_FAILED
                    task.result = "PASS" if return_code == 0 else "FAIL"
                    if return_code != 0:
                        task.last_error = self._failure_reason(task, stdout, stderr, return_code)
                        # A manifest pointer proves the delegation wrapper got
                        # far enough to evaluate/invoke the worker. Import,
                        # validation and other wrapper failures must not be
                        # misattributed to the provider.
                        if re.search(r"^MANIFEST:\s+", stdout, re.MULTILINE):
                            self._record_worker_failure(task, task.last_error)
                            self._attribute_worker_failure(task, task.last_error)
                            # ``None`` is the durable signal that this newly
                            # classified provider failure still needs one
                            # bounded automatic fallback decision.
                            task.fallback_alternatives = ()
                            task.fallback_selected_worker = None
                            task.fallback_automatic = None
                    self._record_usage(task, stdout)
                    self._record_route_outcome(task)
                self.state.upsert_task(task)
                self.state.record_event(
                    category="supervisor",
                    task_id=task.id,
                    level="info" if return_code == 0 else "error",
                    message=f"pid={task.pid} exited {return_code}",
                )
                finished.append(task)
                # The wrapper is done, but a misbehaving descendant (browser,
                # test server, provider helper) may still hold the owned
                # process group. Reap it on both success and failure.
                self._terminate_owned_process(process, grace_seconds=0.2)
                del self._processes[task_id]
            except Exception as exc:  # noqa: BLE001 - a bad reconcile must never kill the daemon
                self.state.record_event(
                    category="supervisor", task_id=task_id, level="error", message=f"poll error: {exc}"
                )
        return finished

    def _record_usage(self, task: Task, stdout: str) -> None:
        if not task.runbook_id:
            return
        record = self.state.get_usage_governance(task.runbook_id)
        if not record:
            return
        from .telemetry import estimated_token_usage, extract_token_usage

        payload = stdout
        pointer = re.search(r"^LOG:\s+(.+)$", stdout, re.MULTILINE)
        if pointer and task.worktree:
            target_root = Path(task.worktree).resolve()
            log_path = (target_root / pointer.group(1).strip()).resolve()
            evidence_root = (target_root / _AGENT_OUTPUT_DIRNAME).resolve()
            if log_path.is_relative_to(evidence_root) and log_path.is_file():
                try:
                    payload = log_path.read_text(encoding="utf-8")
                except OSError:
                    pass
        usage = extract_token_usage(payload)
        if usage.mode == "UNKNOWN" and payload.strip():
            # Explicitly labelled approximation only; never presented as a
            # provider-reported count. Four characters/token is a bounded
            # local preprocessing heuristic, not billing/quota truth.
            usage = estimated_token_usage(
                input_tokens=max(1, sum(len(value) for value in task.command) // 4),
                output_tokens=max(1, len(payload) // 4),
            )
        record["telemetry_quality"] = usage.mode.lower()
        record["input_tokens"] = usage.input_tokens
        record["output_tokens"] = usage.output_tokens
        self.state.upsert_usage_governance(record)

    def _record_route_outcome(self, task: Task) -> None:
        """Finalize the current durable route attempt without erasing history."""

        if not task.runbook_id:
            return
        record = self.state.get_usage_governance(task.runbook_id)
        if not record:
            return
        history = list(record.get("route_history", []))
        for index in range(len(history) - 1, -1, -1):
            attempt = history[index]
            if not isinstance(attempt, dict) or attempt.get("worker") != task.worker:
                continue
            if attempt.get("status") not in {None, "STARTING", "RUNNING"}:
                continue
            outcome = {
                **attempt,
                "status": task.state,
                "result": task.result,
                "ended_at": utc_now_iso(),
            }
            if task.state == TASK_FAILED:
                outcome.update(
                    {
                        "failure_category": task.failure_category or "UNKNOWN",
                        "failure_reason": task.failure_reason_sanitized or task.last_error,
                        "failure_provider": task.failure_provider,
                        "failure_model": task.failure_model,
                    }
                )
            history[index] = outcome
            record["route_history"] = history
            self.state.upsert_usage_governance(record)
            return

    @staticmethod
    def _completed_output(process: subprocess.Popen) -> tuple[str, str]:
        """Collect the wrapper's bounded pointer/stderr after it has exited."""

        communicate = getattr(process, "communicate", None)
        if not callable(communicate):
            return "", ""
        try:
            # ``poll()`` proving process exit does not guarantee its pipe
            # reader has observed EOF in the same scheduler tick.  A zero
            # timeout intermittently discarded the wrapper's already-written
            # MANIFEST pointer, reducing a real quota reason to a generic exit
            # code.  Bound the drain so reconciliation still cannot hang.
            stdout, stderr = communicate(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            return "", ""
        return str(stdout or "")[-8192:], str(stderr or "")[-8192:]

    def _failure_reason(self, task: Task, stdout: str, stderr: str, return_code: int) -> str:
        """Return one short redacted reason, preferring durable manifest evidence."""

        pointer = re.search(r"^MANIFEST:\s+(.+)$", stdout, re.MULTILINE)
        if pointer and task.worktree:
            target_root = Path(task.worktree).resolve()
            manifest_path = (target_root / pointer.group(1).strip()).resolve()
            evidence_root = (target_root / _AGENT_OUTPUT_DIRNAME).resolve()
            if manifest_path.is_relative_to(evidence_root) and manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    notes = manifest.get("notes") or []
                    if notes:
                        return redact_text(str(notes[-1])).strip()[:800]
                except (OSError, json.JSONDecodeError, TypeError):
                    pass

        cleaned = redact_text(stderr or stdout).strip()
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        if lines:
            return lines[-1][:800]
        return f"worker subprocess exited {return_code} without diagnostic output"

    def _record_worker_failure(self, task: Task, reason: str) -> None:
        """Update provider routing truth from a sanitized launch/worker failure."""

        from .provider_state import classify_failure

        provider = self.state.get_provider_state(task.worker)
        if provider is None:
            return
        provider.state = classify_failure(reason)
        provider.consecutive_failures += 1
        provider.last_error = reason
        self.state.upsert_provider_state(provider)

    def _attribute_worker_failure(self, task: Task, reason: str) -> None:
        from .provider_state import failure_attribution

        worker = self.registry.workers.get(task.worker)
        facts = failure_attribution(worker=worker, reason=reason)
        task.failed_worker_id = facts["worker_id"]
        task.failure_execution_system = facts["execution_system"]
        task.failure_provider = facts["provider"]
        task.failure_model = facts["model"]
        task.failure_category = facts["category"]
        task.failure_reason_sanitized = facts["reason"]
        task.failure_reset = facts["reset"]
        task.failure_reset_source = facts["reset_source"]

    def reconcile_recovered_once(self) -> list[Task]:
        """Fail closed when a recovered, non-child PID later disappears."""

        finished: list[Task] = []
        for task in self.state.list_tasks(state=TASK_RUNNING):
            if task.id in self._processes or pid_is_alive(task.pid):
                continue
            task.state = TASK_FAILED
            task.result = "FAIL"
            task.last_error = "recovered worker PID exited; final outcome unavailable"
            self.state.upsert_task(task)
            self.state.record_event(
                category="supervisor",
                task_id=task.id,
                level="error",
                message=task.last_error,
            )
            finished.append(task)
        return finished

    def live_task_ids(self) -> frozenset[str]:
        return frozenset(self._processes)

    def current_branch_is_protected(self, worktree: Path) -> bool:
        from ..runner import PROTECTED_BRANCHES

        try:
            return current_branch(worktree) in PROTECTED_BRANCHES
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return False

    def slot_kind_of(self, worker_name: str) -> str:
        """Best-effort concurrency-class inference from the registry worker."""

        try:
            worker = self.registry.get(worker_name)
        except RegistryError:
            return KIND_WRITE
        return KIND_WRITE if worker.is_write_capable else "read"
