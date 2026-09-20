"""Startup reconciliation: real git worktrees, real PIDs, real write locks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.agents.control_plane.models import (
    KIND_WRITE,
    TASK_QUEUED,
    TASK_RUNNING,
    Task,
)
from scripts.agents.control_plane.recovery import (
    _stale_write_lock_holder,
    discover_git_worktrees,
    pid_is_alive,
    reconcile_tasks,
    reconcile_worktree_locks,
    run_recovery,
)
from scripts.agents.control_plane.state import State


def _dead_pid() -> int:
    """A PID guaranteed to no longer be alive: spawn, then wait for exit."""

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def test_pid_is_alive_true_for_self_false_for_none_and_dead():
    import os

    assert pid_is_alive(os.getpid()) is True
    assert pid_is_alive(None) is False
    assert pid_is_alive(_dead_pid()) is False


def test_reconcile_tasks_reclaims_dead_pid_and_leaves_live_pid_running():
    import os

    state = State(":memory:")
    live = Task(
        id="live",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        kind=KIND_WRITE,
        state=TASK_RUNNING,
        pid=os.getpid(),
    )
    dead = Task(
        id="dead",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        kind=KIND_WRITE,
        state=TASK_RUNNING,
        pid=_dead_pid(),
    )
    state.upsert_task(live)
    state.upsert_task(dead)

    summary = reconcile_tasks(state)

    assert summary == {"reclaimed": 1, "left_running": 1}
    assert state.get_task("live").state == TASK_RUNNING
    reclaimed_task = state.get_task("dead")
    assert reclaimed_task.state == TASK_QUEUED
    assert reclaimed_task.pid is None
    assert reclaimed_task.stale_recovered is True
    assert "reclaimed" in (reclaimed_task.last_error or "")


def test_reconcile_never_duplicates_a_task_with_a_live_pid():
    """A restart must never re-launch a task whose recorded PID is still alive."""

    import os

    state = State(":memory:")
    task = Task(
        id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        kind=KIND_WRITE,
        state=TASK_RUNNING,
        pid=os.getpid(),
    )
    state.upsert_task(task)
    reconcile_tasks(state)
    assert state.get_task("t1").state == TASK_RUNNING
    assert state.get_task("t1").pid == os.getpid()


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def test_discover_git_worktrees_parses_porcelain_output(tmp_path):
    repo = _git_repo(tmp_path)
    worktrees = discover_git_worktrees(repo)
    assert len(worktrees) == 1
    assert worktrees[0].path == str(repo.resolve()) or Path(worktrees[0].path) == repo
    assert worktrees[0].branch and "work" in worktrees[0].branch


def test_discover_git_worktrees_returns_empty_on_non_git_directory(tmp_path):
    assert discover_git_worktrees(tmp_path) == []


def test_stale_write_lock_holder_detects_dead_pid_as_stale(tmp_path):
    import os

    lock_path = tmp_path / ".write-lock"
    lock_path.write_text(f"worker pid={_dead_pid()} at=0", encoding="utf-8")
    holder, stale = _stale_write_lock_holder(lock_path)
    assert holder is not None
    assert stale is True

    lock_path.write_text(f"worker pid={os.getpid()} at=0", encoding="utf-8")
    holder, stale = _stale_write_lock_holder(lock_path)
    assert stale is False

    missing_path = tmp_path / "no-such-lock"
    assert _stale_write_lock_holder(missing_path) == (None, False)


def test_reconcile_worktree_locks_marks_live_lock_and_clears_stale_one(tmp_path):
    from scripts.agents.control_plane.models import WorktreeRecord

    repo = _git_repo(tmp_path)
    (repo / ".agent-output").mkdir()
    import os

    (repo / ".agent-output" / ".write-lock").write_text(f"worker pid={os.getpid()} at=0", encoding="utf-8")
    state = State(":memory:")
    annotated = reconcile_worktree_locks(state, [WorktreeRecord(path=str(repo), branch="work")])
    assert len(annotated) == 1
    assert annotated[0].locked is True
    assert annotated[0].lock_holder is not None
    persisted = state.list_worktrees()
    assert len(persisted) == 1
    assert persisted[0].locked is True


def test_reconcile_releases_only_proven_stale_managed_lock(tmp_path):
    from scripts.agents.control_plane.models import WorktreeRecord

    repo = _git_repo(tmp_path)
    lock_dir = repo / ".agent-output"
    lock_dir.mkdir()
    lock_path = lock_dir / ".write-lock"
    lock_path.write_text(f"claude-code pid={_dead_pid()} at=1", encoding="utf-8")
    state = State(":memory:")
    state.upsert_worktree(WorktreeRecord(path=str(repo), branch="work", managed=True))

    [record] = reconcile_worktree_locks(state, [WorktreeRecord(path=str(repo), branch="work")])

    assert record.stale_lock is False
    assert record.stale_lock_holder is None
    assert record.locked is False
    assert not lock_path.exists()


def test_run_recovery_end_to_end_records_an_event(tmp_path):
    repo = _git_repo(tmp_path)
    state = State(":memory:")
    result = run_recovery(state, repo)
    assert "worktrees" in result and "tasks" in result
    events = state.list_events()
    assert any(e.category == "recovery" for e in events)
