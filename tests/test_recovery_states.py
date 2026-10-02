"""ENG-PC-07 typed restart/orphan recovery and ownership proof."""

from __future__ import annotations

import os

from scripts.agents.control_plane import recovery
from scripts.agents.control_plane.models import (
    KIND_WRITE,
    RUNBOOK_ACCEPTANCE_PENDING,
    RUNBOOK_RUNNING,
    RUNBOOK_WAITING_PROVIDER,
    TASK_FAILED,
    TASK_OWNER_ACTION_REQUIRED,
    TASK_QUEUED,
    TASK_RECOVERABLE_ORPHAN,
    TASK_RUNNING,
    TASK_WAITING_PROVIDER,
    Runbook,
    Task,
)
from scripts.agents.control_plane.state import State


def _task(**changes: object) -> Task:
    values: dict[str, object] = {
        "id": "task-1",
        "task_ref": "ENG-PC-07",
        "role": "primary-implementation",
        "worker": "codex-build",
        "kind": KIND_WRITE,
        "state": TASK_RUNNING,
        "pid": 4321,
    }
    values.update(changes)
    return Task(**values)  # type: ignore[arg-type]


def _runbook(evidence: dict[str, object], *, stage: str = "test") -> Runbook:
    return Runbook(
        id="run-1",
        name="recovery",
        preset="feature",
        objective="recover",
        source_ref="ENG-PC-07",
        branch="eng/recovery",
        worktree="/tmp/not-observed-by-pure-tests",
        parent_worker="codex-build",
        max_duration_minutes=30,
        status=RUNBOOK_ACCEPTANCE_PENDING,
        acceptance_stage=stage,
        acceptance_evidence=evidence,
    )


def test_daemon_crash_requeues_only_a_proven_dead_worker(monkeypatch):
    state = State(":memory:")
    state.upsert_task(_task())
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: False)

    assert recovery.reconcile_tasks(state) == {"reclaimed": 1, "left_running": 0}
    recovered = state.get_task("task-1")
    assert recovered is not None and recovered.state == TASK_QUEUED
    assert recovered.ownership_evidence_class == recovery.EVIDENCE_PID_ABSENT
    assert recovered.stale_recovered is True
    assert [event.event_type for event in state.list_run_events(run_id="task-1")] == [
        "recovery.transition",
        "recovery.transition",
    ]


def test_dashboard_crash_does_not_reclaim_a_genuine_live_owner(monkeypatch):
    state = State(":memory:")
    state.upsert_task(_task(pid=os.getpid(), pid_create_time=100.0))
    monkeypatch.setattr(recovery, "process_create_time", lambda _pid: 100.0)

    recovery.reconcile_tasks(state)
    recovery.reconcile_tasks(state)

    task = state.get_task("task-1")
    assert task is not None and task.state == TASK_RUNNING and task.pid == os.getpid()
    assert task.ownership_evidence_class == recovery.EVIDENCE_PID_CREATE_TIME


def test_worker_orphan_and_reboot_are_restart_safe(monkeypatch):
    state = State(":memory:")
    state.upsert_task(_task())
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: False)

    recovery.reconcile_tasks(state)
    # A second boot observes QUEUED, not RUNNING, and cannot enqueue a duplicate.
    assert recovery.reconcile_tasks(state) == {"reclaimed": 0, "left_running": 0}
    assert state.get_task("task-1").state == TASK_QUEUED  # type: ignore[union-attr]


def test_provider_outage_becomes_waiting_provider_not_terminal():
    state = State(":memory:")
    runbook = _runbook({}, stage="PENDING")
    runbook.status = RUNBOOK_RUNNING
    runbook.task_id = "task-1"
    state.upsert_runbook(runbook)
    state.upsert_task(
        _task(
            state=TASK_FAILED,
            pid=None,
            failure_category="PROVIDER_OUTAGE",
            runbook_id=runbook.id,
        )
    )

    recovery.reconcile_tasks(state)

    assert state.get_task("task-1").state == TASK_WAITING_PROVIDER  # type: ignore[union-attr]
    assert state.get_runbook("run-1").status == RUNBOOK_WAITING_PROVIDER  # type: ignore[union-attr]


def test_stale_pid_reuse_is_proven_by_create_time_after_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(recovery, "process_create_time", lambda _pid: 200.0)
    lock = tmp_path / ".write-lock"
    lock.write_text("codex-build pid=4321 at=100", encoding="utf-8")

    proof = recovery.classify_process_owner(4321, recorded_at=100.0)
    holder, stale = recovery._stale_write_lock_holder(lock)

    assert proof.verdict == recovery.OWNERSHIP_RECLAIMABLE
    assert proof.evidence_class == recovery.EVIDENCE_PID_LOCK_TIME
    assert proof.observed_create_time == 200.0
    assert "created after" in proof.reason
    assert holder == "codex-build pid=4321 at=100" and stale is True


def test_process_that_genuinely_owns_work_is_not_reclaimed(monkeypatch):
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(recovery, "process_create_time", lambda _pid: 100.0)

    proof = recovery.classify_process_owner(4321, recorded_create_time=100.0, recorded_at=200.0)

    assert proof.verdict == recovery.OWNERSHIP_LIVE
    assert proof.evidence_class == recovery.EVIDENCE_PID_CREATE_TIME


def test_unprovable_ownership_is_ambiguous_and_reclaims_nothing(monkeypatch):
    state = State(":memory:")
    state.upsert_task(_task(pid_create_time=None))
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(recovery, "process_create_time", lambda _pid: None)

    assert recovery.reconcile_tasks(state) == {"reclaimed": 0, "left_running": 0}
    task = state.get_task("task-1")
    assert task is not None and task.state == TASK_RECOVERABLE_ORPHAN
    assert task.pid == 4321
    assert task.ownership_evidence_class == recovery.EVIDENCE_PID_ONLY


def test_valid_acceptance_evidence_resumes_minimum_remaining_stage(monkeypatch):
    state = State(":memory:")
    runbook = _runbook(
        {
            "implementation": {"status": "PASS"},
            "review": {"status": "PASS", "tree_sha": "tree-a"},
            "test": {"status": "PASS", "tree_sha": "tree-a"},
            "checkpoint": {"status": "PASS"},
        }
    )
    original_evidence = dict(runbook.acceptance_evidence)
    state.upsert_runbook(runbook)
    monkeypatch.setattr(recovery, "_candidate_tree_sha", lambda _worktree: "tree-a")

    assert recovery.minimum_remaining_acceptance_stage(runbook, tree_sha="tree-a") == "pr_readiness"
    assert recovery.reconcile_acceptance_recovery(state) == 1
    recovered = state.get_runbook("run-1")
    assert recovered is not None and recovered.acceptance_stage == "pr_readiness"
    assert recovered.acceptance_evidence == original_evidence


def test_tree_change_invalidates_exact_tree_gate_and_review_evidence(monkeypatch):
    state = State(":memory:")
    runbook = _runbook(
        {
            "implementation": {"status": "PASS"},
            "review": {"status": "PASS", "tree_sha": "old-tree"},
            "test": {"status": "PASS", "tree_sha": "old-tree"},
        }
    )
    state.upsert_runbook(runbook)
    monkeypatch.setattr(recovery, "_candidate_tree_sha", lambda _worktree: "new-tree")

    assert recovery.minimum_remaining_acceptance_stage(runbook, tree_sha="new-tree") == "review"
    assert recovery.reconcile_acceptance_recovery(state) == 1
    recovered = state.get_runbook("run-1")
    assert recovered is not None and recovered.acceptance_stage == "review"


def test_bounded_ambiguous_attempts_end_in_owner_action_required(monkeypatch):
    state = State(":memory:")
    state.upsert_task(_task(recovery_max_attempts=2))
    monkeypatch.setattr(recovery, "pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(recovery, "process_create_time", lambda _pid: None)

    recovery.reconcile_tasks(state)
    recovery.reconcile_tasks(state)

    task = state.get_task("task-1")
    assert task is not None and task.state == TASK_OWNER_ACTION_REQUIRED
    assert task.recovery_attempts == 2
    assert task.pid == 4321
