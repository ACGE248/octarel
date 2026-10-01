"""ENG-PC-01 (issue #29): durable, CAS'd execution ownership for a worktree.

Covers the acceptance criteria named in the issue directly: concurrent
acquisition, separate processes, crash/restart, stale PID reuse, lease
generation, worktree conflict, and no double writer. Real subprocesses/threads
are used wherever the invariant is about actual concurrency, since a mocked
lock proves nothing about a race -- the same standard
``tests/test_advancement_lease.py`` already holds itself to.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
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
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    # Mirrors ``Supervisor.launch_task``'s real sequence: ``attach_pid`` must
    # succeed (clearing ``spawn_pending``) before a dead recorded pid can ever be
    # taken as proof of no live worker -- see the spawn-pending-ambiguity tests
    # below for the case where it never does.
    lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation, pid=os.getpid())
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    # No sleeping/timeout anywhere in this test: staleness is proven, not waited out.
    grant2 = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert grant2.task_id == "t2"
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.recovery_reason and "no longer alive" in row.recovery_reason


def test_spawn_pending_is_set_on_acquire_and_cleared_by_attach_pid(disk_state, tmp_path):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    assert disk_state.get_execution_lease(str(tmp_path)).spawn_pending is True
    lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation, pid=4242)
    assert disk_state.get_execution_lease(str(tmp_path)).spawn_pending is False


def test_a_dead_owner_pid_with_spawn_still_pending_is_never_auto_reclaimed(disk_state, tmp_path, monkeypatch):
    """The narrow defect-2 shape, reproduced without a real process: ``attach_pid``
    never ran, so the recorded (dead) pid is the launching supervisor's, not the
    worker's -- staleness is unprovable and must never be inferred anyway.
    """

    lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)

    with pytest.raises(lease.ExecutionLeaseConflict) as excinfo:
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert "spawn in flight" in excinfo.value.reason

    summary = lease.reconcile_stale_leases(disk_state)
    assert summary == {"reclaimed": 0, "left_held": 1}
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"


def test_a_reused_supervisor_pid_with_spawn_still_pending_stays_ambiguous(disk_state, tmp_path, monkeypatch):
    """Blocker regression (independent review, exact tree 5d3264e8): the reclaim
    ordering previously contradicted its own docstring -- a create-time mismatch on
    the recorded pid returned ``reclaimable`` *before* ``spawn_pending`` was ever
    consulted. While that flag is set the recorded pid is the launching
    supervisor's, never the worker's, so a reused supervisor pid proves only that
    the supervisor is gone -- never that no worker is running.

    Deliberately never calls ``update_execution_lease_pid`` (which would clear
    ``spawn_pending``): the existing reuse test did exactly that, which is why it
    could not see this bug. Instead the create-time recorded at real ``acquire``
    time is pinned via ``_process_create_time`` itself so the flag stays set.
    """

    monkeypatch.setattr(lease, "_process_create_time", lambda pid: 123.0)
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.spawn_pending is True
    assert row.owner_pid_create_time == 123.0

    # The recorded pid is "alive" (reused by an unrelated process) with a
    # mismatched create-time -- proof the *supervisor* is gone, nothing more.
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(lease, "_process_create_time", lambda pid: 999.0)

    with pytest.raises(lease.ExecutionLeaseConflict) as excinfo:
        lease.acquire(disk_state, worktree=str(tmp_path), task_id="t2", worker="w")
    assert "spawn in flight" in excinfo.value.reason

    summary = lease.reconcile_stale_leases(disk_state)
    assert summary == {"reclaimed": 0, "left_held": 1}
    row = disk_state.get_execution_lease(str(tmp_path))
    assert row.status == "ACQUIRED"
    assert row.generation == grant.generation


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


# Grok Build review (issue #29 follow-up), defect 2: a real session-leader child
# (``start_new_session=True``, exactly mirroring ``Supervisor._spawn``) that
# outlives the "supervisor" process which spawned it -- because that process exits
# (simulating a crash) *before* ever calling ``attach_pid``. Real subprocesses and a
# real signal throughout: the old, narrower coverage
# (``test_lease_is_reclaimable_once_the_owning_process_is_killed`` above) only ever
# killed a holder that had already called ``attach_pid``, which is exactly why it
# could not have caught this.
_ACQUIRE_SPAWN_AND_DIE_BEFORE_ATTACH = """
    import subprocess, sys
    from scripts.agents.control_plane import execution_lease as lease
    from scripts.agents.control_plane.state import State
    state = State(sys.argv[1])
    lease.acquire(state, worktree=sys.argv[2], task_id="doomed-supervisor-task", worker="w")
    # Mirrors Supervisor._spawn exactly: start_new_session=True so this child
    # outlives its parent -- the property that makes a supervisor death here
    # ambiguous rather than provable. stdio is detached (not inherited from this
    # 'supervisor') so the test's subprocess.run() sees real EOF/exit the instant
    # this process exits, instead of hanging on the still-open pipe the long-lived
    # grandchild would otherwise keep alive.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"READY {child.pid}", flush=True)
    # Deliberately exits without ever calling attach_pid -- simulates the
    # supervisor dying in the window between Popen() returning and the real
    # worker pid being durably recorded.
"""


def _spawn_then_die_before_attach(db_path: Path, worktree: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_ACQUIRE_SPAWN_AND_DIE_BEFORE_ATTACH), str(db_path), str(worktree)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True, check=True, timeout=30,
    )
    line = proc.stdout.strip().splitlines()[-1]
    assert line.startswith("READY "), proc.stdout
    return int(line.split()[1])


def test_supervisor_death_before_attach_pid_never_frees_the_worktree_while_the_child_runs(disk_state, tmp_path):
    child_pid = None
    try:
        child_pid = _spawn_then_die_before_attach(disk_state.db_path, tmp_path)
        assert lease.pid_is_alive(child_pid), "the spawned child must actually be running"

        row = disk_state.get_execution_lease(str(tmp_path))
        assert row.status == "ACQUIRED"
        assert row.spawn_pending is True
        assert not lease.pid_is_alive(row.owner_pid), "the recorded 'supervisor' pid has genuinely exited"

        # The periodic sweep must refuse to free this lease: the dead recorded pid
        # is the supervisor's, not the still-running child's.
        summary = lease.reconcile_stale_leases(disk_state)
        assert summary == {"reclaimed": 0, "left_held": 1}
        assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"

        # A fresh acquirer racing in must also be refused, not just the sweep.
        with pytest.raises(lease.ExecutionLeaseConflict):
            lease.acquire(disk_state, worktree=str(tmp_path), task_id="second-writer", worker="w")

        # The child is still the only process that was ever running in this
        # worktree at any point in the test -- no second writer was ever admitted.
        assert lease.pid_is_alive(child_pid)
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)


# ------------------------------------------------------ resolving an ambiguous lease


def test_resolve_ambiguous_lease_refuses_a_live_lease(disk_state, tmp_path):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation, pid=os.getpid())
    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation)
    assert result.resolved is False
    assert "not ambiguous" in result.reason
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"


def test_resolve_ambiguous_lease_refuses_a_reclaimable_lease(disk_state, tmp_path, monkeypatch):
    """A plain reclaimable lease (``spawn_pending`` already clear) has its own
    correct path (``acquire``/``reconcile_stale_leases``) -- this resolution must
    not act on it either, so there is exactly one way to free each kind of lease.
    """

    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    lease.attach_pid(disk_state, worktree=str(tmp_path), generation=grant.generation, pid=os.getpid())
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation)
    assert result.resolved is False
    assert "not ambiguous" in result.reason
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"


def test_resolve_ambiguous_lease_refuses_a_mismatched_generation(disk_state, tmp_path, monkeypatch):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation + 1)
    assert result.resolved is False
    assert "nothing to resolve" in result.reason


def test_resolve_ambiguous_lease_frees_when_no_write_lock_file_exists(disk_state, tmp_path, monkeypatch):
    """The common case: the dead supervisor's spawn never reached, or never will
    reach, ``runner.write_lock`` -- there is no lock file at all, which is the
    clearest OS-checked evidence available that nothing is running.
    """

    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    row = disk_state.get_execution_lease(str(tmp_path))
    classification, _ = lease._classify_owner(row)
    assert classification == lease._OWNER_AMBIGUOUS

    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation)
    assert result.resolved is True
    assert "no .write-lock file" in result.reason
    assert "cannot rule out a worker still starting up" in result.reason
    assert disk_state.get_execution_lease(str(tmp_path)).status == "RELEASED"


def test_resolve_ambiguous_lease_frees_when_the_write_lock_holder_is_also_dead(disk_state, tmp_path, monkeypatch):
    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    lock_dir = tmp_path / ".agent-output"
    lock_dir.mkdir(parents=True)
    dead_pid = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid.wait(timeout=10)
    (lock_dir / ".write-lock").write_text(f"claude-code pid={dead_pid.pid} at=0", encoding="utf-8")

    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation)
    assert result.resolved is True
    assert "is dead" in result.reason
    assert disk_state.get_execution_lease(str(tmp_path)).status == "RELEASED"


def test_resolve_ambiguous_lease_refuses_when_a_live_write_lock_holder_exists(disk_state, tmp_path, monkeypatch):
    """The one case this must never free: an ``.write-lock`` file with a genuinely
    live pid is real OS-enforced evidence a worker exists, even though the
    execution-lease row alone was ambiguous.
    """

    grant = lease.acquire(disk_state, worktree=str(tmp_path), task_id="t1", worker="w")
    monkeypatch.setattr(lease, "pid_is_alive", lambda pid: False)
    lock_dir = tmp_path / ".agent-output"
    lock_dir.mkdir(parents=True)
    (lock_dir / ".write-lock").write_text(f"claude-code pid={os.getpid()} at=0", encoding="utf-8")

    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=grant.generation)
    assert result.resolved is False
    assert "live write-lock holder" in result.reason
    assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"


# Real-process coverage, mirroring ``_ACQUIRE_SPAWN_AND_DIE_BEFORE_ATTACH`` above but
# with the session-leader child also taking the real ``.write-lock`` (exactly what
# ``runner.write_lock`` does once a worker actually starts running) before its
# "supervisor" dies without ever calling ``attach_pid``. This is the one test that
# can actually distinguish a correct implementation from one that merely trusts an
# operator: it proves ``resolve_ambiguous_lease`` cannot be made to free the
# worktree while that child is genuinely still alive and holding the lock.
_ACQUIRE_SPAWN_HOLDING_WRITE_LOCK_AND_DIE_BEFORE_ATTACH = """
    import subprocess, sys, time
    from pathlib import Path
    from scripts.agents.control_plane import execution_lease as lease
    from scripts.agents.control_plane.state import State
    state = State(sys.argv[1])
    worktree = sys.argv[2]
    lease.acquire(state, worktree=worktree, task_id="doomed-supervisor-task", worker="w")
    lock_dir = Path(worktree) / ".agent-output"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".write-lock"
    child_script = (
        "import os, sys, time; "
        "open(sys.argv[1], 'w').write('claude-code pid={} at=0'.format(os.getpid())); "
        "time.sleep(600)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(lock_path)], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for the child to actually write its own lock before this 'supervisor'
    # exits, so the outer test never races the child's own write.
    deadline = time.monotonic() + 10
    while not lock_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    print(f"READY {child.pid}", flush=True)
"""


def _spawn_then_die_before_attach_holding_write_lock(db_path: Path, worktree: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_ACQUIRE_SPAWN_HOLDING_WRITE_LOCK_AND_DIE_BEFORE_ATTACH),
         str(db_path), str(worktree)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True, check=True, timeout=30,
    )
    line = proc.stdout.strip().splitlines()[-1]
    assert line.startswith("READY "), proc.stdout
    return int(line.split()[1])


def test_resolve_ambiguous_lease_never_frees_the_worktree_while_a_live_child_holds_the_write_lock(disk_state, tmp_path):
    child_pid = None
    try:
        child_pid = _spawn_then_die_before_attach_holding_write_lock(disk_state.db_path, tmp_path)
        assert lease.pid_is_alive(child_pid), "the spawned child must actually be running"

        row = disk_state.get_execution_lease(str(tmp_path))
        assert row.status == "ACQUIRED" and row.spawn_pending is True
        assert not lease.pid_is_alive(row.owner_pid), "the recorded 'supervisor' pid has genuinely exited"

        result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=row.generation)
        assert result.resolved is False
        assert "live write-lock holder" in result.reason
        assert disk_state.get_execution_lease(str(tmp_path)).status == "ACQUIRED"

        # Not resolved by this call and never stealable through the ordinary path either.
        with pytest.raises(lease.ExecutionLeaseConflict):
            lease.acquire(disk_state, worktree=str(tmp_path), task_id="second-writer", worker="w")
        assert lease.pid_is_alive(child_pid)
    finally:
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)


def test_resolve_ambiguous_lease_frees_once_that_same_child_is_actually_dead(disk_state, tmp_path):
    child_pid = _spawn_then_die_before_attach_holding_write_lock(disk_state.db_path, tmp_path)
    os.kill(child_pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while lease.pid_is_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not lease.pid_is_alive(child_pid), "the child must actually be dead before this proves anything"

    row = disk_state.get_execution_lease(str(tmp_path))
    result = lease.resolve_ambiguous_lease(disk_state, worktree=str(tmp_path), generation=row.generation)
    assert result.resolved is True
    assert disk_state.get_execution_lease(str(tmp_path)).status == "RELEASED"


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
