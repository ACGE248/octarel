"""Durable typed operator approvals and changed-state handoffs (ENG-PC-10).

Approvals are deliberately narrower than the command layer.  A request names
one of five decision classes and supplies only that class's fixed, validated
fields.  Classification, risk, impact summary, project attribution and the
state fingerprint are all derived again by the server; no command string,
``safe`` flag, or client risk label is accepted or persisted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .commands import apply_command
from .run_events import RunEvent, sanitize_text

ACTION_DESTRUCTIVE_CLEANUP = "DESTRUCTIVE_CLEANUP"
ACTION_METERED_OVERFLOW_ROUTE = "METERED_OVERFLOW_ROUTE"
ACTION_MATERIAL_SCOPE_CHANGE = "MATERIAL_SCOPE_CHANGE"
ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE = "AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE"
ACTION_REMOTE_SENSITIVE = "REMOTE_SENSITIVE_ACTION"

APPROVAL_ACTIONS = frozenset(
    {
        ACTION_DESTRUCTIVE_CLEANUP,
        ACTION_METERED_OVERFLOW_ROUTE,
        ACTION_MATERIAL_SCOPE_CHANGE,
        ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE,
        ACTION_REMOTE_SENSITIVE,
    }
)

STATE_PENDING = "PENDING"
STATE_EXECUTING = "EXECUTING"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"
STATE_EXPIRED = "EXPIRED"
STATE_STALE = "STALE"
STATE_FAILED_SAFE = "FAILED_SAFE"

TERMINAL_STATES = frozenset(
    {STATE_APPROVED, STATE_REJECTED, STATE_EXPIRED, STATE_STALE, STATE_FAILED_SAFE}
)

_RISKS = {
    ACTION_DESTRUCTIVE_CLEANUP: "HIGH",
    ACTION_METERED_OVERFLOW_ROUTE: "CRITICAL",
    ACTION_MATERIAL_SCOPE_CHANGE: "HIGH",
    ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE: "MEDIUM",
    ACTION_REMOTE_SENSITIVE: "HIGH",
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_SECRET_SHAPE = re.compile(
    r"(?i)(authorization|bearer\s+|password|passwd|api[_-]?key|access[_-]?token|private[_-]?key|secret)"
)
_MAX_REASON = 1_000
_MAX_DECISION = 1_000
_MIN_EXPIRY_SECONDS = 60
_MAX_EXPIRY_SECONDS = 24 * 60 * 60


class ApprovalError(ValueError):
    """Safe, operator-actionable approval refusal."""


class ApprovalConflict(ApprovalError):
    """The durable request is expired, stale, or no longer pending."""


@dataclass(frozen=True)
class ApprovalPlan:
    action_type: str
    risk: str
    payload: dict[str, Any]
    safe_payload_summary: dict[str, Any]
    state_fingerprint: str
    state_revision: str


def _stable_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ApprovalError(f"{name} must be a non-empty stable token")
    return value


def _safe_decision_text(name: str, value: Any, *, maximum: int = _MAX_DECISION) -> str:
    if not isinstance(value, str):
        raise ApprovalError(f"{name} must be a string")
    clean = sanitize_text(value).strip()
    if not clean:
        raise ApprovalError(f"{name} must not be empty")
    if len(clean) > maximum:
        raise ApprovalError(f"{name} exceeds {maximum} characters")
    if clean != value.strip() or _SECRET_SHAPE.search(clean):
        raise ApprovalError(f"{name} contains secret-, credential-, or host-path-shaped text")
    return clean


def _payload(raw: Mapping[str, Any] | None, allowed: set[str]) -> dict[str, Any]:
    if raw is None:
        body: dict[str, Any] = {}
    elif isinstance(raw, Mapping):
        body = dict(raw)
    else:
        raise ApprovalError("payload must be an object")
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ApprovalError(f"unsupported approval payload field(s): {', '.join(unexpected)}")
    if any(_SECRET_SHAPE.search(str(key)) for key in body):
        raise ApprovalError("approval payload may not contain secret-bearing fields")
    return body


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _selected_project_id(ctx: Any) -> str:
    project_id = getattr(ctx, "selected_project_id", None)
    if not project_id:
        raise ApprovalError("no managed project is selected")
    return _stable_token("project_id", project_id)


def _task_scope(ctx: Any, project_id: str, task_id: str | None) -> dict[str, Any] | None:
    if task_id is None:
        return None
    task_id = _stable_token("task_id", task_id)
    task = ctx.state.get_task(task_id)
    if task is None or task.project_id != project_id:
        raise ApprovalError("approval task does not belong to the selected project")
    return {"id": task.id, "task_ref": task.task_ref, "state": task.state, "updated_at": task.updated_at}


def _run_scope(ctx: Any, project_id: str, run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    run_id = _stable_token("run_id", run_id)
    runbook = ctx.state.get_runbook(run_id)
    if runbook is None or runbook.project_id != project_id:
        raise ApprovalError("approval run does not belong to the selected project")
    return {
        "id": runbook.id,
        "task_id": runbook.task_id,
        "status": runbook.status,
        "updated_at": runbook.updated_at,
        "acceptance_stage": runbook.acceptance_stage,
    }


def _decision_scope(
    ctx: Any,
    *,
    action_type: str,
    raw_payload: Mapping[str, Any] | None,
    revalidation: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if action_type == ACTION_DESTRUCTIVE_CLEANUP:
        # The public creation contract accepts no cleanup target.  Resolution
        # receives the server-derived execution identity persisted on the row,
        # but discards and re-derives it below so a client can never choose a
        # project root or worktree path.
        _payload(
            raw_payload,
            {"approved_project_id", "approved_project_root", "approved_paths"}
            if revalidation
            else set(),
        )
        from .operations import refresh_worktree_statuses

        rows = refresh_worktree_statuses(ctx)
        eligible = [row for row in rows if row.get("cleanup_eligible") and not row.get("canonical_checkout")]
        project_id = _selected_project_id(ctx)
        approved_paths = sorted(str(row.get("path")) for row in eligible)
        body = {
            "approved_project_id": project_id,
            "approved_project_root": str(ctx.project_root.resolve()),
            "approved_paths": approved_paths,
        }
        state = {
            "eligible": [
                {
                    # The path stays inside the non-public fingerprint input.
                    # Binding it prevents a different checkout with the same
                    # branch/classification shape from inheriting approval.
                    "path": row.get("path"),
                    "branch": row.get("branch") or "UNKNOWN",
                    "classification": row.get("classification") or "UNKNOWN",
                    "dirty": bool(row.get("dirty")),
                }
                for row in eligible
            ],
            "worktree_count": len(rows),
        }
        summary = {
            "operation": "Remove only worktrees still proven FINISHED_CLEAN by the server",
            "eligible_count": len(eligible),
            "protected_count": len(rows) - len(eligible),
            "impact": "Eligible worktree directories may be removed; canonical, dirty, active, paused, queued, and unowned worktrees remain protected.",
        }
        return body, summary, state

    if action_type == ACTION_METERED_OVERFLOW_ROUTE:
        body = _payload(
            raw_payload,
            {"runbook_id", "approved_state"} if revalidation else {"runbook_id"},
        )
        runbook_id = _stable_token("runbook_id", body.get("runbook_id"))
        project_id = _selected_project_id(ctx)
        runbook = ctx.state.get_runbook(runbook_id)
        if runbook is None or runbook.project_id != project_id:
            raise ApprovalError("route-override run does not belong to the selected project")
        usage = ctx.state.get_usage_governance(runbook_id) or {}
        state = {
            "project_id": runbook.project_id,
            "runbook_id": runbook.id,
            "status": runbook.status,
            "updated_at": runbook.updated_at,
            "codex_policy": runbook.codex_policy,
            "codex_auto_eligible": runbook.codex_auto_eligible,
            "max_codex_invocations": runbook.max_codex_invocations,
            "codex_invocations": int(usage.get("codex_invocations", 0)),
            "escalation_state": usage.get("escalation_state"),
        }
        body = {"runbook_id": runbook_id, "approved_state": state}
        summary = {
            "operation": "Authorize one additional existing premium route invocation",
            "runbook_id": runbook_id,
            "impact": "Uses the existing usage-override safeguard; it cannot enable an API key, a disabled provider, paid overflow, or bypass launch/budget/sandbox checks.",
        }
        return body, summary, state

    if action_type == ACTION_MATERIAL_SCOPE_CHANGE:
        body = _payload(raw_payload, {"scope_ref", "decision"})
        body = {
            "scope_ref": _stable_token("scope_ref", body.get("scope_ref")),
            "decision": _safe_decision_text("decision", body.get("decision")),
        }
        summary = {
            "operation": "Record a material scope decision for the bounded task",
            "scope_ref": body["scope_ref"],
            "decision": body["decision"],
            "impact": "Records the handoff only; it does not edit repository policy, waive tests/review, or execute a command.",
        }
        return body, summary, {"decision": body}

    if action_type == ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE:
        body = _payload(raw_payload, {"question_ref", "decision"})
        body = {
            "question_ref": _stable_token("question_ref", body.get("question_ref")),
            "decision": _safe_decision_text("decision", body.get("decision")),
        }
        summary = {
            "operation": "Record an operator product or architecture choice",
            "question_ref": body["question_ref"],
            "decision": body["decision"],
            "impact": "Records the decision for the run; deterministic review and gate requirements remain unchanged.",
        }
        return body, summary, {"decision": body}

    if action_type == ACTION_REMOTE_SENSITIVE:
        body = _payload(raw_payload, {"service_id", "action", "worktree_path", "runbook_id"})
        service_id = _stable_token("service_id", body.get("service_id"))
        action = _stable_token("action", body.get("action"))
        if action not in {"start", "stop", "restart"}:
            raise ApprovalError("remote-sensitive runtime action must be start, stop, or restart")
        worktree_path = body.get("worktree_path")
        if worktree_path is not None:
            worktree_path = str(worktree_path)
        runbook_id = body.get("runbook_id")
        if runbook_id is not None:
            runbook_id = _stable_token("runbook_id", runbook_id)
        services = ctx.approval_runtime_manager.list_services(include_scopes=True)
        service = next((row for row in services if row.get("id") == service_id), None)
        if service is None:
            raise ApprovalError("runtime service does not belong to the selected project")
        if worktree_path is not None and worktree_path != service.get("worktree_path"):
            raise ApprovalError("runtime service worktree scope changed")
        if runbook_id is not None and runbook_id != service.get("runbook_id"):
            raise ApprovalError("runtime service run scope changed")
        if not bool((service.get("actions") or {}).get(action)):
            raise ApprovalError(f"runtime service {action} is not currently server-eligible")
        body = {
            "service_id": service_id,
            "action": action,
            "worktree_path": service.get("worktree_path"),
            "runbook_id": service.get("runbook_id"),
        }
        state = {
            "service_id": service_id,
            "health": service.get("health"),
            "ownership": service.get("ownership"),
            "pid": service.get("pid"),
            "process_create_time": service.get("process_create_time"),
            "process_session_id": service.get("process_session_id"),
            "cwd": service.get("cwd"),
            "argv": service.get("argv"),
            "worktree_path": service.get("worktree_path"),
            "runbook_id": service.get("runbook_id"),
            "action_eligible": bool((service.get("actions") or {}).get(action)),
            "eligible": True,
        }
        summary = {
            "operation": f"{action.title()} the declared runtime service",
            "service_id": service_id,
            "runbook_id": service.get("runbook_id"),
            "impact": "The runtime supervisor will re-prove the selected-project scope and full process identity before any launch or signal.",
        }
        return body, summary, state

    raise ApprovalError(f"unsupported approval action {action_type!r}")


def build_plan(
    ctx: Any,
    *,
    action_type: str,
    payload: Mapping[str, Any] | None,
    task_id: str | None,
    run_id: str | None,
    revalidation: bool = False,
) -> ApprovalPlan:
    """Derive classification, impact and current-state identity on the server."""

    action_type = _stable_token("action_type", action_type)
    if action_type not in APPROVAL_ACTIONS:
        raise ApprovalError(f"unsupported approval action {action_type!r}")
    project_id = _selected_project_id(ctx)
    task_scope = _task_scope(ctx, project_id, task_id)
    run_scope = _run_scope(ctx, project_id, run_id)
    normalized, summary, action_state = _decision_scope(
        ctx,
        action_type=action_type,
        raw_payload=payload,
        revalidation=revalidation,
    )
    fingerprint_payload = {
        "project_id": project_id,
        "action_type": action_type,
        "payload": normalized,
        "task": task_scope,
        "run": run_scope,
        "action_state": action_state,
    }
    fingerprint = _canonical_digest(fingerprint_payload)
    return ApprovalPlan(
        action_type=action_type,
        risk=_RISKS[action_type],
        payload=normalized,
        safe_payload_summary=summary,
        state_fingerprint=fingerprint,
        state_revision=f"sha256:{fingerprint}",
    )


def create_request(
    ctx: Any,
    *,
    action_type: str,
    payload: Mapping[str, Any] | None,
    reason: str,
    requested_by: str,
    task_id: str | None = None,
    run_id: str | None = None,
    expires_in_seconds: int = 3_600,
) -> dict[str, Any]:
    project_id = _selected_project_id(ctx)
    reason = _safe_decision_text("reason", reason, maximum=_MAX_REASON)
    requested_by = _safe_decision_text("requested_by", requested_by, maximum=320)
    try:
        expiry = int(expires_in_seconds)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("expires_in_seconds must be an integer") from exc
    if not _MIN_EXPIRY_SECONDS <= expiry <= _MAX_EXPIRY_SECONDS:
        raise ApprovalError("expires_in_seconds must be between 60 and 86400")
    plan = build_plan(
        ctx, action_type=action_type, payload=payload, task_id=task_id, run_id=run_id
    )
    now = dt.datetime.now(dt.UTC)
    row = {
        "id": f"approval-{uuid.uuid4().hex}",
        "project_id": project_id,
        "task_id": task_id,
        "run_id": run_id,
        "action_type": plan.action_type,
        "risk": plan.risk,
        "safe_payload_summary": plan.safe_payload_summary,
        "action_payload": plan.payload,
        "reason": reason,
        "requested_by": requested_by,
        "expires_at": (now + dt.timedelta(seconds=expiry)).isoformat(timespec="seconds"),
        "state": STATE_PENDING,
        "state_fingerprint": plan.state_fingerprint,
        "state_revision": plan.state_revision,
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "resolved_at": None,
        "resolved_by": None,
        "resolution_note": None,
        "result_summary": None,
    }
    created = ctx.state.create_approval_request(row)
    _record_event(ctx, created, "approval.requested", requested_by, "approval requested")
    return created


def _record_event(ctx: Any, request: Mapping[str, Any], event_type: str, actor: str, message: str) -> None:
    ctx.state.record_run_event(
        RunEvent(
            run_id=request.get("run_id") or request["id"],
            event_class="approval",
            event_type=event_type,
            source="control_plane.approvals",
            provenance="MEASURED",
            message=message,
            task_id=request.get("task_id"),
            project_id=request["project_id"],
            level="warning" if event_type in {"approval.stale", "approval.expired", "approval.failed_safe"} else "info",
            data={
                "approval_id": request["id"],
                "action_type": request["action_type"],
                "risk": request["risk"],
                "state": request["state"],
                "actor": actor,
            },
        )
    )


def _execute(ctx: Any, request: Mapping[str, Any], actor: str) -> dict[str, Any]:
    action_type = request["action_type"]
    payload = request["action_payload"]
    if action_type == ACTION_DESTRUCTIVE_CLEANUP:
        result = apply_command(
            ctx,
            "worktree_cleanup",
            confirm=True,
            approved_project_id=payload["approved_project_id"],
            approved_project_root=payload["approved_project_root"],
            approved_paths=payload["approved_paths"],
        )
        return {
            "ok": result.ok,
            "message": result.message,
            "removed": (result.data or {}).get("removed", []),
            "failed": (result.data or {}).get("failed", []),
        }
    if action_type == ACTION_METERED_OVERFLOW_ROUTE:
        result = apply_command(
            ctx,
            "usage_override",
            runbook_id=payload["runbook_id"],
            reason=request["reason"],
            expected_state=payload["approved_state"],
        )
        return {"ok": result.ok, "message": result.message}
    if action_type in {
        ACTION_MATERIAL_SCOPE_CHANGE,
        ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE,
    }:
        return {
            "ok": True,
            "message": "operator decision recorded; no executable command was authorized",
        }
    if action_type == ACTION_REMOTE_SENSITIVE:
        result = ctx.approval_runtime_manager.action(
            payload["action"],
            actor,
            worktree_path=payload.get("worktree_path"),
            runbook_id=payload.get("runbook_id"),
            service_id=payload["service_id"],
        )
        return {
            "ok": True,
            "message": f"runtime service {payload['action']} completed",
            "service_id": result.get("id"),
            "health": result.get("health"),
        }
    raise ApprovalError("approval action has no allowlisted handler")


def resolve_request(
    ctx: Any,
    *,
    approval_id: str,
    decision: str,
    resolved_by: str,
    resolution_note: str,
) -> dict[str, Any]:
    """Atomically claim a resolution, then run exactly one typed handler."""

    approval_id = _stable_token("approval_id", approval_id)
    resolved_by = _safe_decision_text("resolved_by", resolved_by, maximum=320)
    resolution_note = _safe_decision_text("resolution_note", resolution_note)
    decision = _stable_token("decision", decision).upper()
    if decision not in {"APPROVE", "REJECT"}:
        raise ApprovalError("decision must be APPROVE or REJECT")
    project_id = _selected_project_id(ctx)
    current = ctx.state.get_approval_request(approval_id, project_id=project_id)
    if current is None:
        raise ApprovalError("approval does not belong to the selected project")
    if current["state"] != STATE_PENDING:
        raise ApprovalConflict(f"approval is already {current['state']}")

    fingerprint = None
    revision = None
    if decision == "APPROVE":
        try:
            plan = build_plan(
                ctx,
                action_type=current["action_type"],
                payload=current["action_payload"],
                task_id=current.get("task_id"),
                run_id=current.get("run_id"),
                revalidation=True,
            )
        except ApprovalError:
            # Missing/deleted scope or newly ineligible handler state is itself
            # a changed-state result. Feed a guaranteed mismatch into the CAS
            # so an expired request still becomes EXPIRED (expiry wins), while
            # an unexpired one becomes durably STALE rather than lingering.
            fingerprint = "current-state-unavailable"
            revision = "current-state-unavailable"
        else:
            # Classification itself is immutable and server-owned. A future
            # code change that maps the action differently invalidates old
            # approvals.
            if plan.action_type != current["action_type"] or plan.risk != current["risk"]:
                fingerprint = "classification-changed"
                revision = "classification-changed"
            else:
                fingerprint = plan.state_fingerprint
                revision = plan.state_revision

    try:
        claimed = ctx.state.claim_approval_resolution(
            approval_id,
            project_id=project_id,
            decision=decision,
            resolved_by=resolved_by,
            resolution_note=resolution_note,
            current_fingerprint=fingerprint,
            current_revision=revision,
        )
    except ValueError as exc:
        raise ApprovalConflict(str(exc)) from None
    if claimed["state"] == STATE_REJECTED:
        _record_event(ctx, claimed, "approval.rejected", resolved_by, "approval rejected")
        return claimed
    if claimed["state"] == STATE_EXPIRED:
        _record_event(ctx, claimed, "approval.expired", resolved_by, "expired approval refused")
        raise ApprovalConflict("approval expired before resolution")
    if claimed["state"] == STATE_STALE:
        _record_event(ctx, claimed, "approval.stale", resolved_by, "stale approval refused")
        raise ApprovalConflict("approval state changed; create a fresh request from current server state")
    if claimed["state"] != STATE_EXECUTING:
        raise ApprovalConflict(f"approval is already {claimed['state']}")

    result: dict[str, Any] | None = None
    try:
        result = _execute(ctx, claimed, resolved_by)
        if not result.get("ok"):
            raise ApprovalError(str(result.get("message") or "typed approval handler failed"))
    except Exception as exc:  # noqa: BLE001 - every handler failure must become FAILED_SAFE
        failure_summary = dict(result or {})
        failure_summary["ok"] = False
        failure_summary["message"] = sanitize_text(str(exc))[:1_000]
        failed = ctx.state.finalize_approval_request(
            approval_id,
            project_id=project_id,
            state=STATE_FAILED_SAFE,
            result_summary=failure_summary,
        )
        _record_event(ctx, failed, "approval.failed_safe", resolved_by, "approved action failed safely")
        raise ApprovalConflict(f"approved action failed safely: {sanitize_text(str(exc))}") from None

    approved = ctx.state.finalize_approval_request(
        approval_id,
        project_id=project_id,
        state=STATE_APPROVED,
        result_summary=result,
    )
    _record_event(ctx, approved, "approval.approved", resolved_by, "approval executed")
    return approved


def expire_pending(ctx: Any) -> int:
    """Mark selected-project requests whose explicit deadline has passed."""

    project_id = _selected_project_id(ctx)
    expired = ctx.state.expire_approval_requests(project_id=project_id)
    for request in expired:
        _record_event(
            ctx,
            request,
            "approval.expired",
            "system:expiry",
            "approval expired before resolution",
        )
    return len(expired)


__all__ = [
    "ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE",
    "ACTION_DESTRUCTIVE_CLEANUP",
    "ACTION_MATERIAL_SCOPE_CHANGE",
    "ACTION_METERED_OVERFLOW_ROUTE",
    "ACTION_REMOTE_SENSITIVE",
    "APPROVAL_ACTIONS",
    "ApprovalConflict",
    "ApprovalError",
    "build_plan",
    "create_request",
    "expire_pending",
    "resolve_request",
]
