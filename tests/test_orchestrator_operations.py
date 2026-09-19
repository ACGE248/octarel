"""Focused safety and truthfulness checks for ENG-AGENT-02-S9 operations."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import (
    CommandContext,
    CommandError,
    apply_command,
)
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import TASK_RUNNING, Task
from scripts.agents.control_plane.operations import (
    AppLifecycleManager,
    OperationError,
    cleanup_worktrees,
    derived_roadmap,
    refresh_worktree_statuses,
    sanitize_terminal_command,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture()
def repo_ctx(tmp_path: Path) -> CommandContext:
    root = tmp_path / "octages"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test Developer")
    (root / "README.md").write_text("truth\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "initial truth")
    registry = load_registry()
    state = State(":memory:")
    ctx = CommandContext(state=state, registry=registry, scheduler=Scheduler(), supervisor=Supervisor(registry=registry, repo_root=root, state=state), repo_root=root)
    refresh_worktree_statuses(ctx)
    return ctx


def test_worktree_status_surfaces_review_checkout_origin_and_reclaim_state(repo_ctx: CommandContext):
    """ENG-AGENT-14 (issue #140): an auto-provisioned review checkout's PR/

    head-SHA origin must be surfaced truthfully in the Worktrees status row
    (previously a static "NOT_REPORTED" placeholder for every worktree).
    """

    from scripts.agents.control_plane.models import WorktreeRecord

    head_sha = git(repo_ctx.repo_root, "rev-parse", "HEAD")
    repo_ctx.state.upsert_worktree(WorktreeRecord(
        path=str(repo_ctx.repo_root), branch=None, managed=True,
        review_repository="ACGE248/octages", review_pr=139, review_head_sha=head_sha,
    ))

    row = refresh_worktree_statuses(repo_ctx)[0]

    assert row["review_pr"] == 139
    assert row["pr"] == 139
    assert row["review_repository"] == "ACGE248/octages"
    assert row["review_head_sha"] == head_sha
    assert row["review_head_matches_current"] is True


def test_list_worktree_statuses_surfaces_review_origin_without_git_cache(repo_ctx: CommandContext):
    """GET /api/worktrees must not drop ENG-AGENT-14 origin when the cache is empty."""

    from scripts.agents.control_plane.models import WorktreeRecord
    from scripts.agents.control_plane.operations import list_worktree_statuses

    head_sha = git(repo_ctx.repo_root, "rev-parse", "HEAD")
    repo_ctx.state.upsert_worktree(WorktreeRecord(
        path=str(repo_ctx.repo_root), branch=None, managed=True,
        review_repository="ACGE248/octages", review_pr=139, review_head_sha=head_sha,
    ))
    repo_ctx.state.set_project_setting("worktree_status_cache", "[]", repo_ctx.selected_project_id)

    row = list_worktree_statuses(repo_ctx)[0]

    assert row["review_pr"] == 139
    assert row["pr"] == 139
    assert row["review_repository"] == "ACGE248/octages"
    assert row["review_head_sha"] == head_sha


def test_discovered_worktree_is_truthful_and_read_only_until_adopted(repo_ctx: CommandContext):
    row = refresh_worktree_statuses(repo_ctx)[0]
    assert row["management"] == "DISCOVERED"
    assert row["read_only"] is True
    assert row["head_sha"] == git(repo_ctx.repo_root, "rev-parse", "HEAD")
    assert row["commit_subject"] == "initial truth"
    assert row["dirty"] is False
    result = apply_command(repo_ctx, "worktree_adopt", path=str(repo_ctx.repo_root))
    assert result.ok is True
    assert refresh_worktree_statuses(repo_ctx)[0]["management"] == "MANAGED"
    with pytest.raises(CommandError, match="already managed"):
        apply_command(repo_ctx, "worktree_adopt", path=str(repo_ctx.repo_root))


def test_adoption_rejects_non_worktree_and_active_writer(repo_ctx: CommandContext, tmp_path: Path):
    with pytest.raises(CommandError, match="not a discovered worktree"):
        apply_command(repo_ctx, "worktree_adopt", path=str(tmp_path))
    task = Task(id="writer", task_ref="ENG-AGENT-02-S9", role="primary-implementation", worker="claude-code", state=TASK_RUNNING, pid=1, worktree=str(repo_ctx.repo_root))
    repo_ctx.state.upsert_task(task)
    with pytest.raises(CommandError, match="active writer"):
        apply_command(repo_ctx, "worktree_adopt", path=str(repo_ctx.repo_root))


def test_pull_and_push_preview_never_mutate_and_dirty_pull_is_blocked(repo_ctx: CommandContext):
    (repo_ctx.repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
    result = apply_command(repo_ctx, "git_operation", action="pull", path=str(repo_ctx.repo_root))
    assert result.ok is False
    assert result.data["state"] == "BLOCKED"
    assert "dirty" in result.message
    assert all("--force" not in op["message"] for op in repo_ctx.state.list_operations())


def test_prepare_merge_requires_gate_review_docs_and_clean_feature_branch(repo_ctx: CommandContext):
    git(repo_ctx.repo_root, "switch", "-c", "eng/test")
    refresh_worktree_statuses(repo_ctx)
    result = apply_command(repo_ctx, "git_operation", action="prepare_merge", path=str(repo_ctx.repo_root))
    assert result.ok is False
    assert result.data["state"] == "BLOCKED"
    assert "local gate" in result.message
    merge = apply_command(repo_ctx, "git_operation", action="merge", path=str(repo_ctx.repo_root), confirm=True, prepare_id="missing")
    assert merge.ok is False
    assert "missing or stale" in merge.message


def test_terminal_history_redaction_and_bounded_storage(repo_ctx: CommandContext):
    assert sanitize_terminal_command("echo hello") == "echo hello"
    assert sanitize_terminal_command("export API_KEY=super-secret") == "[sensitive command hidden]"
    assert sanitize_terminal_command("curl -H 'Authorization: Bearer abcdefghijklmnop'") == "[sensitive command hidden]"
    repo_ctx.state.record_terminal_command(actor="local", session_id="pty-1", cwd=str(repo_ctx.repo_root), branch="main", command="echo hello")
    assert repo_ctx.state.list_terminal_commands(limit=20)[0]["command"] == "echo hello"
    apply_command(repo_ctx, "terminal_history_clear")
    assert repo_ctx.state.list_terminal_commands() == []


def test_roadmap_percentage_only_has_deterministic_denominator(tmp_path: Path):
    ledger = tmp_path / "V1_CHECKLIST.md"
    ledger.write_text("| ID | Status | Notes |\n|---|---|---|\n| V1-01 | complete | x |\n| V1-02 | pending | y |\n", encoding="utf-8")
    rows = derived_roadmap(tmp_path, [{"Program / feature family": "Core production app", "Product version": "V1"}])
    core = rows[0]
    assert core["percentage"] == 50.0
    assert core["percentage_label"] == "50% DERIVED"
    voice = next(row for row in rows if row["program"] == "Voice AI Director")
    assert voice["percentage"] is None
    assert "not computable" in voice["percentage_label"]


def test_app_manager_refuses_to_stop_unowned_process(repo_ctx: CommandContext):
    manager = AppLifecycleManager(repo_ctx)
    with pytest.raises(OperationError, match="no Control-Center-managed"):
        manager.action("stop", "local")


def test_state_migrates_managed_worktree_and_operation_tables(tmp_path: Path):
    state = State(tmp_path / "state.db")
    columns = {row[1] for row in state._conn.execute("PRAGMA table_info(worktrees)").fetchall()}
    assert {"managed", "discovered_at"} <= columns
    assert state._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()
    assert state._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='terminal_commands'").fetchone()


def test_cleanup_preview_and_apply_only_remove_clean_merged_managed_worktree(repo_ctx: CommandContext, tmp_path: Path):
    child = tmp_path / "octages-finished"
    git(repo_ctx.repo_root, "worktree", "add", "-b", "eng/finished", str(child), "main")
    refresh_worktree_statuses(repo_ctx)
    apply_command(repo_ctx, "worktree_adopt", path=str(child))
    preview = cleanup_worktrees(repo_ctx)
    assert [row["path"] for row in preview["eligible"]] == [str(child.resolve())]
    assert any(row["path"] == str(repo_ctx.repo_root.resolve()) for row in preview["protected"])
    assert child.exists()
    applied = cleanup_worktrees(repo_ctx, confirm=True)
    assert applied["removed"] == [str(child.resolve())]
    assert not child.exists()
    assert str(repo_ctx.repo_root.resolve()) in git(repo_ctx.repo_root, "worktree", "list", "--porcelain")


def test_dirty_and_unmanaged_worktrees_are_protected(repo_ctx: CommandContext, tmp_path: Path):
    dirty = tmp_path / "octages-dirty"
    manual = tmp_path / "octages-manual"
    git(repo_ctx.repo_root, "worktree", "add", "-b", "eng/dirty", str(dirty), "main")
    git(repo_ctx.repo_root, "worktree", "add", "-b", "eng/manual", str(manual), "main")
    refresh_worktree_statuses(repo_ctx)
    apply_command(repo_ctx, "worktree_adopt", path=str(dirty))
    (dirty / "local.txt").write_text("protect me\n", encoding="utf-8")
    rows = refresh_worktree_statuses(repo_ctx)
    by_path = {row["path"]: row for row in rows}
    assert by_path[str(dirty.resolve())]["classification"] == "FINISHED_DIRTY"
    assert by_path[str(dirty.resolve())]["cleanup_eligible"] is False
    assert by_path[str(manual.resolve())]["classification"] == "UNRELATED_MANUAL"
    assert by_path[str(manual.resolve())]["cleanup_eligible"] is False


def test_clean_idle_main_projects_idle_without_fabricating_gate(repo_ctx: CommandContext, tmp_path: Path):
    client = TestClient(create_app(repo_ctx, roadmap_path=tmp_path / "missing-roadmap.md"))
    idle = client.get("/api/local-gate").json()
    assert idle["status"] == "IDLE"
    assert idle["ready"] is False
    assert idle["reason"] == "no active candidate"
    repo_ctx.state.upsert_task(Task(id="queued", task_ref="ENG-117", role="primary-implementation", worker="claude-code", state="QUEUED", worktree=str(repo_ctx.repo_root)))
    candidate = client.get("/api/local-gate").json()
    assert candidate["status"] != "IDLE"
