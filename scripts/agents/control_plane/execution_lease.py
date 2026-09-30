"""ENG-PC-01 (issue #29): atomic execution ownership for a worktree.

Three ownership mechanisms already exist in this control plane, each protecting a
different invariant:

- ``advancement_lease.py`` -- exactly one process may advance one *runbook's*
  acceptance pipeline at a time. An OS ``flock``, kernel-released on crash.
- ``runner.write_lock`` (the ``.write-lock`` file) -- the real, OS-enforced mutual
  exclusion a write-capable worker *subprocess* holds for the duration of its own
  run inside its worktree. Also kernel-released (the file is removed by whichever
  process still runs to completion; ``recovery.py`` reclaims it otherwise).
- ``task_intake_claims`` (``intake.py``) -- exactly one ``owner_ref`` may claim a
  stable external task id at *intake* time, so two dispatch passes never create two
  Task rows for the same upstream issue.

None of those answer "which execution attempt is currently entitled to *launch*
into this worktree" -- the decision ``Supervisor.launch_task`` makes before either
of the first two even come into play. A durable ``flock`` gives excellent liveness
(kernel-released on death) but no durable generation and no compare-and-swap; the
required behaviour here is CAS/expected-state acquisition with an explicit
generation, which wants a durable row instead. This module is that row's policy
layer (the row itself, plus its compare-and-swap primitives, live in
``state.py``): it composes with the three mechanisms above by governing a strictly
earlier decision, and it deliberately *defers* liveness proof to the same
``recovery.pid_is_alive`` evidence ``.write-lock`` reclamation already uses, rather
than re-implementing a second staleness authority.

Acquisition never blocks and never spins: a conflict with a live, unproven-dead
owner raises immediately. Reclaiming a lease whose owner is provably dead is the
only recovery path -- never a timeout, and never a match on executable name.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any

from .models import EXECUTION_LEASE_ACQUIRED, ExecutionLease
from .recovery import pid_is_alive
from .state import State


class ExecutionLeaseConflict(RuntimeError):
    """Raised when a worktree's execution lease cannot be acquired right now.

    The caller must not launch a second writer. ``holder`` is the current durable
    row (informational) so the caller can report who holds it.
    """

    def __init__(self, reason: str, *, holder: ExecutionLease | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.holder = holder


@dataclass(frozen=True)
class LeaseGrant:
    worktree: str
    generation: int
    task_id: str | None


def _process_create_time(pid: int) -> float | None:
    """Best-effort process start time for PID-reuse detection (see ``operations.py``'s identical use)."""

    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:  # pragma: no cover - psutil optional / platform-dependent / pid gone
        return None


def _is_stale(lease: ExecutionLease) -> tuple[bool, str | None]:
    """Never a timeout or executable-name match: only proven process death or PID reuse."""

    if not lease.owner_pid:
        return True, "no owner pid recorded"
    if not pid_is_alive(lease.owner_pid):
        return True, f"owner pid {lease.owner_pid} is no longer alive"
    if lease.owner_pid_create_time is not None:
        current = _process_create_time(lease.owner_pid)
        if current is not None and abs(current - lease.owner_pid_create_time) > 1:
            return True, f"pid {lease.owner_pid} was reused by a different process (create-time mismatch)"
    return False, None


def acquire(
    state: State,
    *,
    worktree: str,
    task_id: str,
    project_id: str | None = None,
    stable_task_id: str | None = None,
    runbook_id: str | None = None,
    worker: str | None = None,
) -> LeaseGrant:
    """Try, once, to become the sole execution owner of ``worktree``.

    Binds project + stable task + runbook + worker + worktree + generation into one
    transactional row (``state.try_acquire_execution_lease``). Raises
    :class:`ExecutionLeaseConflict` immediately on any conflict -- a live,
    unproven-dead owner, or losing a genuine race to another acquirer -- rather
    than blocking or retrying. The owner recorded is *this* process
    (``os.getpid()``); ``attach_pid`` below swaps in the real worker-subprocess pid
    once ``Popen`` returns, which is the pid actually proven dead/alive for future
    staleness decisions (mirroring ``recovery.reconcile_tasks``'s existing use of
    the worker subprocess's own pid, not the daemon's).
    """

    current = state.get_execution_lease(worktree)
    expected_generation = 0
    recovery_reason: str | None = None
    if current is not None:
        expected_generation = current.generation
        if current.status == EXECUTION_LEASE_ACQUIRED:
            stale, why = _is_stale(current)
            if not stale:
                conflict_reason = (
                    f"worktree {worktree!r} is already owned by pid={current.owner_pid} "
                    f"(task={current.task_id!r}, generation={current.generation})"
                )
                state.record_execution_lease_conflict(worktree=worktree, reason=conflict_reason)
                raise ExecutionLeaseConflict(conflict_reason, holder=current)
            recovery_reason = f"reclaimed stale lease: {why}"

    host = socket.gethostname()
    pid = os.getpid()
    granted = state.try_acquire_execution_lease(
        worktree=worktree,
        expected_generation=expected_generation,
        task_id=task_id,
        project_id=project_id,
        stable_task_id=stable_task_id,
        runbook_id=runbook_id,
        worker=worker,
        owner_host=host,
        owner_pid=pid,
        owner_pid_create_time=_process_create_time(pid),
        reason=recovery_reason,
    )
    if granted is None:
        conflict_reason = f"lost the race to acquire the execution lease for worktree {worktree!r}"
        state.record_execution_lease_conflict(worktree=worktree, reason=conflict_reason)
        raise ExecutionLeaseConflict(conflict_reason, holder=state.get_execution_lease(worktree))
    if recovery_reason:
        state.record_event(
            category="execution_lease",
            task_id=task_id,
            level="warning",
            message=f"worktree {worktree!r} {recovery_reason}",
            project_id=project_id,
        )
    return LeaseGrant(worktree=worktree, generation=granted.generation, task_id=task_id)


def attach_pid(state: State, *, worktree: str, generation: int, pid: int) -> bool:
    """Record the real launched-subprocess pid once ``Popen`` has returned."""

    return state.update_execution_lease_pid(
        worktree=worktree,
        expected_generation=generation,
        pid=pid,
        pid_create_time=_process_create_time(pid),
    )


def release(state: State, *, worktree: str, expected_pid: int, reason: str) -> bool:
    """Graceful self-release by the process whose pid is currently the recorded owner."""

    return state.release_execution_lease(worktree=worktree, expected_pid=expected_pid, reason=reason)


def reconcile_stale_leases(state: State, *, project_id: str | None = None) -> dict[str, int]:
    """Startup/periodic sweep: free only leases whose owner is proven dead.

    Mirrors ``recovery.reconcile_tasks``'s shape (safe to call repeatedly; never
    raises on one bad row). Never reclaims by timeout or executable name -- the
    same staleness proof ``acquire`` uses inline is reused here so a lease can also
    be freed without anyone attempting to re-acquire it first (surfacing it to
    Attention as recovered, not merely leaving it silently claimable).
    """

    reclaimed = 0
    left_held = 0
    for lease in state.list_execution_leases(project_id=project_id):
        if lease.status != EXECUTION_LEASE_ACQUIRED:
            continue
        stale, why = _is_stale(lease)
        if not stale:
            left_held += 1
            continue
        if state.force_release_execution_lease(
            worktree=lease.worktree, expected_generation=lease.generation, reason=f"stale: {why}"
        ):
            reclaimed += 1
            state.record_event(
                category="execution_lease",
                task_id=lease.task_id,
                level="warning",
                message=f"reclaimed stale execution lease for worktree {lease.worktree!r}: {why}",
                project_id=lease.project_id,
            )
    return {"reclaimed": reclaimed, "left_held": left_held}


def _to_dict(lease: ExecutionLease) -> dict[str, Any]:
    return {
        "worktree": lease.worktree,
        "generation": lease.generation,
        "status": lease.status,
        "task_id": lease.task_id,
        "stable_task_id": lease.stable_task_id,
        "runbook_id": lease.runbook_id,
        "worker": lease.worker,
        "owner_host": lease.owner_host,
        "owner_pid": lease.owner_pid,
        "acquired_at": lease.acquired_at,
        "heartbeat_at": lease.heartbeat_at,
        "released_at": lease.released_at,
        "release_reason": lease.release_reason,
        "recovery_reason": lease.recovery_reason,
        "conflict_reason": lease.conflict_reason,
    }


def lease_facts(state: State, worktree: str | None) -> dict[str, Any] | None:
    """Read-only projection for UI surfaces (Runs/Tasks inspector). ``None`` when unset/unknown."""

    if not worktree:
        return None
    lease = state.get_execution_lease(worktree)
    return _to_dict(lease) if lease is not None else None


def stale_leases(state: State, *, project_id: str | None = None) -> list[dict[str, Any]]:
    """Currently-``ACQUIRED`` leases already provably stale, for the Attention surface.

    Read-only: unlike ``reconcile_stale_leases`` this never mutates a row, so it is
    safe to call on every dashboard read without racing the daemon's own recovery
    pass. Classification is entirely from typed columns (``status``, ``owner_pid``)
    and ``recovery.pid_is_alive`` -- never from matching any free-text field.
    """

    findings: list[dict[str, Any]] = []
    for lease in state.list_execution_leases(project_id=project_id):
        if lease.status != EXECUTION_LEASE_ACQUIRED:
            continue
        stale, why = _is_stale(lease)
        if stale:
            findings.append({**_to_dict(lease), "stale_reason": why})
    return findings
