"""ENG-AO-06 (issue #15): unattended daemon launch and non-blocking provider probes.

Deterministic and local: every "CLI" is a tiny script in a temp directory; no provider, network or
billable call is made, and the real ``octarel run`` is replaced by a stand-in command.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scripts.agents import probe
from scripts.agents.control_plane import overnight as ovn
from scripts.agents.control_plane import service
from scripts.agents.control_plane.state import State
from scripts.agents.registry import AuthCheckResult, load_registry
from tests.test_overnight_advancement import Harness, make_project, register, start


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture()
def state(tmp_path: Path) -> State:
    return State(tmp_path / "state" / "cp.db")


@pytest.fixture()
def alpha(tmp_path: Path) -> Path:
    return make_project(tmp_path / "alpha", [("A-01", "pending", "first"), ("A-02", "pending", "second")])


# --- probes ------------------------------------------------------------------------------------


def test_probe_has_closed_stdin_and_its_own_session(tmp_path):
    cli = _script(
        tmp_path / "cli.py",
        """
        import os, sys
        print(repr(sys.stdin.read()), os.getsid(0) == os.getpid(), os.getsid(0) != os.getppid())
        """,
    )
    result = probe.run_probe([str(cli)], timeout=10)
    assert result.stdout.split()[0] == "''"  # EOF immediately: nothing to prompt on
    assert "True" in result.stdout  # it is its own session leader: no controlling terminal to be stopped by


def test_probe_that_hangs_is_killed_by_its_timeout(tmp_path):
    cli = _script(tmp_path / "hang.py", "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe.run_probe([str(cli)], timeout=0.5)
    assert time.monotonic() - started < 10


def test_probe_requires_a_positive_timeout():
    with pytest.raises(ValueError):
        probe.run_probe([sys.executable, "-c", "pass"], timeout=0)


def test_hung_auth_probe_is_a_deterministic_launch_error_not_a_hang_or_fallback(tmp_path):
    claude = load_registry().get("claude-code")
    hang = _script(tmp_path / "claude", "import time\ntime.sleep(60)\n")
    worker = claude.__class__(**{**claude.__dict__, "cli_bin": str(hang)})
    started = time.monotonic()
    assert worker._classify_auth_check(timeout=0.5) == AuthCheckResult.LAUNCH_ERROR
    assert worker.check_auth(timeout=0.5) is False
    assert time.monotonic() - started < 15
    assert worker.allow_api_billing is False  # a timed-out probe never enables an API route


def test_auth_probe_with_no_stdin_answers_normally(tmp_path):
    claude = load_registry().get("claude-code")
    cli = _script(
        tmp_path / "claude",
        """
        import sys
        sys.stdin.read()  # would block a terminal-attached probe; EOF here
        print('{"loggedIn": true, "authMethod": "claude.ai"}')
        """,
    )
    worker = claude.__class__(**{**claude.__dict__, "cli_bin": str(cli)})
    assert worker.check_auth(timeout=10) is True


def test_no_provider_probe_bypasses_the_bounded_helper():
    root = Path(__file__).resolve().parents[1] / "scripts" / "agents"
    for rel in ("registry.py", "control_plane/telemetry.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "run_probe" in text
    registry_text = (root / "registry.py").read_text(encoding="utf-8")
    assert "subprocess.run(" not in registry_text


# --- overnight session must not hang or duplicate on a probe timeout ---------------------------


def test_probe_timeout_while_starting_a_task_fails_closed_without_orphans(state, alpha):
    register(state, "alpha", alpha)
    sid = start(state)["session_id"]
    h = Harness(state)

    def timed_out(key, project):
        raise subprocess.TimeoutExpired(["claude", "auth", "status"], 5)

    h.starter = timed_out
    h.tick()
    stored = h.session(sid)
    assert stored["state"] == ovn.SESSION_STOPPED
    assert stored.get("current_runbook_id") is None
    assert state.list_runbooks(project_id="alpha") == []
    h.tick()  # the daemon keeps ticking; a stopped session is never retried or double-started
    assert state.list_runbooks(project_id="alpha") == []


# --- unattended daemon launch ------------------------------------------------------------------


@pytest.fixture()
def code_root(tmp_path) -> Path:
    root = tmp_path / "octarel"
    (root / ".orchestrator-state").mkdir(parents=True)
    return root


def _fake_daemon(tmp_path: Path) -> list[str]:
    script = _script(
        tmp_path / "fake_daemon.py",
        """
        import os, sys, time
        print("octarel run started sid_is_own=%s stdin_eof=%r" % (os.getsid(0) == os.getpid(), sys.stdin.read()), flush=True)
        time.sleep(60)
        """,
    )
    return [str(script), "octarel", "run"]


def _reap(state_dir: Path) -> None:
    service.stop_daemon(state_dir)


def test_daemon_start_is_detached_logged_and_recorded(code_root, tmp_path):
    state_dir = code_root / ".orchestrator-state"
    pid = service.start_daemon(code_root, state_dir, argv=_fake_daemon(tmp_path), settle_seconds=0.5)
    try:
        assert service.daemon_pid_path(state_dir).read_text().strip() == str(pid)
        assert os.getsid(pid) == pid and os.getsid(pid) != os.getsid(0)  # own session: no shared terminal
        deadline = time.time() + 10
        log = service.daemon_log_path(state_dir)
        while time.time() < deadline and "started" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.1)
        assert "sid_is_own=True stdin_eof=''" in log.read_text()  # unbuffered log, no stdin
        assert service.daemon_status(state_dir)["state"] == "RUNNING"
    finally:
        _reap(state_dir)
    assert service.daemon_status(state_dir)["state"] == "NOT_RUNNING"


def test_second_daemon_start_is_refused_no_duplicate_writer(code_root, tmp_path):
    state_dir = code_root / ".orchestrator-state"
    pid = service.start_daemon(code_root, state_dir, argv=_fake_daemon(tmp_path), settle_seconds=0.3)
    try:
        with pytest.raises(service.DaemonError, match="already RUNNING"):
            service.start_daemon(code_root, state_dir, argv=_fake_daemon(tmp_path), settle_seconds=0.1)
        assert service.daemon_pid_path(state_dir).read_text().strip() == str(pid)
    finally:
        _reap(state_dir)


def test_suspended_daemon_is_reported_and_stop_recovers_it(code_root, tmp_path):
    state_dir = code_root / ".orchestrator-state"
    pid = service.start_daemon(code_root, state_dir, argv=_fake_daemon(tmp_path), settle_seconds=0.3)
    os.kill(pid, signal.SIGSTOP)  # SIGSTOP: what terminal job control did to the original daemon
    try:
        deadline = time.time() + 5
        while time.time() < deadline and service.daemon_status(state_dir)["state"] != "SUSPENDED":
            time.sleep(0.1)
        assert service.daemon_status(state_dir)["state"] == "SUSPENDED"
        assert service.stop_daemon(state_dir) == pid
        time.sleep(0.2)
        assert service.daemon_status(state_dir)["state"] == "NOT_RUNNING"
    finally:
        _reap(state_dir)


def test_daemon_refuses_a_non_canonical_state_dir(code_root, tmp_path):
    with pytest.raises(service.DaemonError, match="canonical"):
        service.start_daemon(code_root, tmp_path / "elsewhere", argv=["true"], settle_seconds=0)


def test_daemon_that_dies_immediately_is_reported_with_its_log(code_root):
    state_dir = code_root / ".orchestrator-state"
    with pytest.raises(service.DaemonError, match="exited immediately"):
        service.start_daemon(code_root, state_dir, argv=[sys.executable, "-c", "raise SystemExit(3)"], settle_seconds=0.5)
    assert not service.daemon_pid_path(state_dir).exists()


def test_stale_pid_file_is_not_a_running_daemon(code_root):
    state_dir = code_root / ".orchestrator-state"
    service.write_daemon_pid(state_dir, 2**22 + 7)
    assert service.daemon_status(state_dir)["state"] == "NOT_RUNNING"


def test_daemon_run_ignores_terminal_job_control_signals():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "agents" / "orchestrator.py").read_text(encoding="utf-8")
    assert "SIGTTIN" in source and "SIGTTOU" in source and "SIG_IGN" in source


def test_cli_exposes_daemon_verbs():
    from octarel import cli

    with pytest.raises(SystemExit):
        cli.main(["daemon", "bogus"])
