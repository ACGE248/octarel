"""ENG-PC-10 durable typed approvals, API boundaries, and hard safeguards."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import approvals
from scripts.agents.control_plane.commands import CommandContext, CommandResult
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import ProviderState, Runbook, Task
from scripts.agents.control_plane.project import ProjectContract
from scripts.agents.control_plane.project_registry import (
    contract_to_row,
    select_project,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def _context(root: Path, *, db_path: Path | None = None, project_id: str = "project-a") -> CommandContext:
    root.mkdir(parents=True, exist_ok=True)
    state = State(db_path or root / "state.db")
    if state.get_project(project_id) is None:
        state.upsert_project(
            contract_to_row(
                ProjectContract(project_id=project_id, display_name=project_id, local_repo_root=root)
            )
        )
    select_project(state, project_id)
    registry = load_registry()
    ctx = CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=root, state=state),
        repo_root=root,
    )
    ctx.approval_runtime_manager = SimpleNamespace(list_services=lambda **_kw: [])
    return ctx


def _seed_run(ctx: CommandContext, *, run_id: str = "run-1", task_id: str = "task-1") -> Runbook:
    project_id = ctx.selected_project_id
    ctx.state.upsert_task(
        Task(
            id=task_id,
            task_ref="ENG-PC-10",
            role="primary-implementation",
            worker="codex-build",
            project_id=project_id,
        )
    )
    runbook = Runbook(
        id=run_id,
        name="Typed approval fixture",
        preset="test-fix",
        objective="exercise typed approval",
        source_ref="ENG-PC-10",
        branch="eng/approval-fixture",
        worktree=str(ctx.repo_root),
        parent_worker="codex-build",
        max_duration_minutes=30,
        task_id=task_id,
        project_id=project_id,
    )
    ctx.state.upsert_runbook(runbook)
    return ctx.state.get_runbook(run_id)


def _request(ctx: CommandContext, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "action_type": approvals.ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE,
        "payload": {"question_ref": "ADR-10", "decision": "Keep the existing event store"},
        "reason": "The implementation needs an explicit owner choice",
        "requested_by": "local-operator",
        "expires_in_seconds": 3600,
    }
    values.update(changes)
    return approvals.create_request(ctx, **values)


def test_exactly_five_server_owned_decision_classes_and_client_flags_are_rejected(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    assert approvals.APPROVAL_ACTIONS == {
        "DESTRUCTIVE_CLEANUP",
        "METERED_OVERFLOW_ROUTE",
        "MATERIAL_SCOPE_CHANGE",
        "AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE",
        "REMOTE_SENSITIVE_ACTION",
    }
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))
    for field in ("risk", "safe", "destructive", "project_id", "requested_by", "command"):
        response = client.post(
            "/api/approvals",
            json={
                "action_type": approvals.ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE,
                "payload": {"question_ref": "ADR-10", "decision": "one"},
                "reason": "choose",
                field: "client-controlled",
            },
        )
        assert response.status_code == 400


def test_all_five_classes_have_server_derived_risk_and_fixed_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.agents.control_plane import operations

    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    monkeypatch.setattr(operations, "refresh_worktree_statuses", lambda _ctx: [])
    service = {
        "id": "runtime-project-a-1",
        "worktree_path": None,
        "runbook_id": None,
        "health": "STOPPED",
        "ownership": "OCTAREL_DECLARED",
        "pid": None,
        "process_create_time": None,
        "updated_at": "2026-10-02T00:00:00+00:00",
        "actions": {"start": True, "stop": False, "restart": False},
    }
    ctx.approval_runtime_manager = SimpleNamespace(list_services=lambda **_kw: [service])
    cases = {
        approvals.ACTION_DESTRUCTIVE_CLEANUP: ({}, "HIGH"),
        approvals.ACTION_METERED_OVERFLOW_ROUTE: ({"runbook_id": runbook.id}, "CRITICAL"),
        approvals.ACTION_MATERIAL_SCOPE_CHANGE: (
            {"scope_ref": "ENG-PC-10", "decision": "keep the bounded task"},
            "HIGH",
        ),
        approvals.ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE: (
            {"question_ref": "ADR-10", "decision": "reuse events"},
            "MEDIUM",
        ),
        approvals.ACTION_REMOTE_SENSITIVE: (
            {"service_id": service["id"], "action": "start"},
            "HIGH",
        ),
    }
    for action_type, (payload, risk) in cases.items():
        plan = approvals.build_plan(
            ctx, action_type=action_type, payload=payload, task_id=None, run_id=None
        )
        assert plan.action_type == action_type
        assert plan.risk == risk
        assert "operation" in plan.safe_payload_summary
        assert "command" not in json.dumps(plan.payload).lower()


@pytest.mark.parametrize(
    "unsafe",
    [
        {"question_ref": "ADR-10", "decision": "token=abcdefghijklmnop"},
        {"question_ref": "ADR-10", "decision": "read /private/operator/.env"},
        {"question_ref": "ADR-10", "decision": "ok", "command": "rm -rf target"},
    ],
)
def test_secret_paths_and_arbitrary_commands_never_reach_durable_payload(
    tmp_path: Path, unsafe: dict[str, str]
) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(approvals.ApprovalError):
        _request(ctx, payload=unsafe)
    assert ctx.state.list_approval_requests(project_id="project-a") == []


def test_request_contract_is_immutable_and_api_exposes_only_safe_summary(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    created = _request(ctx)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ctx.state._conn.execute(
            "UPDATE approval_requests SET risk = 'LOW' WHERE id = ?", (created["id"],)
        )
    ctx.state._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="durable audit evidence"):
        ctx.state._conn.execute("DELETE FROM approval_requests WHERE id = ?", (created["id"],))
    ctx.state._conn.rollback()

    response = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md")).get("/api/approvals")
    assert response.status_code == 200
    public = response.json()[0]
    assert "action_payload" not in public
    assert "state_fingerprint" not in public
    assert public["safe_payload_summary"]["operation"].startswith("Record an operator")


def test_rejection_is_terminal_audited_and_executes_no_handler(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    created = _request(ctx)
    rejected = approvals.resolve_request(
        ctx,
        approval_id=created["id"],
        decision="REJECT",
        resolved_by="local-operator",
        resolution_note="Use the alternate design",
    )
    assert rejected["state"] == "REJECTED"
    assert rejected["result_summary"] is None
    with pytest.raises(approvals.ApprovalConflict, match="already REJECTED"):
        approvals.resolve_request(
            ctx,
            approval_id=created["id"],
            decision="APPROVE",
            resolved_by="local-operator",
            resolution_note="changed mind",
        )
    events = ctx.state.list_run_events(project_id="project-a", event_class="approval")
    assert [event.event_type for event in events] == ["approval.requested", "approval.rejected"]


def test_expired_request_is_atomically_refused_and_marked_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.agents.control_plane import state as state_module

    ctx = _context(tmp_path)
    created = _request(ctx, expires_in_seconds=60)
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat(timespec="seconds")
    monkeypatch.setattr(state_module, "utc_now_iso", lambda: future)
    with pytest.raises(approvals.ApprovalConflict, match="expired"):
        approvals.resolve_request(
            ctx,
            approval_id=created["id"],
            decision="APPROVE",
            resolved_by="local-operator",
            resolution_note="too late",
        )
    assert ctx.state.get_approval_request(created["id"])["state"] == "EXPIRED"


def test_changed_run_state_invalidates_approval_before_handler_execution(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    created = _request(ctx, task_id=runbook.task_id, run_id=runbook.id)
    runbook.status = "CANCELLED"
    ctx.state.upsert_runbook(runbook)

    with pytest.raises(approvals.ApprovalConflict, match="state changed"):
        approvals.resolve_request(
            ctx,
            approval_id=created["id"],
            decision="APPROVE",
            resolved_by="local-operator",
            resolution_note="approve old state",
        )
    stored = ctx.state.get_approval_request(created["id"])
    assert stored["state"] == "STALE"
    assert stored["result_summary"] is None


def test_deleted_run_scope_is_durably_stale_instead_of_remaining_pending(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    created = _request(ctx, task_id=runbook.task_id, run_id=runbook.id)
    ctx.state.delete_runbook(runbook.id)

    with pytest.raises(approvals.ApprovalConflict, match="state changed"):
        approvals.resolve_request(
            ctx,
            approval_id=created["id"],
            decision="APPROVE",
            resolved_by="local-operator",
            resolution_note="approve missing run",
        )
    assert ctx.state.get_approval_request(created["id"])["state"] == "STALE"


def test_concurrent_double_resolution_has_exactly_one_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    first = _context(tmp_path, db_path=db_path)
    created = _request(first)
    second = _context(tmp_path, db_path=db_path)

    def reject(ctx: CommandContext, actor: str) -> str:
        try:
            return approvals.resolve_request(
                ctx,
                approval_id=created["id"],
                decision="REJECT",
                resolved_by=actor,
                resolution_note="concurrent decision",
            )["state"]
        except approvals.ApprovalConflict:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: reject(*pair), [(first, "alice"), (second, "bob")]))
    assert sorted(outcomes) == ["CONFLICT", "REJECTED"]
    stored = first.state.get_approval_request(created["id"])
    assert stored["state"] == "REJECTED"
    assert stored["resolved_by"] in {"alice", "bob"}


def test_cross_project_reads_and_resolution_fail_closed(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "a", project_id="project-a")
    ctx.state.upsert_project(
        contract_to_row(
            ProjectContract(
                project_id="project-b",
                display_name="project-b",
                local_repo_root=tmp_path / "b",
            )
        )
    )
    created = _request(ctx)
    select_project(ctx.state, "project-b")
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))
    assert client.get("/api/approvals").json() == []
    refused = client.post(
        f"/api/approvals/{created['id']}/resolve",
        json={"decision": "REJECT", "resolution_note": "not my project"},
    )
    assert refused.status_code == 404
    assert ctx.state.get_approval_request(created["id"])["state"] == "PENDING"


def test_route_approval_reuses_bounded_override_without_clearing_provider_safeguards(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    ctx.state.upsert_provider_state(
        ProviderState(
            name="codex-build",
            execution_system="Codex CLI",
            provider="OpenAI",
            cost_class="premium-subscription",
            state="COST_BLOCKED",
            reason="AVAILABLE",
            last_error="operator cost block",
        )
    )
    created = approvals.create_request(
        ctx,
        action_type=approvals.ACTION_METERED_OVERFLOW_ROUTE,
        payload={"runbook_id": runbook.id},
        reason="Authorize exactly one existing premium invocation",
        requested_by="local-operator",
        task_id=runbook.task_id,
        run_id=runbook.id,
    )
    approved = approvals.resolve_request(
        ctx,
        approval_id=created["id"],
        decision="APPROVE",
        resolved_by="local-operator",
        resolution_note="bounded exception accepted",
    )
    assert approved["state"] == "APPROVED"
    updated = ctx.state.get_runbook(runbook.id)
    assert updated.codex_policy == "unrestricted"
    assert updated.max_codex_invocations == 1
    provider = ctx.state.get_provider_state("codex-build")
    assert provider.state == "COST_BLOCKED"
    assert provider.last_error == "operator cost block"


def test_dashboard_premium_override_creates_request_before_mutating_usage_governance(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    runbook.codex_policy = "conserve"
    runbook.codex_auto_eligible = False
    runbook.max_codex_invocations = 0
    ctx.state.upsert_runbook(runbook)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))

    initiated = client.post(
        "/api/commands/usage_override",
        json={
            "runbook_id": runbook.id,
            "reason": "Control Center operator override",
            "confirm": True,
        },
    )

    assert initiated.status_code == 200
    assert initiated.json()["data"]["mode"] == "APPROVAL_REQUESTED"
    approval = initiated.json()["data"]["approval_request"]
    assert approval["action_type"] == approvals.ACTION_METERED_OVERFLOW_ROUTE
    unchanged = ctx.state.get_runbook(runbook.id)
    assert unchanged.codex_policy == "conserve"
    assert unchanged.codex_auto_eligible is False
    assert unchanged.max_codex_invocations == 0
    assert ctx.state.get_usage_governance(runbook.id) is None

    resolved = client.post(
        f"/api/approvals/{approval['id']}/resolve",
        json={"decision": "APPROVE", "resolution_note": "Allow one bounded invocation"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["state"] == "APPROVED"
    updated = ctx.state.get_runbook(runbook.id)
    assert updated.codex_policy == "unrestricted"
    assert updated.codex_auto_eligible is True
    assert updated.max_codex_invocations == 1


def test_steering_premium_override_creates_request_without_mutating_usage_governance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.agents.control_plane import dashboard_api

    ctx = _context(tmp_path)
    runbook = _seed_run(ctx)
    runbook.codex_policy = "conserve"
    runbook.codex_auto_eligible = False
    runbook.max_codex_invocations = 0
    ctx.state.upsert_runbook(runbook)
    direct_calls: list[tuple[str, dict[str, object]]] = []

    def direct_execute(_ctx: CommandContext, verb: str, **kwargs: object) -> CommandResult:
        direct_calls.append((verb, kwargs))
        return CommandResult(ok=True, message="direct steering command executed")

    monkeypatch.setattr(dashboard_api, "apply_command", direct_execute)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))

    initiated = client.post(
        "/api/steering/execute",
        json={
            "verb": "usage_override",
            "args": {"runbook_id": runbook.id, "reason": "Crafted steering request"},
            "confirm": True,
            "raw_text": "/crafted usage override",
        },
    )

    assert initiated.status_code == 200
    assert initiated.json()["data"]["mode"] == "APPROVAL_REQUESTED"
    approval = initiated.json()["data"]["approval_request"]
    assert approval["action_type"] == approvals.ACTION_METERED_OVERFLOW_ROUTE
    assert approval["state"] == "PENDING"
    assert direct_calls == []
    unchanged = ctx.state.get_runbook(runbook.id)
    assert unchanged.codex_policy == "conserve"
    assert unchanged.codex_auto_eligible is False
    assert unchanged.max_codex_invocations == 0
    assert ctx.state.get_usage_governance(runbook.id) is None


@pytest.mark.parametrize(
    ("verb", "args", "confirm", "expected_status"),
    [
        ("usage_override", {"runbook_id": "run-1"}, False, 409),
        ("worktree_cleanup", {}, False, 409),
        ("usage_override", {"runbook_id": "run-1", "command": "bypass"}, True, 400),
        ("worktree_cleanup", {"path": "/caller/supplied"}, True, 400),
    ],
)
def test_steering_approval_verbs_fail_closed_before_direct_command_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    args: dict[str, object],
    confirm: bool,
    expected_status: int,
) -> None:
    from scripts.agents.control_plane import dashboard_api

    ctx = _context(tmp_path)
    _seed_run(ctx)
    direct_calls: list[tuple[str, dict[str, object]]] = []

    def direct_execute(_ctx: CommandContext, command_verb: str, **kwargs: object) -> CommandResult:
        direct_calls.append((command_verb, kwargs))
        return CommandResult(ok=True, message="direct steering command executed")

    monkeypatch.setattr(dashboard_api, "apply_command", direct_execute)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))

    response = client.post(
        "/api/steering/execute",
        json={"verb": verb, "args": args, "confirm": confirm},
    )

    assert response.status_code == expected_status
    assert direct_calls == []


def test_confirmed_cleanup_hands_off_without_removal_until_approved_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.agents.control_plane import operations

    ctx = _context(tmp_path)
    eligible_path = str(tmp_path / "finished-clean")
    rows = [
        {
            "path": eligible_path,
            "branch": "eng/finished-clean",
            "classification": "FINISHED_CLEAN",
            "dirty": False,
            "cleanup_eligible": True,
            "canonical_checkout": False,
        }
    ]
    monkeypatch.setattr(operations, "refresh_worktree_statuses", lambda _ctx: rows)
    handler_calls: list[tuple[str, dict[str, object]]] = []

    def execute(_ctx: CommandContext, verb: str, **kwargs: object) -> CommandResult:
        handler_calls.append((verb, kwargs))
        return CommandResult(ok=True, message="removed one server-proven worktree")

    monkeypatch.setattr(approvals, "apply_command", execute)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))

    initiated = client.post("/api/commands/worktree_cleanup", json={"confirm": True})

    assert initiated.status_code == 200
    assert initiated.json()["data"]["mode"] == "APPROVAL_REQUESTED"
    approval = initiated.json()["data"]["approval_request"]
    assert approval["action_type"] == approvals.ACTION_DESTRUCTIVE_CLEANUP
    assert handler_calls == []

    rows[0]["branch"] = "eng/changed-after-request"
    stale = client.post(
        f"/api/approvals/{approval['id']}/resolve",
        json={"decision": "APPROVE", "resolution_note": "Attempt the stale cleanup"},
    )
    assert stale.status_code == 409
    assert "state changed" in stale.json()["detail"]
    assert handler_calls == []

    refreshed = client.post("/api/commands/worktree_cleanup", json={"confirm": True})
    assert refreshed.status_code == 200
    current_approval = refreshed.json()["data"]["approval_request"]

    resolved = client.post(
        f"/api/approvals/{current_approval['id']}/resolve",
        json={"decision": "APPROVE", "resolution_note": "Cleanup preview is still current"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["state"] == "APPROVED"
    assert handler_calls == [("worktree_cleanup", {"confirm": True})]


def test_steering_cleanup_never_calls_command_handler_before_later_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.agents.control_plane import dashboard_api, operations

    ctx = _context(tmp_path)
    eligible_path = str(tmp_path / "finished-clean")
    monkeypatch.setattr(
        operations,
        "refresh_worktree_statuses",
        lambda _ctx: [
            {
                "path": eligible_path,
                "branch": "eng/finished-clean",
                "classification": "FINISHED_CLEAN",
                "dirty": False,
                "cleanup_eligible": True,
                "canonical_checkout": False,
            }
        ],
    )
    direct_calls: list[tuple[str, dict[str, object]]] = []
    approved_calls: list[tuple[str, dict[str, object]]] = []

    def direct_execute(_ctx: CommandContext, verb: str, **kwargs: object) -> CommandResult:
        direct_calls.append((verb, kwargs))
        return CommandResult(ok=True, message="direct steering cleanup executed")

    def approved_execute(_ctx: CommandContext, verb: str, **kwargs: object) -> CommandResult:
        approved_calls.append((verb, kwargs))
        return CommandResult(ok=True, message="approved cleanup executed")

    monkeypatch.setattr(dashboard_api, "apply_command", direct_execute)
    monkeypatch.setattr(approvals, "apply_command", approved_execute)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))

    initiated = client.post(
        "/api/steering/execute",
        json={
            "verb": "worktree_cleanup",
            "args": {},
            "confirm": True,
            "raw_text": "/crafted cleanup",
        },
    )

    assert initiated.status_code == 200
    assert initiated.json()["data"]["mode"] == "APPROVAL_REQUESTED"
    approval = initiated.json()["data"]["approval_request"]
    assert approval["action_type"] == approvals.ACTION_DESTRUCTIVE_CLEANUP
    assert approval["state"] == "PENDING"
    assert direct_calls == []
    assert approved_calls == []

    resolved = client.post(
        f"/api/approvals/{approval['id']}/resolve",
        json={"decision": "APPROVE", "resolution_note": "Cleanup preview remains current"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["state"] == "APPROVED"
    assert direct_calls == []
    assert approved_calls == [("worktree_cleanup", {"confirm": True})]


def test_remote_sensitive_handler_rechecks_supervisor_and_fails_safe(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    service = {
        "id": "runtime-project-a-1",
        "worktree_path": None,
        "runbook_id": None,
        "health": "HEALTHY",
        "ownership": "OWNED_VERIFIED",
        "pid": 7123,
        "process_create_time": 100.5,
        "updated_at": "2026-10-02T00:00:00+00:00",
        "actions": {"start": False, "stop": True, "restart": True},
    }

    class RefusingRuntimeManager:
        def list_services(self, **_kw):
            return [service]

        def action(self, *_args, **_kwargs):
            raise ValueError("full process ownership proof changed")

    ctx.approval_runtime_manager = RefusingRuntimeManager()
    created = approvals.create_request(
        ctx,
        action_type=approvals.ACTION_REMOTE_SENSITIVE,
        payload={"service_id": service["id"], "action": "stop"},
        reason="Remote operator requested an owned-service stop",
        requested_by="remote:maintainer@example.test",
    )
    with pytest.raises(approvals.ApprovalConflict, match="failed safely"):
        approvals.resolve_request(
            ctx,
            approval_id=created["id"],
            decision="APPROVE",
            resolved_by="remote:maintainer@example.test",
            resolution_note="Stop after ownership revalidation",
        )
    stored = ctx.state.get_approval_request(created["id"])
    assert stored["state"] == "FAILED_SAFE"
    assert stored["result_summary"]["ok"] is False
    assert "ownership proof changed" in stored["result_summary"]["message"]


def test_resolution_api_records_server_identity_and_approval_event(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing.md"))
    created = client.post(
        "/api/approvals",
        json={
            "action_type": approvals.ACTION_MATERIAL_SCOPE_CHANGE,
            "payload": {"scope_ref": "ENG-PC-10", "decision": "Include the shared Run Detail"},
            "reason": "The task contract requires the shared inspector",
        },
    )
    assert created.status_code == 200
    approval_id = created.json()["id"]
    assert created.json()["requested_by"] == "local-operator"
    resolved = client.post(
        f"/api/approvals/{approval_id}/resolve",
        json={"decision": "APPROVE", "resolution_note": "Proceed within bounded scope"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "APPROVED"
    assert resolved.json()["resolved_by"] == "local-operator"
    event_types = [
        event.event_type
        for event in ctx.state.list_run_events(project_id="project-a", event_class="approval")
    ]
    assert event_types == ["approval.requested", "approval.approved"]
