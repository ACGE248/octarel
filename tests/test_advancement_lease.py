"""ENG-AO-07 (issue #17): single-writer runbook advancement.

Regression coverage for the VE-BRIDGE-01 overnight defect: a daemon and a dashboard both advanced the
same runbook into acceptance and ran the exact-tree gate concurrently in one worktree. Deterministic and
offline: the acceptance pipeline is replaced by a counting fake, and "another process" is a real
subprocess so the OS-level lease (not an in-memory lock) is what is exercised.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import advancement_lease as lease
from scripts.agents.control_plane import dashboard_api, runbooks
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import (
    RUNBOOK_BLOCKED,
    RUNBOOK_OWNER_ACTION_REQUIRED,
    RUNBOOK_PAUSED,
    RUNBOOK_RUNNING,
    TASK_SUCCEEDED,
    Task,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "eng/lease-test", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True)
    return root


def _acceptance_runbook(state: State, worktree: Path, runbook_id: str = "RB-lease"):
    task = Task(
        id=f"{runbook_id}-session", task_ref="V-LEASE", role="primary-implementation", worker="claude-code",
        state=TASK_SUCCEEDED, result="PASS", worktree=str(worktree),
    )
    state.upsert_task(task)
    rb = runbooks.create_runbook(
        state=state, registry=load_registry(), name="lease test", preset="test-fix",
        source_ref="V-LEASE", branch="eng/lease-test", worktree=str(worktree),
    )
    rb.id = runbook_id
    rb.task_id = task.id
    rb.status = RUNBOOK_RUNNING
    task.runbook_id = rb.id
    state.upsert_task(task)
    state.upsert_runbook(rb)
    return rb, task


class GateCounter:
    """Stands in for ``advance_acceptance_pipeline`` (whose Test stage runs the exact-tree gate)."""

    def __init__(self, hold: float = 0.0):
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.hold = hold
        self._guard = threading.Lock()

    def __call__(self, *, state, runbook, **_kwargs):
        with self._guard:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.hold)
        runbook.status = RUNBOOK_BLOCKED  # a terminal outcome the winner persists
        state.upsert_runbook(runbook)
        with self._guard:
            self.active -= 1


def _hold_in_subprocess(script: str, *args: str) -> subprocess.Popen:
    """Run ``script`` in a separate Python process; it prints READY once it holds its lock."""

    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"
    return proc


HOLD_LEASE = """
    import sys, time
    from scripts.agents.control_plane.advancement_lease import advancement_lease
    from scripts.agents.control_plane.state import State
    with advancement_lease(State(sys.argv[1]), sys.argv[2], holder="other-process") as owner:
        assert owner
        print("READY", flush=True)
        time.sleep(600)
"""

HOLD_DAEMON = """
    import sys, time
    from scripts.agents.control_plane.advancement_lease import claim_daemon_authority
    from scripts.agents.control_plane.state import State
    assert claim_daemon_authority(State(sys.argv[1]))
    print("READY", flush=True)
    time.sleep(600)
"""


@pytest.fixture()
def gate(monkeypatch):
    counter = GateCounter()
    monkeypatch.setattr(runbooks, "advance_acceptance_pipeline", counter)
    monkeypatch.setattr(runbooks, "check_runtime_freshness", lambda **_k: None)
    return counter


@pytest.fixture()
def disk_state(tmp_path):
    return State(tmp_path / "cp" / "cp.db")


# 1 + 2 ---------------------------------------------------------------- exactly one gate execution


def test_two_concurrent_reconcilers_run_the_gate_exactly_once(tmp_path, disk_state, gate):
    repo = _git_repo(tmp_path / "wt")
    _acceptance_runbook(disk_state, repo)
    gate.hold = 0.4
    other = State(disk_state.db_path)  # an independent connection, like a second process
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def run(state: State) -> None:
        barrier.wait()
        results.append(runbooks.reconcile_runbooks(state=state, repo_root=repo))

    threads = [threading.Thread(target=run, args=(s,)) for s in (disk_state, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert gate.calls == 1
    assert gate.max_active == 1
    assert sorted(r["skipped_not_owner"] for r in results) == [0, 1]


def test_loser_that_arrives_after_the_winner_finished_rereads_and_does_not_rerun(tmp_path, disk_state, gate):
    repo = _git_repo(tmp_path / "wt")
    _acceptance_runbook(disk_state, repo)
    # a reconciler that listed before the winner ran
    stale_view = [r for r in State(disk_state.db_path).list_runbooks() if r.id == "RB-lease"]

    runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)
    assert gate.calls == 1
    assert stale_view[0].status == RUNBOOK_RUNNING  # its snapshot is stale
    again = runbooks.reconcile_runbooks(state=State(disk_state.db_path), repo_root=repo)

    assert gate.calls == 1  # the terminal state is honoured, not the stale snapshot
    assert again["skipped_not_owner"] == 0


# 3 ------------------------------------------------- non-owner observes without running the gate


def test_non_owner_process_observes_state_and_never_runs_the_gate(tmp_path, disk_state, gate):
    repo = _git_repo(tmp_path / "wt")
    rb, _ = _acceptance_runbook(disk_state, repo)
    proc = _hold_in_subprocess(HOLD_LEASE, str(disk_state.db_path), rb.id)
    try:
        result = runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)
        owner = lease.lease_owner(disk_state, rb.id)
    finally:
        proc.kill()
        proc.wait()

    assert gate.calls == 0
    assert result["skipped_not_owner"] == 1
    assert disk_state.get_runbook(rb.id).status == RUNBOOK_RUNNING  # untouched, still observable
    assert owner is not None and owner["pid"] == proc.pid and owner["holder"] == "other-process"


# 4 ------------------------------------------------------------ stale / crashed owner recovery


def test_lease_is_recovered_after_the_owner_process_is_killed(tmp_path, disk_state, gate):
    repo = _git_repo(tmp_path / "wt")
    rb, _ = _acceptance_runbook(disk_state, repo)
    proc = _hold_in_subprocess(HOLD_LEASE, str(disk_state.db_path), rb.id)
    assert runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)["skipped_not_owner"] == 1
    proc.kill()  # SIGKILL: no cleanup code runs in the owner
    proc.wait()

    result = runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)

    assert result["skipped_not_owner"] == 0
    assert gate.calls == 1


def test_lease_is_reentrant_in_thread_and_released_after_use(tmp_path, disk_state):
    with lease.advancement_lease(disk_state, "RB-x") as outer:
        assert outer
        with lease.advancement_lease(disk_state, "RB-x") as inner:  # advance_after_success under reconcile
            assert inner
    proc = _hold_in_subprocess(HOLD_LEASE, str(disk_state.db_path), "RB-x")  # would assert if still held
    proc.kill()
    proc.wait()


def test_in_memory_state_still_serialises_advancement():
    state = State(":memory:")
    with lease.advancement_lease(state, "RB-m") as first:
        assert first
        results: list[bool] = []

        def contend() -> None:
            with lease.advancement_lease(state, "RB-m") as second:
                results.append(second)

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join()
    assert results == [False]


# 5 ---------------------------------------------------------------- pause / resume / stop remain valid


def test_pause_and_stop_still_hold_the_acceptance_pipeline_under_the_lease(tmp_path, disk_state, gate):
    repo = _git_repo(tmp_path / "wt")
    rb, _ = _acceptance_runbook(disk_state, repo)
    runbooks.pause_runbook(state=disk_state, runbook_id=rb.id)

    runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)
    assert gate.calls == 0
    assert disk_state.get_runbook(rb.id).status == RUNBOOK_PAUSED

    runbooks.resume_runbook(state=disk_state, runbook_id=rb.id)
    runbooks.stop_runbook(state=disk_state, runbook_id=rb.id)
    runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)
    assert gate.calls == 0
    assert disk_state.get_runbook(rb.id).status == RUNBOOK_OWNER_ACTION_REQUIRED

    # the lease is free again afterwards, so a resume proceeds to a single gate run
    runbooks.retry_acceptance(state=disk_state, runbook_id=rb.id)
    runbooks.reconcile_runbooks(state=disk_state, repo_root=repo)
    assert gate.calls == 1


# 1 + 6 ------------------------------------------------------ daemon + dashboard together


@pytest.fixture()
def dashboard(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "wt")
    registry = load_registry()
    state = State(tmp_path / "cp" / "cp.db")
    ctx = CommandContext(
        state=state, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=repo, state=state), repo_root=repo,
    )
    monkeypatch.setattr(dashboard_api, "RECONCILE_INTERVAL_SECONDS", 0.02)
    return ctx, repo


def _count_reconciles(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = runbooks.reconcile_runbooks

    def counting(**kwargs):
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(runbooks, "reconcile_runbooks", counting)
    return calls


def test_dashboard_only_observes_while_a_live_daemon_holds_authority_and_stays_usable(
    dashboard, gate, monkeypatch
):
    ctx, repo = dashboard
    rb, _ = _acceptance_runbook(ctx.state, repo)
    calls = _count_reconciles(monkeypatch)
    daemon = _hold_in_subprocess(HOLD_DAEMON, str(ctx.state.db_path))
    try:
        with TestClient(create_app(ctx)) as client:
            time.sleep(0.4)  # many dashboard ticks
            assert client.get("/api/overview").status_code == 200  # monitoring works
            assert client.get("/api/tasks").status_code == 200
            assert client.get(f"/api/runbooks/{rb.id}/report").status_code in (200, 404, 409)
        assert calls == [] and gate.calls == 0
        assert ctx.state.get_runbook(rb.id).status == RUNBOOK_RUNNING
    finally:
        daemon.kill()
        daemon.wait()


def test_dashboard_resumes_advancing_when_the_daemon_dies(dashboard, gate, monkeypatch):
    ctx, repo = dashboard
    rb, _ = _acceptance_runbook(ctx.state, repo)
    daemon = _hold_in_subprocess(HOLD_DAEMON, str(ctx.state.db_path))
    with TestClient(create_app(ctx)):
        time.sleep(0.2)
        assert gate.calls == 0
        daemon.kill()  # crash: the kernel drops daemon authority
        daemon.wait()
        deadline = time.time() + 5
        while gate.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
    assert gate.calls == 1


def test_daemon_and_dashboard_reconcilers_together_execute_the_gate_once(dashboard, gate, monkeypatch):
    ctx, repo = dashboard
    _acceptance_runbook(ctx.state, repo)
    gate.hold = 0.3
    daemon_state = State(ctx.state.db_path)  # the daemon's own connection/process view
    with TestClient(create_app(ctx)):  # dashboard loop ticking every 20ms, no daemon authority claimed
        runbooks.reconcile_runbooks(state=daemon_state, repo_root=repo)  # the "daemon" tick
        time.sleep(0.3)
    assert gate.calls == 1
    assert gate.max_active == 1


def test_advance_endpoint_refuses_a_runbook_owned_by_another_process(dashboard):
    ctx, repo = dashboard
    rb, _ = _acceptance_runbook(ctx.state, repo)
    other = _hold_in_subprocess(HOLD_LEASE, str(ctx.state.db_path), rb.id)
    try:
        response = TestClient(create_app(ctx)).post(f"/api/runbooks/{rb.id}/advance")
    finally:
        other.kill()
        other.wait()
    assert response.status_code == 409
    assert response.json()["detail"]["owner"]["pid"] == other.pid


def test_a_second_daemon_cannot_claim_authority(tmp_path, disk_state):
    daemon = _hold_in_subprocess(HOLD_DAEMON, str(disk_state.db_path))
    try:
        assert lease.daemon_authority_active(disk_state) is True
        assert lease.claim_daemon_authority(disk_state) is False
    finally:
        daemon.kill()
        daemon.wait()
    assert lease.daemon_authority_active(disk_state) is False
    assert lease.claim_daemon_authority(disk_state) is True
    lease.release_daemon_authority(disk_state)


def test_distinct_runbook_ids_never_share_a_lease(tmp_path, disk_state):
    with lease.advancement_lease(disk_state, "a/b") as first, lease.advancement_lease(disk_state, "a:b") as second:
        assert first and second  # both sanitise to "a_b" but hash to different lease files


def test_daemon_authority_probe_tolerates_a_missing_or_removed_file(tmp_path, disk_state):
    assert lease.daemon_authority_active(disk_state) is False  # never claimed
    assert lease.claim_daemon_authority(disk_state) is True
    lease.release_daemon_authority(disk_state)
    (disk_state.db_path.parent / lease.DAEMON_AUTHORITY_FILENAME).unlink()
    assert lease.daemon_authority_active(disk_state) is False  # removed: no exception
    assert lease.daemon_authority_active(State(":memory:")) is False
