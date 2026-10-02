"""ENG-PC-09 durable runtime-service safety and isolation."""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.models import Runbook, WorktreeRecord
from scripts.agents.control_plane.project import ProjectContract
from scripts.agents.control_plane.project_registry import contract_to_row
from scripts.agents.control_plane.runtime_services import (
    RuntimeServiceError,
    RuntimeServiceManager,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def _project(root: Path, project_id: str = "project-a", **capabilities: str) -> ProjectContract:
    declared = {
        "runtime_service_argv": json.dumps(["python3", "-m", "http.server", "43123"]),
        "runtime_service_port": "43123",
        "runtime_service_host": "127.0.0.1",
        "runtime_service_name": "Fixture preview",
    }
    declared.update(capabilities)
    return ProjectContract(
        project_id=project_id,
        display_name=project_id,
        local_repo_root=root,
        capabilities=declared,
    )


def _manager(tmp_path: Path, project: ProjectContract | None = None) -> RuntimeServiceManager:
    state = State(tmp_path / "state.db")
    return RuntimeServiceManager(
        SimpleNamespace(state=state, selected_project=project or _project(tmp_path), repo_root=tmp_path)
    )


def _persisted(manager: RuntimeServiceManager, **changes: object) -> dict[str, object]:
    row = manager._declared_row()
    row.update(
        pid=7123,
        process_create_time=100.5,
        process_session_id=7123,
        process_argv=list(row["argv"]),
        health="HEALTHY",
        ownership="OWNED_VERIFIED",
        owner="local",
        started_at="2026-10-02T10:00:00+00:00",
    )
    row.update(changes)
    return manager.ctx.state.upsert_runtime_service(row)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_state_migration_creates_project_scoped_runtime_service_contract(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    legacy = State(path)
    legacy._conn.execute("ALTER TABLE runtime_services DROP COLUMN process_argv")
    legacy._conn.commit()
    legacy.close()

    state = State(path)
    columns = {row[1] for row in state._conn.execute("PRAGMA table_info(runtime_services)")}
    assert {
        "project_id", "worktree_path", "runbook_id", "argv", "process_argv", "cwd", "pid",
        "process_create_time", "process_session_id", "port", "preview_url",
        "health", "ownership", "owner", "log_pointer", "exit_code",
    } <= columns


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "python3 -m http.server 43123"],
        ["python3", "server.py;curl", "example.invalid"],
        ["python3", "server.py", "--api-token=not-allowed"],
        ["python3", "server.py", "--host=0.0.0.0"],
    ],
)
def test_declaration_rejects_shell_interpolation_and_secret_arguments(tmp_path: Path, argv: list[str]) -> None:
    project = _project(tmp_path, runtime_service_argv=json.dumps(argv))
    manager = _manager(tmp_path, project)
    with pytest.raises(RuntimeServiceError):
        manager._declaration()


@pytest.mark.parametrize(
    "launcher",
    [
        "sh", "bash", "dash", "ash", "zsh", "fish", "ksh", "mksh", "csh", "tcsh",
        "pwsh", "powershell", "cmd", "cmd.exe", "env", "sudo", "doas", "su", "runuser",
    ],
)
def test_declaration_rejects_shell_command_and_privilege_wrappers(
    tmp_path: Path, launcher: str
) -> None:
    project = _project(
        tmp_path,
        runtime_service_argv=json.dumps([launcher, "python3", "-m", "http.server", "43123"]),
    )
    with pytest.raises(RuntimeServiceError, match="shell, command, env, or privilege wrapper"):
        _manager(tmp_path, project)._declaration()


@pytest.mark.parametrize(
    "bind_arg",
    [
        "0.0.0.0:8765",
        "--bind=0.0.0.0:8765",
        "[::]:8765",
        "--host=[::]:8765",
        "--host=:::8765",
        "--bind=:8765",
        "tcp://0.0.0.0:8765",
        "*:8765",
    ],
)
def test_declaration_rejects_wildcard_hosts_with_ports(tmp_path: Path, bind_arg: str) -> None:
    project = _project(
        tmp_path,
        runtime_service_argv=json.dumps(["python3", "server.py", bind_arg]),
    )
    with pytest.raises(RuntimeServiceError, match="public bind"):
        _manager(tmp_path, project)._declaration()


@pytest.mark.parametrize(
    "bind_arg",
    ["127.0.0.1:8765", "--bind=127.0.0.1:8765", "[::1]:8765", "--host=localhost:8765"],
)
def test_declaration_preserves_explicit_loopback_hosts(tmp_path: Path, bind_arg: str) -> None:
    project = _project(
        tmp_path,
        runtime_service_argv=json.dumps(["python3", "server.py", bind_arg]),
    )
    assert _manager(tmp_path, project)._declaration()[0][-1] == bind_arg


def test_declaration_requires_an_explicit_port(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.capabilities.pop("runtime_service_port")
    manager = _manager(tmp_path, project)
    with pytest.raises(RuntimeServiceError, match="does not declare runtime_service_port"):
        manager._declaration()


def test_new_runtime_declaration_requires_explicit_loopback_host(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.capabilities.pop("runtime_service_host")
    manager = _manager(tmp_path, project)
    with pytest.raises(RuntimeServiceError, match="explicitly declared as 127.0.0.1"):
        manager._declaration()


def test_runtime_child_environment_does_not_inherit_provider_credentials(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-runtime-boundary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-cross-runtime-boundary")
    environment = _manager(tmp_path)._child_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment


def test_pid_reuse_and_incomplete_identity_never_signal(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 999.0,
            "cwd": row["cwd"],
            "argv": row["process_argv"],
            "session_id": row["process_session_id"],
            "alive": True,
        },
    )
    monkeypatch.setattr("os.killpg", lambda *_a: pytest.fail("PID reuse must never be signalled"))
    with pytest.raises(RuntimeServiceError, match="PID create time changed"):
        manager._stop(row, "local")

    incomplete = dict(row, process_session_id=None)
    with pytest.raises(RuntimeServiceError, match="identity is incomplete"):
        manager._stop(incomplete, "local")

    normalized = dict(
        row,
        process_argv=["/absolute/Python.app/Contents/MacOS/Python", *row["argv"][1:]],
    )
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": normalized["process_create_time"],
            "cwd": normalized["cwd"],
            "argv": ["/absolute/unrelated", *row["argv"][1:]],
            "session_id": normalized["process_session_id"],
            "alive": True,
        },
    )
    with pytest.raises(RuntimeServiceError, match="captured process identity"):
        manager._stop(normalized, "local")


def test_stop_can_verify_a_durable_owner_after_launch_declaration_is_removed(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": row["process_create_time"],
            "cwd": row["cwd"],
            "argv": row["process_argv"],
            "session_id": row["process_session_id"],
            "alive": True,
        },
    )
    monkeypatch.setattr(manager, "_port_active", lambda _port: True)
    manager.ctx.selected_project.capabilities.pop("runtime_service_argv")
    assert manager.status()["actions"] == {"start": False, "stop": True, "restart": False}
    assert manager.list_services(include_scopes=False)[0]["actions"] == {
        "start": False,
        "stop": True,
        "restart": False,
    }
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(manager, "_stop", lambda stored, _actor: captured.append(stored) or {"health": "STOPPED"})
    assert manager.action("stop", "local", service_id=row["id"]) == {"health": "STOPPED"}
    assert captured[0]["id"] == row["id"]


def test_start_persists_complete_identity_and_stop_signals_only_verified_session(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()

    class FakeProcess:
        pid = 8123

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def terminate() -> None:  # pragma: no cover - failure path only
            pytest.fail("a valid launch must not be terminated during capture")

        @staticmethod
        def wait(*, timeout: float) -> int:
            assert 0 < timeout <= 8
            return -15

    observed_argv = ["/Library/Frameworks/Python.framework/Python.app/Contents/MacOS/Python", *row["argv"][1:]]
    facts = {
        "create_time": 321.25,
        "cwd": row["cwd"],
        "argv": observed_argv,
        "session_id": 8123,
        "alive": True,
    }
    initial_facts = dict(facts, argv=row["argv"])
    fact_probes = 0

    def process_facts(_pid: int) -> dict[str, object]:
        nonlocal fact_probes
        fact_probes += 1
        return initial_facts if fact_probes == 1 else facts

    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr(manager, "_process_facts", process_facts)
    monkeypatch.setattr(manager, "_child_environment", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: FakeProcess())
    started = manager._start(row, "operator@example.com")
    assert started["health"] == "STARTING"
    stored = manager.ctx.state.get_runtime_service(row["id"], project_id="project-a")
    assert stored is not None
    assert (stored["pid"], stored["process_create_time"], stored["process_session_id"]) == (8123, 321.25, 8123)
    assert stored["cwd"] == row["cwd"]
    assert stored["argv"] == row["argv"]
    assert stored["process_argv"] == observed_argv
    assert (tmp_path / stored["log_pointer"]).is_file()

    probes = iter([facts, None, None, None])
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: next(probes))
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: False)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda session, sig: signalled.append((session, sig)))
    stopped = manager._stop(stored, "operator@example.com")
    assert signalled and signalled[0][0] == 8123
    assert stopped["health"] == "STOPPED"
    assert stopped["exit_code"] == -15


def test_identity_capture_retries_transient_untrusted_facts_until_stably_valid(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()

    class Process:
        pid = 8229

        @staticmethod
        def poll() -> None:
            return None

    valid = {
        "create_time": 455.0,
        "cwd": row["cwd"],
        "argv": row["argv"],
        "session_id": Process.pid,
        "alive": True,
    }
    transient = iter(
        [
            dict(valid, cwd=str(tmp_path / "pre-exec-cwd")),
            dict(valid, session_id=Process.pid + 1),
            dict(valid, argv=["unexpected-launcher", *row["argv"][1:]]),
        ]
    )
    probes = 0

    def process_facts(_pid: int) -> dict[str, object]:
        nonlocal probes
        probes += 1
        return next(transient, valid)

    monkeypatch.setattr(manager, "_process_facts", process_facts)
    captured = manager._capture_process_identity(Process(), row)

    assert captured == valid
    assert probes > 3


def test_identity_capture_never_accepts_persistently_invalid_facts(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()

    class Process:
        pid = 8230

        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 455.5,
            "cwd": row["cwd"],
            "argv": ["unexpected-launcher", *row["argv"][1:]],
            "session_id": Process.pid,
            "alive": True,
        },
    )

    with pytest.raises(RuntimeServiceError, match="argv is not a permitted normalization"):
        manager._capture_process_identity(Process(), row)


def test_failed_identity_capture_terminates_group_waits_reaps_and_forgets_handle(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()
    waits: list[float] = []

    class FailedCaptureProcess:
        pid = 8234

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            waits.append(timeout)
            return -signal.SIGTERM

    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 456.0,
            "cwd": str(tmp_path / "wrong-cwd"),
            "argv": row["argv"],
            "session_id": FailedCaptureProcess.pid,
            "alive": True,
        },
    )
    monkeypatch.setattr(manager, "_child_environment", lambda: {"PATH": "/usr/bin"})
    process = FailedCaptureProcess()
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: process)
    monkeypatch.setattr("os.getsid", lambda pid: pid)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("os.killpg", lambda session, sig: signals.append((session, sig)))

    with pytest.raises(RuntimeServiceError, match="terminated.*reaped"):
        manager._start(row, "local")

    assert signals == [(process.pid, signal.SIGTERM)]
    assert len(waits) == 1 and 0 < waits[0] <= 8
    assert row["id"] not in manager._processes


def test_failed_identity_capture_that_will_not_exit_stays_tracked_without_force_kill(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()

    class StuckCaptureProcess:
        pid = 8235

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: float) -> int:
            raise subprocess.TimeoutExpired(cmd="fixture", timeout=timeout)

    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 457.0,
            "cwd": str(tmp_path / "wrong-cwd"),
            "argv": row["argv"],
            "session_id": StuckCaptureProcess.pid,
            "alive": True,
        },
    )
    monkeypatch.setattr(manager, "_child_environment", lambda: {"PATH": "/usr/bin"})
    process = StuckCaptureProcess()
    monkeypatch.setattr("subprocess.Popen", lambda *_a, **_k: process)
    monkeypatch.setattr("os.getsid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda *_args: None)

    with pytest.raises(RuntimeServiceError, match="remains tracked.*refuses to force-kill"):
        manager._start(row, "local")

    assert manager._processes[row["id"]] is process


def test_live_retained_failed_launch_handle_blocks_new_popen_without_signal(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()
    polls = 0

    class LiveRetainedProcess:
        pid = 8236

        @staticmethod
        def poll() -> None:
            nonlocal polls
            polls += 1

    process = LiveRetainedProcess()
    manager._processes[row["id"]] = process
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("a live retained child must block Popen"),
    )
    monkeypatch.setattr(
        "os.killpg",
        lambda *_args: pytest.fail("a retained child must not be signalled by a later start"),
    )

    with pytest.raises(RuntimeServiceError, match="previously launched child is still alive"):
        manager._start(row, "local")

    assert polls == 1
    assert manager._processes[row["id"]] is process


def test_public_actions_refuse_live_retained_child_but_allow_exited_handle(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()

    class LiveRetainedProcess:
        @staticmethod
        def poll() -> None:
            return None

    class ExitedRetainedProcess:
        @staticmethod
        def poll() -> int:
            return -signal.SIGTERM

    monkeypatch.setattr(
        "os.killpg",
        lambda *_args: pytest.fail("public action derivation must never signal a retained child"),
    )
    live = LiveRetainedProcess()
    manager._processes[row["id"]] = live
    observed = manager._public(row, port_active=False)
    assert observed["actions"] == {"start": False, "stop": False, "restart": False}
    assert "previously launched child is still alive" in observed["status_reason"]
    assert manager._processes[row["id"]] is live

    exited = ExitedRetainedProcess()
    manager._processes[row["id"]] = exited
    observed = manager._public(row, port_active=False)
    assert observed["actions"] == {"start": True, "stop": False, "restart": False}
    assert manager._processes[row["id"]] is exited


def test_exited_retained_failed_launch_handle_is_reaped_before_new_popen(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = manager._declared_row()
    events: list[str] = []

    class ExitedRetainedProcess:
        pid = 8237

        @staticmethod
        def poll() -> int:
            events.append("poll-retained")
            return -signal.SIGTERM

        @staticmethod
        def wait() -> int:
            events.append("wait-retained")
            return -signal.SIGTERM

    class NewProcess:
        pid = 8238

    retained = ExitedRetainedProcess()
    launched = NewProcess()
    manager._processes[row["id"]] = retained
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr(manager, "_child_environment", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(
        manager,
        "_capture_process_identity",
        lambda _process, launch_row: {
            "create_time": 458.0,
            "cwd": launch_row["cwd"],
            "argv": launch_row["argv"],
            "session_id": launched.pid,
            "alive": True,
        },
    )
    monkeypatch.setattr(manager, "_reconcile", lambda stored: stored)

    def popen(*_args, **_kwargs):
        assert events == ["poll-retained", "wait-retained"]
        assert row["id"] not in manager._processes
        events.append("popen-new")
        return launched

    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr(
        "os.killpg",
        lambda *_args: pytest.fail("reaping an exited retained child must not signal it"),
    )

    started = manager._start(row, "local")

    assert events == ["poll-retained", "wait-retained", "popen-new"]
    assert manager._processes[row["id"]] is launched
    assert started["pid"] == launched.pid


def test_stop_waits_for_and_reaps_the_exact_child_before_persisting(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    facts = {
        "create_time": row["process_create_time"],
        "cwd": row["cwd"],
        "argv": row["process_argv"],
        "session_id": row["process_session_id"],
        "alive": True,
    }
    fact_probes = iter([facts, None])
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: next(fact_probes))
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr("os.killpg", lambda *_args: None)
    waited: list[float] = []

    class ExactProcess:
        pid = row["pid"]

        @staticmethod
        def wait(*, timeout: float) -> int:
            waited.append(timeout)
            return -15

        @staticmethod
        def poll() -> int:  # pragma: no cover - regression guard
            pytest.fail("stop must wait/reap the exact child instead of polling once")

    manager._processes[row["id"]] = ExactProcess()
    stopped = manager._stop(row, "local")

    assert len(waited) == 1
    assert 0 < waited[0] <= 8
    assert row["id"] not in manager._processes
    assert stopped["health"] == "STOPPED"
    assert stopped["exit_code"] == -15


def test_durable_stop_does_not_treat_unavailable_facts_as_exit_while_pid_exists(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    live_facts = {
        "create_time": row["process_create_time"],
        "cwd": row["cwd"],
        "argv": row["process_argv"],
        "session_id": row["process_session_id"],
        "alive": True,
    }
    fact_probes = iter([live_facts, None, None, None])
    pid_probes = iter([True, False, False, False])
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: next(fact_probes))
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: next(pid_probes))
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr("os.killpg", lambda *_args: None)

    stopped = manager._stop(row, "local")

    assert stopped["health"] == "STOPPED"
    assert list(pid_probes) == []


def test_durable_stop_fails_closed_when_uninspectable_pid_outlives_deadline(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    live_facts = {
        "create_time": row["process_create_time"],
        "cwd": row["cwd"],
        "argv": row["process_argv"],
        "session_id": row["process_session_id"],
        "alive": True,
    }
    fact_probes = iter([live_facts, None])
    monotonic = iter([100.0, 100.0, 108.0])
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: next(fact_probes))
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr("os.killpg", lambda *_args: None)
    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeServiceError, match="refusing to force-kill"):
        manager._stop(row, "local")

    stored = manager.ctx.state.get_runtime_service(row["id"], project_id=row["project_id"])
    assert stored is not None
    assert stored["health"] == "HEALTHY"


def test_real_python_service_survives_exec_argv_normalization_and_stops_safely(tmp_path: Path) -> None:
    port = _unused_loopback_port()
    listener_script = tmp_path / "loopback_listener.py"
    listener_script.write_text(
        """\
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", int(sys.argv[1])))
    listener.listen()
    while True:
        connection, _ = listener.accept()
        connection.close()
""",
        encoding="utf-8",
    )
    declared_argv = ["python3", str(listener_script), str(port)]
    project = _project(
        tmp_path,
        runtime_service_argv=json.dumps(declared_argv),
        runtime_service_port=str(port),
    )
    manager = _manager(tmp_path, project)
    process = None
    try:
        started = manager.action("start", "regression-test")
        process = manager._processes[started["id"]]
        deadline = time.monotonic() + 5
        service = started
        while service["health"] != "HEALTHY" and time.monotonic() < deadline:
            time.sleep(0.05)
            service = manager.list_services(include_scopes=False)[0]

        assert service["health"] == "HEALTHY", service["status_reason"]
        assert service["ownership"] == "OWNED_VERIFIED"
        assert service["argv"] == declared_argv
        assert service["process_argv"]
        assert service["process_argv"][1:] == declared_argv[1:]
        stopped = manager.action("stop", "regression-test", service_id=service["id"])
        assert stopped["health"] == "STOPPED"
        assert process.poll() is not None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def test_external_port_is_informational_and_non_stoppable(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    monkeypatch.setattr(manager, "_port_active", lambda _port: True)
    service = manager.list_services(include_scopes=False)[0]
    assert service["health"] == "EXTERNAL"
    assert service["ownership"] == "EXTERNAL_UNOWNED"
    assert service["actions"] == {"start": False, "stop": False, "restart": False}
    with pytest.raises(RuntimeServiceError, match="start refused"):
        manager._start(service, "local")
    with pytest.raises(RuntimeServiceError, match="identity is incomplete"):
        manager._stop(service, "local")


def test_live_but_uninspectable_process_fails_closed_without_becoming_crashed(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager)
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: None)
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    observed = manager._public(row)
    assert observed["health"] == "UNVERIFIED"
    assert observed["ownership"] == "OWNERSHIP_UNPROVEN"
    assert observed["actions"] == {"start": False, "stop": False, "restart": False}


def test_terminal_pid_reuse_preserves_evidence_but_allows_safe_fresh_start(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    row = _persisted(
        manager,
        health="STOPPED",
        ownership="OWNED_EXITED",
        stopped_at="2026-10-02T10:05:00+00:00",
        exit_code=-15,
    )
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 999.0,
            "cwd": row["cwd"],
            "argv": row["process_argv"],
            "session_id": row["process_session_id"],
            "alive": True,
        },
    )
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    monkeypatch.setattr("os.killpg", lambda *_args: pytest.fail("a reused PID must never be signalled"))

    observed = manager._reconcile(row)
    assert observed["health"] == "STOPPED"
    assert observed["ownership"] == "OWNED_EXITED"
    assert observed["pid"] == row["pid"]
    assert observed["process_create_time"] == row["process_create_time"]
    assert observed["actions"] == {"start": True, "stop": False, "restart": False}
    assert "PID create time changed" in observed["status_reason"]

    launches: list[dict[str, object]] = []
    monkeypatch.setattr(
        manager,
        "_start",
        lambda candidate, _actor: launches.append(candidate) or {"health": "STARTING"},
    )
    assert manager.action("start", "local") == {"health": "STARTING"}
    assert launches[0]["pid"] is None
    stored = manager.ctx.state.get_runtime_service(row["id"], project_id=row["project_id"])
    assert stored is not None and stored["pid"] == row["pid"]


def test_crashed_service_preserves_evidence_and_is_restart_eligible(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = _persisted(manager, log_pointer="runtime-services/preserved.log")
    monkeypatch.setattr(manager, "_process_facts", lambda _pid: None)
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)
    observed = manager._reconcile(row)
    assert observed["health"] == "CRASHED"
    assert observed["log_pointer"] == "runtime-services/preserved.log"
    assert observed["actions"]["start"] is True

    current_argv = ["python3", "-m", "http.server", "43123", "--directory", "."]
    manager.ctx.selected_project.capabilities["runtime_service_argv"] = json.dumps(current_argv)
    restarted: list[dict[str, object]] = []
    monkeypatch.setattr(manager, "_start", lambda candidate, _actor: restarted.append(candidate) or {"health": "STARTING"})
    assert manager.action("restart", "local") == {"health": "STARTING"}
    assert restarted[0]["log_pointer"] == "runtime-services/preserved.log"
    assert restarted[0]["argv"] == current_argv


def test_worktree_and_runbook_scope_is_server_validated(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    manager.ctx.state.upsert_worktree(
        WorktreeRecord(path=str(worktree), project_id="project-a", managed=True)
    )
    runbook = Runbook(
        id="run-1",
        name="run",
        preset="finish-current-pr",
        objective="test",
        source_ref="ENG-PC-09",
        branch="eng/runtime",
        worktree=str(worktree),
        parent_worker="codex-build",
        max_duration_minutes=60,
        project_id="project-a",
    )
    manager.ctx.state.upsert_runbook(runbook)
    scoped = manager._declared_row(str(worktree), runbook.id)
    assert scoped["worktree_path"] == str(worktree.resolve())
    assert scoped["runbook_id"] == runbook.id
    assert scoped["cwd"] == str(worktree.resolve())

    probes: list[int] = []
    monkeypatch.setattr(manager, "_port_active", lambda port: probes.append(port) or False)
    assert len(manager.list_services(include_scopes=True)) == 3
    assert probes == [43123], "one poll probes a shared declared port only once"

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RuntimeServiceError, match="not a known worktree"):
        manager._declared_row(str(outside), None)


def test_runtime_rows_are_isolated_by_project(tmp_path: Path) -> None:
    state = State(tmp_path / "state.db")
    first = RuntimeServiceManager(SimpleNamespace(state=state, selected_project=_project(tmp_path, "one")))
    second_root = tmp_path / "two"
    second_root.mkdir()
    second = RuntimeServiceManager(SimpleNamespace(state=state, selected_project=_project(second_root, "two")))
    state.upsert_runtime_service(first._declared_row())
    state.upsert_runtime_service(second._declared_row())
    assert {row["project_id"] for row in state.list_runtime_services(project_id="one")} == {"one"}
    assert {row["project_id"] for row in state.list_runtime_services(project_id="two")} == {"two"}
    assert state.count_project_rows("one")["runtime_services"] == 1


def test_shutdown_checks_owned_services_across_project_selection(monkeypatch, tmp_path: Path) -> None:
    state = State(tmp_path / "state.db")
    first = RuntimeServiceManager(SimpleNamespace(state=state, selected_project=_project(tmp_path, "one")))
    second_root = tmp_path / "two"
    second_root.mkdir()
    second = RuntimeServiceManager(SimpleNamespace(state=state, selected_project=_project(second_root, "two")))
    state.upsert_runtime_service(first._declared_row())
    state.upsert_runtime_service(second._declared_row())
    monkeypatch.setattr(first, "_ownership_proof", lambda _row: (True, "fixture verified"))
    stopped: list[str] = []
    monkeypatch.setattr(first, "_stop", lambda row, _actor: stopped.append(row["project_id"]) or row)
    assert first.shutdown_owned() is True
    assert set(stopped) == {"one", "two"}


def test_stale_poll_cannot_overwrite_identity_launched_by_concurrent_start(
    monkeypatch, tmp_path: Path
) -> None:
    manager = _manager(tmp_path)
    stale = manager.ctx.state.upsert_runtime_service(manager._declared_row())
    poll_captured = threading.Event()
    release_poll = threading.Event()
    start_attempted = threading.Event()
    launch_entered = threading.Event()
    errors: list[Exception] = []
    original_reconcile = manager._reconcile

    def delayed_reconcile(row: dict[str, object], *, port_active: bool | None = None) -> dict[str, object]:
        assert row["pid"] is None
        poll_captured.set()
        assert release_poll.wait(timeout=2)
        return original_reconcile(row, port_active=port_active)

    def launched(candidate: dict[str, object], actor: str) -> dict[str, object]:
        launch_entered.set()
        updated = dict(candidate)
        updated.update(
            pid=9345,
            process_create_time=700.0,
            process_session_id=9345,
            process_argv=list(candidate["argv"]),
            health="STARTING",
            ownership="OWNED_VERIFIED",
            owner=actor,
        )
        return manager.ctx.state.upsert_runtime_service(updated)

    monkeypatch.setattr(manager, "_reconcile", delayed_reconcile)
    monkeypatch.setattr(manager, "_start", launched)
    monkeypatch.setattr(manager, "_port_active", lambda _port: False)

    def poll() -> None:
        try:
            manager.list_services(include_scopes=False)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - surfaced below
            errors.append(exc)

    def start() -> None:
        start_attempted.set()
        try:
            manager.action("start", "threadpool-start", service_id=stale["id"])
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - surfaced below
            errors.append(exc)

    poll_thread = threading.Thread(target=poll)
    start_thread = threading.Thread(target=start)
    poll_thread.start()
    assert poll_captured.wait(timeout=2)
    start_thread.start()
    assert start_attempted.wait(timeout=2)
    assert not launch_entered.wait(timeout=0.1), "start must wait for stale reconciliation to finish"
    release_poll.set()
    poll_thread.join(timeout=2)
    start_thread.join(timeout=2)

    assert not poll_thread.is_alive() and not start_thread.is_alive()
    assert errors == []
    assert launch_entered.is_set()
    stored = manager.ctx.state.get_runtime_service(stale["id"], project_id=stale["project_id"])
    assert stored is not None and stored["pid"] == 9345
    assert stored["process_create_time"] == 700.0


def test_historical_runbook_with_deleted_worktree_does_not_hide_canonical_service(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.ctx.state.upsert_runbook(
        Runbook(
            id="historical",
            name="historical",
            preset="finish-current-pr",
            objective="test",
            source_ref="ENG-PC-09",
            branch="eng/old",
            worktree=str(tmp_path / "deleted-worktree"),
            parent_worker="codex-build",
            max_duration_minutes=60,
            project_id="project-a",
        )
    )
    services = manager.list_services(include_scopes=True)
    assert len(services) == 1
    assert services[0]["runbook_id"] is None


def test_selected_project_runtime_api_exposes_server_derived_action_state(monkeypatch, tmp_path: Path) -> None:
    state = State(tmp_path / "state.db")
    project = _project(tmp_path)
    state.upsert_project(contract_to_row(project))
    state.set_control_setting("selected_project_id", project.project_id)
    registry = load_registry()
    ctx = CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )
    app = create_app(ctx, roadmap_path=tmp_path / "missing-roadmap.md")
    monkeypatch.setattr(app.state.app_lifecycle, "_port_active", lambda _port: False)
    client = TestClient(app)

    services = client.get("/api/runtime-services", params={"include_scopes": False}).json()
    assert len(services) == 1
    service = services[0]
    assert service["project_id"] == project.project_id
    assert service["actions"] == {"start": True, "stop": False, "restart": False}
    assert service["preview_is_validation_evidence"] is False

    unconfirmed = client.post(f"/api/runtime-services/{service['id']}/stop", json={})
    assert unconfirmed.status_code == 409
    refused = client.post(
        f"/api/runtime-services/{service['id']}/stop",
        json={"confirm": True},
    )
    assert refused.status_code == 409
    assert "no Control-Center-managed" in refused.json()["detail"]
    legacy_refused = client.post("/api/app-lifecycle/stop", json={"confirm": True})
    assert legacy_refused.status_code == 409
    assert "no Control-Center-managed" in legacy_refused.json()["detail"]


def test_generic_and_legacy_stop_apis_preserve_identity_and_signal_refusal_reasons(
    monkeypatch, tmp_path: Path
) -> None:
    state = State(tmp_path / "state.db")
    project = _project(tmp_path)
    state.upsert_project(contract_to_row(project))
    state.set_control_setting("selected_project_id", project.project_id)
    registry = load_registry()
    ctx = CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )
    app = create_app(ctx, roadmap_path=tmp_path / "missing-roadmap.md")
    manager = app.state.app_lifecycle
    row = _persisted(manager)
    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": 999.0,
            "cwd": row["cwd"],
            "argv": row["process_argv"],
            "session_id": row["process_session_id"],
            "alive": True,
        },
    )
    monkeypatch.setattr("os.killpg", lambda *_args: pytest.fail("identity refusal must not signal"))
    client = TestClient(app)

    responses = [
        client.post(
            f"/api/runtime-services/{row['id']}/stop",
            json={"confirm": True},
        ),
        client.post("/api/app-lifecycle/stop", json={"confirm": True}),
    ]
    assert all(response.status_code == 409 for response in responses)
    for response in responses:
        detail = response.json()["detail"]
        assert "PID create time changed" in detail
        assert "no Control-Center-managed" not in detail

    monkeypatch.setattr(
        manager,
        "_process_facts",
        lambda _pid: {
            "create_time": row["process_create_time"],
            "cwd": row["cwd"],
            "argv": row["process_argv"],
            "session_id": row["process_session_id"],
            "alive": True,
        },
    )

    def refuse_signal(*_args: object) -> None:
        raise PermissionError(1, "fixture signal permission denied")

    monkeypatch.setattr("os.killpg", refuse_signal)
    signal_responses = [
        client.post(
            f"/api/runtime-services/{row['id']}/stop",
            json={"confirm": True},
        ),
        client.post("/api/app-lifecycle/stop", json={"confirm": True}),
    ]
    assert all(response.status_code == 409 for response in signal_responses)
    for response in signal_responses:
        detail = response.json()["detail"]
        assert "SIGTERM could not be sent" in detail
        assert "fixture signal permission denied" in detail
        assert "no Control-Center-managed" not in detail
