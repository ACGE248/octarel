"""End-to-end Quick Start start (ENG-AGENT-02-S7, issue #97).

Covers the piece that used to be missing entirely: turning a resolved Quick
Start option into a real, running Runbook — including provisioning a
worktree on the operator's behalf when the resolved task has none yet, so
"Start Development" works even the very first time a task becomes eligible.

No real worker CLI is ever spawned here: ``Supervisor._spawn`` is
monkeypatched to a fake process, exactly like the existing Runbook lifecycle
tests, so this stays a fast, deterministic, zero-subprocess-worker test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import CommandContext, apply_command
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.intake import check_and_claim
from scripts.agents.control_plane.models import RUNBOOK_RUNNING
from scripts.agents.control_plane.provider_state import reconcile_provider_states
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    QuickStartError,
    start_quickstart_option,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import REASON_AVAILABLE, load_registry

_LEDGER = """\
| ID | Status | Notes/evidence |
|---|---|---|
| V1-01 | pending | Local import. |
"""


def _git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-m", "main"], cwd=root, check=True)


def _write_ledger(repo_root: Path) -> None:
    ledger_path = repo_root / VIDEO_EDITOR_LEDGER_RELATIVE
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(_LEDGER, encoding="utf-8")


class _FakeProcess:
    pid = 4242


def _fake_running_supervisor(monkeypatch) -> None:
    monkeypatch.setattr(Supervisor, "_spawn", lambda self, argv, cwd: _FakeProcess())  # noqa: ARG005
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)


def test_start_quickstart_option_provisions_a_worktree_when_none_exists(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    _fake_running_supervisor(monkeypatch)

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)

    runbook = start_quickstart_option(
        state=state, registry=registry, supervisor=supervisor, repo_root=repo_root, key="continue-video-editor"
    )

    assert runbook.status == RUNBOOK_RUNNING
    assert runbook.source_ref.startswith("V1-01")
    assert Path(runbook.worktree).is_dir()
    assert Path(runbook.worktree).parent == repo_root.parent
    assert runbook.branch.startswith("video-editor/v1-01")


def test_start_quickstart_option_reuses_an_already_existing_worktree(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    existing_worktree = tmp_path / "repo-v1-01"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(existing_worktree), "-b", "video-editor/V1-01-local-import"],
        cwd=repo_root,
        check=True,
    )
    _fake_running_supervisor(monkeypatch)

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)

    runbook = start_quickstart_option(
        state=state, registry=registry, supervisor=supervisor, repo_root=repo_root, key="continue-video-editor"
    )

    assert runbook.status == RUNBOOK_RUNNING
    assert runbook.worktree == str(existing_worktree)
    assert runbook.branch == "video-editor/V1-01-local-import"
    # Reused an already-existing worktree: provisioning must not have created
    # a second one.
    assert len(list(tmp_path.glob("repo-v1-01*"))) == 1


def test_start_quickstart_option_reuses_the_same_task_identity_owner(tmp_path, monkeypatch):
    """A prior managed intake claim by the same stable task is idempotent.

    Quick Start's ledger field is a stable task ID, not an issue reference;
    treating it as a different raw owner would falsely block the launch while
    still leaving genuine different-owner collisions enforced by intake.
    """

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    _fake_running_supervisor(monkeypatch)

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)
    check_and_claim(
        state=state,
        repo_root=repo_root,
        task_ref="V1-01",
        owner_ref=None,
        source="prior managed intake",
    )

    runbook = start_quickstart_option(
        state=state, registry=registry, supervisor=supervisor, repo_root=repo_root, key="continue-video-editor"
    )

    assert runbook.status == RUNBOOK_RUNNING
    claims = state.list_task_identity_claims()
    assert claims[0]["stable_task_id"] == "V1-01"
    assert claims[0]["owner_ref"] == "task:V1-01"


def test_start_quickstart_option_dry_run_never_spawns_the_real_worker_cli(tmp_path, monkeypatch):
    """``dry_run=True`` must reach start_runbook's own guarantee unchanged: a

    cheap placeholder subprocess runs instead of the real `claude`/`grok`
    binary (see Supervisor._build_argv) -- this is what test/tooling
    automation uses to exercise the Quick Start "Start Development" endpoint
    safely, exactly like the existing runbook_start command's dry_run flag.
    Patching Supervisor._spawn (not subprocess.Popen/run globally) keeps
    provisioning's and validate_target's own legitimate `git` calls working,
    matching the established pattern in test_orchestrator_runbooks.py.
    """

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    spawned_argv = []
    monkeypatch.setattr(
        Supervisor, "_spawn", lambda self, argv, cwd: (spawned_argv.append(argv), _FakeProcess())[1]  # noqa: ARG005
    )
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)

    runbook = start_quickstart_option(
        state=state,
        registry=registry,
        supervisor=supervisor,
        repo_root=repo_root,
        key="continue-video-editor",
        dry_run=True,
    )
    assert runbook.status == RUNBOOK_RUNNING
    assert Path(runbook.worktree).is_dir()
    assert spawned_argv, "dry-run still spawns a cheap placeholder subprocess"
    assert "claude" not in spawned_argv[0][0]


def test_start_quickstart_option_rejects_an_unknown_key(tmp_path):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    with pytest.raises(QuickStartError, match="unknown quickstart option"):
        start_quickstart_option(
            state=state, registry=registry, supervisor=supervisor, repo_root=tmp_path, key="not-a-real-option"
        )


def test_start_quickstart_option_is_honest_when_nothing_is_pending(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    (repo_root / VIDEO_EDITOR_LEDGER_RELATIVE).write_text(_LEDGER.replace("pending", "complete"), encoding="utf-8")

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)

    with pytest.raises(QuickStartError, match="no task with status"):
        start_quickstart_option(
            state=state, registry=registry, supervisor=supervisor, repo_root=repo_root, key="continue-video-editor"
        )


def test_quickstart_start_command_is_reachable_through_apply_command(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    _fake_running_supervisor(monkeypatch)

    registry = load_registry()
    state = State(":memory:")
    ctx = CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=repo_root, state=state),
        repo_root=repo_root,
    )

    result = apply_command(ctx, "quickstart_start", key="continue-video-editor")
    assert result.ok is True
    assert result.data["status"] == RUNBOOK_RUNNING


def _quickstart_http_context(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git_repo(repo_root)
    _write_ledger(repo_root)
    existing_worktree = tmp_path / "repo-v1-01"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(existing_worktree), "-b", "video-editor/V1-01-local-import"],
        cwd=repo_root,
        check=True,
    )
    spawned = []

    class FakeProcess:
        pid = 4247

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        Supervisor,
        "_spawn",
        lambda self, argv, cwd: (spawned.append((argv, cwd)), FakeProcess())[1],  # noqa: ARG005
    )
    monkeypatch.setattr(
        "scripts.agents.registry.Worker.availability_reason",
        lambda self, probe=True: REASON_AVAILABLE,
    )
    registry = load_registry()
    state = State(":memory:")
    reconcile_provider_states(state, registry)
    scheduler = Scheduler()
    supervisor = Supervisor(registry=registry, repo_root=repo_root, state=state)
    ctx = CommandContext(
        state=state,
        registry=registry,
        scheduler=scheduler,
        supervisor=supervisor,
        repo_root=repo_root,
    )
    return ctx, TestClient(create_app(ctx)), spawned, existing_worktree


def test_real_quickstart_http_path_falls_back_from_known_claude_quota_to_codex(tmp_path, monkeypatch):
    """Exercise the production endpoint used by the actual Start Development button."""

    ctx, client, spawned, existing_worktree = _quickstart_http_context(tmp_path, monkeypatch)
    claude = ctx.state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    claude.reason = "weekly limit reached; resets 8pm (America/Toronto)"
    ctx.state.upsert_provider_state(claude)
    codex = ctx.state.get_provider_state("codex-build")
    codex.configured = True
    codex.state = "AVAILABLE"
    codex.reason = REASON_AVAILABLE
    ctx.state.upsert_provider_state(codex)
    option_before = client.get("/api/quickstart").json()[0]

    response = client.post(
        "/api/commands/quickstart_start",
        json={"key": "continue-video-editor"},
    )

    assert response.status_code == 200
    runbook_id = response.json()["data"]["runbook_id"]
    runbook = ctx.state.get_runbook(runbook_id)
    task = ctx.state.get_task(runbook.task_id)
    usage = ctx.state.get_usage_governance(runbook_id)
    assert len(spawned) == 1
    launched_argv = spawned[0][0]
    assert Path(launched_argv[0]).name.startswith("python")
    assert launched_argv[launched_argv.index("--worker") + 1] == "codex-build"
    assert task.worker == "codex-build"
    assert task.failed_worker_id == "claude-code"
    assert task.failure_category == "QUOTA"
    assert task.fallback_automatic is True
    assert runbook.id == runbook_id
    assert task.id == f"{runbook_id}-session"
    assert runbook.name == option_before["title"]
    assert runbook.source_ref == option_before["source_ref"]
    assert runbook.objective == option_before["objective"]
    assert runbook.branch == option_before["branch"] == "video-editor/V1-01-local-import"
    assert runbook.worktree == option_before["worktree"] == str(existing_worktree)
    assert runbook.permission_profile == option_before["permission_profile"]
    assert runbook.codex_policy == "balanced"
    assert runbook.codex_auto_eligible is True
    assert runbook.max_codex_invocations == 1
    assert usage["codex_invocations"] == 1
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code", "codex-build"]
    assert [item["status"] for item in usage["route_history"]] == ["UNAVAILABLE", "RUNNING"]
    policy = usage["context_manifest"]["policy_manifest"]
    assert policy["role"] == "IMPLEMENTER"
    assert policy["workflow"] == "IMPLEMENT"
    assert policy["read_write_mode"] == "write"
    assert policy["api_billing_enabled"] is False
    assert ctx.registry.get("codex-build").allow_api_billing is False


def test_real_quickstart_http_path_blocks_truthfully_when_codex_is_unavailable(tmp_path, monkeypatch):
    ctx, client, spawned, _existing_worktree = _quickstart_http_context(tmp_path, monkeypatch)
    claude = ctx.state.get_provider_state("claude-code")
    claude.state = "QUOTA_EXHAUSTED"
    claude.reason = "weekly quota exhausted"
    ctx.state.upsert_provider_state(claude)
    codex = ctx.state.get_provider_state("codex-build")
    codex.configured = False
    codex.state = "NOT_CONFIGURED"
    codex.reason = "NOT_AUTHENTICATED"
    ctx.state.upsert_provider_state(codex)

    response = client.post(
        "/api/commands/quickstart_start",
        json={"key": "continue-video-editor"},
    )

    assert response.status_code == 400
    assert spawned == []
    [runbook] = ctx.state.list_runbooks()
    task = ctx.state.get_task(runbook.task_id)
    usage = ctx.state.get_usage_governance(runbook.id)
    assert runbook.status == "FAILED"
    assert task.failed_worker_id == "claude-code"
    assert task.fallback_automatic is False
    assert [item["worker"] for item in usage["route_history"]] == ["claude-code"]
    assert "codex-build: provider is not configured" in runbook.recovery_note
    assert "grok-build: permission profile repo_configured_auto is unsupported" in runbook.recovery_note
