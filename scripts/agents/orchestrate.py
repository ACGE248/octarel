#!/usr/bin/env python3
"""Provider-neutral local delegation orchestrator (``ENG-AGENT-01``).

Runs exactly one delegated worker per invocation, records a redacted auditable
manifest plus a concise summary under
``.agent-output/<TASK-ID>/<worker>/<RUN-ID>/``, and prints only a compact pointer
block to stdout.

This tool never runs in CI, never makes product/provider API calls of its own,
never commits or pushes, and never falls back to a paid worker when a cheaper one
fails or is unavailable.

Usage::

    python -m scripts.agents.orchestrate list-workers
    python -m scripts.agents.orchestrate route --role focused-tests
    python -m scripts.agents.orchestrate run --task ENG-AGENT-01 \\
        --worker antigravity-focused-tests --role focused-tests \\
        --intensity low --dry-run -- "run tests/test_agents_orchestration.py"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time as _time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .manifest import (
    RESULT_BLOCKED,
    RESULT_DRY_RUN,
    RESULT_FAIL,
    RESULT_PASS,
    RESULT_UNSUPPORTED,
    RunRecord,
    compact_pointer,
    write_artifacts,
)
from .policy import PolicyError, compose_policy_bundle
from .redaction import redact_text
from .registry import (
    KNOWN_PERMISSION_PROFILES,
    PERMISSION_STANDARD,
    Registry,
    RegistryError,
    load_registry,
)

try:
    from scripts.ci.reviewer_presets import ensure_opencode_agent_preset_for_command
except ModuleNotFoundError:  # pragma: no cover - always importable as a package in this repo
    from ci.reviewer_presets import (
        ensure_opencode_agent_preset_for_command,  # type: ignore
    )
from .runner import (
    WriteSafetyError,
    assert_write_safety,
    repo_root,
    run_worker_process,
    structured_actual_model,
    structured_failure,
    worktree_snapshot,
    write_lock,
)
from .validation import ValidationError, validate_scope_path, validate_task_id

# Exit codes used by ``run``.
EXIT_OK = 0
EXIT_WORKER_FAILED = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED_OR_BLOCKED = 3

_AGENT_OUTPUT_DIRNAME = ".agent-output"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class DelegationResult:
    record: RunRecord
    manifest: dict
    exit_code: int


def agent_output_dir(root: Path, task: str, worker: str, run_id: str) -> Path:
    return root / _AGENT_OUTPUT_DIRNAME / task / worker / run_id


def _agent_preset_block_reason(root: Path, command: list[str]) -> str | None:
    """``None`` when ``command`` is safe to launch; a blocked-worker note otherwise.

    issue #148: a worker whose CLI template names a ``--agent`` preset (e.g.
    OpenCode2's diff-review route) must never silently substitute a default
    agent because ``root`` (often a candidate worktree on an old branch)
    lacks that preset file -- self-heal it from this running code's own
    canonical checkout first, and fail closed, never silently, when even
    that canonical copy does not exist. Shared by every worker-launch path
    (``run_delegation`` and ``run_session``) so a future change to this
    check or its message applies identically everywhere.
    """

    if ensure_opencode_agent_preset_for_command(root, command):
        return None
    return (
        f"command {command!r} names an agent preset that is not available on {root} and could not be "
        "self-healed from this checkout's own .opencode/agents/ -- refusing to launch a worker that "
        "would silently fall back to an unconfigured default agent"
    )


def run_delegation(
    *,
    registry: Registry,
    root: Path,
    task: str,
    worker_name: str,
    role: str,
    model: str | None,
    intensity: str | None,
    why: str,
    prompt_args: list[str],
    scope_paths: list[str],
    dry_run: bool,
    allow_write: bool,
    allow_overflow: bool,
    timeout: float | None,
    tests_or_checks: list[str] | None = None,
    include_diff: bool = False,
    diff_base: str = "origin/main",
    permission_profile: str = PERMISSION_STANDARD,
    contract_paths: list[str] | None = None,
    workflow: str | None = None,
    fallback_reason: str | None = None,
) -> DelegationResult:
    """Execute (or plan) a single delegated worker run and persist its manifest."""

    validate_task_id(task)
    if not scope_paths:
        raise ValidationError("at least one narrow --scope path is required")
    # A diff-review scope is only ever used to build a bounded git-diff
    # pathspec (below) and an informational access line for the read-only
    # reviewer's prompt -- never a filesystem read -- so a path the candidate
    # diff deletes must still resolve here instead of being rejected as
    # nonexistent (issue #144).
    resolved_scopes = [
        validate_scope_path(raw, root, require_exists=(role != "diff-review")).relative_to(root).as_posix()
        for raw in scope_paths
    ]

    worker = registry.get(worker_name)
    if role not in worker.roles:
        raise ValidationError(f"worker {worker_name!r} does not declare role {role!r}")
    if allow_write and not worker.is_write_capable:
        raise ValidationError(f"--allow-write cannot upgrade read-only worker {worker_name!r}")
    if role == "diff-review" and not include_diff:
        raise ValidationError("diff-review requires --include-diff so the reviewer receives bounded current context")
    if permission_profile != PERMISSION_STANDARD:
        # `run` is ENG-AGENT-01's general narrow-scope mechanical-delegation verb, used for
        # many roles unrelated to Runbooks; it must never be a second way to reach an
        # unattended profile. Only the `session` verb (ENG-AGENT-02-S5's whole-worktree
        # Runbook mechanism, see `run_session` below) may accept a non-standard profile
        # (Grok Build independent review, issue #94).
        raise ValidationError(
            f"permission_profile {permission_profile!r} is not supported by the 'run' verb; "
            "the unattended profile is only available through the 'session' verb"
        )
    resolved_intensity = intensity or worker.default_intensity
    if resolved_intensity not in registry.intensities:
        raise ValidationError(
            f"invalid intensity {resolved_intensity!r}; expected one of {', '.join(registry.intensities)}"
        )

    user_prompt = " ".join(prompt_args).strip()
    if not user_prompt:
        raise ValidationError("worker prompt must not be empty")
    if not why.strip():
        raise ValidationError("--why is required for delegated-worker audit evidence")
    access = "read-only; do not modify any file" if worker.is_read_only else "write only within the authorized scope"
    task_prompt = (
        f"\n--- TASK ENVELOPE ---\nTask: {task}. Route: {role}. "
        f"Other repository access is limited to: {', '.join(resolved_scopes)}. Access: {access}. "
        "Do not read .env, data/, credential/secret files, or unrelated paths. Do not expose secrets, "
        "make live or billable provider calls, commit, push, or weaken tests. "
        f"Requested work: {user_prompt}"
    )
    bundle = compose_policy_bundle(
        root=root,
        registry=registry,
        worker_name=worker_name,
        route_role=role,
        contracts=contract_paths or (),
        workflow=workflow,
        fallback_reason=fallback_reason,
        acceptance_criteria=user_prompt,
    )
    policy_manifest = dict(bundle.manifest)
    policy_manifest["actual_worker"] = worker.name
    policy_manifest["actual_execution_system"] = worker.execution_system
    policy_manifest["actual_provider"] = worker.provider
    policy_manifest["actual_model"] = model or worker.default_model
    policy_manifest["actual_intensity"] = resolved_intensity
    bounded_prompt = bundle.prompt + task_prompt
    if include_diff:
        if not worker.is_read_only:
            raise ValidationError("--include-diff is limited to read-only workers")
        candidate_tree_sha = subprocess.run(
            ["git", "write-tree"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        try:
            diff_result = subprocess.run(
                ["git", "diff", "HEAD", candidate_tree_sha, "--no-ext-diff", "--", *resolved_scopes],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ValidationError(f"unable to build scoped review diff: {exc.stderr.strip()}") from None
        scoped_diff = redact_text(diff_result.stdout)
        if not scoped_diff.strip():
            # ENG-AGENT-18 (issue #155): an empty working-tree/index diff
            # against HEAD does not mean there is nothing to review -- a
            # clean, already-committed feature candidate has no uncommitted
            # changes by construction. Fall back to the bounded committed
            # diff of HEAD against this candidate's base-ref merge-base
            # (mirroring scripts/ci/local_gate.py's own committed-candidate
            # path) before concluding there is genuinely nothing to review.
            try:
                merge_base = subprocess.run(
                    ["git", "merge-base", diff_base, "HEAD"], cwd=root, capture_output=True, text=True, check=True
                ).stdout.strip()
            except subprocess.CalledProcessError as exc:
                raise ValidationError(
                    f"unable to resolve review base {diff_base!r}: {exc.stderr.strip()}"
                ) from None
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
            ).stdout.strip()
            if merge_base != head_sha:
                try:
                    diff_result = subprocess.run(
                        ["git", "diff", merge_base, "HEAD", "--no-ext-diff", "--", *resolved_scopes],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    raise ValidationError(
                        f"unable to build scoped committed-candidate review diff: {exc.stderr.strip()}"
                    ) from None
                scoped_diff = redact_text(diff_result.stdout)
        if not scoped_diff.strip():
            # Neither the working tree/index nor the committed branch history
            # (relative to diff_base) carries a scoped product change: a
            # genuinely no-change candidate, which must still fail closed
            # rather than being marked reviewable.
            raise ValidationError("--include-diff found no scoped changes; expose untracked files with git add -N before review")
        if len(scoped_diff.encode("utf-8")) > 200_000:
            raise ValidationError("scoped diff exceeds the 200 KB review-context limit; narrow --scope")
        bounded_prompt += (
            "\n\nThe scoped diff below is redacted before provider transmission. Secret-shaped test fixtures "
            "or assignments may appear as placeholders; do not treat those placeholders as source edits."
            "\n```diff\n" + scoped_diff + "\n```"
        )
    else:
        candidate_tree_sha = None
    command = worker.build_command(
        model=model, intensity=resolved_intensity, prompt=bounded_prompt, permission_profile=permission_profile
    )
    started_at = _now()
    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker_dir = agent_output_dir(root, task, worker_name, run_id)
    worker_dir.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []
    if permission_profile != PERMISSION_STANDARD:
        notes.append(f"launched with non-standard permission_profile={permission_profile}")

    record = RunRecord(
        task=task,
        role=role,
        worker=worker_name,
        planned_execution_system=worker.execution_system,
        planned_provider=worker.provider,
        planned_model=model or worker.default_model,
        planned_intensity=resolved_intensity,
        requested_command=command,
        why_this_worker=why,
        started_at=started_at,
        notes=notes,
        tests_or_checks=list(tests_or_checks or []),
        files_changed=[],
        candidate_tree_sha=candidate_tree_sha,
        policy_manifest=policy_manifest,
    )

    def finish(log_text: str) -> DelegationResult:
        record.finished_at = _now()
        paths = write_artifacts(worker_dir, record, log_text=log_text, repo_root=root)
        manifest = record.to_manifest(paths=paths)
        exit_code = {
            RESULT_PASS: EXIT_OK,
            RESULT_DRY_RUN: EXIT_OK,
            RESULT_FAIL: EXIT_WORKER_FAILED,
            RESULT_UNSUPPORTED: EXIT_UNSUPPORTED_OR_BLOCKED,
            RESULT_BLOCKED: EXIT_UNSUPPORTED_OR_BLOCKED,
        }[record.result]
        return DelegationResult(record=record, manifest=manifest, exit_code=exit_code)

    # Overflow / disabled workers are never used implicitly.
    if not worker.enabled and not allow_overflow:
        record.result = RESULT_BLOCKED
        record.notes.append(
            f"{worker_name} is disabled in the registry (cost_class={worker.cost_class}); "
            "pass --allow-overflow and confirm authorization. No automatic paid fallback is performed."
        )
        return finish("")

    # A dry run previews the (redacted) command without executing it, even when
    # the worker CLI is not installed locally yet.
    if dry_run:
        record.result = RESULT_DRY_RUN
        record.notes.append("dry run: command was built and validated but not executed")
        if not worker.cli_available():
            record.notes.append(f"note: CLI {worker.cli_bin!r} is not currently installed")
        return finish("[dry run: worker not executed]\n")

    # Unsupported worker: CLI is not installed. Record and stop; do not fall back.
    if not worker.cli_available():
        record.result = RESULT_UNSUPPORTED
        record.notes.append(
            f"CLI {worker.cli_bin!r} for {worker_name} is not installed or not on PATH. "
            "Implement/verify the local CLI, then re-run. No other worker was invoked."
        )
        return finish("")

    # Git worktree safety for write-capable workers.
    checkout_lock_dir = root / _AGENT_OUTPUT_DIRNAME
    try:
        assert_write_safety(worker, root, allow_write=allow_write, lock_dir=checkout_lock_dir)
    except WriteSafetyError as exc:
        record.result = RESULT_BLOCKED
        record.notes.append(f"write-safety block: {exc}")
        return finish("")

    preset_block_reason = _agent_preset_block_reason(root, command)
    if preset_block_reason is not None:
        record.result = RESULT_BLOCKED
        record.notes.append(preset_block_reason)
        return finish("")

    before = worktree_snapshot(root)
    started = _time.monotonic()
    try:
        with write_lock(worker, checkout_lock_dir):
            exit_status, log_text = run_worker_process(command, root, timeout=timeout)
    except WriteSafetyError as exc:
        record.result = RESULT_BLOCKED
        record.notes.append(f"write-safety block: {exc}")
        return finish("")
    record.duration_seconds = round(_time.monotonic() - started, 3)
    record.exit_status = exit_status
    record.actual_execution_system = worker.execution_system
    record.actual_provider = worker.provider
    record.actual_model = structured_actual_model(log_text, model or worker.default_model)
    record.actual_intensity = resolved_intensity

    after = worktree_snapshot(root)
    changed_during_run = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    record.files_changed = changed_during_run

    if worker.is_read_only and changed_during_run:
        record.result = RESULT_FAIL
        record.notes.append(
            "read-only worker modified the working tree: "
            + ", ".join(changed_during_run)
            + ". Treat as a contract violation."
        )
        return finish(log_text)

    failure_reason = structured_failure(log_text)
    if failure_reason:
        record.result = RESULT_FAIL
        record.notes.append(failure_reason)
    else:
        record.result = RESULT_PASS if exit_status == 0 else RESULT_FAIL
    return finish(log_text)


def run_session(
    *,
    registry: Registry,
    root: Path,
    task: str,
    worker_name: str,
    role: str,
    model: str | None,
    intensity: str | None,
    why: str,
    prompt: str,
    dry_run: bool,
    timeout: float | None,
    permission_profile: str = PERMISSION_STANDARD,
    contract_paths: list[str] | None = None,
    workflow: str | None = None,
    fallback_reason: str | None = None,
) -> DelegationResult:
    """Run one whole-worktree unattended session (ENG-AGENT-02-S5 runbooks).

    A sibling to :func:`run_delegation` for exactly one case that tool's
    mandatory ``--scope`` narrowing cannot express: a bounded, hours-long
    autonomous development session (e.g. "finish this PR") that legitimately
    needs the whole worktree, not one pre-declared path. Everything else is
    reused unchanged: the same registry/worker lookup, the same
    ``assert_write_safety``/``write_lock`` git-worktree enforcement, the same
    ``run_worker_process`` execution, and the same redacted
    manifest/summary/log artifacts under ``.agent-output/``. The caller's
    complete objective, safety profile, stop conditions, and routing policy
    are retained as the bundle's bounded acceptance criteria.
    """

    validate_task_id(task)
    worker = registry.get(worker_name)
    if role not in worker.roles:
        raise ValidationError(f"worker {worker_name!r} does not declare role {role!r}")
    resolved_intensity = intensity or worker.default_intensity
    if resolved_intensity not in registry.intensities:
        raise ValidationError(
            f"invalid intensity {resolved_intensity!r}; expected one of {', '.join(registry.intensities)}"
        )
    if not prompt.strip():
        raise ValidationError("session prompt must not be empty")
    if not why.strip():
        raise ValidationError("--why is required for delegated-worker audit evidence")
    if permission_profile != PERMISSION_STANDARD:
        if worker.is_read_only:
            raise ValidationError(
                f"permission_profile {permission_profile!r} cannot be used with read-only worker {worker_name!r}"
            )
        if not worker.supports_permission_profile(permission_profile):
            raise ValidationError(f"worker {worker_name!r} does not support permission_profile {permission_profile!r}")

    bundle = compose_policy_bundle(
        root=root,
        registry=registry,
        worker_name=worker_name,
        route_role=role,
        contracts=contract_paths or (),
        workflow=workflow,
        fallback_reason=fallback_reason,
        acceptance_criteria=prompt,
    )
    policy_manifest = dict(bundle.manifest)
    policy_manifest["actual_worker"] = worker.name
    policy_manifest["actual_execution_system"] = worker.execution_system
    policy_manifest["actual_provider"] = worker.provider
    policy_manifest["actual_model"] = model or worker.default_model
    policy_manifest["actual_intensity"] = resolved_intensity
    composed_prompt = bundle.prompt
    command = worker.build_command(
        model=model, intensity=resolved_intensity, prompt=composed_prompt, permission_profile=permission_profile
    )
    started_at = _now()
    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = f"{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    worker_dir = agent_output_dir(root, task, worker_name, run_id)
    worker_dir.mkdir(parents=True, exist_ok=True)

    notes = ["unattended whole-worktree session (ENG-AGENT-02-S5); --scope narrowing does not apply"]
    if permission_profile != PERMISSION_STANDARD:
        notes.append(f"launched with non-standard permission_profile={permission_profile}")

    record = RunRecord(
        task=task,
        role=role,
        worker=worker_name,
        planned_execution_system=worker.execution_system,
        planned_provider=worker.provider,
        planned_model=model or worker.default_model,
        planned_intensity=resolved_intensity,
        requested_command=command,
        why_this_worker=why,
        started_at=started_at,
        notes=notes,
        tests_or_checks=[],
        files_changed=[],
        policy_manifest=policy_manifest,
    )

    def finish(log_text: str) -> DelegationResult:
        record.finished_at = _now()
        paths = write_artifacts(worker_dir, record, log_text=log_text, repo_root=root)
        manifest = record.to_manifest(paths=paths)
        exit_code = {
            RESULT_PASS: EXIT_OK,
            RESULT_DRY_RUN: EXIT_OK,
            RESULT_FAIL: EXIT_WORKER_FAILED,
            RESULT_UNSUPPORTED: EXIT_UNSUPPORTED_OR_BLOCKED,
            RESULT_BLOCKED: EXIT_UNSUPPORTED_OR_BLOCKED,
        }[record.result]
        return DelegationResult(record=record, manifest=manifest, exit_code=exit_code)

    if dry_run:
        record.result = RESULT_DRY_RUN
        record.notes.append("dry run: command was built and validated but not executed")
        if not worker.cli_available():
            record.notes.append(f"note: CLI {worker.cli_bin!r} is not currently installed")
        return finish("[dry run: worker not executed]\n")

    if not worker.cli_available():
        record.result = RESULT_UNSUPPORTED
        record.notes.append(
            f"CLI {worker.cli_bin!r} for {worker_name} is not installed or not on PATH. "
            "Implement/verify the local CLI, then re-run. No other worker was invoked."
        )
        return finish("")

    checkout_lock_dir = root / _AGENT_OUTPUT_DIRNAME
    try:
        assert_write_safety(worker, root, allow_write=True, lock_dir=checkout_lock_dir)
    except WriteSafetyError as exc:
        record.result = RESULT_BLOCKED
        record.notes.append(f"write-safety block: {exc}")
        return finish("")

    preset_block_reason = _agent_preset_block_reason(root, command)
    if preset_block_reason is not None:
        record.result = RESULT_BLOCKED
        record.notes.append(preset_block_reason)
        return finish("")

    before = worktree_snapshot(root)
    started = _time.monotonic()
    try:
        with write_lock(worker, checkout_lock_dir):
            exit_status, log_text = run_worker_process(command, root, timeout=timeout)
    except WriteSafetyError as exc:
        record.result = RESULT_BLOCKED
        record.notes.append(f"write-safety block: {exc}")
        return finish("")
    record.duration_seconds = round(_time.monotonic() - started, 3)
    record.exit_status = exit_status
    record.actual_execution_system = worker.execution_system
    record.actual_provider = worker.provider
    record.actual_model = structured_actual_model(log_text, model or worker.default_model)
    record.actual_intensity = resolved_intensity

    after = worktree_snapshot(root)
    record.files_changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))

    failure_reason = structured_failure(log_text)
    if failure_reason:
        record.result = RESULT_FAIL
        record.notes.append(failure_reason)
    else:
        record.result = RESULT_PASS if exit_status == 0 else RESULT_FAIL
    return finish(log_text)


# --------------------------------------------------------------------------- CLI


def _cmd_list_workers(registry: Registry, _args: argparse.Namespace) -> int:
    header = f"{'worker':<30} {'system':<14} {'provider':<10} {'capability':<13} {'cost_class':<24} cli"
    print(header)
    print("-" * len(header))
    for name in sorted(registry.workers):
        w = registry.workers[name]
        state = "ok" if w.cli_available() else "missing"
        if not w.enabled:
            state += ",disabled"
        print(f"{name:<30} {w.execution_system:<14} {w.provider:<10} {w.capability:<13} {w.cost_class:<24} {w.cli_bin} ({state})")
    return EXIT_OK


def _cmd_route(registry: Registry, args: argparse.Namespace) -> int:
    try:
        candidates = registry.route(args.role)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"role: {args.role}")
    print("preference order (cheapest capable first):")
    for name in candidates:
        w = registry.workers[name]
        state = "installed" if w.cli_available() else "not installed"
        note = "" if w.enabled else " [disabled]"
        print(f"  - {name}: {w.execution_system}/{w.provider} ({state}){note}")
    chosen = registry.first_available(args.role)
    print(f"first available: {chosen.name if chosen else '(none installed/enabled)'}")
    return EXIT_OK


def _cmd_run(registry: Registry, args: argparse.Namespace) -> int:
    try:
        root = repo_root(Path(args.repo_root).resolve() if args.repo_root else None)
    except Exception as exc:  # noqa: BLE001 - surface any git failure as usage
        print(f"error: not inside a git repository ({exc})", file=sys.stderr)
        return EXIT_USAGE

    try:
        result = run_delegation(
            registry=registry,
            root=root,
            task=args.task,
            worker_name=args.worker,
            role=args.role,
            model=args.model,
            intensity=args.intensity,
            why=args.why or "",
            prompt_args=args.prompt,
            scope_paths=args.scope or [],
            dry_run=args.dry_run,
            allow_write=args.allow_write,
            allow_overflow=args.allow_overflow,
            timeout=args.timeout,
            tests_or_checks=args.check,
            include_diff=args.include_diff,
            diff_base=args.diff_base,
            contract_paths=args.contract,
            workflow=args.workflow,
            fallback_reason=args.fallback_reason,
        )
    except (ValidationError, RegistryError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(compact_pointer(result.manifest))
    return result.exit_code


def _cmd_session(registry: Registry, args: argparse.Namespace) -> int:
    try:
        root = repo_root(Path(args.repo_root).resolve() if args.repo_root else None)
    except Exception as exc:  # noqa: BLE001 - surface any git failure as usage
        print(f"error: not inside a git repository ({exc})", file=sys.stderr)
        return EXIT_USAGE

    try:
        result = run_session(
            registry=registry,
            root=root,
            task=args.task,
            worker_name=args.worker,
            role=args.role,
            model=args.model,
            intensity=args.intensity,
            why=args.why or "",
            prompt=" ".join(args.prompt).strip(),
            dry_run=args.dry_run,
            timeout=args.timeout,
            permission_profile=args.permission_profile,
            contract_paths=args.contract,
            workflow=args.workflow,
            fallback_reason=args.fallback_reason,
        )
    except (ValidationError, RegistryError, PolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(compact_pointer(result.manifest))
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.agents.orchestrate",
        description="Provider-neutral local delegation orchestrator (ENG-AGENT-01). Never runs in CI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-workers", help="print the worker registry and CLI availability")

    p_route = sub.add_parser("route", help="show the preferred worker order for a role")
    p_route.add_argument("--role", required=True)

    p_run = sub.add_parser("run", help="run (or --dry-run) exactly one delegated worker")
    p_run.add_argument("--task", required=True, help="stable task id, e.g. ENG-AGENT-01")
    p_run.add_argument("--repo-root", default=None, help="explicit target Git worktree root")
    p_run.add_argument("--worker", required=True, help="registry worker name")
    p_run.add_argument("--role", required=True, help="delegated role, for the audit record")
    p_run.add_argument("--model", default=None, help="override the worker's default model")
    p_run.add_argument("--intensity", default=None, choices=["low", "medium", "high"])
    p_run.add_argument("--why", required=True, help="one line: why this worker/model was chosen")
    p_run.add_argument(
        "--check",
        action="append",
        default=[],
        help="test/check the worker was asked to run (repeatable; recorded in the manifest)",
    )
    p_run.add_argument(
        "--scope",
        action="append",
        default=None,
        metavar="PATH",
        help="repository path the worker is scoped to (validated; repeatable)",
    )
    p_run.add_argument("--dry-run", action="store_true", help="build and validate the command but do not execute")
    p_run.add_argument(
        "--include-diff",
        action="store_true",
        help="append a redacted, size-bounded git diff for selected scopes (read-only workers only)",
    )
    p_run.add_argument(
        "--diff-base",
        default="origin/main",
        help=(
            "base ref used to build the --include-diff review context for a clean, already-committed "
            "candidate (merge-base against HEAD); ignored when the working tree/index has scoped changes"
        ),
    )
    p_run.add_argument(
        "--allow-write",
        action="store_true",
        help="permit a write-capable worker (must be its own dedicated worktree, not main)",
    )
    p_run.add_argument(
        "--allow-overflow",
        action="store_true",
        help="permit a registry-disabled overflow worker (requires prior authorization)",
    )
    p_run.add_argument("--timeout", type=float, default=1800.0, help="worker subprocess timeout in seconds")
    p_run.add_argument("--contract", action="append", default=[], help="bounded task/program/ADR contract path")
    p_run.add_argument("--workflow", default=None, help="canonical workflow override")
    p_run.add_argument("--fallback-reason", default=None, help="recorded fallback/escalation reason")
    p_run.add_argument(
        "prompt",
        nargs=argparse.REMAINDER,
        help="text passed verbatim to the worker after '--'",
    )

    p_session = sub.add_parser(
        "session",
        help="run (or --dry-run) one whole-worktree unattended session (ENG-AGENT-02-S5 runbooks)",
    )
    p_session.add_argument("--task", required=True, help="stable task id, e.g. ENG-AGENT-02-S5")
    p_session.add_argument("--repo-root", default=None, help="explicit target Git worktree root")
    p_session.add_argument("--worker", required=True, help="registry worker name (must be write-capable)")
    p_session.add_argument("--role", required=True, help="delegated role, for the audit record")
    p_session.add_argument("--model", default=None, help="override the worker's default model")
    p_session.add_argument("--intensity", default=None, choices=["low", "medium", "high"])
    p_session.add_argument("--why", required=True, help="one line: why this worker/model was chosen")
    p_session.add_argument("--dry-run", action="store_true", help="build and validate the command but do not execute")
    p_session.add_argument(
        "--timeout", type=float, default=None, help="session subprocess timeout in seconds (the run's stop deadline)"
    )
    p_session.add_argument("--contract", action="append", default=[], help="bounded task/program/ADR contract path")
    p_session.add_argument("--workflow", default=None, help="canonical workflow override")
    p_session.add_argument("--fallback-reason", default=None, help="recorded fallback/escalation reason")
    p_session.add_argument(
        "--permission-profile",
        default=PERMISSION_STANDARD,
        choices=sorted(KNOWN_PERMISSION_PROFILES),
        help=(
            "worker permission-launch profile; 'standard' (default) never changes the worker's normal "
            "invocation. 'repo_configured_auto' is the bounded unattended Runbook profile and is only "
            "honored for a write-capable worker that declares it in workers.json."
        ),
    )
    p_session.add_argument(
        "prompt",
        nargs=argparse.REMAINDER,
        help="the full session prompt, passed verbatim to the worker after '--'",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = load_registry()

    if args.command == "list-workers":
        return _cmd_list_workers(registry, args)
    if args.command == "route":
        return _cmd_route(registry, args)
    if args.command in ("run", "session"):
        # argparse.REMAINDER keeps a leading "--"; drop it for the worker.
        if args.prompt and args.prompt[0] == "--":
            args.prompt = args.prompt[1:]
        return _cmd_run(registry, args) if args.command == "run" else _cmd_session(registry, args)
    parser.error(f"unknown command {args.command!r}")
    return EXIT_USAGE  # pragma: no cover - parser.error exits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
