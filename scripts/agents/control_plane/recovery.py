"""Startup reconciliation: real git worktrees, real PIDs, real write locks.

Run once when the daemon starts (and safe to re-run any time) so a restart
never spawns a duplicate agent for a task the durable store still thinks is
``RUNNING`` when its process actually died, and never leaves a task claiming a
live worktree lock that a crashed process abandoned.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import (
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_IMPLEMENTATION_COMPLETE,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_RECOVERABLE_ORPHAN,
    RUNBOOK_RUNNING,
    RUNBOOK_WAITING_PROVIDER,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_OWNER_ACTION_REQUIRED,
    TASK_QUEUED,
    TASK_READY_BUT_UNMERGED,
    TASK_READY_LOCAL,
    TASK_RECOVERABLE_ORPHAN,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    TASK_WAITING_APPROVAL,
    TASK_WAITING_EXTERNAL,
    TASK_WAITING_PROVIDER,
    Runbook,
    Task,
    WorktreeRecord,
)
from .run_events import RunEvent
from .state import State

_WRITE_LOCK_NAME = ".write-lock"
_AGENT_OUTPUT_DIRNAME = ".agent-output"

# Public state-machine vocabulary. These aliases intentionally point at the
# existing authoritative task-state values instead of introducing a parallel
# recovery status that could disagree with ``Task.state``.
RUNNING = TASK_RUNNING
WAITING_EXTERNAL = TASK_WAITING_EXTERNAL
WAITING_APPROVAL = TASK_WAITING_APPROVAL
WAITING_PROVIDER = TASK_WAITING_PROVIDER
OWNER_ACTION_REQUIRED = TASK_OWNER_ACTION_REQUIRED
RECOVERABLE_ORPHAN = TASK_RECOVERABLE_ORPHAN
SUCCEEDED = TASK_SUCCEEDED
READY_LOCAL = TASK_READY_LOCAL
READY_BUT_UNMERGED = TASK_READY_BUT_UNMERGED
FAILED = TASK_FAILED
CANCELLED = TASK_CANCELLED
TERMINAL_STATES = frozenset({SUCCEEDED, READY_LOCAL, READY_BUT_UNMERGED, FAILED, CANCELLED})

OWNERSHIP_LIVE = "LIVE"
OWNERSHIP_RECLAIMABLE = "RECLAIMABLE"
OWNERSHIP_AMBIGUOUS = "AMBIGUOUS"

EVIDENCE_PID_CREATE_TIME = "PID_CREATE_TIME"
EVIDENCE_PID_LOCK_TIME = "PID_LOCK_TIME"
EVIDENCE_PID_ABSENT = "PID_ABSENT"
EVIDENCE_PID_ONLY = "PID_ONLY"


@dataclass(frozen=True)
class OwnershipProof:
    verdict: str
    evidence_class: str
    reason: str
    observed_create_time: float | None = None


def process_create_time(pid: int) -> float | None:
    """Return the OS-reported process create time without a new dependency.

    Reuse the repository's existing psutil dependency (also used by operations
    ownership), then fall back to ``ps lstart`` on supported macOS/Linux hosts.
    Failure remains a weaker evidence class rather than silently becoming proof.
    """

    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - fall through to the dependency-free OS query
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        value = result.stdout.strip()
        parsed = _dt.datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
        # ``ps`` renders lstart in the host's local zone. ``timestamp()`` on a
        # naive datetime deliberately interprets that same local zone.
        return parsed.timestamp()
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def classify_process_owner(
    pid: int | None,
    *,
    recorded_create_time: float | None = None,
    recorded_at: float | None = None,
) -> OwnershipProof:
    """Classify process identity from recorded facts; never from argv/name.

    A live numeric PID alone is ambiguous. It becomes identity evidence only
    when its current create time matches the create time recorded at launch,
    or when it demonstrably existed before a lock/task record was written.
    Conversely, a process created after that record proves PID reuse.
    """

    if not pid or not pid_is_alive(pid):
        return OwnershipProof(OWNERSHIP_RECLAIMABLE, EVIDENCE_PID_ABSENT, "recorded pid is not alive")
    observed = process_create_time(pid)
    if observed is None:
        return OwnershipProof(
            OWNERSHIP_AMBIGUOUS,
            EVIDENCE_PID_ONLY,
            "pid is live but process create time could not be observed; identity is unproven",
        )
    if recorded_create_time is not None:
        if abs(observed - recorded_create_time) <= 1:
            return OwnershipProof(
                OWNERSHIP_LIVE,
                EVIDENCE_PID_CREATE_TIME,
                "live pid create time matches the orchestrator's launch record",
                observed,
            )
        return OwnershipProof(
            OWNERSHIP_RECLAIMABLE,
            EVIDENCE_PID_CREATE_TIME,
            "live pid has a different create time and was reused by another process",
            observed,
        )
    if recorded_at is not None:
        # Pre-ENG-PC-07 tests and hand-written legacy locks sometimes used 0/1
        # as a placeholder rather than an epoch. It is not evidence of time;
        # fail closed as ambiguous instead of calling it a reuse proof.
        if recorded_at <= 1:
            return OwnershipProof(
                OWNERSHIP_AMBIGUOUS,
                EVIDENCE_PID_ONLY,
                "pid is live but the recorded write time is a legacy placeholder",
                observed,
            )
        if observed > recorded_at:
            return OwnershipProof(
                OWNERSHIP_RECLAIMABLE,
                EVIDENCE_PID_LOCK_TIME,
                "live pid was created after the ownership record and is a reused pid",
                observed,
            )
        return OwnershipProof(
            OWNERSHIP_LIVE,
            EVIDENCE_PID_LOCK_TIME,
            "live process existed before the ownership record was written",
            observed,
        )
    return OwnershipProof(
        OWNERSHIP_AMBIGUOUS,
        EVIDENCE_PID_ONLY,
        "pid is live but no recorded create-time or write-time boundary proves identity",
        observed,
    )


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else; treat as alive.
        return True
    return True


def discover_git_worktrees(repo_root: Path) -> list[WorktreeRecord]:
    """Parse ``git worktree list --porcelain`` into durable rows.

    Never raises on a missing/broken git: an empty list is returned and the
    caller keeps whatever worktree rows are already on disk, since this is
    reconciliation, not the sole source of truth.
    """

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    records: list[WorktreeRecord] = []
    path: str | None = None
    branch: str | None = None
    locked = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                records.append(WorktreeRecord(path=path, branch=branch, locked=locked))
            path = line.removeprefix("worktree ").strip()
            branch = None
            locked = False
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").strip()
        elif line == "locked" or line.startswith("locked "):
            locked = True
    if path is not None:
        records.append(WorktreeRecord(path=path, branch=branch, locked=locked))
    return records


def _stale_write_lock_holder(lock_path: Path) -> tuple[str | None, bool]:
    """Return ``(holder_text, is_stale)`` for a write-lock file, if present.

    Mirrors ``runner._active_lock_holder``'s PID-liveness technique without
    importing that private helper, so recovery can report a stale lock even
    when it chooses not to delete it itself (deletion stays ``runner.py``'s
    job during an actual run, to avoid two modules racing to remove one file).
    """

    if not lock_path.exists():
        return None, False
    try:
        holder = lock_path.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - unreadable lock file
        return None, False
    match = re.search(r"\bpid=(\d+)\b", holder)
    if not match:
        return holder or "unknown holder", False
    at_match = re.search(r"\bat=(\d+(?:\.\d+)?)\b", holder)
    created_match = re.search(r"\bcreated=(\d+(?:\.\d+)?)\b", holder)
    proof = classify_process_owner(
        int(match.group(1)),
        recorded_create_time=float(created_match.group(1)) if created_match else None,
        recorded_at=float(at_match.group(1)) if at_match else None,
    )
    return holder, proof.verdict == OWNERSHIP_RECLAIMABLE


def _emit_transition(
    state: State,
    task: Task,
    *,
    previous: str,
    current: str,
    proof: OwnershipProof,
) -> None:
    """Contained ENG-PC-04 emission after the task mutation is committed."""

    run_id = task.runbook_id or task.id
    if not run_id:
        return
    try:
        state.record_run_event(
            RunEvent(
                run_id=run_id,
                event_class="process",
                event_type="recovery.transition",
                category="recovery",
                source="control_plane.recovery",
                provenance=(
                    "UNKNOWN"
                    if proof.evidence_class == EVIDENCE_PID_ONLY
                    else "DERIVED"
                    if proof.evidence_class == "PROVIDER_FAILURE_ATTRIBUTION"
                    else "MEASURED"
                ),
                task_id=task.id,
                project_id=task.project_id,
                level="error" if current == TASK_OWNER_ACTION_REQUIRED else "warning",
                message=f"Recovery transitioned task from {previous} to {current}: {proof.reason}",
                data={
                    "previous_state": previous,
                    "state": current,
                    "ownership_verdict": proof.verdict,
                    "evidence_class": proof.evidence_class,
                    "recovery_attempts": task.recovery_attempts,
                    "recovery_max_attempts": task.recovery_max_attempts,
                },
            )
        )
    except Exception:  # noqa: BLE001 - timeline failure cannot undo committed recovery
        pass


def _sync_runbook_recovery_state(state: State, task: Task) -> None:
    """Keep the owning runbook's existing ``status`` aligned with task recovery."""

    if not task.runbook_id:
        return
    runbook = state.get_runbook(task.runbook_id)
    if runbook is None:
        return
    mapped = {
        TASK_RUNNING: RUNBOOK_RUNNING,
        TASK_QUEUED: RUNBOOK_RUNNING,
        TASK_RECOVERABLE_ORPHAN: RUNBOOK_RECOVERABLE_ORPHAN,
        TASK_OWNER_ACTION_REQUIRED: RUNBOOK_OWNER_ACTION_REQUIRED,
        TASK_WAITING_PROVIDER: RUNBOOK_WAITING_PROVIDER,
    }.get(task.state)
    if mapped is None or mapped == runbook.status:
        return
    runbook.status = mapped
    runbook.recovery_note = f"underlying task recovery state is {task.state}"
    state.upsert_runbook(runbook)


def release_owned_write_lock(
    state: State,
    *,
    worktree: str,
    expected_pid: int,
    project_id: str | None = None,
) -> bool:
    """Release only the dead standard lock owned by a just-terminated process."""

    saved = next(
        (
            item
            for item in state.list_worktrees(project_id=project_id)
            if Path(item.path).resolve() == Path(worktree).resolve()
        ),
        None,
    )
    if saved is None or not saved.managed:
        return False
    lock_path = Path(worktree) / _AGENT_OUTPUT_DIRNAME / _WRITE_LOCK_NAME
    holder, stale = _stale_write_lock_holder(lock_path)
    match = re.fullmatch(r"[\w.-]+\s+pid=(\d+)\s+at=\d+(?:\s+created=\d+(?:\.\d+)?)?", holder or "")
    if not stale or match is None or int(match.group(1)) != expected_pid:
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    saved.locked = False
    saved.lock_holder = None
    saved.stale_lock = False
    saved.stale_lock_holder = None
    state.upsert_worktree(saved)
    state.record_event(
        category="recovery",
        level="warning",
        message=f"released terminated owned-process write lock at {lock_path} (pid={expected_pid})",
        project_id=project_id,
    )
    return True


def reconcile_worktree_locks(
    state: State, worktrees: list[WorktreeRecord], *, project_id: str | None = None
) -> list[WorktreeRecord]:
    """Annotate each discovered worktree with its real write-lock state and persist it.

    ENG-CP-03 (issue #165): ``worktrees`` is always a snapshot of a *single*
    repository, so both the prior-state lookup and the persisted replacement are
    scoped to ``project_id``. Without that scoping, observing Project A's
    worktrees would delete every worktree row belonging to Project B.
    """

    prior = {Path(item.path).resolve(): item for item in state.list_worktrees(project_id=project_id)}
    annotated: list[WorktreeRecord] = []
    for worktree in worktrees:
        lock_path = Path(worktree.path) / _AGENT_OUTPUT_DIRNAME / _WRITE_LOCK_NAME
        holder, stale = _stale_write_lock_holder(lock_path)
        saved = prior.get(Path(worktree.path).resolve())
        released = False
        # Only an already-adopted worktree and a normal orchestrator lock
        # signature prove ownership strongly enough for automatic release.
        if stale and saved and saved.managed and holder and re.match(
            r"^[\w.-]+\s+pid=\d+\s+at=\d+(?:\s+created=\d+(?:\.\d+)?)?$", holder
        ):
            try:
                lock_path.unlink()
                released = True
                state.record_event(
                    category="recovery", level="warning",
                    message=f"released stale orchestrator write lock at {lock_path} (holder was {holder})",
                    project_id=project_id,
                )
            except OSError:
                released = False
        record = WorktreeRecord(
            path=worktree.path,
            branch=worktree.branch,
            locked=bool(holder) and not stale,
            lock_holder=holder if (holder and not stale) else None,
            managed=bool(saved and saved.managed),
            stale_lock=bool(stale and not released),
            stale_lock_holder=holder if (stale and not released) else None,
            project_id=project_id,
            # ENG-AGENT-14: Git discovery does not observe PR/head origin.
            # Copy durable stamps onto the in-memory snapshot so refresh
            # and GET /api/worktrees do not depend on replace_worktrees
            # mutating these objects later.
            review_repository=saved.review_repository if saved else None,
            review_pr=saved.review_pr if saved else None,
            review_head_sha=saved.review_head_sha if saved else None,
        )
        annotated.append(record)
    state.replace_worktrees(annotated, project_id=project_id)
    return annotated


def _recorded_at(task: Task) -> float | None:
    try:
        return _dt.datetime.fromisoformat(task.updated_at).timestamp()
    except (TypeError, ValueError):
        return None


def ownership_proof_for_task(task: Task) -> OwnershipProof:
    """Observe the current owner using the canonical task identity evidence."""

    # Only legacy rows lack the strong ``pid_create_time`` stamped at launch.
    # Their ``updated_at`` fallback is deliberately conservative: if a PID was
    # reused before a later row update, it can over-preserve that process as
    # LIVE, but it cannot wrongly reclaim it. Such a legacy orphan may therefore
    # require the bounded owner-action path instead of automatic recovery.
    return classify_process_owner(
        task.pid,
        recorded_create_time=task.pid_create_time,
        recorded_at=_recorded_at(task),
    )


def orphan_recovery_eligibility(task: Task) -> tuple[bool, OwnershipProof, str | None]:
    """Return server-owned one-click eligibility for one persisted task.

    This is intentionally stricter than merely checking the task's state.  A
    dashboard can be stale, so every caller receives a newly observed
    :class:`OwnershipProof`; only a currently reclaimable orphan is eligible.
    """

    proof = ownership_proof_for_task(task)
    if task.state != TASK_RECOVERABLE_ORPHAN:
        return False, proof, "the task is not in the recoverable-orphan state"
    if task.recovery_attempts >= task.recovery_max_attempts:
        return False, proof, "the bounded recovery-attempt limit has been reached"
    if proof.verdict != OWNERSHIP_RECLAIMABLE:
        return False, proof, proof.reason
    return True, proof, None


def recover_orphan_task(state: State, task: Task) -> tuple[bool, OwnershipProof, str | None]:
    """Re-verify and queue one orphan, refusing every unproven ownership case."""

    eligible, proof, refusal = orphan_recovery_eligibility(task)
    if not eligible:
        return False, proof, refusal
    previous = task.state
    task.recovery_attempts += 1
    task.state = TASK_QUEUED
    task.pid = None
    task.pid_create_time = None
    task.ownership_evidence_class = proof.evidence_class
    task.stale_recovered = True
    task.last_error = f"operator recovery queued after ownership proof: {proof.reason}"
    state.upsert_task(task)
    _sync_runbook_recovery_state(state, task)
    _emit_transition(state, task, previous=previous, current=task.state, proof=proof)
    state.record_event(
        category="recovery",
        task_id=task.id,
        level="warning",
        message="operator recovery queued a task after re-verifying its prior owner is reclaimable",
        project_id=task.project_id,
    )
    return True, proof, None


def reconcile_tasks(state: State, *, project_id: str | None = None) -> dict[str, int]:
    """Reclaim tasks marked ``RUNNING`` whose recorded PID is no longer alive.

    A task with a live PID is left untouched (never duplicated). A task with a
    dead/missing PID is requeued if it has none, or marked ``FAILED`` if a
    successful requeue is unsafe to infer automatically (default: requeue, on
    the theory that a killed daemon should retry rather than silently drop
    work; the scheduler's normal concurrency/priority rules apply on retry).
    """

    reclaimed = 0
    left_running = 0
    candidates = state.list_tasks(state=TASK_RUNNING, project_id=project_id)
    candidates.extend(state.list_tasks(state=TASK_RECOVERABLE_ORPHAN, project_id=project_id))
    for task in candidates:
        proof = ownership_proof_for_task(task)
        task.ownership_evidence_class = proof.evidence_class
        if proof.verdict == OWNERSHIP_LIVE:
            if task.state == TASK_RECOVERABLE_ORPHAN:
                previous = task.state
                task.state = TASK_RUNNING
                state.upsert_task(task)
                _sync_runbook_recovery_state(state, task)
                _emit_transition(state, task, previous=previous, current=task.state, proof=proof)
            else:
                state.upsert_task(task)
            left_running += 1
            continue
        if proof.verdict == OWNERSHIP_AMBIGUOUS:
            previous = task.state
            task.recovery_attempts += 1
            if task.recovery_attempts >= task.recovery_max_attempts:
                task.state = TASK_OWNER_ACTION_REQUIRED
            else:
                task.state = TASK_RECOVERABLE_ORPHAN
            task.last_error = proof.reason
            state.upsert_task(task)
            _sync_runbook_recovery_state(state, task)
            _emit_transition(state, task, previous=previous, current=task.state, proof=proof)
            continue
        previous = task.state
        task.state = TASK_RECOVERABLE_ORPHAN
        task.recovery_attempts += 1
        task.last_error = proof.reason
        state.upsert_task(task)
        _sync_runbook_recovery_state(state, task)
        _emit_transition(state, task, previous=previous, current=task.state, proof=proof)
        task.state = TASK_QUEUED
        task.pid = None
        task.pid_create_time = None
        task.stale_recovered = True
        task.last_error = f"reclaimed on daemon restart: {proof.reason}"
        state.upsert_task(task)
        _sync_runbook_recovery_state(state, task)
        _emit_transition(
            state, task, previous=TASK_RECOVERABLE_ORPHAN, current=TASK_QUEUED, proof=proof
        )
        state.record_event(
            category="recovery",
            task_id=task.id,
            level="warning",
            message="reclaimed RUNNING task with a dead PID on daemon restart",
            project_id=task.project_id,
        )
        reclaimed += 1
    for task in state.list_tasks(state=TASK_FAILED, project_id=project_id):
        if (task.failure_category or "").lower().replace("_", "-") not in {
            "provider-outage", "rate-limit", "quota", "auth-cli"
        }:
            continue
        previous = task.state
        task.state = TASK_WAITING_PROVIDER
        proof = OwnershipProof(
            OWNERSHIP_AMBIGUOUS,
            "PROVIDER_FAILURE_ATTRIBUTION",
            "provider availability failure is retryable and not a terminal task outcome",
        )
        state.upsert_task(task)
        _sync_runbook_recovery_state(state, task)
        _emit_transition(state, task, previous=previous, current=task.state, proof=proof)
    # Keep the established summary contract stable; the typed task rows and
    # timeline carry the richer ambiguous/owner-action outcomes.
    return {"reclaimed": reclaimed, "left_running": left_running}


def minimum_remaining_acceptance_stage(runbook: Runbook, *, tree_sha: str) -> str:
    """Return the first acceptance stage whose evidence is absent or stale.

    Gate and review PASS evidence are exact-tree claims. A moved tree invalidates
    those claims and recovery resumes at review/test rather than treating an old
    result as current. Implementation completion is preserved because it records
    the worker outcome, not an exact-tree gate.
    """

    evidence = runbook.acceptance_evidence
    implementation = evidence.get("implementation", {})
    if implementation.get("status") != "PASS":
        return "implementation"
    review = evidence.get("review", {})
    if review.get("status") not in {"PASS", "NOT_APPLICABLE"}:
        return "review"
    if review.get("status") == "PASS" and review.get("tree_sha") != tree_sha:
        return "review"
    gate = evidence.get("test", {})
    if gate.get("status") != "PASS" or gate.get("tree_sha") != tree_sha:
        return "test"
    for stage in ("checkpoint", "pr_readiness"):
        if evidence.get(stage, {}).get("status") not in {"PASS", "NOT_APPLICABLE"}:
            return stage
    return "DONE"


def _candidate_tree_sha(worktree: str) -> str | None:
    """Observe a candidate without changing its index.

    A dirty checkout is deliberately represented by a sentinel that cannot
    equal stored exact-tree evidence. Computing a dirty tree would require
    staging files, which startup recovery must not do.
    """

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        )
        if status.stdout.strip():
            return "DIRTY_CANDIDATE"
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return tree or None
    except (OSError, subprocess.CalledProcessError):
        return None


def acceptance_recovery_chain(runbook: Runbook) -> dict[str, object]:
    """Project preserved and remaining stages without exposing tree/path data."""

    stages = ("implementation", "review", "test", "checkpoint", "pr_readiness")
    tree_sha = _candidate_tree_sha(runbook.worktree)
    remaining = (
        minimum_remaining_acceptance_stage(runbook, tree_sha=tree_sha)
        if tree_sha is not None
        else runbook.acceptance_stage or "UNKNOWN"
    )
    remaining_index = stages.index(remaining) if remaining in stages else len(stages)
    rows: list[dict[str, str]] = []
    for index, stage in enumerate(stages):
        evidence = runbook.acceptance_evidence.get(stage, {})
        status = str(evidence.get("status") or "NOT_REPORTED")
        if stage in {"review", "test"} and status == "PASS":
            if tree_sha is None:
                disposition = "VALIDITY_UNKNOWN"
            elif evidence.get("tree_sha") != tree_sha:
                disposition = "INVALIDATED_BY_TREE_CHANGE"
            elif index < remaining_index or remaining == "DONE":
                disposition = "PRESERVED"
            else:
                disposition = "REMAINS"
        elif status in {"PASS", "NOT_APPLICABLE"} and (index < remaining_index or remaining == "DONE"):
            disposition = "PRESERVED"
        elif status in {"PASS", "NOT_APPLICABLE"}:
            disposition = "RECHECK_REQUIRED"
        else:
            disposition = "REMAINS"
        rows.append({"stage": stage, "evidence_status": status, "disposition": disposition})
    return {
        "minimum_remaining_stage": remaining,
        "tree_observation": "OBSERVED" if tree_sha is not None else "UNKNOWN",
        "stages": rows,
    }


def reconcile_acceptance_recovery(state: State, *, project_id: str | None = None) -> int:
    """Resume acceptance at the minimum stage supported by exact-tree evidence."""

    reconciled = 0
    for runbook in state.list_runbooks(project_id=project_id):
        if runbook.status not in {RUNBOOK_IMPLEMENTATION_COMPLETE, RUNBOOK_ACCEPTANCE_PENDING}:
            continue
        tree_sha = _candidate_tree_sha(runbook.worktree)
        if tree_sha is None:
            continue
        stage = minimum_remaining_acceptance_stage(runbook, tree_sha=tree_sha)
        if stage == runbook.acceptance_stage:
            continue
        previous = runbook.acceptance_stage
        runbook.acceptance_stage = stage
        runbook.recovery_note = (
            f"restart recovery preserved valid acceptance evidence and resumed at {stage}; "
            f"previous stage was {previous}"
        )
        state.upsert_runbook(runbook)
        try:
            state.record_run_event(
                RunEvent(
                    run_id=runbook.id,
                    event_class="lifecycle",
                    event_type="recovery.acceptance_resumed",
                    category="recovery",
                    source="control_plane.recovery",
                    provenance="DERIVED",
                    task_id=runbook.task_id,
                    project_id=runbook.project_id,
                    message=f"Acceptance recovery resumed at minimum remaining stage {stage}",
                    data={"previous_stage": previous, "stage": stage, "tree_sha": tree_sha},
                )
            )
        except Exception:  # noqa: BLE001 - committed recovery is not undone by timeline failure
            pass
        reconciled += 1
    return reconciled


def run_recovery(state: State, repo_root: Path, *, project_id: str | None = None) -> dict[str, object]:
    """Full startup reconciliation pass for one project. Safe to call repeatedly.

    ENG-CP-03 (issue #165): ``project_id`` names the managed project whose
    ``repo_root`` is being observed, and is threaded all the way into
    :func:`reconcile_worktree_locks` -> ``State.replace_worktrees``. Passing it
    is what makes startup recovery *scoped*: an independent review (Grok/xAI)
    caught that omitting it here made every daemon start run an unscoped
    ``DELETE FROM worktrees``, which both destroyed a second project's
    persisted worktree rows and rewrote the just-migrated OctaScene rows back
    to ``project_id IS NULL`` -- hiding them from every project-scoped read for
    the life of the process.
    """

    worktrees = discover_git_worktrees(repo_root)
    if worktrees:
        annotated = reconcile_worktree_locks(state, worktrees, project_id=project_id)
    else:
        # ``discover_git_worktrees`` returns [] when the Git observation itself
        # failed (a real repository always reports at least its main worktree),
        # so an empty snapshot means "could not observe", never "there are
        # none". Replacing rows from it would delete the persisted worktrees
        # and insert nothing. ``refresh_worktree_statuses`` already made this
        # distinction; an independent review (Grok/xAI) caught that startup
        # recovery did not, leaving the same data-loss class this slice's
        # scoping was meant to close.
        annotated = state.list_worktrees(project_id=project_id)
    task_summary = reconcile_tasks(state, project_id=project_id)
    acceptance_reconciled = reconcile_acceptance_recovery(state, project_id=project_id)
    # ENG-PC-01 (issue #29): free only execution leases whose owner this same
    # ``pid_is_alive`` evidence already proves dead -- the same startup moment
    # ``reconcile_tasks`` above uses it for, never a separate/weaker check.
    from .execution_lease import reconcile_stale_leases

    lease_summary = reconcile_stale_leases(state, project_id=project_id)
    # Both are existing ownership authorities. Recovery calls their public
    # entry points and never writes either module's tables directly.
    from .wake_queue import recover_on_restart as recover_wakes_on_restart

    wakes_recovered = recover_wakes_on_restart(state, project_id=project_id)
    state.record_event(
        category="recovery",
        level="info",
        message=f"startup recovery: {len(annotated)} worktrees observed, {task_summary}, leases {lease_summary}",
        project_id=project_id,
    )
    return {
        "worktrees": annotated,
        "tasks": task_summary,
        "execution_leases": lease_summary,
        "wakes_recovered": wakes_recovered,
        "acceptance_reconciled": acceptance_reconciled,
    }
