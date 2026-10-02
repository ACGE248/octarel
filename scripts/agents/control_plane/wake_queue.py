"""ENG-PC-03 (issue #31): durable wake queue and trigger coalescing.

Adapts Paperclip's DB-backed wake-request/heartbeat idea into this control plane without
creating a second scheduler. The authoritative scheduling decisions already live
elsewhere -- ``scheduler.Scheduler`` (admission/concurrency), ``runbooks.reconcile_runbooks``
plus ``advancement_lease`` (single-writer acceptance advancement), and the daemon's own
``orchestrator._cmd_run`` loop (the only process that may hold
``advancement_lease.claim_daemon_authority``). This module never schedules, admits, or
advances anything itself; it only durably records *why* a reconcile pass should happen and
coalesces duplicate asks so a burst of identical triggers is one queue row, not N.

A wake's lifecycle (enqueued/coalesced/claimed/completed/failed/poisoned) is emitted into
the existing ENG-PC-04 ``RunEvent`` timeline (``event_class="wake"``) exactly like
``execution_lease.py`` emits ``event_class="lease"`` events -- see that module's
``acquire``/``release`` for the precedent this follows: emit only when a ``run_id`` can be
derived, and never let a logging failure block the underlying state mutation.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from .models import WAKE_POISONED, WAKE_REASONS, WakeRequest
from .run_events import RunEvent
from .state import State

DEFAULT_MAX_ATTEMPTS = 5
# Bounded exponential backoff: 30s, 60s, 120s, 240s, ... capped at one hour. Never
# retried forever -- once ``attempts`` reaches ``max_attempts`` the row becomes the
# explicit ``POISONED`` dead-letter state instead of computing another backoff.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600


class InvalidWakeReason(ValueError):
    """Raised for a reason outside the typed ``WAKE_REASONS`` contract."""


def _validate_reason(reason: str) -> str:
    if reason not in WAKE_REASONS:
        raise InvalidWakeReason(f"unsupported wake reason {reason!r}; expected one of {sorted(WAKE_REASONS)}")
    return reason


def _future_iso(seconds: float) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def backoff_seconds(attempts: int) -> int:
    """Exponential, capped, deterministic from the typed ``attempts`` count alone."""

    return min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


def _emit(
    state: State,
    wake: WakeRequest,
    *,
    event_type: str,
    message: str,
    level: str = "info",
    extra: dict[str, Any] | None = None,
) -> None:
    """Mirror the wake's lifecycle into the existing run-event timeline.

    Only emitted when a ``run_id`` is available, exactly like
    ``execution_lease.acquire``/``release`` -- a wake enqueued before any run/runbook
    exists for it (e.g. a bare ``SCHEDULE``/``OVERNIGHT_TICK`` tick with no specific
    task yet) still queues correctly; it simply has no per-run timeline entry to join.

    Contained at this boundary: unlike ``execution_lease``'s emit (which always runs
    strictly *after* the state mutation it describes is already durable, so a raise
    there can never undo anything), this module's ``claim()`` sits *between* a
    committed state transition and a required follow-up (``complete()``/``fail()``).
    A ``record_run_event`` failure here -- a busy ``BEGIN IMMEDIATE`` under
    contention, or ``normalize_run_event`` rejecting a bad field -- must never be
    allowed to propagate out and abort that follow-up, or the row would be stranded
    CLAIMED with nothing left to move it on. So this is the one place in the module
    that swallows: every caller below may assume ``_emit`` never raises.
    """

    run_id = wake.run_id or wake.task_id
    if not run_id:
        return
    try:
        state.record_run_event(
            RunEvent(
                run_id=run_id,
                event_class="wake",
                event_type=event_type,
                source="control_plane.wake_queue",
                provenance="MEASURED",
                task_id=wake.task_id,
                project_id=wake.project_id,
                level=level,
                message=message,
                data={
                    "wake_id": wake.id,
                    "reason": wake.reason,
                    "stage": wake.stage or "NOT_REPORTED",
                    "status": wake.status,
                    "coalesced_count": wake.coalesced_count,
                    "attempts": wake.attempts,
                    "max_attempts": wake.max_attempts,
                    **(extra or {}),
                },
            )
        )
    except Exception:  # noqa: BLE001 - a logging failure must never block the state transition it follows
        pass


def enqueue(
    state: State,
    *,
    reason: str,
    source: str,
    project_id: str | None = None,
    task_id: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
    provenance: str = "MEASURED",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> WakeRequest:
    """Durably request a wake, coalescing into any existing pending wake for the same
    project/task/stage. Always returns the current (possibly coalesced) row.

    ``source`` is a short, stable token naming the caller (e.g. a module/function name)
    and is recorded on the contribution row for provenance, never inferred or guessed.
    """

    _validate_reason(reason)
    wake = state.enqueue_wake(
        project_id=project_id,
        task_id=task_id,
        stage=stage,
        reason=reason,
        source=source,
        provenance=provenance,
        run_id=run_id,
        max_attempts=max_attempts,
    )
    first_contribution = wake.coalesced_count == 1
    _emit(
        state,
        wake,
        event_type="wake.enqueued" if first_contribution else "wake.coalesced",
        message=(
            f"Wake requested: {reason}"
            if first_contribution
            else f"Wake coalesced: {reason} (now {wake.coalesced_count} pending triggers)"
        ),
    )
    return wake


def claim(state: State, *, claimed_by: str) -> WakeRequest | None:
    """Claim the oldest due wake (``PENDING`` or backoff-elapsed ``FAILED``). ``None`` if
    nothing is due. Never blocks, never schedules anything itself -- the caller decides
    what, if anything, a claimed wake should trigger."""

    wake = state.claim_next_wake(claimed_by=claimed_by)
    if wake is not None:
        _emit(state, wake, event_type="wake.claimed", message=f"Wake claimed by {claimed_by}: {wake.reason}")
    return wake


def complete(state: State, *, wake_id: int) -> bool:
    """Mark a claimed wake handled. ``False`` if it was not (still) ``CLAIMED``."""

    wake = state.complete_wake(wake_id=wake_id)
    if wake is None:
        return False
    _emit(state, wake, event_type="wake.completed", message=f"Wake completed: {wake.reason}")
    return True


def fail(state: State, *, wake_id: int, error: str) -> WakeRequest | None:
    """Record a failed processing attempt. Below ``max_attempts`` this schedules a bounded
    backoff retry (``FAILED`` with ``next_attempt_at`` set); at or above it, the row becomes
    the explicit ``POISONED`` dead-letter state and is never retried again -- surfaced via
    ``attention`` instead. The decision is made entirely from the typed ``attempts``/
    ``max_attempts`` columns, never by matching ``error`` text.
    """

    current = state.get_wake(wake_id)
    if current is None:
        return None
    next_attempt_at = _future_iso(backoff_seconds(current.attempts))
    wake = state.fail_wake(wake_id=wake_id, error=error, next_attempt_at=next_attempt_at)
    if wake is None:
        return None
    if wake.status == WAKE_POISONED:
        _emit(
            state, wake, event_type="wake.poisoned", level="error",
            message=f"Wake poisoned after {wake.attempts} attempt(s): {wake.reason}: {error}",
        )
    else:
        _emit(
            state, wake, event_type="wake.failed", level="warning",
            message=f"Wake attempt {wake.attempts} failed, retrying after backoff: {wake.reason}: {error}",
            extra={"next_attempt_at": wake.next_attempt_at or "NOT_REPORTED"},
        )
    return wake


def _safe_record_event(state: State, **kwargs: Any) -> None:
    """Record an operator-facing event, but never let the recording itself raise.

    Used only by the recovery paths below, which must stay no-raise end to end (see
    ``drain_due``'s and ``recover_on_restart``'s own "never raises" promises) -- a
    failure to log a recovery must never become a second, different way for queue
    state to take the daemon down.
    """

    try:
        state.record_event(**kwargs)
    except Exception:  # noqa: BLE001 - logging must never be fatal
        pass


def _recover_stranded_claims(state: State, *, claimed_by: str) -> None:
    """Self-heal within this same daemon, no restart and no timeout: resolve any wake
    still ``CLAIMED`` by this ``claimed_by`` identity from a previous pass (back to
    ``PENDING``, or merged into an already-PENDING sibling -- see
    ``State.recover_claimed_wakes``) before a new pass claims anything.

    ``claimed_by`` is a single fixed identity per daemon (``orchestrator._cmd_run``
    always calls ``drain_due`` with ``claimed_by="daemon"``), so a row still ``CLAIMED``
    under it when a new pass begins cannot belong to any other claimant -- it can only
    be this same process's own prior ``claim()``/``complete()`` sequence, which must
    have raised somewhere between the two (e.g. ``complete_wake``'s own write hitting a
    busy database under contention). That is identity evidence, not an elapsed-time
    guess, so it is safe to run on every pass rather than inferring staleness.

    Never raises: this runs at the very start of every ``drain_due`` pass, and
    ``drain_due``'s own docstring already promises the daemon loop never dies on a bad
    row here, not just on a bad claim/complete later in the same pass.
    """

    try:
        outcome = state.recover_claimed_wakes(claimed_by=claimed_by)
    except Exception as exc:  # noqa: BLE001 - queue-state recovery must never be fatal to the daemon loop
        _safe_record_event(
            state, category="wake_queue", level="error",
            message=f"drain_due: recovering wakes CLAIMED by {claimed_by!r} failed, skipped this pass: {exc}",
        )
        return
    if outcome.reset:
        _safe_record_event(
            state, category="wake_queue", level="warning",
            message=(
                f"drain_due: {outcome.reset} wake(s) still CLAIMED by {claimed_by!r} from a prior "
                "pass reset to PENDING (claim/complete did not reach COMPLETED)"
            ),
        )
    if outcome.merged:
        _safe_record_event(
            state, category="wake_queue", level="warning",
            message=(
                f"drain_due: {outcome.merged} wake(s) still CLAIMED by {claimed_by!r} from a prior "
                "pass merged into an already-PENDING sibling for the same project/task/stage"
            ),
        )


def drain_due(state: State, *, claimed_by: str, limit: int = 10) -> int:
    """Claim and mark handled up to ``limit`` currently-due wakes. Returns how many.

    This is pure bookkeeping, never a second scheduler: it does not itself run
    ``reconcile_runbooks``, ``overnight_tick``, or admission -- the daemon's existing main
    loop already runs those unconditionally every tick (see ``orchestrator._cmd_run``)
    regardless of queue contents. Calling this right after that unconditional pass is an
    honest "the daemon did examine state after this was requested" record, never a claim
    that the wake *caused* the reconciliation. A claimed wake is marked ``COMPLETED``
    immediately rather than left ``CLAIMED`` across ticks, because nothing here performs
    multi-tick work on a wake's behalf; a future producer that needs the pass to actually
    fail/retry a specific wake should call ``claim``/``complete``/``fail`` directly instead
    of this helper. Never raises -- a bad row is skipped, not fatal to the daemon loop.

    Every pass opens with ``_recover_stranded_claims`` so a row this same daemon left
    ``CLAIMED`` on a prior pass (``complete_wake`` itself raising, e.g. a busy database
    under contention -- ``_emit`` is contained and can no longer be the cause) is
    returned to ``PENDING`` and can be claimed again in this same pass, not just on the
    next restart.
    """

    _recover_stranded_claims(state, claimed_by=claimed_by)
    drained = 0
    for _ in range(max(0, limit)):
        try:
            wake = claim(state, claimed_by=claimed_by)
        except Exception:  # noqa: BLE001 - the daemon loop must never die on a drain error
            break
        if wake is None:
            break
        try:
            complete(state, wake_id=wake.id)
        except Exception:  # noqa: BLE001 - see above; recovered on the next pass by
            # _recover_stranded_claims rather than left stuck until a restart
            pass
        drained += 1
    return drained


def recover_on_restart(state: State, *, project_id: str | None = None) -> int:
    """Daemon-start recovery: any wake left ``CLAIMED`` at this point belonged to a
    previous process instance that is now gone, so it is resolved -- reset to
    ``PENDING`` and retried, or merged into an already-PENDING sibling for the same
    key (see ``State.recover_claimed_wakes``) -- rather than silently lost (restart
    durability). Mirrors ``runbooks.recover_runbooks_on_restart``'s shape: a single
    bounded sweep logged once, not a per-row event flood.

    This is restart-scoped, not the only route a ``CLAIMED`` row can recover through:
    a *live* daemon can also strand one mid-run (``complete_wake`` itself raising), and
    that case is recovered every ``drain_due`` pass by ``_recover_stranded_claims``
    instead -- this function only ever needs to run once, at process start, because a
    live daemon no longer depends on it to get unstuck.

    Never raises: daemon startup must not be blocked by recoverable queue state. A
    failure here is reported and treated as "nothing recovered yet" rather than
    aborting the start -- the very next ``drain_due`` pass gets another chance at the
    same rows via ``_recover_stranded_claims``.
    """

    stranded = [
        wake
        for wake in state.list_wakes(project_id=project_id, status="CLAIMED")
    ]
    try:
        outcome = state.recover_claimed_wakes(project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - queue-state recovery must never block daemon startup
        _safe_record_event(
            state, category="wake_queue", level="error",
            message=f"daemon restart: recovering claimed wakes failed, continuing startup: {exc}",
            project_id=project_id,
        )
        return 0
    for prior in stranded:
        current = state.get_wake(prior.id)
        recovered = current or prior
        _emit(
            state,
            recovered,
            event_type="wake.recovered",
            level="warning",
            message=(
                f"Wake recovered after daemon restart: {prior.reason} "
                f"({'reset to PENDING' if current is not None else 'merged into pending sibling'})"
            ),
            extra={"recovery_outcome": "RESET" if current is not None else "MERGED"},
        )
    if outcome.reset:
        _safe_record_event(
            state, category="wake_queue", level="warning",
            message=f"daemon restart: {outcome.reset} claimed wake(s) reset to PENDING for retry",
            project_id=project_id,
        )
    if outcome.merged:
        _safe_record_event(
            state, category="wake_queue", level="warning",
            message=(
                f"daemon restart: {outcome.merged} claimed wake(s) merged into an "
                "already-PENDING sibling for the same project/task/stage"
            ),
            project_id=project_id,
        )
    return outcome.total


@dataclass(frozen=True)
class QueueDepth:
    pending: int
    claimed: int
    failed_awaiting_retry: int
    poisoned: int
    oldest_pending_age_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "claimed": self.claimed,
            "failed_awaiting_retry": self.failed_awaiting_retry,
            "poisoned": self.poisoned,
            "oldest_pending_age_seconds": self.oldest_pending_age_seconds,
        }


def queue_depth(state: State, *, project_id: str | None = None) -> QueueDepth:
    """Read-only projection for the System surface: depth and oldest age, local state
    only, no provider/AI call of any kind. Safe to call on every dashboard read."""

    pending = state.list_wakes(project_id=project_id, status="PENDING", limit=10_000)
    claimed = state.list_wakes(project_id=project_id, status="CLAIMED", limit=10_000)
    failed = state.list_wakes(project_id=project_id, status="FAILED", limit=10_000)
    poisoned = state.list_wakes(project_id=project_id, status="POISONED", limit=10_000)
    oldest_age: float | None = None
    if pending:
        oldest_created = min(pending, key=lambda w: w.created_at).created_at
        try:
            parsed = _dt.datetime.fromisoformat(oldest_created)
            oldest_age = max(0.0, (_dt.datetime.now(_dt.timezone.utc) - parsed).total_seconds())
        except ValueError:  # pragma: no cover - created_at is always our own isoformat
            oldest_age = None
    return QueueDepth(
        pending=len(pending),
        claimed=len(claimed),
        failed_awaiting_retry=len(failed),
        poisoned=len(poisoned),
        oldest_pending_age_seconds=oldest_age,
    )


def attention(state: State, *, project_id: str | None = None) -> list[dict[str, Any]]:
    """Poisoned (dead-letter) wakes for the Attention surface.

    Deliberately limited to the explicit ``POISONED`` state -- never a time-based "this
    CLAIMED row looks stuck" guess. That mirrors ``execution_lease``'s refusal to classify
    staleness from elapsed time: a wake is only ever surfaced here once typed evidence
    (``attempts >= max_attempts``) proves it, not inferred from how long it has sat.
    """

    return [
        {
            "id": wake.id,
            "project_id": wake.project_id,
            "task_id": wake.task_id,
            "stage": wake.stage,
            "reason": wake.reason,
            "coalesced_count": wake.coalesced_count,
            "attempts": wake.attempts,
            "max_attempts": wake.max_attempts,
            "last_error": wake.last_error,
            "created_at": wake.created_at,
            "updated_at": wake.updated_at,
        }
        for wake in state.list_wakes(project_id=project_id, status=WAKE_POISONED, limit=500)
    ]
