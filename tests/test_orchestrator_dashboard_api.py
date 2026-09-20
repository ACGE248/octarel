"""Control-center dashboard: read endpoints, control endpoints, and independence
from both the OctaScene app (port 8765) and any provider/model network call.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import (
    create_app,
    probe_octascene_app,
)
from scripts.agents.control_plane.models import Runbook, Task
from scripts.agents.control_plane.provider_state import seed_provider_states
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry

ROADMAP_FIXTURE = """# Roadmap

## Program index

| Program / feature family | Product version | Status source | Implementation source |
|---|---|---|---|
| Core production app | V1 | tests | existing domains/UI |
| Orchestrator control center | engineering governance | `ENG-AGENT-02` | `scripts/agents/README.md` |

## Feature-addition protocol

unrelated section
"""


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    task = Task(
        id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        dependencies=(),
    )
    state.upsert_task(task)
    state.record_event(category="test", message="seed event")
    return CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


@pytest.fixture()
def roadmap_file(tmp_path: Path) -> Path:
    path = tmp_path / "PRODUCT_ROADMAP.md"
    path.write_text(ROADMAP_FIXTURE, encoding="utf-8")
    return path


@pytest.fixture()
def client(ctx, roadmap_file) -> TestClient:
    app = create_app(ctx, roadmap_path=roadmap_file)
    return TestClient(app)


# --------------------------------------------------------------------------- read endpoints


def test_overview_endpoint(client):
    body = client.get("/api/overview").json()
    assert body["task_count"] == 0
    assert body["task_counts"]["queued"] == 1
    assert body["provider_count"] > 0
    assert body["octascene_app_status"] in {"RUNNING", "STOPPED", "UNKNOWN"}


def test_agent_and_provider_endpoints_expose_registry_driven_human_metadata(client):
    agent = next(item for item in client.get("/api/models").json() if item["worker"] == "claude-code")
    assert agent["display_name"] == "Claude Code"
    assert agent["description"]
    assert agent["best_for"]
    assert agent["availability"] in {"AVAILABLE", "DISABLED", "NOT_CONFIGURED", "UNKNOWN"}
    provider = next(item for item in client.get("/api/providers").json() if item["name"] == "claude-code")
    assert provider["display_name"] == "Claude Code"
    assert provider["route_type"] == "Subscription"
    assert provider["models"] == [agent["default_model"]]
    assert "quota" in provider["quota_visibility"].lower()


def test_terminal_info_is_resolved_from_the_allowlisted_repository(client, ctx):
    body = client.get("/api/terminal/info").json()
    assert body["title"] == "OctaScene Control Center Terminal"
    assert body["repository"] == str(ctx.repo_root.resolve())
    assert body["virtual_environment"] in {"Active", "Unavailable"}
    assert body["shell"] == "zsh"


def test_tasks_endpoint(client):
    tasks = client.get("/api/tasks").json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "t1"
    assert tasks[0]["projection"] == "QUEUED"


def test_runbook_api_projects_durable_automatic_fallback_chain(client, ctx, tmp_path):
    runbook = Runbook(
        id="RB-fallback", name="Fallback", preset="finish-pr", objective="same objective",
        source_ref="ENG-119", branch="eng/119", worktree=str(tmp_path), parent_worker="codex-build",
        max_duration_minutes=120, status="RUNNING", task_id="RB-fallback-session",
    )
    ctx.state.upsert_runbook(runbook)
    ctx.state.upsert_task(Task(
        id=runbook.task_id, task_ref="ENG-119", role="primary-implementation", worker="codex-build",
        state="RUNNING", worktree=str(tmp_path), runbook_id=runbook.id,
        failed_worker_id="claude-code", failure_category="QUOTA",
        failure_reason_sanitized="weekly quota exhausted", fallback_automatic=True,
        fallback_selected_worker="codex-build",
    ))
    ctx.state.upsert_usage_governance({
        "runbook_id": runbook.id,
        "task_id": runbook.task_id,
        "route_history": [
            {"worker": "claude-code", "status": "FAILED", "failure_category": "QUOTA"},
            {
                "worker": "codex-build", "status": "RUNNING", "automatic": True,
                "from_worker": "claude-code", "failure_category": "QUOTA",
            },
        ],
    })

    body = next(item for item in client.get("/api/runbooks").json() if item["id"] == runbook.id)

    assert [(item["worker_id"], item["status"]) for item in body["attempt_history"]] == [
        ("claude-code", "FAILED"), ("codex-build", "RUNNING"),
    ]
    assert body["fallback_transition"] == {
        "status": "RUNNING",
        "from_worker": "claude-code",
        "to_worker": "codex-build",
        "to_worker_name": "Codex Build",
        "automatic": True,
    }
    assert body["failure_reason"] == "weekly quota exhausted"


def test_runbook_api_projects_prelaunch_unavailability_before_automatic_fallback(client, ctx, tmp_path):
    runbook = Runbook(
        id="RB-prelaunch-fallback", name="Prelaunch fallback", preset="overnight-development",
        objective="same objective", source_ref="ENG-AGENT-08", branch="eng/121", worktree=str(tmp_path),
        parent_worker="codex-build", max_duration_minutes=120, status="RUNNING",
        task_id="RB-prelaunch-fallback-session",
    )
    ctx.state.upsert_runbook(runbook)
    ctx.state.upsert_task(Task(
        id=runbook.task_id, task_ref="ENG-AGENT-08", role="primary-implementation", worker="codex-build",
        state="RUNNING", worktree=str(tmp_path), runbook_id=runbook.id,
        failed_worker_id="claude-code", failure_category="QUOTA",
        failure_reason_sanitized="worker 'claude-code' is unavailable before launch: QUOTA_EXHAUSTED",
        fallback_automatic=True, fallback_selected_worker="codex-build",
    ))
    ctx.state.upsert_usage_governance({
        "runbook_id": runbook.id,
        "task_id": runbook.task_id,
        "route_history": [
            {"worker": "claude-code", "status": "UNAVAILABLE", "failure_category": "QUOTA"},
            {
                "worker": "codex-build", "status": "RUNNING", "automatic": True,
                "from_worker": "claude-code", "failure_category": "QUOTA",
            },
        ],
    })

    body = next(item for item in client.get("/api/runbooks").json() if item["id"] == runbook.id)

    assert [(item["worker_name"], item["status"]) for item in body["attempt_history"]] == [
        ("Claude Code", "UNAVAILABLE"), ("Codex Build", "RUNNING"),
    ]
    assert body["fallback_transition"]["automatic"] is True
    assert body["fallback_transition"]["to_worker_name"] == "Codex Build"


def test_canonical_task_projection_keeps_history_out_of_active_count(client, ctx):
    ctx.state.upsert_task(Task(id="running", task_ref="ENG-2", role="focused-tests", worker="opencode2-gemini-flash-lite", state="RUNNING"))
    ctx.state.upsert_task(Task(id="failed", task_ref="ENG-3", role="focused-tests", worker="opencode2-gemini-flash-lite", state="FAILED"))
    ctx.state.upsert_task(Task(id="done", task_ref="ENG-4", role="focused-tests", worker="opencode2-gemini-flash-lite", state="SUCCEEDED"))
    overview = client.get("/api/overview").json()
    tasks = client.get("/api/tasks").json()
    assert overview["task_count"] == 1
    assert overview["task_counts"] == {"active": 1, "queued": 1, "paused": 0, "needs_attention": 1, "historical": 1}
    assert [task["id"] for task in tasks if task["projection"] == "ACTIVE"] == ["running"]


def test_processes_endpoint_is_empty_before_launch(client):
    assert client.get("/api/processes").json() == []


def test_workers_endpoint_reports_slot_shape(client):
    body = client.get("/api/workers").json()
    assert set(body) == {"write", "read", "heavy"}
    assert set(body["write"]) == {"used", "limit"}


def test_flow_endpoint_returns_nodes_and_edges(client):
    body = client.get("/api/flow").json()
    assert body["nodes"][0]["id"] == "t1"
    assert body["edges"] == []


def test_workflow_endpoint_only_promotes_persisted_tasks_to_stages(client):
    body = client.get("/api/workflow").json()
    assert body["task"]["id"] == "ENG-AGENT-02"
    assert body["task"]["progress"] is None
    assert body["task"]["eta"] is None
    assert body["orchestrator"]["execution_system"] == "OctaScene Control Plane"
    assert [stage["id"] for stage in body["stages"]] == ["t1"]
    stage = body["stages"][0]
    assert stage["label"] == "Testing & QA"
    assert stage["current_action"] is None
    assert stage["current_file"] is None
    assert stage["subagents"] == []


def test_workflow_uses_latest_linked_runbook_state_after_successful_fallback(client, ctx, tmp_path):
    ctx.state.delete_task("t1")
    ctx.state.upsert_task(Task(
        id="RB-old-session", task_ref="V1-01", role="primary-implementation", worker="claude-code",
        state="FAILED", result="FAIL", worktree=str(tmp_path), created_at="2026-09-14T10:00:00+00:00",
    ))
    ctx.state.upsert_task(Task(
        id="RB-current-session", task_ref="V1-01", role="primary-implementation", worker="codex-build",
        state="SUCCEEDED", result="PASS", worktree=str(tmp_path), created_at="2026-09-14T11:00:00+00:00",
    ))
    ctx.state.upsert_runbook(Runbook(
        id="RB-current", name="Continue Video Editor — V1-01", preset="overnight-development",
        objective="same objective", source_ref="V1-01 (Local import.)", branch="video-editor/V1-01-local-import",
        worktree=str(tmp_path), parent_worker="codex-build", max_duration_minutes=480,
        status="SUCCEEDED", task_id="RB-current-session",
    ))

    body = client.get("/api/workflow").json()

    assert body["task"]["id"] == "V1-01"
    assert body["task"]["title"] == "Continue Video Editor — V1-01"
    assert body["task"]["state"] == "SUCCEEDED"
    assert body["orchestrator"]["state"] == "SUCCEEDED"
    assert [(stage["worker"], stage["state"]) for stage in body["stages"]] == [
        ("claude-code", "FAILED"),
        ("codex-build", "SUCCEEDED"),
    ]


def test_workflow_endpoint_has_an_explicit_idle_shape(client, ctx):
    ctx.state.delete_task("t1")
    assert client.get("/api/workflow").json() == {
        "task": None,
        "orchestrator": None,
        "stages": [],
        "worktrees": [],
        "results": [],
    }


def test_flow_endpoint_enriches_nodes_with_the_full_pipeline_chain(client):
    """Slice 4: orchestrator -> task -> worker -> role -> system ->
    provider/model/intensity -> worktree must all be visible per node."""

    node = client.get("/api/flow").json()["nodes"][0]
    for key in (
        "role",
        "worker",
        "execution_system",
        "provider",
        "model",
        "intensity",
        "provider_state",
        "worktree",
        "kind",
        "priority",
        "stages",
    ):
        assert key in node
    stages = {stage["key"]: stage for stage in node["stages"]}
    assert set(stages) == {
        "task",
        "orchestrator",
        "worker",
        "worktree",
        "implementation",
        "tests",
        "review",
        "checkpoint",
        "pr",
        "ci",
    }
    assert stages["tests"] == {
        "key": "tests",
        "label": "Tests",
        "state": "NOT_REPORTED",
        "source": "NOT_EXPOSED",
    }


def test_flow_endpoint_reports_concurrent_running_tasks(client, ctx):
    from scripts.agents.control_plane.models import Task

    ctx.state.upsert_task(Task(id="running1", task_ref="X", role="focused-tests", worker="grok-build", state="RUNNING"))
    ctx.state.upsert_task(Task(id="running2", task_ref="X", role="focused-tests", worker="grok-build", state="RUNNING"))
    body = client.get("/api/flow").json()
    assert set(body["concurrent_running"]) == {"running1", "running2"}


def test_providers_endpoint_includes_not_configured_catalog_rows(client):
    providers = client.get("/api/providers").json()
    names = {p["name"] for p in providers}
    # "glm" has no adapter/CLI in this repository at all (fixed catalog row).
    assert "glm" in names
    not_configured = [p for p in providers if p["name"] == "glm"][0]
    assert not_configured["configured"] is False
    assert not_configured["state"] == "NOT_CONFIGURED"
    assert not_configured["reason"] == "CATALOG_ONLY"


def test_providers_endpoint_reports_codex_as_a_real_worker_not_a_catalog_row(client):
    # ENG-AGENT-02-S7 (issue #97): OpenAI/Codex used to be a permanent
    # catalog-only NOT_CONFIGURED row; it is now a real registry worker whose
    # `reason` is truthfully derived from local CLI presence + auth session.
    providers = client.get("/api/providers").json()
    codex = [p for p in providers if p["name"] == "codex-review"][0]
    assert codex["configured"] is True
    # Passive seeding never probes (no subprocess spawn), so only these two
    # reasons are reachable here; NOT_AUTHENTICATED requires an explicit
    # `provider_probe` command.
    assert codex["reason"] in {"AVAILABLE", "CLI_MISSING"}
    assert codex["model"]
    assert codex["intensity"]
    assert "diff-review" in codex["roles"]
    assert codex["execution_route"] == "SUBSCRIPTION"
    assert codex["running_task_count"] == 0
    assert "openai-codex" not in {p["name"] for p in providers}


def test_routing_endpoint_excludes_not_configured(client):
    body = client.get("/api/routing", params={"role": "focused-tests", "mode": "B"}).json()
    assert "glm" not in body["percentages"]
    assert abs(sum(body["percentages"].values()) - 100.0) < 0.01


def test_routing_endpoint_rejects_unknown_role(client):
    resp = client.get("/api/routing", params={"role": "not-a-real-role"})
    assert resp.status_code == 400


def test_models_endpoint_reflects_workers_json(client):
    models = client.get("/api/models").json()
    names = {m["worker"] for m in models}
    assert "claude-code" in names
    assert "grok-build" in names
    claude = next(m for m in models if m["worker"] == "claude-code")
    assert claude["provider_policy"] == ".agents/providers/CLAUDE.md"
    assert claude["allowed_policy_roles"] == ["ORCHESTRATOR", "IMPLEMENTER"]
    assert claude["repository_data_authorization"] == "explicit-worker-selection"
    google = next(m for m in models if m["worker"] == "opencode2-gemini-flash-lite")
    grok = next(m for m in models if m["worker"] == "grok-build")
    assert google["repository_data_authorization"] == "preauthorized-scoped-reuse"
    assert google["repository_data_authorization_source"] == "provider-level"
    assert grok["repository_data_authorization"] == "preauthorized-scoped-reuse"
    assert grok["repository_data_authorization_source"] == "provider-level"
    assert claude["api_billing_enabled"] is False
    assert claude["requires_isolated_worktree"] is True


def test_worktrees_endpoint_starts_empty(client):
    assert client.get("/api/worktrees").json() == []


def test_usage_endpoint_reports_a_fact_per_worker_never_fabricated(client, monkeypatch):
    import subprocess as subprocess_module

    class FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(subprocess_module, "run", lambda *a, **k: FakeCompleted())
    body = client.get("/api/usage").json()
    assert "facts" in body and body["facts"]
    workers_seen = {f["worker"] for f in body["facts"]}
    assert "claude-code" in workers_seen
    for fact in body["facts"]:
        assert fact["source"] in {"CLI_REPORTED", "LOCAL_ACCOUNTING", "NOT_EXPOSED"}
        if fact["source"] == "NOT_EXPOSED":
            assert fact["value"] is None


def test_usage_endpoint_is_cached_and_does_not_spawn_a_subprocess_on_every_call(client, monkeypatch):
    import subprocess as subprocess_module

    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(*a, **k):
        calls.append(a)
        return FakeCompleted()

    monkeypatch.setattr(subprocess_module, "run", fake_run)
    client.get("/api/usage")
    first_call_count = len(calls)
    assert first_call_count > 0
    client.get("/api/usage")
    assert len(calls) == first_call_count, "a second call within the TTL must reuse the cached result"

    client.get("/api/usage", params={"refresh": "true"})
    assert len(calls) > first_call_count, "?refresh=true must force a fresh read"


def test_quickstart_endpoint_is_honest_when_the_ledger_does_not_exist(client):
    # ctx.repo_root is a bare tmp_path with no docs/video-editor ledger, so
    # this must report "not ready" with a real reason rather than a fabricated
    # or stale task proposal.
    options = client.get("/api/quickstart").json()
    assert len(options) == 6
    option = options[0]
    assert option["key"] == "continue-video-editor"
    assert option["ready"] is False
    assert options[-1]["key"] == "custom-run"
    assert options[-1]["action"] == "advanced"
    assert option["unavailable_reason"]


def test_telemetry_endpoint_reports_routes_quota_and_checkpoints(client):
    body = client.get("/api/telemetry").json()
    routes = {p["name"]: p["execution_route"] for p in body["providers"]}
    assert routes["claude-code"] == "SUBSCRIPTION"
    assert routes["opencode2-gemini-flash-lite"] == "FREE"
    assert routes["glm"] == "UNKNOWN"
    labels = {w["label"] for w in body["quota_windows"]}
    assert labels == {"claude_5h", "claude_weekly"}
    assert all(w["state"] == "UNKNOWN" for w in body["quota_windows"])
    assert body["checkpoints"] == []


def test_telemetry_endpoint_includes_checkpoint_for_a_real_worktree(ctx, roadmap_file):
    from scripts.agents.control_plane.models import WorktreeRecord

    ctx.state.upsert_worktree(WorktreeRecord(path=str(ctx.repo_root), branch="main"))
    app = create_app(ctx, roadmap_path=roadmap_file)
    client = TestClient(app)
    body = client.get("/api/telemetry").json()
    assert len(body["checkpoints"]) == 1
    assert body["checkpoints"][0]["worktree"] == str(ctx.repo_root)


def test_tests_endpoint_reports_authoritative_local_gate(client):
    body = client.get("/api/tests").json()
    assert body["authority"] == "local-deterministic-gate"
    assert body["local_gate"]["ready"] is False
    assert body["local_gate"]["reason"] == "no local-gate evidence"


def test_development_throughput_is_truthful_local_read_model(client):
    body = client.get("/api/development-throughput").json()
    assert body["authority"] == "local-deterministic-gate"
    assert body["provider_calls"] == 0
    assert body["current"]["tier"] == "UNKNOWN"
    assert body["task_timeline"][0]["provider_wait_seconds"] == "UNKNOWN"


def test_events_endpoint_includes_seeded_event(client):
    events = client.get("/api/events").json()
    assert any(e["message"] == "seed event" for e in events)


def test_attention_endpoint_shape(client):
    body = client.get("/api/attention").json()
    assert "tasks" in body and "providers" in body


def test_attention_endpoint_includes_cost_blocked_providers(client, ctx):
    """A COST_BLOCKED provider (Slice 3) is exactly the kind of thing an
    operator needs to notice, alongside quota/rate-limit/failed states."""

    provider = ctx.state.get_provider_state("grok-build")
    provider.state = "COST_BLOCKED"
    ctx.state.upsert_provider_state(provider)
    body = client.get("/api/attention").json()
    names = {p["name"] for p in body["providers"]}
    assert "grok-build" in names


def test_roadmap_endpoint_parses_program_index_table(client):
    rows = client.get("/api/roadmap").json()
    assert len(rows) == 2
    assert rows[1]["Program / feature family"] == "Orchestrator control center"
    assert rows[1]["Status source"] == "`ENG-AGENT-02`"


def test_resources_endpoint_reports_psutil_metrics(client):
    body = client.get("/api/resources").json()
    assert body["available"] is True
    assert "cpu_percent" in body


def test_app_status_endpoint(client):
    body = client.get("/api/app-status").json()
    assert body["status"] in {"RUNNING", "STOPPED", "UNKNOWN"}


def test_identity_endpoint_returns_honest_local_operator(client):
    body = client.get("/api/identity").json()
    assert body["role"] == "Developer"
    assert body["name"]
    assert body["name"] != "Alex Chen"
    assert body["source"] in {"git", "os", "fallback"}
    assert body["branch"]


def test_dashboard_root_serves_static_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Octarel Control Center" in resp.text


# --------------------------------------------------------------------------- control endpoints


def test_enqueue_and_start_dry_run_through_the_http_api(client, ctx, monkeypatch):
    spawned = []
    monkeypatch.setattr(ctx.supervisor, "_spawn", lambda argv, cwd: spawned.append(argv) or _FakeProcess())

    resp = client.post(
        "/api/commands/enqueue",
        json={
            "task_id": "t2",
            "task_ref": "ENG-AGENT-02",
            "role": "focused-tests",
            "worker": "opencode2-gemini-flash-lite",
            "prompt": ["run tests"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = client.post("/api/commands/start", json={"task_id": "t2", "dry_run": True})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert spawned and "--dry-run" in spawned[0]


def test_unknown_command_returns_400(client):
    resp = client.post("/api/commands/not-a-real-verb", json={})
    assert resp.status_code == 400


def test_start_with_empty_queue_does_not_crash(client, ctx):
    ctx.state.upsert_task(
        Task(
            id="t1",
            task_ref="ENG-AGENT-02",
            role="focused-tests",
            worker="opencode2-gemini-flash-lite",
            state="SUCCEEDED",
        )
    )
    resp = client.post("/api/commands/start", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "no runnable" in body["message"]


# --------------------------------------------------------------------------- steering (ENG-AGENT-02-S4)


def test_steering_parse_slash_command_returns_a_proposal_without_executing(client, ctx):
    resp = client.post("/api/steering/parse", json={"text": "/pause t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PARSED"
    assert body["verb"] == "pause"
    assert body["args"] == {"task_id": "t1"}
    assert body["destructive"] is False
    # parsing alone must never mutate task state
    assert ctx.state.get_task("t1").state != "PAUSED"


def test_steering_parse_video_editor_intent_attaches_the_resolved_prepared_run(client, ctx):
    # ctx.repo_root is a bare tmp_path with no docs/video-editor ledger, so
    # the resolved option must honestly report "not ready" rather than a
    # fabricated task -- but the enrichment itself must always be attached
    # for this verb, never left for the frontend to fetch blind.
    resp = client.post("/api/steering/parse", json={"text": "continue the video editor"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PARSED"
    assert body["verb"] == "quickstart_start"
    assert body["args"] == {"key": "continue-video-editor"}
    assert "quickstart_option" in body
    assert body["quickstart_option"]["key"] == "continue-video-editor"
    # parsing alone must never create a runbook or a runbook-session task
    # (quickstart_start always creates a launch_mode="session" task; its
    # absence here proves nothing was actually started by parsing).
    assert ctx.state.list_runbooks() == []
    assert not any(t.launch_mode == "session" for t in ctx.state.list_tasks())


def test_steering_parse_unrecognized_reports_ai_escalation_eligibility(client):
    resp = client.post("/api/steering/parse", json={"text": "do something clever"})
    body = resp.json()
    assert body["status"] == "UNRECOGNIZED"
    assert body["verb"] is None
    assert "ai_escalation" in body
    assert set(body["ai_escalation"].keys()) == {"available", "worker", "reason"}


def test_steering_execute_non_destructive_action_succeeds_without_confirm(client, ctx):
    resp = client.post("/api/steering/execute", json={"verb": "pause", "args": {"task_id": "t1"}})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert ctx.state.get_task("t1").state == "PAUSED"


def test_steering_execute_destructive_action_requires_confirm(client):
    resp = client.post("/api/steering/execute", json={"verb": "stop", "args": {"task_id": "t1"}})
    assert resp.status_code == 409


def test_steering_execute_destructive_action_with_confirm_succeeds(client, ctx):
    resp = client.post(
        "/api/steering/execute",
        json={"verb": "stop", "args": {"task_id": "t1"}, "confirm": True},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert ctx.state.get_task("t1").state == "CANCELLED"


def test_steering_execute_client_cannot_bypass_confirm_gate_by_omitting_it(client):
    """Even if a client only ever sends confirm=false/absent for a destructive
    verb, the server must independently classify it as destructive — the
    gate is not client-trust-based."""

    resp = client.post(
        "/api/steering/execute",
        json={"verb": "provider_disable", "args": {"name": "grok-build"}, "confirm": False},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("confirm_value", ["false", "no", "0", [], {}, None])
def test_steering_execute_rejects_truthy_but_non_true_confirm_values(client, confirm_value):
    """Independent review (Grok Build) found that ``bool(payload.get("confirm"))``
    treats a non-empty JSON string like "false" as truthy, silently passing
    the gate. Only the JSON literal ``true`` may count as confirmed."""

    resp = client.post(
        "/api/steering/execute",
        json={"verb": "stop", "args": {"task_id": "t1"}, "confirm": confirm_value},
    )
    assert resp.status_code == 409, confirm_value


def test_steering_execute_rejects_non_dict_args(client):
    resp = client.post(
        "/api/steering/execute",
        json={"verb": "pause", "args": ["t1"], "confirm": True},
    )
    assert resp.status_code == 400


def test_steering_ai_route_endpoint_reports_shape(client):
    resp = client.get("/api/steering/ai-route")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"available", "worker", "reason"}


def test_steering_endpoints_never_spawn_a_subprocess(client, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("steering parse/execute must never spawn a subprocess by themselves")

    monkeypatch.setattr("subprocess.Popen", _boom)

    client.post("/api/steering/parse", json={"text": "/pause t1"})
    client.post("/api/steering/parse", json={"text": "ambiguous free text"})
    client.post("/api/steering/execute", json={"verb": "resume", "args": {"task_id": "t1"}})


class _FakeProcess:
    pid = 999

    def poll(self):
        return None


class _FakeExitedProcess:
    """A fake subprocess handle that has already exited with ``returncode``."""

    def __init__(self, returncode: int, pid: int = 4242) -> None:
        self.returncode = returncode
        self.pid = pid

    def poll(self) -> int:
        return self.returncode


def _wait_for_task_state(ctx, task_id: str, state: str, *, timeout: float = 5.0):
    """Poll in-process state for the dashboard's own reconciliation loop to catch up."""

    deadline = time.monotonic() + timeout
    task = ctx.state.get_task(task_id)
    while time.monotonic() < deadline:
        task = ctx.state.get_task(task_id)
        if task.state == state:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached {state}; last seen state={task.state!r}")


def test_dashboard_reconciles_a_dashboard_launched_task_that_exits_zero(ctx, roadmap_file, monkeypatch):
    """ENG-AGENT-02-S4 hotfix: a task launched via the dashboard's own control
    endpoints must not stay RUNNING forever once its subprocess exits — the
    dashboard's lifespan reconciliation loop must call ``poll_once()`` on its
    own Supervisor even though nothing schedules queued work for it."""

    monkeypatch.setattr(ctx.supervisor, "_spawn", lambda argv, cwd: _FakeExitedProcess(0))
    app = create_app(ctx, roadmap_path=roadmap_file)

    with TestClient(app) as client:
        resp = client.post("/api/commands/start", json={"task_id": "t1", "dry_run": True})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        task = _wait_for_task_state(ctx, "t1", "SUCCEEDED")
        assert task.result == "PASS"
        assert task.pid == 4242

    events = ctx.state.list_events()
    assert any(e.task_id == "t1" and "exited 0" in e.message for e in events)


def test_dashboard_reconciles_a_dashboard_launched_task_that_exits_nonzero(ctx, roadmap_file, monkeypatch):
    monkeypatch.setattr(ctx.supervisor, "_spawn", lambda argv, cwd: _FakeExitedProcess(1))
    app = create_app(ctx, roadmap_path=roadmap_file)

    with TestClient(app) as client:
        resp = client.post("/api/commands/start", json={"task_id": "t1", "dry_run": True})
        assert resp.status_code == 200

        task = _wait_for_task_state(ctx, "t1", "FAILED")
        assert task.result == "FAIL"
        assert task.last_error and "exited 1" in task.last_error


def test_dashboard_reconciliation_loop_only_polls_its_own_supervisor(ctx, roadmap_file, monkeypatch, tmp_path):
    """A second, independent CommandContext's Supervisor must never be touched
    by this dashboard's reconciliation loop."""

    from scripts.agents.control_plane.models import Task
    from scripts.agents.control_plane.state import State
    from scripts.agents.control_plane.supervisor import Supervisor
    from scripts.agents.registry import load_registry

    other_state = State(":memory:")
    other_registry = load_registry()
    other_supervisor = Supervisor(registry=other_registry, repo_root=tmp_path, state=other_state)
    other_task = Task(id="other1", task_ref="X", role="focused-tests", worker="opencode2-gemini-flash-lite")
    other_state.upsert_task(other_task)
    monkeypatch.setattr(other_supervisor, "_spawn", lambda argv, cwd: _FakeExitedProcess(0))
    other_supervisor.launch_task(other_task, dry_run=True)

    monkeypatch.setattr(ctx.supervisor, "_spawn", lambda argv, cwd: _FakeExitedProcess(0))
    app = create_app(ctx, roadmap_path=roadmap_file)

    with TestClient(app) as client:
        resp = client.post("/api/commands/start", json={"task_id": "t1", "dry_run": True})
        assert resp.status_code == 200
        _wait_for_task_state(ctx, "t1", "SUCCEEDED")

    assert other_state.get_task("other1").state == "RUNNING"


# --------------------------------------------------------------------------- independence guarantees


def test_octascene_app_probe_reports_stopped_when_nothing_listens():
    assert probe_octascene_app(port=1) in {"STOPPED", "UNKNOWN"}


def test_octascene_app_probe_reports_running_against_a_real_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        port = server.getsockname()[1]
        assert probe_octascene_app(port=port) == "RUNNING"
    finally:
        server.close()


def test_dashboard_keeps_serving_when_octascene_app_is_stopped(client, monkeypatch):
    monkeypatch.setattr("scripts.agents.control_plane.dashboard_api.probe_octascene_app", lambda *a, **k: "STOPPED")
    body = client.get("/api/overview").json()
    assert body["octascene_app_status"] == "STOPPED"
    assert client.get("/api/tasks").status_code == 200


def test_zero_ai_calls_across_a_simulated_multi_refresh_loop(client, monkeypatch):
    """Every read endpoint must be pollable indefinitely with zero subprocess/model calls."""

    def _boom(*_a, **_k):
        raise AssertionError("a dashboard read must never spawn a subprocess")

    monkeypatch.setattr("subprocess.Popen", _boom)

    endpoints = [
        "/api/overview",
        "/api/tasks",
        "/api/processes",
        "/api/workers",
        "/api/flow",
        "/api/providers",
        "/api/models",
        "/api/worktrees",
        "/api/tests",
        "/api/development-throughput",
        "/api/events",
        "/api/attention",
        "/api/roadmap",
        "/api/resources",
        "/api/app-status",
        "/api/telemetry",
        "/api/steering/ai-route",
    ]
    for _ in range(3):
        for endpoint in endpoints:
            resp = client.get(endpoint)
            assert resp.status_code == 200, endpoint
        # A steering *parse* is also part of ordinary dashboard operation (the
        # operator types into the steering panel on every visit) and must stay
        # zero-AI exactly like every read endpoint above.
        resp = client.post("/api/steering/parse", json={"text": "/pause t1"})
        assert resp.status_code == 200


def test_dashboard_never_imports_app_or_frontend():
    import ast
    import sys

    module = sys.modules["scripts.agents.control_plane.dashboard_api"]
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert "app" not in imported_roots
    assert "frontend" not in imported_roots


# --------------------------------------------------------------------------- real port independence


def test_server_starts_on_two_independent_ports_in_the_same_session(tmp_path):
    """A real socket-binding integration test: two dashboards, two ports, one process."""

    registry = load_registry()

    def _make_ctx(root: Path) -> CommandContext:
        state = State(":memory:")
        for provider in seed_provider_states(registry):
            state.upsert_provider_state(provider)
        return CommandContext(
            state=state,
            registry=registry,
            scheduler=Scheduler(),
            supervisor=Supervisor(registry=registry, repo_root=root, state=state),
            repo_root=root,
        )

    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    port_a, port_b = _free_port(), _free_port()
    app_a = create_app(_make_ctx(tmp_path / "a"))
    app_b = create_app(_make_ctx(tmp_path / "b"))

    config_a = uvicorn.Config(app_a, host="127.0.0.1", port=port_a, log_level="error")
    config_b = uvicorn.Config(app_b, host="127.0.0.1", port=port_b, log_level="error")
    server_a = uvicorn.Server(config_a)
    server_b = uvicorn.Server(config_b)

    thread_a = threading.Thread(target=server_a.run, daemon=True)
    thread_b = threading.Thread(target=server_b.run, daemon=True)
    thread_a.start()
    thread_b.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (server_a.started and server_b.started):
            time.sleep(0.05)
        assert server_a.started and server_b.started

        import httpx

        resp_a = httpx.get(f"http://127.0.0.1:{port_a}/api/overview", timeout=5)
        resp_b = httpx.get(f"http://127.0.0.1:{port_b}/api/overview", timeout=5)
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
    finally:
        server_a.should_exit = True
        server_b.should_exit = True
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)


# --------------------------------------------------------------------------- runbooks


def _git_repo(tmp_path: Path, *, branch: str = "eng/test-runbook") -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


@pytest.fixture()
def git_client(tmp_path: Path, monkeypatch) -> TestClient:
    repo = _git_repo(tmp_path)
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    class FakeProcess:
        pid = 4321

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd: FakeProcess())
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor, repo_root=repo)
    app = create_app(ctx)
    return TestClient(app)


def test_runbook_presets_endpoint_is_a_static_zero_ai_catalog(git_client):
    presets = git_client.get("/api/runbooks/presets").json()
    keys = {p["key"] for p in presets}
    assert keys == {"overnight-development", "finish-pr", "test-fix", "ui-polish", "review-only"}


def test_runbooks_list_is_empty_initially(git_client):
    assert git_client.get("/api/runbooks").json() == []


def test_runbook_full_lifecycle_via_dashboard_endpoints(git_client, tmp_path):
    create_resp = git_client.post(
        "/api/commands/runbook_create",
        json={
            "name": "Nightly finish",
            "preset": "finish-pr",
            "source_ref": "PR #92",
            "branch": "eng/test-runbook",
            "worktree": str(tmp_path),
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    body = create_resp.json()
    assert body["ok"] is True
    runbook_id = body["data"]["runbook_id"]
    assert body["data"]["status"] == "DRAFT"

    detail = git_client.get(f"/api/runbooks/{runbook_id}").json()
    assert detail["status"] == "DRAFT"
    assert detail["preset"] == "finish-pr"

    start_resp = git_client.post("/api/commands/runbook_start", json={"runbook_id": runbook_id})
    assert start_resp.status_code == 200, start_resp.text
    assert start_resp.json()["data"]["status"] == "RUNNING"

    # A second attempt to start the same (now RUNNING) runbook is rejected.
    second = git_client.post("/api/commands/runbook_start", json={"runbook_id": runbook_id})
    assert second.status_code == 400

    # Stop requires explicit confirm=true, same tamper-proof pattern as steering.
    unconfirmed = git_client.post("/api/commands/runbook_stop", json={"runbook_id": runbook_id})
    assert unconfirmed.status_code == 409

    confirmed = git_client.post("/api/commands/runbook_stop", json={"runbook_id": runbook_id, "confirm": True})
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["status"] == "STOPPING"


def test_runbook_report_endpoint_reports_none_before_completion(git_client, tmp_path):
    create_resp = git_client.post(
        "/api/commands/runbook_create",
        json={
            "name": "x",
            "preset": "test-fix",
            "source_ref": "x",
            "branch": "eng/test-runbook",
            "worktree": str(tmp_path),
        },
    )
    runbook_id = create_resp.json()["data"]["runbook_id"]
    report = git_client.get(f"/api/runbooks/{runbook_id}/report").json()
    assert report["report_markdown"] is None


def test_runbook_start_with_unregistered_worktree_returns_400(git_client, tmp_path):
    other = tmp_path.parent / "not-a-worktree"
    other.mkdir(exist_ok=True)
    create_resp = git_client.post(
        "/api/commands/runbook_create",
        json={"name": "x", "preset": "test-fix", "source_ref": "x", "branch": "b", "worktree": str(other)},
    )
    runbook_id = create_resp.json()["data"]["runbook_id"]
    resp = git_client.post("/api/commands/runbook_start", json={"runbook_id": runbook_id})
    assert resp.status_code == 400


# ------------------------------------------------ telemetry checkpoint cost (large projects)


def _checkpoint_probe(monkeypatch):
    """Count checkpoint_status() calls; each one is a git subprocess in production."""

    from scripts.agents.control_plane import dashboard_api

    calls: list[str] = []

    class _Status:
        def __init__(self, path: Path) -> None:
            self.path = path

        def as_dict(self) -> dict:
            return {"worktree": str(self.path)}

    def fake(path: Path):
        calls.append(str(path))
        return _Status(path)

    monkeypatch.setattr(dashboard_api, "checkpoint_status", fake)
    return calls


def test_telemetry_checkpoints_are_computed_once_per_ttl_not_per_poll(ctx, roadmap_file, monkeypatch, tmp_path):
    from scripts.agents.control_plane.models import WorktreeRecord

    calls = _checkpoint_probe(monkeypatch)
    for index in range(5):
        path = tmp_path / f"wt{index}"
        path.mkdir()
        ctx.state.upsert_worktree(WorktreeRecord(path=str(path), branch=f"b{index}"))
    client = TestClient(create_app(ctx, roadmap_path=roadmap_file))

    bodies = [client.get("/api/telemetry").json() for _ in range(6)]  # six 2-second polls
    assert len(calls) == 5  # one git status per worktree, once -- not 30
    assert all(len(body["checkpoints"]) == 5 for body in bodies)


def test_telemetry_checkpoints_recompute_after_the_ttl(ctx, roadmap_file, monkeypatch, tmp_path):
    from scripts.agents.control_plane import dashboard_api
    from scripts.agents.control_plane.models import WorktreeRecord

    calls = _checkpoint_probe(monkeypatch)
    path = tmp_path / "wt"
    path.mkdir()
    ctx.state.upsert_worktree(WorktreeRecord(path=str(path), branch="b"))
    client = TestClient(create_app(ctx, roadmap_path=roadmap_file))
    client.get("/api/telemetry")
    monkeypatch.setattr(dashboard_api, "CHECKPOINT_CACHE_TTL_SECONDS", 0.0)
    client.get("/api/telemetry")
    assert len(calls) == 2


def test_worktree_endpoint_caches_expensive_git_scan_within_poll_window(ctx, roadmap_file, monkeypatch):
    from scripts.agents.control_plane import dashboard_api

    calls = []

    def slow_scan(_ctx):
        calls.append(time.monotonic())
        time.sleep(0.08)
        return [{"path": str(_ctx.repo_root), "branch": "main", "dirty": False}]

    monkeypatch.setattr(dashboard_api, "list_worktree_statuses", slow_scan)
    client = TestClient(create_app(ctx, roadmap_path=roadmap_file))
    started = time.monotonic()
    for _ in range(4):
        assert client.get("/api/worktrees").status_code == 200
    elapsed = time.monotonic() - started

    assert len(calls) == 1
    assert elapsed < 0.25


def test_concurrent_telemetry_requests_share_one_computation(ctx, roadmap_file, monkeypatch, tmp_path):
    from scripts.agents.control_plane import dashboard_api
    from scripts.agents.control_plane.models import WorktreeRecord

    started = threading.Event()
    calls: list[str] = []

    class _Status:
        def as_dict(self) -> dict:
            return {"worktree": "x"}

    def slow(path: Path):
        calls.append(str(path))
        started.set()
        time.sleep(0.3)
        return _Status()

    monkeypatch.setattr(dashboard_api, "checkpoint_status", slow)
    path = tmp_path / "wt"
    path.mkdir()
    ctx.state.upsert_worktree(WorktreeRecord(path=str(path), branch="b"))
    client = TestClient(create_app(ctx, roadmap_path=roadmap_file))
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(len(client.get("/api/telemetry").json()["checkpoints"]))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [1] * 6 and len(calls) == 1  # a stampede of pollers costs one git status


def test_telemetry_checkpoints_are_scoped_to_the_selected_project(ctx, roadmap_file, monkeypatch, tmp_path):
    import subprocess

    from scripts.agents.control_plane import project_registry as pr
    from scripts.agents.control_plane.models import WorktreeRecord

    def repo(name: str) -> Path:
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return root

    calls = _checkpoint_probe(monkeypatch)
    for project_id in ("proj-a", "proj-b"):
        pr.register_project(ctx.state, {"project_id": project_id, "display_name": project_id, "local_repo_root": str(repo(project_id))})
    pr.select_project(ctx.state, "proj-a")
    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    client = TestClient(create_app(ctx, roadmap_path=roadmap_file))  # startup re-observes real Git worktrees
    ctx.state.upsert_worktree(WorktreeRecord(path=str(mine), branch="m", project_id="proj-a"))
    ctx.state.upsert_worktree(WorktreeRecord(path=str(other), branch="o", project_id="proj-b"))
    paths = {row["worktree"] for row in client.get("/api/telemetry").json()["checkpoints"]}
    assert str(mine) in paths and str(other) not in paths
    assert str(other) not in calls  # the other project's worktree is never shelled out to

    # Switching project invalidates the cache immediately (no stale cross-project rows).
    pr.select_project(ctx.state, "proj-b")
    paths = {row["worktree"] for row in client.get("/api/telemetry").json()["checkpoints"]}
    assert str(other) in paths and str(mine) not in paths
