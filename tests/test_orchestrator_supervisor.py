"""Process launch/track behavior, including real write-lock integration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.agents.control_plane.models import (
    KIND_WRITE,
    TASK_BLOCKED,
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
    Task,
    WorktreeRecord,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def _write_task(tmp_path: Path, *, worker="claude-code", command=(), worktree: Path | None = None) -> Task:
    return Task(
        id="t1",
        task_ref="ENG-AGENT-02",
        role="secondary-implementation" if worker != "claude-code" else "primary-implementation",
        worker=worker,
        kind=KIND_WRITE,
        worktree=str(worktree if worktree is not None else tmp_path),
        command=tuple(command),
    )


def test_launch_task_refuses_a_second_write_worker_holding_the_real_lock_file(tmp_path, monkeypatch):
    """Reuses runner.py's exact write-lock file format: a true integration test."""

    repo = _git_repo(tmp_path)
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)

    lock_dir = repo / ".agent-output"
    lock_dir.mkdir()
    (lock_dir / ".write-lock").write_text(f"other-worker pid={os.getpid()} at=0", encoding="utf-8")

    spawned = []
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: spawned.append(argv) or object())

    task = _write_task(repo)
    result = supervisor.launch_task(task)

    assert result.state == TASK_BLOCKED
    assert "write-safety" in (result.last_error or "").lower()
    assert spawned == [], "a blocked write task must never reach subprocess spawn"


def test_launch_task_allows_a_write_worker_when_no_lock_is_held(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)

    class FakeProcess:
        pid = 424242

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    # current_branch would fail without a real git repo at tmp_path; the write
    # safety pre-check only calls it when a lock exists in this refusal-first
    # implementation, but guard against environment surprises regardless.
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    task = _write_task(tmp_path)
    result = supervisor.launch_task(task)

    assert result.state == TASK_RUNNING
    assert result.pid == 424242
    assert task.id in supervisor.live_task_ids()


def test_launch_task_records_unknown_worker_as_blocked(tmp_path):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = _write_task(tmp_path, worker="does-not-exist")
    result = supervisor.launch_task(task)
    assert result.state == TASK_BLOCKED
    assert "unknown worker" in (result.last_error or "").lower()


def test_build_argv_includes_scope_prompt_and_dry_run_flag(tmp_path):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = _write_task(tmp_path, command=["scope:scripts/agents", "run the tests please"])

    argv = supervisor._build_argv(task, dry_run=True)

    assert "--scope" in argv
    assert argv[argv.index("--scope") + 1] == "scripts/agents"
    assert "--dry-run" in argv
    assert argv[-1] == "run the tests please"


def test_build_argv_restores_escaped_scope_like_prompt(tmp_path):
    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=State(":memory:"))
    task = _write_task(
        tmp_path,
        command=["scope:scripts/agents", "literal:scope:explain this literally"],
    )
    argv = supervisor._build_argv(task, dry_run=True)
    assert argv[argv.index("--scope") + 1] == "scripts/agents"
    assert argv[-1] == "scope:explain this literally"


def test_build_argv_translates_base_entry_into_diff_base_for_review(tmp_path):
    """ENG-AGENT-18 (issue #155): the acceptance pipeline's diff-review Task

    encodes its configured base ref as a ``base:<ref>`` command entry
    (mirroring ``scope:<path>``); the daemon must translate it into
    ``--diff-base`` so a clean, already-committed candidate's review is
    built against the right base instead of always failing on an empty
    working-tree diff.
    """

    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=State(":memory:"))
    task = Task(
        id="t2",
        task_ref="ENG-AGENT-02-REVIEW-ABCD1234",
        role="diff-review",
        worker="antigravity-diff-review",
        kind=KIND_WRITE,
        worktree=str(tmp_path),
        command=("scope:scripts/agents", "base:origin/main", "review this candidate"),
    )

    argv = supervisor._build_argv(task, dry_run=True)

    assert "--include-diff" in argv
    assert argv[argv.index("--diff-base") + 1] == "origin/main"
    assert argv[-1] == "review this candidate"


def test_build_argv_restores_escaped_base_like_prompt(tmp_path):
    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=State(":memory:"))
    task = Task(
        id="t3",
        task_ref="ENG-AGENT-02-REVIEW-ABCD1234",
        role="diff-review",
        worker="antigravity-diff-review",
        kind=KIND_WRITE,
        worktree=str(tmp_path),
        command=("scope:scripts/agents", "literal:base:explain this literally"),
    )

    argv = supervisor._build_argv(task, dry_run=True)

    assert "--diff-base" not in argv
    assert argv[-1] == "base:explain this literally"


def test_poll_once_marks_success_and_failure_from_exit_code(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FakeProcess:
        def __init__(self, code):
            self._code = code
            self.pid = 1

        def poll(self):
            return self._code

    # ENG-PC-01: distinct worktrees, matching production (every write task owns
    # its own dedicated checkout) and the durable execution lease's real
    # invariant -- two unrelated tasks sharing one worktree is refused.
    ok_task = _write_task(tmp_path, command=[], worktree=tmp_path / "ok")
    ok_task.id = "ok"
    fail_task = _write_task(tmp_path, command=[], worktree=tmp_path / "fail")
    fail_task.id = "fail"

    processes = iter([FakeProcess(0), FakeProcess(1)])
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: next(processes))

    supervisor.launch_task(ok_task)
    supervisor.launch_task(fail_task)

    finished = supervisor.poll_once()
    finished_by_id = {t.id: t for t in finished}
    assert finished_by_id["ok"].state == TASK_SUCCEEDED
    assert finished_by_id["fail"].state == TASK_FAILED
    assert supervisor.live_task_ids() == frozenset()


def test_poll_once_preserves_operator_cancellation(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FakeProcess:
        pid = 22

        def poll(self):
            return 0

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    task = _write_task(tmp_path)
    supervisor.launch_task(task)
    task.state = TASK_CANCELLED
    state.upsert_task(task)
    supervisor.poll_once()
    assert state.get_task(task.id).state == TASK_CANCELLED
    assert state.get_task(task.id).result == "CANCELLED"


def test_terminate_task_reaps_only_the_owned_process_group(tmp_path):
    state = State(":memory:")
    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=state)
    task = _write_task(tmp_path)
    task.runbook_id = "RB-cancel"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    task.state = TASK_RUNNING
    task.pid = process.pid
    state.upsert_task(task)
    state.upsert_worktree(WorktreeRecord(path=str(tmp_path), managed=True))
    state.upsert_usage_governance({
        "runbook_id": task.runbook_id, "task_id": task.id,
        "route_history": [{"worker": task.worker, "status": "RUNNING"}],
    })
    lock_dir = tmp_path / ".agent-output"
    lock_dir.mkdir()
    lock_path = lock_dir / ".write-lock"
    lock_path.write_text(f"{task.worker} pid={process.pid} at=0", encoding="utf-8")
    supervisor._processes[task.id] = process
    supervisor._owned_process_groups.add(process.pid)

    assert supervisor.terminate_task(task.id) is True
    assert process.poll() is not None
    assert supervisor.live_task_ids() == frozenset()
    saved = state.get_task(task.id)
    assert saved.state == TASK_CANCELLED and saved.pid is None
    assert not lock_path.exists()
    attempt = state.get_usage_governance(task.runbook_id)["route_history"][-1]
    assert attempt["status"] == TASK_CANCELLED
    assert attempt["result"] == "CANCELLED"


def test_terminate_task_preserves_live_unrelated_write_lock(tmp_path):
    state = State(":memory:")
    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=state)
    task = _write_task(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    task.state = TASK_RUNNING
    task.pid = process.pid
    state.upsert_task(task)
    state.upsert_worktree(WorktreeRecord(path=str(tmp_path), managed=True))
    lock_dir = tmp_path / ".agent-output"
    lock_dir.mkdir()
    lock_path = lock_dir / ".write-lock"
    lock_path.write_text(f"other-worker pid={os.getpid()} at=0", encoding="utf-8")
    supervisor._processes[task.id] = process
    supervisor._owned_process_groups.add(process.pid)

    assert supervisor.terminate_task(task.id) is True
    assert lock_path.read_text(encoding="utf-8") == f"other-worker pid={os.getpid()} at=0"


def test_shutdown_all_reaps_every_owned_child(tmp_path):
    state = State(":memory:")
    supervisor = Supervisor(registry=load_registry(), repo_root=tmp_path, state=state)
    processes = []
    for index in range(2):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        processes.append(process)
        task = _write_task(tmp_path)
        task.id = f"owned-{index}"
        task.state = TASK_RUNNING
        task.pid = process.pid
        state.upsert_task(task)
        supervisor._processes[task.id] = process
        supervisor._owned_process_groups.add(process.pid)

    assert supervisor.shutdown_all() == 2
    assert all(process.poll() is not None for process in processes)


def test_reconcile_recovered_pid_that_exited_fails_closed(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = _write_task(tmp_path)
    task.state = TASK_RUNNING
    task.pid = 424242
    state.upsert_task(task)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.pid_is_alive", lambda pid: False)
    finished = supervisor.reconcile_recovered_once()
    assert [item.id for item in finished] == [task.id]
    assert state.get_task(task.id).state == TASK_FAILED
    assert "outcome unavailable" in state.get_task(task.id).last_error


def test_poll_once_never_raises_even_if_state_write_fails(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FakeProcess:
        pid = 1

        def poll(self):
            return 0

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    task = _write_task(tmp_path)
    supervisor.launch_task(task)

    def _boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state, "upsert_task", _boom)

    # Must not raise: a bad reconcile is recorded as an event, never crashes
    # the daemon loop.
    finished = supervisor.poll_once()
    assert finished == []


def _git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo, matching test_agents_orchestration.py's own fixture.

    Running the real (but always ``--dry-run``) subprocess against a scratch
    repo rather than this worktree keeps ``.agent-output`` test artifacts out
    of the actual checkout.
    """

    subprocess.run(["git", "init", "-q", "-b", "work", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    return tmp_path


def test_real_dry_run_end_to_end_through_orchestrate_spawns_no_worker_cli():
    """One real, cheap subprocess call to prove the wiring, always in --dry-run.

    Never invokes a worker CLI or a billable model call: ``run_delegation``
    short-circuits before checking CLI availability when dry-running.

    Runs against this actual worktree (it must be the checkout that owns
    ``scripts/agents`` for ``python -m scripts.agents.orchestrate`` module
    resolution to work from a spawned subprocess, matching real production
    usage: a task's worktree is always a full repository checkout). Any
    ``.agent-output/ENG-AGENT-02-SUPERVISOR-TEST`` evidence this creates is
    removed again at the end so the real checkout is left clean.
    """

    import json as _json
    import shutil

    repo = Path(__file__).resolve().parents[1]
    task_ref = "ENG-AGENT-02-SUPERVISOR-TEST"
    evidence_dir = repo / ".agent-output" / task_ref
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=repo, state=state)
    task = Task(
        id="dryrun1",
        task_ref=task_ref,
        role="focused-tests",
        worker="opencode2-gemini-flash-lite",
        kind=KIND_WRITE,
        worktree=str(repo),
        command=("scope:scripts/agents", "describe the registry"),
    )
    try:
        result = supervisor.launch_task(task, dry_run=True)
        assert result.state == TASK_RUNNING
        assert result.pid is not None
        assert result.pid != os.getpid()
        assert sys.executable  # the argv used the current interpreter

        # Wait for the short-lived dry-run subprocess and confirm it recorded
        # a DRY_RUN manifest, never a worker invocation.
        process = supervisor._processes[task.id]
        return_code = process.wait(timeout=30)
        assert return_code == 0
        manifests = list((evidence_dir / "opencode2-gemini-flash-lite").glob("*/manifest.json"))
        assert len(manifests) == 1
        payload = _json.loads(manifests[0].read_text(encoding="utf-8"))
        assert payload["result"] == "DRY_RUN"
        assert payload["actual"]["execution_system"] == ""
    finally:
        shutil.rmtree(evidence_dir, ignore_errors=True)


# ------------------------------------------------- ENG-PC-01 execution lease wiring


def test_launch_task_refuses_a_second_task_into_a_worktree_a_live_lease_already_owns(tmp_path, monkeypatch):
    """The durable CAS gate refuses a racing second writer even with no real .write-lock file yet."""

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FakeProcess:
        pid = os.getpid()  # a real, verifiably alive pid

        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    first = _write_task(tmp_path)
    first.id = "first"
    result = supervisor.launch_task(first)
    assert result.state == TASK_RUNNING

    spawned = []
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: spawned.append(argv) or FakeProcess())
    second = _write_task(tmp_path)
    second.id = "second"
    result = supervisor.launch_task(second)

    assert result.state == TASK_BLOCKED
    assert "execution lease conflict" in (result.last_error or "").lower()
    assert spawned == [], "a lease conflict must never reach subprocess spawn"


def test_terminate_task_frees_the_execution_lease_for_a_new_launch(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: process)
    first = _write_task(tmp_path)
    first.id = "first"
    supervisor.launch_task(first)
    supervisor._owned_process_groups.add(process.pid)

    assert supervisor.terminate_task("first") is True

    class FakeProcess:
        pid = os.getpid()

        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    second = _write_task(tmp_path)
    second.id = "second"
    result = supervisor.launch_task(second)
    assert result.state == TASK_RUNNING


def test_launch_task_spawn_failure_releases_the_lease_and_leaves_worktree_launchable(tmp_path, monkeypatch):
    """Grok Build review (issue #29 follow-up), defect 1: a synchronous ``_spawn``
    failure (bad argv, ``OSError``, permissions, missing interpreter) must not
    strand the lease ``ACQUIRED`` under this (live) supervisor's own pid forever --
    that would make the worktree permanently unlaunchable for the daemon's life.
    """

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    def _boom(argv, cwd, env=None):
        raise OSError("no such file or directory: fake-interpreter")

    monkeypatch.setattr(supervisor, "_spawn", _boom)
    task = _write_task(tmp_path)
    result = supervisor.launch_task(task)

    assert result.state == TASK_BLOCKED
    assert "failed to spawn" in (result.last_error or "").lower()
    lease_row = state.get_execution_lease(str(tmp_path))
    assert lease_row.status == "RELEASED"
    assert task.id not in supervisor.live_task_ids()

    # The worktree must still be launchable immediately afterwards, not stuck.
    class FakeProcess:
        pid = os.getpid()

        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    second = _write_task(tmp_path)
    second.id = "second"
    result2 = supervisor.launch_task(second)
    assert result2.state == TASK_RUNNING


def test_launch_task_terminates_the_child_when_attach_pid_loses_the_race(tmp_path, monkeypatch):
    """Grok Build review (issue #29 follow-up), defect 2 (the ``attach_pid`` return
    value): if the lease was reclaimed out from under this acquirer between winning
    the CAS and the subprocess starting, proceeding to track the just-spawned child
    as this task's own running process would admit a second, unrecognized writer
    into a worktree this process no longer owns. A real subprocess and a real
    SIGTERM/SIGKILL, so the assertion is that the child is actually dead -- not
    merely that bookkeeping forgot about it.
    """

    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: process)
    monkeypatch.setattr(
        "scripts.agents.control_plane.supervisor.execution_lease.attach_pid", lambda *a, **k: False
    )

    task = _write_task(tmp_path)
    try:
        result = supervisor.launch_task(task)

        assert result.state == TASK_BLOCKED
        assert "reclaimed" in (result.last_error or "").lower()
        assert task.id not in supervisor.live_task_ids()
        assert process.wait(timeout=5) is not None  # actually terminated, not merely disowned
    finally:
        if process.poll() is None:  # pragma: no cover - safety net if the assertion above failed
            process.kill()
            process.wait()


def test_poll_once_frees_the_execution_lease_on_completion(tmp_path, monkeypatch):
    registry = load_registry()
    state = State(":memory:")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    monkeypatch.setattr("scripts.agents.control_plane.supervisor.assert_write_safety", lambda *a, **k: None)

    class FinishingProcess:
        pid = os.getpid()

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FinishingProcess())
    first = _write_task(tmp_path)
    first.id = "first"
    supervisor.launch_task(first)
    supervisor.poll_once()

    class FakeProcess:
        pid = os.getpid()

        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "_spawn", lambda argv, cwd, env=None: FakeProcess())
    second = _write_task(tmp_path)
    second.id = "second"
    result = supervisor.launch_task(second)
    assert result.state == TASK_RUNNING
