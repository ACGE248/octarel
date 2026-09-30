"""ENG-PC-01 (issue #29): durable, CAS'd execution ownership for a worktree.

Covers the acceptance criteria named in the issue directly: concurrent
acquisition, separate processes, crash/restart, stale PID reuse, lease
generation, worktree conflict, and no double writer. Real subprocesses/threads
are used wherever the invariant is about actual concurrency, since a mocked
lock proves nothing about a race -- the same standard
``tests/test_advancement_lease.py`` already holds itself to.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from scripts.agents.control_plane import execution_lease as lease
from scripts.agents.control_plane.state import State

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def disk_state(tmp_path):
    return State(tmp_path / "cp" / "cp.db")


# --------------------------------------------------------------- fresh acquisition


def test_fresh_acquire_starts_at_generation_one(disk_state, tmp_path):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="claude-code")
    assert grant.generation == 1
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.status == "ACQUIRED"
    assert row.task_id == "t1"
    assert row.owner_pid == os.getpid()
    assert row.acquired_at is not None


def test_generation_increments_on_every_successful_acquisition(disk_state, tmp_path):
    first = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    lease.release(disk_state, worktree=str(tmp_path), expected_pid=os.getpid(), reason="done")
    second = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert second.generation == first.generation + 1 == 2


# ----------------------------------------------------------------- worktree conflict


def test_live_owner_refuses_a_second_acquirer_without_blocking(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    with pytest.raises(lease.ExecutionLeaseConflict) as excinfo:
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert "t1" in excinfo.value.reason
    assert excinfo.value.holder is not None and excinfo.value.holder.task_id == "t1"
    # never started a second writer: the row is still owned by the first task
    assert disk_state.get_execution_lease(str(tmp_path)).task_id == "t1"


def test_two_different_tasks_racing_the_same_worktree_never_both_win(disk_state, tmp_path):
    """The 'worktree conflict' acceptance case: distinct task ids, one worktree."""

    lease.acquire(disk_state, worktree=str(tmp_path), task_id="task-a", worker="w")
    with pytest.raises(lease.ExecutionLeaseConflict):
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="task-b", worker="w")


def test_conflict_records_an_informational_reason_never_used_for_cas(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    with pytest.raises(lease.ExecutionLeaseConflict):
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.conflict_reason and "t2" not in row.conflict_reason  # the loser never became truth
    assert row.task_id == "t1"  # the conflict annotation never touched ownership


# ------------------------------------------------------------------- no double writer


def test_releasing_frees_the_worktree_for_a_new_owner(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    assert lease.release(disk_state, worktree=str(tmp_path), expected_pid=os.getpid(), reason="finished") is True
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert grant.task_id == "t2"


def test_release_by_a_non_owner_pid_is_refused(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    assert lease.release(disk_state, worktree=str(tmp_path), expected_pid=999999, reason="not mine") is False
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"


def test_attach_pid_is_guarded_by_generation(disk_state, tmp_path):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    assert lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation, pid=4242) is True
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.owner_pid == 4242
    # a stale (wrong) generation must never be able to overwrite the real owner pid
    assert lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation - 1, pid=1) is False
    assert disk_state.get_execution_lease(str(tmp_path)).owner_pid == 4242


# ---------------------------------------------------------------- crash / restart


def test_dead_owner_pid_is_reclaimable_without_a_timeout(disk_state, tmp_path, monkeypatch):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    # No sleeping/timeout anywhere in this test: staleness is proven, not waited out.
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert grant.task_id == "t2"
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.recovery_reason and "no longer alive" in row.recovery_reason


def test_reconcile_stale_leases_frees_only_provably_dead_owners(disk_state, tmp_path, monkeypatch):
    live_wt, dead_wt = tmp_path / "live", tmp_path / "dead"
    lease.acquire(disk_state, worktree=str(live_wt), task_id="t-live", worker="w")
    lease.acquire(disk_state, worktree=str(dead_wt), task_id="t-dead", worker="w")

    real_alive = lease.pid_is_alive
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: pid != os.getpid() and real_alive(pid))
    # simulate the dead worktree's owner being a different (dead) pid
    disk_state.update_execution_lease_pid(worktree=str(dead_wt), expected_generation=1, pid=999999, pid_create_time=None)
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: pid != 999999)

    summary = lease.reconcile_stale_leases(disk_state)
    assert summary == {"reclaimed": 1, "left_held": 1}
    assert disk_state.get_execution_lease(str(dead_wt)).status == "RELEASED"
    assert disk_state.get_execution_lease(str(live_wt)).status == "ACQUIRED"


def test_reconcile_stale_leases_never_touches_an_already_released_row(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    lease.release(disk_state, worktree=str(tmp_path), expected_pid=os.getpid(), reason="done")
    summary = lease.reconcile_stale_leases(disk_state)
    assert summary == {"reclaimed": 0, "left_held": 0}


# ------------------------------------------------------------------ stale PID reuse


def test_a_live_pid_whose_create_time_no_longer_matches_is_treated_as_reused(disk_state, tmp_path, monkeypatch):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    disk_state.update_execution_lease_pid(
        worktree=str(tmp_path), expected_generation=grant.generation, pid=os.getpid(), pid_create_time=123.0
    )
    # the pid is genuinely alive (it's this test process) but the recorded
    # create-time no longer matches -- proof of PID reuse, not a live original owner
    monkeypatch.setattr(lease, "_process_create_time", lambda pid: 999.0)
    grant2 = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert grant2.task_id == "t2"
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.recovery_reason and "reused" in row.recovery_reason


def test_a_live_pid_with_matching_create_time_is_never_reclaimed(disk_state, tmp_path, monkeypatch):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    disk_state.update_execution_lease_pid(
        worktree=str(tmp_path), expected_generation=grant.generation, pid=os.getpid(), pid_create_time=123.0
    )
    monkeypatch.setattr(lease, "_process_create_time", lambda pid: 123.0)
    with pytest.raises(lease.ExecutionLeaseConflict):
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")


# ----------------------------------------------------------- concurrent acquisition


def test_concurrent_threads_racing_one_worktree_exactly_one_wins(disk_state, tmp_path):
    winners: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(task_id: str) -> None:
        barrier.wait()
        try:
            lease.acquire(disk_state, worktree=str(tmp_path), task_id=task_id, worker="w")
            winners.append(task_id)
        except lease.ExecutionLeaseConflict:
            pass

    threads = [threading.Thread(target=attempt, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert disk_state.get_execution_lease(str(tmp_path)).task_id == winners[0]


# ------------------------------------------------------------------ separate processes


_ACQUIRE_AND_HOLD = """
    import sys, time
    from scripts.agents.control_plane import execution_lease as lease
    from scripts.agents.control_plane.state import State
    state = State(sys.argv[1])
    grant = lease.acquire(state, worktree=sys.argv[2], task_id="other-process-task", worker="w")
    lease.attach_pid(state, worktree=sys.argv[2], generation=grant.generation, pid=__import__("os").getpid())
    print("READY", flush=True)
    time.sleep(600)
"""


def _hold_in_subprocess(db_path: Path, worktree: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_ACQUIRE_AND_HOLD), str(db_path), str(worktree)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"
    return proc


def test_a_second_real_process_cannot_acquire_a_live_lease(disk_state, tmp_path):
    other = _hold_in_subprocess(disk_state.db_path, tmp_path)
    try:
        with pytest.raises(lease.ExecutionLeaseConflict) as excinfo:
            lease.acquire(disk_state, worktree=str(tmp_path), task_id="this-process-task", worker="w")
        assert excinfo.value.holder is not None and excinfo.value.holder.owner_pid == other.pid
    finally:
        other.kill()
        other.wait()


def test_lease_is_reclaimable_once_the_owning_process_is_killed(disk_state, tmp_path):
    other = _hold_in_subprocess(disk_state.db_path, tmp_path)
    other.kill()  # SIGKILL: no release code runs in the owner
    other.wait()

    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="recovering-task", worker="w")
    assert grant.task_id == "recovering-task"


# --------------------------------------------------------------------------- misc


def test_lease_facts_is_none_for_an_unknown_worktree(disk_state, tmp_path):
    assert lease.lease_facts(disk_state, str(tmp_path)) is None
    assert lease.lease_facts(disk_state, None) is None


def test_stale_leases_is_read_only_and_does_not_mutate_the_row(disk_state, tmp_path, monkeypatch):
    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    findings = lease.stale_leases(disk_state)
    assert len(findings) == 1 and findings[0]["task_id"] == "t1"
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"  # unchanged


def test_project_scoping_isolates_lease_listings(disk_state, tmp_path):
    lease.acquire(disk_state, worktree=str(tmp_path / "a"), task_id="a", worker="w", project_id="proj-a")
    lease.acquire(disk_state, worktree=str(tmp_path / "b"), task_id="b", worker="w", project_id="proj-b")
    assert [item.task_id for item in disk_state.list_execution_leases(project_id="proj-a")] == ["a"]
