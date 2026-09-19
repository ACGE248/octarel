"""ENG-AGENT-10 managed dispatch, intake, balancing, and admission contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import CommandContext, apply_command
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.dispatch import managed_admit
from scripts.agents.control_plane.intake import IntakeCollision, check_and_claim
from scripts.agents.control_plane.models import (
    KIND_HEAVY,
    KIND_READ,
    TASK_BLOCKED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    ProviderState,
    Task,
)
from scripts.agents.control_plane.provider_state import STATE_AVAILABLE
from scripts.agents.control_plane.scheduler import ConcurrencyPolicy, Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.registry import load_registry


class FakeSupervisor:
    def __init__(self, state: State):
        self.state = state
        self.launched: list[str] = []

    def launch_task(self, task: Task, *, dry_run: bool = False) -> Task:  # noqa: ARG002
        task.state = TASK_RUNNING
        task.pid = 4242
        self.state.upsert_task(task)
        self.launched.append(task.id)
        return task


def _available_state() -> tuple[State, object]:
    state = State(":memory:")
    registry = load_registry()
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(
                name=worker.name,
                execution_system=worker.execution_system,
                provider=worker.provider,
                cost_class=worker.cost_class,
                state=STATE_AVAILABLE,
                configured=True,
            )
        )
    return state, registry


def _managed(repo: Path, task: Task, *, state: State | None = None, policy: ConcurrencyPolicy | None = None):
    state, registry = (state, load_registry()) if state is not None else _available_state()
    supervisor = FakeSupervisor(state)
    state.upsert_task(task)
    result = managed_admit(
        state=state,
        registry=registry,
        scheduler=Scheduler(policy),
        supervisor=supervisor,
        repo_root=repo,
        task=task,
        dry_run=True,
    )
    return result, state, registry, supervisor


def test_cost_first_selection_is_explainable_and_durable(tmp_path):
    result, state, _, _ = _managed(
        tmp_path,
        Task(id="d1", task_ref="ENG-X-1", owner_ref="issue:#1", role="primary-implementation", worker="claude-code"),
    )
    assert result.task.worker == "grok-build"
    assert "capability/safety/cost/load/fairness/diversity" in result.task.selected_worker_reason
    decision = state.list_dispatch_decisions()[0]
    assert decision["selected_worker"] == "grok-build"
    assert decision["alternatives"]
    assert decision["scores"]["grok-build"][0] < decision["scores"]["claude-code"][0]


def test_fairness_and_stable_tie_breaking_apply_only_after_cost_and_pressure(tmp_path):
    state, registry = _available_state()
    grok = state.get_provider_state("grok-build")
    grok.state = "DISABLED"
    state.upsert_provider_state(grok)
    state.record_dispatch_decision(
        task_id="prior", stable_task_id="ENG-OLD", owner_ref="issue:#9", outcome="ADMITTED",
        reason="prior selection", selected_worker="claude-code",
    )
    task = Task(
        id="d2", task_ref="ENG-X-2", owner_ref="issue:#2", role="primary-implementation",
        worker="claude-code", codex_policy="balanced", codex_auto_eligible=True,
    )
    supervisor = FakeSupervisor(state)
    state.upsert_task(task)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=task, dry_run=True,
    )
    assert result.task.worker == "codex-build"


def test_codex_pressure_and_permission_profile_filter_before_balancing(tmp_path):
    task = Task(
        id="d3", task_ref="ENG-X-3", owner_ref="issue:#3", role="primary-implementation",
        worker="claude-code", permission_profile="repo_configured_auto",
        codex_policy="balanced", codex_auto_eligible=False,
    )
    result, _, _, _ = _managed(tmp_path, task)
    assert result.task.worker == "claude-code"
    assert "codex-build" not in result.task.selection_alternatives


def test_preauthorized_providers_still_obey_capability_permission_and_worktree_gates(tmp_path):
    state, registry = _available_state()
    supervisor = FakeSupervisor(state)

    google_write = Task(
        id="google-write", task_ref="ENG-AUTH-1", owner_ref="issue:#201",
        role="focused-tests", worker="opencode2-gemini-flash-lite", kind="write",
        required_capability="write",
    )
    state.upsert_task(google_write)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=google_write, dry_run=True,
    )
    assert not result.launched
    assert "not write-capable" in result.reason

    for name in ("claude-code", "codex-build"):
        provider = state.get_provider_state(name)
        provider.state = "DISABLED"
        state.upsert_provider_state(provider)
    grok_unattended = Task(
        id="grok-auto", task_ref="ENG-AUTH-2", owner_ref="issue:#202",
        role="primary-implementation", worker="grok-build",
        permission_profile="repo_configured_auto", worktree=str(tmp_path / "grok"),
    )
    state.upsert_task(grok_unattended)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=grok_unattended, dry_run=True,
    )
    assert not result.launched
    assert "permission profile repo_configured_auto unsupported" in result.reason

    running = Task(
        id="grok-running", task_ref="ENG-AUTH-3", role="primary-implementation",
        worker="grok-build", state=TASK_RUNNING, worktree=str(tmp_path / "shared"),
    )
    waiting = Task(
        id="grok-waiting", task_ref="ENG-AUTH-4", role="primary-implementation",
        worker="grok-build", worktree=str(tmp_path / "shared"),
    )
    assert "worktree already owned" in Scheduler().block_reason(waiting, [running, waiting])


def test_provider_diverse_reviewer_and_failed_reviewer_are_excluded(tmp_path):
    state, registry = _available_state()
    google = state.get_provider_state("opencode2-gemini-flash-lite-review")
    google.consecutive_failures = 1
    google.last_error = "adapter schema failed"
    state.upsert_provider_state(google)
    task = Task(
        id="r1", task_ref="ENG-X-4", owner_ref="issue:#4", role="diff-review",
        worker="codex-review", kind=KIND_READ, avoid_provider="OpenAI",
    )
    supervisor = FakeSupervisor(state)
    state.upsert_task(task)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=task, dry_run=True,
    )
    assert result.task.worker == "antigravity-diff-review"
    assert result.task.worker != "codex-review"


def test_dependency_waves_missing_dependencies_and_cycles_are_truthful():
    scheduler = Scheduler()
    root = Task(id="root", task_ref="ENG-X", role="primary-implementation", worker="claude-code")
    child = Task(id="child", task_ref="ENG-Y", role="primary-implementation", worker="claude-code", dependencies=("root",))
    grandchild = Task(id="grand", task_ref="ENG-Z", role="primary-implementation", worker="claude-code", dependencies=("child",))
    assert scheduler.dependency_wave(root, {t.id: t for t in (root, child, grandchild)}) == 0
    assert scheduler.dependency_wave(grandchild, {t.id: t for t in (root, child, grandchild)}) == 2
    missing = Task(id="missing", task_ref="ENG-M", role="primary-implementation", worker="claude-code", dependencies=("none",))
    assert "missing" in scheduler.block_reason(missing, [missing])
    a = Task(id="a", task_ref="ENG-A", role="primary-implementation", worker="claude-code", dependencies=("b",))
    b = Task(id="b", task_ref="ENG-B", role="primary-implementation", worker="claude-code", dependencies=("a",))
    assert "cycle" in scheduler.block_reason(a, [a, b])


def test_dependency_ordering_and_queue_reason(tmp_path):
    dependency = Task(id="dep", task_ref="ENG-D", role="primary-implementation", worker="claude-code", state=TASK_PENDING)
    task = Task(
        id="next", task_ref="ENG-X-5", owner_ref="issue:#5", role="primary-implementation",
        worker="claude-code", dependencies=("dep",),
    )
    state, registry = _available_state()
    state.upsert_task(dependency)
    supervisor = FakeSupervisor(state)
    state.upsert_task(task)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=task, dry_run=True,
    )
    assert result.task.state == "QUEUED"
    assert result.task.dependency_wave == 1
    assert result.reason == "waiting for dependencies: dep"
    dependency.state = TASK_SUCCEEDED
    state.upsert_task(dependency)
    result = managed_admit(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=state.get_task("next"), dry_run=True,
    )
    assert result.launched


def test_global_kind_provider_and_single_heavy_caps():
    running = Task(id="h1", task_ref="H1", role="focused-tests", worker="opencode2-gemini-flash-lite", kind=KIND_HEAVY, state=TASK_RUNNING)
    waiting = Task(id="h2", task_ref="H2", role="focused-tests", worker="opencode2-gemini-flash-lite", kind=KIND_HEAVY)
    scheduler = Scheduler(ConcurrencyPolicy(max_global_workers=4, max_heavy_jobs=1, max_per_provider=1))
    reason = scheduler.block_reason(waiting, [running, waiting])
    assert reason == "heavy concurrency cap reached (1)"
    global_scheduler = Scheduler(ConcurrencyPolicy(max_global_workers=1, max_heavy_jobs=2, max_per_provider=2))
    assert global_scheduler.block_reason(waiting, [running, waiting]) == "global concurrency cap reached (1)"

    google_running = Task(
        id="g1", task_ref="G1", role="focused-tests", worker="opencode2-gemini-flash-lite",
        kind=KIND_READ, state=TASK_RUNNING, selected_provider="Google",
    )
    google_waiting = Task(
        id="g2", task_ref="G2", role="focused-tests", worker="antigravity-focused-tests",
        kind=KIND_READ, selected_provider="Google",
    )
    provider_scheduler = Scheduler(
        ConcurrencyPolicy(max_extra_read_workers=2, max_per_provider=1)
    )
    assert provider_scheduler.block_reason(google_waiting, [google_running, google_waiting]) == (
        "provider cap reached for Google (1)"
    )


def test_parallel_writers_require_separate_worktrees_and_nonoverlapping_paths():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=2))
    first = Task(
        id="w1", task_ref="W1", role="primary-implementation", worker="claude-code",
        state=TASK_RUNNING, worktree="/tmp/w1", changed_paths=("scripts/agents",),
    )
    same_tree = Task(id="w2", task_ref="W2", role="primary-implementation", worker="grok-build", worktree="/tmp/w1")
    assert "worktree already owned" in scheduler.block_reason(same_tree, [first, same_tree])
    overlap = Task(
        id="w3", task_ref="W3", role="primary-implementation", worker="grok-build",
        worktree="/tmp/w3", changed_paths=("scripts/agents/control_plane",),
    )
    assert "integration overlap" in scheduler.block_reason(overlap, [first, overlap])
    overlap.dependencies = ("w1",)
    first.state = TASK_SUCCEEDED
    assert scheduler.block_reason(overlap, [first, overlap]) is None


def test_durable_intake_collision_blocks_without_relabelling(tmp_path):
    state = State(":memory:")
    first = check_and_claim(
        state=state, repo_root=tmp_path, task_ref="ENG-AGENT-10", owner_ref="issue #125", source="issue intake"
    )
    assert first.owner_ref == "issue:#125"
    with pytest.raises(IntakeCollision, match="already owned"):
        check_and_claim(
            state=state, repo_root=tmp_path, task_ref="ENG-AGENT-10", owner_ref="issue #999", source="collision"
        )
    assert state.list_task_identity_claims()[0]["owner_ref"] == "issue:#125"


def test_maintained_contract_issue_collision_blocks_before_claim(tmp_path):
    contract = tmp_path / "docs" / "engineering" / "ENG-AGENT-10.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("# ENG-AGENT-10\n\nIssue: #124\n", encoding="utf-8")
    with pytest.raises(IntakeCollision, match="maintained issue evidence"):
        check_and_claim(
            state=State(":memory:"), repo_root=tmp_path, task_ref="ENG-AGENT-10",
            owner_ref="issue #125", source="managed CLI",
        )


def test_control_plane_and_operator_command_share_managed_dispatch(tmp_path):
    state, registry = _available_state()
    supervisor = FakeSupervisor(state)
    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor, repo_root=tmp_path)
    result = apply_command(
        ctx, "managed_dispatch", task_id="manual", task_ref="ENG-X-6", owner_ref="issue:#6",
        role="primary-implementation", worker="claude-code", prompt=["bounded work"], dry_run=True,
    )
    assert result.ok
    assert result.data["admission"] == "ADMITTED"
    assert supervisor.launched == ["manual"]

    caps = apply_command(
        ctx, "set_concurrency", global_count=3, write_count=2, read_count=2,
        heavy_count=1, provider_count=1,
    )
    assert caps.ok
    assert ctx.scheduler.policy.max_global_workers == 3
    assert state.get_control_setting("max_provider_workers") == "1"


def test_control_plane_duplicate_owner_is_persisted_as_blocked(tmp_path):
    state, registry = _available_state()
    supervisor = FakeSupervisor(state)
    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=supervisor, repo_root=tmp_path)
    first = apply_command(
        ctx, "enqueue", task_id="first", task_ref="ENG-X-9", owner_ref="issue:#9",
        role="primary-implementation", worker="claude-code",
    )
    second = apply_command(
        ctx, "enqueue", task_id="collision", task_ref="ENG-X-9", owner_ref="issue:#10",
        role="primary-implementation", worker="claude-code",
    )
    assert first.ok
    assert not second.ok
    assert state.get_task("collision").state == TASK_BLOCKED
    assert "already owned by issue:#9" in state.get_task("collision").admission_reason


def test_optional_overflow_never_becomes_managed_dispatch_fallback(tmp_path):
    task = Task(
        id="overflow", task_ref="ENG-X-10", owner_ref="issue:#10", role="overflow",
        worker="deepseek-overflow", required_capability="focused-edit",
    )
    result, _, _, supervisor = _managed(tmp_path, task)
    assert not result.launched
    assert result.task.state == TASK_BLOCKED
    assert "API/paid overflow is not eligible" in result.reason
    assert supervisor.launched == []


def test_fallback_reservation_survives_restart_and_is_not_double_counted(tmp_path):
    db = tmp_path / "state.db"
    state, registry = _available_state()
    # Use a file-backed state for the actual restart assertion.
    state.close()
    state = State(db)
    for worker in registry.workers.values():
        state.upsert_provider_state(
            ProviderState(worker.name, worker.execution_system, worker.provider, worker.cost_class, STATE_AVAILABLE, True)
        )
    task = Task(
        id="fallback", task_ref="ENG-X-7", owner_ref="issue:#7", role="primary-implementation",
        worker="codex-build", permission_profile="repo_configured_auto", fallback_selected_worker="codex-build",
        fallback_automatic=True, codex_policy="balanced", codex_auto_eligible=True,
    )
    state.upsert_task(task)
    state.upsert_usage_governance({
        "runbook_id": "RB-X", "task_id": task.id, "classification": "routine", "codex_policy": "balanced",
        "codex_auto_eligible": True, "max_codex_invocations": 1, "codex_invocations": 1,
        "route_history": [{"worker": "codex-build", "status": "STARTING"}],
    })
    task.runbook_id = "RB-X"
    state.upsert_task(task)
    state.close()
    reopened = State(db)
    supervisor = FakeSupervisor(reopened)
    result = managed_admit(
        state=reopened, registry=registry, scheduler=Scheduler(), supervisor=supervisor,
        repo_root=tmp_path, task=reopened.get_task("fallback"), dry_run=True,
    )
    assert result.launched
    assert result.task.worker == "codex-build"


def test_pre_eng_agent_10_task_table_migrates_with_safe_dispatch_defaults(tmp_path):
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, task_ref TEXT NOT NULL, role TEXT NOT NULL, "
        "worker TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'write', state TEXT NOT NULL, "
        "priority INTEGER NOT NULL DEFAULT 0, dependencies TEXT NOT NULL DEFAULT '[]', pid INTEGER, "
        "worktree TEXT, command TEXT NOT NULL DEFAULT '[]', result TEXT, last_error TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO tasks (id, task_ref, role, worker, state, created_at, updated_at) "
        "VALUES ('old', 'ENG-OLD', 'primary-implementation', 'claude-code', 'PENDING', 'a', 'a')"
    )
    connection.commit()
    connection.close()

    with State(db) as state:
        task = state.get_task("old")
        assert task.admission_state == "PENDING"
        assert task.changed_paths == ()
        assert task.selection_alternatives == ()
        assert task.codex_policy == "conserve"


def test_control_center_projects_only_persisted_dispatch_truth(tmp_path):
    state, registry = _available_state()
    task = Task(
        id="ui", task_ref="ENG-X-8", owner_ref="issue:#8", role="primary-implementation",
        worker="grok-build", state=TASK_PENDING, dependency_wave=2, admission_state="QUEUED",
        admission_reason="waiting for dependencies: build", selection_alternatives=("claude-code",),
        worktree=str(tmp_path / "worktree"),
    )
    state.upsert_task(task)
    ctx = CommandContext(
        state=state, registry=registry, scheduler=Scheduler(), supervisor=FakeSupervisor(state), repo_root=tmp_path,
    )
    with TestClient(create_app(ctx)) as client:
        task_body = client.get("/api/tasks").json()[0]
        dispatch_body = client.get("/api/dispatch").json()
    assert task_body["dependency_wave"] == 2
    assert task_body["admission_reason"] == "waiting for dependencies: build"
    assert dispatch_body["waves"][0]["wave"] == 2
    assert dispatch_body["waves"][0]["alternatives"] == ["claude-code"]
    assert dispatch_body["caps"]["global"]["used"] == 0
