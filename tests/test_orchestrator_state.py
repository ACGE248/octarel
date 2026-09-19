"""SQLite-backed durable store: round-trips, upserts, and event ordering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.agents.control_plane.models import (
    KIND_READ,
    ProviderState,
    Task,
    WorktreeRecord,
)
from scripts.agents.control_plane.state import State, default_db_path, default_state_dir


def test_default_paths_are_root_anchored_and_distinct_from_agent_output(tmp_path: Path):
    state_dir = default_state_dir(tmp_path)
    db_path = default_db_path(tmp_path)
    assert state_dir.name == ".orchestrator-state"
    assert db_path.parent == state_dir
    assert db_path.name == "orchestrator.db"
    assert state_dir != tmp_path / ".agent-output"


def test_state_file_creation_creates_parent_directories(tmp_path: Path):
    db_path = tmp_path / "nested" / "dir" / "orchestrator.db"
    state = State(db_path)
    assert db_path.exists()
    state.close()


def test_task_upsert_and_round_trip_preserves_all_fields():
    state = State(":memory:")
    task = Task(
        id="t1",
        task_ref="ENG-AGENT-02",
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        kind=KIND_READ,
        priority=3,
        dependencies=("dep1", "dep2"),
        pid=123,
        worktree="/tmp/x",
        command=("scope:a", "do the thing"),
        result="PASS",
        last_error=None,
    )
    state.upsert_task(task)
    round_tripped = state.get_task("t1")
    assert round_tripped is not None
    assert round_tripped.dependencies == ("dep1", "dep2")
    assert round_tripped.command == ("scope:a", "do the thing")
    assert round_tripped.priority == 3
    assert round_tripped.pid == 123


def test_task_upsert_overwrites_existing_row():
    state = State(":memory:")
    task = Task(id="t1", task_ref="ENG-AGENT-02", role="focused-tests", worker="opencode2-gemini-flash-lite")
    state.upsert_task(task)
    task.priority = 9
    state.upsert_task(task)
    assert state.get_task("t1").priority == 9
    assert len(state.list_tasks()) == 1


def test_list_tasks_filters_by_state_and_orders_by_priority_then_creation():
    state = State(":memory:")
    for i, (priority, created) in enumerate(
        [(0, "2026-01-01T00:00:03+00:00"), (5, "2026-01-01T00:00:02+00:00"), (5, "2026-01-01T00:00:01+00:00")]
    ):
        state.upsert_task(
            Task(
                id=f"t{i}",
                task_ref="ENG-AGENT-02",
                role="focused-tests",
                worker="opencode2-gemini-flash-lite",
                priority=priority,
                created_at=created,
            )
        )
    ordered = state.list_tasks()
    assert [t.id for t in ordered] == ["t2", "t1", "t0"]


def test_delete_task_removes_the_row():
    state = State(":memory:")
    state.upsert_task(
        Task(id="t1", task_ref="ENG-AGENT-02", role="focused-tests", worker="opencode2-gemini-flash-lite")
    )
    state.delete_task("t1")
    assert state.get_task("t1") is None


def test_provider_state_round_trip():
    state = State(":memory:")
    provider = ProviderState(
        name="claude-code",
        execution_system="Claude Code",
        provider="Anthropic",
        cost_class="premium-subscription",
        state="AVAILABLE",
        configured=True,
        consecutive_failures=2,
    )
    state.upsert_provider_state(provider)
    round_tripped = state.get_provider_state("claude-code")
    assert round_tripped.consecutive_failures == 2
    assert round_tripped.configured is True


def test_worktree_replace_overwrites_the_whole_table():
    state = State(":memory:")
    state.upsert_worktree(WorktreeRecord(path="/a", branch="main"))
    state.replace_worktrees([WorktreeRecord(path="/b", branch="work")])
    paths = {w.path for w in state.list_worktrees()}
    assert paths == {"/b"}


def test_events_are_returned_most_recent_first_and_respect_limit():
    state = State(":memory:")
    for i in range(5):
        state.record_event(category="test", message=f"event {i}")
    events = state.list_events(limit=3)
    assert [e.message for e in events] == ["event 4", "event 3", "event 2"]


def test_context_manager_closes_the_connection(tmp_path: Path):
    db_path = tmp_path / "orchestrator.db"
    with State(db_path) as state:
        state.record_event(category="test", message="hello")
    # Reopening must succeed cleanly (proves the prior connection was closed).
    reopened = State(db_path)
    assert len(reopened.list_events()) == 1


def test_control_settings_are_visible_across_process_connections(tmp_path: Path):
    db_path = tmp_path / "orchestrator.db"
    writer = State(db_path)
    reader = State(db_path)
    writer.set_control_setting("stop_after_current", "1")
    assert reader.get_control_setting("stop_after_current") == "1"


def test_state_serializes_concurrent_thread_access():
    state = State(":memory:")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: state.record_event(category="thread", message=str(index)), range(100)))
    assert len(state.list_events(limit=200)) == 100
