"""ENG-PC-02 (issue #30): safe task-scoped native agent sessions.

The durable row belongs to the existing :class:`~scripts.agents.control_plane.state.State`
store. Resume is deliberately fail-closed: a session is continued only when the
worker explicitly declares ``cli.resume``, every identity component still matches,
and a screened opaque state value exists. The identity fingerprint is useful for
indexing and a cheap equality fast-path, but never explains an invalidation; the
component-wise comparison below is the sole authority for operator-facing reasons.

Opaque continuation state is stored in SQLite and read only by
:func:`resume_args_for_session`. The resume argv returned by that function is the
one construct from this module that legitimately carries the opaque value. This
module does not put that argv into a RunRecord, manifest, summary, event, or generic
session-listing response. Prompts are never accepted or persisted by this module.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..redaction import redact_text
from ..registry import Worker
from .models import utc_now_iso
from .run_events import RunEvent
from .state import State

MODE_FRESH = "FRESH"
MODE_RESUMED = "RESUMED"

REASON_ADAPTER_DOES_NOT_SUPPORT_RESUME = "adapter does not support resume"
REASON_NO_PRIOR_SESSION = "no prior session"
REASON_TREE_CHANGED = "tree changed"
REASON_WORKTREE_CHANGED = "worktree changed"
REASON_PROVIDER_CHANGED = "provider changed"
REASON_MODEL_CHANGED = "model changed"
REASON_PERMISSION_PROFILE_CHANGED = "permission profile changed"
REASON_POLICY_DIGEST_CHANGED = "policy digest changed"
REASON_OPERATOR_FORCED_FRESH = "operator forced fresh"
REASON_NO_STORED_STATE = "no stored state"
REASON_STORED_STATE_REJECTED_SECRET = "stored state rejected as secret-bearing"
REASON_PROJECT_CHANGED = "project changed"
REASON_TASK_CHANGED = "task changed"
REASON_WORKER_CHANGED = "worker changed"
REASON_CAPABILITY_CHANGED = "capability changed"
REASON_RESUME_COMPATIBLE = "compatible stored session resumed"
REASON_SESSION_STATE_RECORDED = "adapter session continuation state recorded"
REASON_SESSION_OUTCOME_MISSING = "adapter result carried no declared session state"
REASON_SESSION_CONTENTION = "session replacement contention exhausted"

_MAX_SESSION_REPLACE_ATTEMPTS = 3


@dataclass(frozen=True)
class SessionIdentity:
    project_id: str
    task_id: str
    worker: str
    provider: str
    effective_model: str
    worktree_path: str
    tree_sha: str
    permission_profile: str
    capability: str
    policy_digest: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionDecision:
    mode: str
    session_id: str
    continuation_count: int
    session_age_seconds: float | None
    last_activity_at: str
    reason: str
    identity_fingerprint: str


_COMPONENT_REASONS: tuple[tuple[str, str], ...] = (
    ("project_id", REASON_PROJECT_CHANGED),
    ("task_id", REASON_TASK_CHANGED),
    ("worker", REASON_WORKER_CHANGED),
    ("provider", REASON_PROVIDER_CHANGED),
    ("effective_model", REASON_MODEL_CHANGED),
    ("worktree_path", REASON_WORKTREE_CHANGED),
    ("tree_sha", REASON_TREE_CHANGED),
    ("permission_profile", REASON_PERMISSION_PROFILE_CHANGED),
    ("capability", REASON_CAPABILITY_CHANGED),
    ("policy_digest", REASON_POLICY_DIGEST_CHANGED),
)


def _invalidation_reason(stored: Mapping[str, Any], current: SessionIdentity) -> str | None:
    if stored.get("identity_fingerprint") == current.fingerprint:
        return None
    values = asdict(current)
    for field, reason in _COMPONENT_REASONS:
        old = stored.get(field)
        new = values[field]
        if old != new:
            return f"{reason}: stored={old!r}, current={new!r}"
    # A fingerprint implementation/version change is not evidence that policy,
    # tree, or any other named component changed. Component equality wins.
    return None


def _age_seconds(created_at: str, now: str) -> float | None:
    try:
        created = _dt.datetime.fromisoformat(created_at)
        current = _dt.datetime.fromisoformat(now)
        return max(0.0, (current - created).total_seconds())
    except (TypeError, ValueError):
        return None


def _decision(row: Mapping[str, Any], *, mode: str, reason: str, now: str) -> SessionDecision:
    return SessionDecision(
        mode=mode,
        session_id=str(row["id"]),
        continuation_count=int(row["continuation_count"]),
        session_age_seconds=_age_seconds(str(row["created_at"]), now),
        last_activity_at=str(row["last_activity_at"]),
        reason=reason,
        identity_fingerprint=str(row["identity_fingerprint"]),
    )


def _emit(state: State, identity: SessionIdentity, decision: SessionDecision, *, run_id: str | None) -> None:
    derived_run_id = run_id or identity.task_id
    if not derived_run_id:
        return
    try:
        state.record_run_event(
            RunEvent(
                run_id=derived_run_id,
                event_class="session",
                event_type="session.resumed" if decision.mode == MODE_RESUMED else "session.fresh",
                category="agent_session",
                source="control_plane.agent_session",
                provenance="MEASURED",
                task_id=identity.task_id,
                project_id=identity.project_id,
                provider=identity.provider,
                message=(
                    "Compatible task-scoped agent session resumed"
                    if decision.mode == MODE_RESUMED
                    else f"Fresh task-scoped agent session selected: {decision.reason}"
                ),
                data={
                    "session_id": decision.session_id,
                    "mode": decision.mode,
                    "continuation_count": decision.continuation_count,
                    "reason": decision.reason,
                    "worker": identity.worker,
                },
            )
        )
    except Exception:  # noqa: BLE001 - event failure must never undo committed session state
        return


def _fresh_row(identity: SessionIdentity, *, now: str, run_id: str | None, reason: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        **asdict(identity),
        "identity_fingerprint": identity.fingerprint,
        "continuation_state": None,
        "continuation_count": 0,
        "force_fresh_next": 0,
        "active": 1,
        "created_at": now,
        "last_activity_at": now,
        "last_run_id": run_id,
        "last_mode": MODE_FRESH,
        "reason": reason,
        "state_reason": None,
    }


def resolve_session(
    state: State,
    identity: SessionIdentity,
    *,
    worker: Worker | None = None,
    can_resume_session: bool | None = None,
    run_id: str | None = None,
    now: str | None = None,
    force_fresh: bool = False,
) -> SessionDecision:
    """Choose ``FRESH`` or ``RESUMED`` and durably account for the attempt.

    ``worker`` is preferred because it binds support to the registry declaration.
    ``can_resume_session`` is accepted for narrow persistence fixtures; callers may
    not use it to override a supplied worker's declaration.
    """

    now = now or utc_now_iso()
    supported = worker.supports_native_resume if worker is not None else bool(can_resume_session)
    for attempt in range(_MAX_SESSION_REPLACE_ATTEMPTS):
        current = state.get_active_agent_session(
            project_id=identity.project_id, task_id=identity.task_id, worker=identity.worker
        )

        reason: str | None = None
        if not supported:
            reason = REASON_ADAPTER_DOES_NOT_SUPPORT_RESUME
        elif current is None:
            reason = REASON_NO_PRIOR_SESSION
        elif force_fresh or current["force_fresh_next"]:
            reason = REASON_OPERATOR_FORCED_FRESH
        else:
            reason = _invalidation_reason(current, identity)
            if reason is None and not current["has_stored_state"]:
                reason = (
                    REASON_STORED_STATE_REJECTED_SECRET
                    if current.get("state_reason") == REASON_STORED_STATE_REJECTED_SECRET
                    else REASON_NO_STORED_STATE
                )

        if reason is None and current is not None:
            resumed = state.try_resume_agent_session(
                session_id=str(current["id"]),
                identity_fingerprint=identity.fingerprint,
                now=now,
                run_id=run_id,
                reason=REASON_RESUME_COMPATIBLE,
            )
            if resumed is not None:
                decision = _decision(resumed, mode=MODE_RESUMED, reason=REASON_RESUME_COMPATIBLE, now=now)
                _emit(state, identity, decision, run_id=run_id)
                return decision
            # A concurrent process changed the row after our read. Re-read on
            # the next bounded attempt and never resume uncertain state.
            current = state.get_active_agent_session(
                project_id=identity.project_id, task_id=identity.task_id, worker=identity.worker
            )
            reason = REASON_NO_STORED_STATE

        expected_id = str(current["id"]) if current else None
        fresh = _fresh_row(identity, now=now, run_id=run_id, reason=reason or REASON_NO_STORED_STATE)
        stored, won = state.replace_active_agent_session(row=fresh, expected_session_id=expected_id)
        if won:
            decision = _decision(stored, mode=MODE_FRESH, reason=reason or REASON_NO_STORED_STATE, now=now)
            _emit(state, identity, decision, run_id=run_id)
            return decision
        # The partial-index/CAS conflict is an expected concurrent outcome. Its
        # winner is never blindly resumed; the next attempt revalidates it.
        if attempt + 1 < _MAX_SESSION_REPLACE_ATTEMPTS:
            continue

    # Sustained contention must not escape into a daemon caller or borrow the
    # competing row's identity. An unpersisted FRESH decision cannot resume or
    # overwrite opaque state and is therefore the safe degraded outcome.
    fresh = _fresh_row(identity, now=now, run_id=run_id, reason=REASON_SESSION_CONTENTION)
    decision = _decision(fresh, mode=MODE_FRESH, reason=REASON_SESSION_CONTENTION, now=now)
    _emit(state, identity, decision, run_id=run_id)
    return decision


def request_forced_fresh(state: State, *, project_id: str, task_id: str, worker: str) -> bool:
    """Force exactly the next resolve for one active task/worker session fresh."""

    return state.request_agent_session_fresh(project_id=project_id, task_id=task_id, worker=worker)


def record_session_outcome(
    state: State,
    *,
    decision: SessionDecision,
    worker: Worker,
    structured_result: Mapping[str, Any],
    run_id: str | None = None,
    now: str | None = None,
) -> str:
    """Persist only the worker-declared continuation field from structured output.

    Secret-looking opaque state is rejected, never redacted and later replayed.
    The return value is a safe reason suitable for ``RunRecord``; it is never the
    continuation payload itself.
    """

    now = now or utc_now_iso()
    if not worker.supports_native_resume:
        return REASON_ADAPTER_DOES_NOT_SUPPORT_RESUME
    raw = structured_result.get(worker.resume_state_source)
    if not isinstance(raw, str) or not raw:
        state.store_agent_session_state(
            session_id=decision.session_id,
            continuation_state=None,
            now=now,
            run_id=run_id,
            reason=REASON_SESSION_OUTCOME_MISSING,
        )
        return REASON_SESSION_OUTCOME_MISSING
    if redact_text(raw) != raw:
        state.store_agent_session_state(
            session_id=decision.session_id,
            continuation_state=None,
            now=now,
            run_id=run_id,
            reason=REASON_STORED_STATE_REJECTED_SECRET,
        )
        return REASON_STORED_STATE_REJECTED_SECRET
    state.store_agent_session_state(
        session_id=decision.session_id,
        continuation_state=raw,
        now=now,
        run_id=run_id,
        reason=REASON_SESSION_STATE_RECORDED,
    )
    return REASON_SESSION_STATE_RECORDED


def resume_args_for_session(state: State, *, decision: SessionDecision, worker: Worker) -> tuple[str, ...]:
    """Build native resume argv for a positively resolved decision.

    This is the only opaque-state read path. It is gated by the worker's valid
    declaration, a ``RESUMED`` decision, and the exact identity fingerprint.

    SECURITY CONTRACT: the returned argv contains the opaque continuation value.
    Callers must never store it in ``RunRecord.requested_command``, a manifest,
    a summary, or an event.
    """

    if decision.mode != MODE_RESUMED or not worker.supports_native_resume:
        raise ValueError("resume args require a RESUMED decision and a resume-capable worker")
    opaque_state = state._agent_session_resume_state(
        session_id=decision.session_id,
        identity_fingerprint=decision.identity_fingerprint,
    )
    if opaque_state is None:
        raise ValueError(REASON_NO_STORED_STATE)
    return worker.build_resume_args(opaque_state)
