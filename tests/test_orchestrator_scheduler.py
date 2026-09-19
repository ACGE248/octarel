"""Concurrency caps and dependency ordering (ENG-AGENT-02 Slice 1)."""

from __future__ import annotations

from scripts.agents.control_plane.models import (
    KIND_HEAVY,
    KIND_READ,
    KIND_WRITE,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_READY_BUT_UNMERGED,
    TASK_READY_LOCAL,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Task,
)
from scripts.agents.control_plane.scheduler import ConcurrencyPolicy, Scheduler


def _task(id_, *, kind=KIND_WRITE, state=TASK_PENDING, priority=0, deps=(), created_at="2026-01-01T00:00:00+00:00"):
    return Task(
        id=id_,
        task_ref="ENG-AGENT-02",
        role="secondary-implementation",
        worker="claude-code",
        kind=kind,
        state=state,
        priority=priority,
        dependencies=tuple(deps),
        created_at=created_at,
        updated_at=created_at,
    )


def test_third_write_task_queues_not_runs():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=2))
    tasks = [
        _task("w1", state=TASK_RUNNING),
        _task("w2", state=TASK_RUNNING),
        _task("w3", state=TASK_PENDING, created_at="2026-01-01T00:00:03+00:00"),
    ]
    runnable = scheduler.next_runnable(tasks)
    assert [t.id for t in runnable] == []


def test_a_free_write_slot_is_used_by_the_oldest_pending_task():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=2))
    tasks = [
        _task("w1", state=TASK_RUNNING),
        _task("w2", state=TASK_PENDING, created_at="2026-01-01T00:00:01+00:00"),
        _task("w3", state=TASK_PENDING, created_at="2026-01-01T00:00:02+00:00"),
    ]
    runnable = scheduler.next_runnable(tasks)
    assert [t.id for t in runnable] == ["w2"]


def test_read_and_heavy_slots_are_independent_of_write_slots():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=2, max_extra_read_workers=1, max_heavy_jobs=1))
    tasks = [
        _task("w1", state=TASK_RUNNING, kind=KIND_WRITE),
        _task("w2", state=TASK_RUNNING, kind=KIND_WRITE),
        _task("r1", state=TASK_PENDING, kind=KIND_READ),
        _task("h1", state=TASK_PENDING, kind=KIND_HEAVY),
    ]
    runnable = {t.id for t in scheduler.next_runnable(tasks)}
    assert runnable == {"r1", "h1"}


def test_second_heavy_task_queues_while_first_heavy_runs():
    scheduler = Scheduler(ConcurrencyPolicy(max_heavy_jobs=1))
    tasks = [
        _task("h1", state=TASK_RUNNING, kind=KIND_HEAVY),
        _task("h2", state=TASK_PENDING, kind=KIND_HEAVY),
    ]
    assert scheduler.next_runnable(tasks) == []


def test_dependent_task_never_starts_before_dependency_is_terminal_success():
    scheduler = Scheduler(ConcurrencyPolicy())
    for blocking_state in (TASK_PENDING, TASK_RUNNING, TASK_FAILED, TASK_BLOCKED):
        tasks = [
            _task("dep", state=blocking_state),
            _task("downstream", state=TASK_PENDING, deps=["dep"], created_at="2026-01-01T00:00:01+00:00"),
        ]
        runnable_ids = {t.id for t in scheduler.next_runnable(tasks)}
        assert "downstream" not in runnable_ids, f"downstream ran while dependency was {blocking_state}"


def test_dependency_satisfied_states_unblock_downstream_task():
    scheduler = Scheduler(ConcurrencyPolicy())
    for satisfying_state in (TASK_SUCCEEDED, TASK_READY_LOCAL, TASK_READY_BUT_UNMERGED):
        tasks = [
            _task("dep", state=satisfying_state),
            _task("downstream", state=TASK_PENDING, deps=["dep"], created_at="2026-01-01T00:00:01+00:00"),
        ]
        runnable_ids = {t.id for t in scheduler.next_runnable(tasks)}
        assert "downstream" in runnable_ids, f"downstream blocked despite dependency being {satisfying_state}"


def test_missing_dependency_id_blocks_the_task():
    scheduler = Scheduler(ConcurrencyPolicy())
    tasks = [_task("downstream", state=TASK_PENDING, deps=["nonexistent"])]
    assert scheduler.next_runnable(tasks) == []


def test_blocked_task_is_not_retried_without_operator_action():
    scheduler = Scheduler(ConcurrencyPolicy())
    assert scheduler.next_runnable([_task("blocked", state=TASK_BLOCKED)]) == []


def test_higher_priority_task_runs_before_lower_priority_within_the_same_slot():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=1))
    tasks = [
        _task("low", state=TASK_PENDING, priority=0, created_at="2026-01-01T00:00:01+00:00"),
        _task("high", state=TASK_PENDING, priority=5, created_at="2026-01-01T00:00:02+00:00"),
    ]
    runnable = scheduler.next_runnable(tasks)
    assert [t.id for t in runnable] == ["high"]


def test_slot_usage_reports_used_and_limit_per_kind():
    scheduler = Scheduler(ConcurrencyPolicy(max_write_workers=2, max_extra_read_workers=1, max_heavy_jobs=1))
    tasks = [_task("w1", state=TASK_RUNNING, kind=KIND_WRITE)]
    usage = scheduler.slot_usage(tasks)
    assert usage.write_used == 1 and usage.write_limit == 2
    assert usage.read_used == 0 and usage.read_limit == 1
    assert usage.heavy_used == 0 and usage.heavy_limit == 1
    assert usage.as_dict()["write"] == {"used": 1, "limit": 2}
