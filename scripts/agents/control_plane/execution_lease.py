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


_OWNER_LIVE = "live"
_OWNER_RECLAIMABLE = "reclaimable"
_OWNER_AMBIGUOUS = "ambiguous"


def _classify_owner(lease: ExecutionLease) -> tuple[str, str | None]:
    """Classify an ``ACQUIRED`` lease's recorded owner. Never a timeout or name match.

    Three outcomes, not two -- this is the fix for the supervisor-death race Grok
    Build's review named: a dead ``owner_pid`` is proof of *nothing* by itself while
    ``lease.spawn_pending`` is still set, because that pid is then still the
    *launching supervisor's* own pid (``acquire`` records ``os.getpid()``), not yet
    the real worker subprocess's. ``Supervisor._spawn`` uses
    ``start_new_session=True`` precisely so a misbehaving worker can be killed as a
    whole process group -- but the same property means a session-leader worker
    outlives a dead parent, so "the supervisor died" never implies "no worker is
    running" during that window.

    - ``live``: the recorded pid is alive with a matching create-time. Never reclaim.
    - ``reclaimable``: the recorded pid is provably dead (or was reused by an
      unrelated process) *and* ``spawn_pending`` is already clear, i.e. the recorded
      pid was always the real worker's own pid (``attach_pid`` succeeded before it
      died, mirroring ``recovery.reconcile_tasks``'s use of the worker's own pid).
      Safe to free -- this is the only ``True`` case the old two-way ``_is_stale``
      used to return.
    - ``ambiguous``: the recorded pid is dead while ``spawn_pending`` is still set.
      A session-leader worker process may or may not exist and cannot be ruled out
      from this row alone. Never auto-reclaimed by any caller; always surfaced
      instead, exactly like the "never a timeout" philosophy this module already
      applies to ordinary staleness.
    """

    if not lease.owner_pid:
        return _OWNER_RECLAIMABLE, "no owner pid recorded"
    if pid_is_alive(lease.owner_pid):
        if lease.owner_pid_create_time is not None:
            current = _process_create_time(lease.owner_pid)
            if current is not None and abs(current - lease.owner_pid_create_time) > 1:
                return _OWNER_RECLAIMABLE, f"pid {lease.owner_pid} was reused by a different process (create-time mismatch)"
        return _OWNER_LIVE, None
    if lease.spawn_pending:
        return _OWNER_AMBIGUOUS, (
            f"launching supervisor pid {lease.owner_pid} is dead but its spawn was still in "
            "flight (no worker pid was ever attached); a session-leader worker process may "
            "still be running and cannot be ruled out from this row alone"
        )
    return _OWNER_RECLAIMABLE, f"owner pid {lease.owner_pid} is no longer alive"


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
            classification, why = _classify_owner(current)
            if classification == _OWNER_AMBIGUOUS:
                conflict_reason = (
                    f"worktree {worktree!r} has a spawn in flight from dead supervisor pid "
                    f"{current.owner_pid} (task={current.task_id!r}, generation={current.generation}); "
                    f"refusing to acquire because a worker process cannot be ruled out: {why}"
                )
                state.record_execution_lease_conflict(worktree=worktree, reason=conflict_reason)
                raise ExecutionLeaseConflict(conflict_reason, holder=current)
            if classification == _OWNER_LIVE:
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
    """Record the real launched-subprocess pid once ``Popen`` has returned, clearing
    ``spawn_pending``.

    A ``False`` return means the generation no longer matches: the lease was
    reclaimed out from under the caller in the window between winning the CAS and
    the subprocess actually starting. The caller does not own the worktree and must
    not treat the child it just spawned as its own -- it must be terminated, never
    tracked as a running task (``Supervisor.launch_task`` does exactly this).
    """

    return state.update_execution_lease_pid(
        worktree=worktree,
        expected_generation=generation,
        pid=pid,
        pid_create_time=_process_create_time(pid),
    )


def release(state: State, *, worktree: str, expected_pid: int, reason: str) -> bool:
    """Graceful self-release by the process whose pid is currently the recorded owner."""

    return state.release_execution_lease(worktree=worktree, expected_pid=expected_pid, reason=reason)


def release_before_spawn(state: State, *, worktree: str, generation: int, reason: str) -> bool:
    """Undo a just-won acquisition whose spawn attempt raised before any subprocess
    pid could be attached (``Supervisor._spawn`` failing on bad argv, ``OSError``,
    permissions, a missing interpreter, ...).

    Without this, the lease row stays ``ACQUIRED`` forever with the supervisor's own
    (live) pid recorded as owner: the supervisor is alive, so every future ``acquire``
    correctly refuses to steal it, and the worktree becomes permanently unlaunchable
    for the life of the daemon -- the fail-closed-into-deadlock shape this function
    exists to avoid for a failure the caller already knows about and caused itself
    (unlike the true supervisor-death race ``spawn_pending``/``_classify_owner``
    handle, where nobody is left alive to call this).
    """

    return state.release_execution_lease_before_spawn(
        worktree=worktree, expected_generation=generation, reason=reason
    )


def reconcile_stale_leases(state: State, *, project_id: str | None = None) -> dict[str, int]:
    """Startup/periodic sweep: free only leases whose owner is proven dead.

    Mirrors ``recovery.reconcile_tasks``'s shape (safe to call repeatedly; never
    raises on one bad row). Never reclaims by timeout or executable name -- the
    same ``_classify_owner`` proof ``acquire`` uses inline is reused here so a lease
    can also be freed without anyone attempting to re-acquire it first (surfacing it
    to Attention as recovered, not merely leaving it silently claimable).

    An ``ambiguous`` lease (a dead supervisor mid-spawn; see ``_classify_owner``) is
    never force-released here either -- doing so is exactly the second-writer race
    this sweep exists to prevent, not narrow. It counts toward ``left_held`` like any
    other live-or-unproven lease, but is logged at ``error`` (not ``warning``) so it
    is distinguishable in the event stream and stands out on the Attention surface
    (``stale_leases`` below reports it explicitly) until an operator or a later
    ``attach_pid``/``release_before_spawn`` call from the same acquisition resolves
    it -- never a timeout.
    """

    reclaimed = 0
    left_held = 0
    for lease in state.list_execution_leases(project_id=project_id):
        if lease.status != EXECUTION_LEASE_ACQUIRED:
            continue
        classification, why = _classify_owner(lease)
        if classification == _OWNER_LIVE:
            left_held += 1
            continue
        if classification == _OWNER_AMBIGUOUS:
            left_held += 1
            state.record_event(
                category="execution_lease",
                task_id=lease.task_id,
                level="error",
                message=(
                    f"worktree {lease.worktree!r} has an ambiguous spawn-in-flight lease and "
                    f"was NOT auto-reclaimed: {why}"
                ),
                project_id=lease.project_id,
            )
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
        "spawn_pending": lease.spawn_pending,
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
    """Currently-``ACQUIRED`` leases that are not provably live, for the Attention surface.

    Read-only: unlike ``reconcile_stale_leases`` this never mutates a row, so it is
    safe to call on every dashboard read without racing the daemon's own recovery
    pass. Classification is entirely from typed columns (``status``, ``owner_pid``,
    ``spawn_pending``) and ``recovery.pid_is_alive`` -- never from matching any
    free-text field. Includes both ``reclaimable`` findings (what
    ``reconcile_stale_leases`` would free) and ``ambiguous`` ones (a dead supervisor
    mid-spawn, which it deliberately never auto-frees) so an ambiguous lease is still
    visible to an operator even though nothing will resolve it automatically.
    """

    findings: list[dict[str, Any]] = []
    for lease in state.list_execution_leases(project_id=project_id):
        if lease.status != EXECUTION_LEASE_ACQUIRED:
            continue
        classification, why = _classify_owner(lease)
        if classification != _OWNER_LIVE:
            findings.append({
                **_to_dict(lease),
                "stale_reason": why,
                "reclaimable": classification == _OWNER_RECLAIMABLE,
            })
    return findings
