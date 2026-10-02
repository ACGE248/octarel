"""ENG-PC-09 durable runtime-service safety and isolation."""

from __future__ import annotations

import json
import socket
import subprocess
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
