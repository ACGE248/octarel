"""CPX-06 dashboard persistence helpers: CLI python, plist, duplicate bind."""

from __future__ import annotations

import plistlib
from pathlib import Path

from scripts.agents.control_plane.service import (
    LABEL,
    _prefer_frameworks_cli,
    install_user_wrapper,
    is_octarel_listener,
    listener_pid,
    refuse_if_foreign_listener,
    render_launchd_plist,
    resolve_cli_python,
)


def test_prefer_frameworks_cli_rewrites_python_app(tmp_path: Path) -> None:
    version = tmp_path / "Python.framework" / "Versions" / "3.14"
    app = version / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    cli = version / "bin" / "python3"
    app.parent.mkdir(parents=True)
    cli.parent.mkdir(parents=True)
    app.write_text("", encoding="utf-8")
    cli.write_text("", encoding="utf-8")
    app.chmod(0o755)
    cli.chmod(0o755)
    assert _prefer_frameworks_cli(app) == cli


def test_resolve_cli_python_uses_venv_bin(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    py = bin_dir / "python3"
    py.write_text("", encoding="utf-8")
    py.chmod(0o755)
    assert resolve_cli_python(tmp_path) == py.resolve()


def test_render_launchd_plist_has_no_placeholder_secrets(tmp_path: Path) -> None:
    blob = render_launchd_plist(code_root=tmp_path, octascene_root=tmp_path / "octages")
    data = plistlib.loads(blob)
    env = data["EnvironmentVariables"]
    assert data["Label"] == LABEL
    assert data["ProgramArguments"][1].endswith("octarel-dashboard-service.sh")
    assert env["OCTAREL_CODE_ROOT"] == str(tmp_path)
    assert "WorkingDirectory" not in data
    assert data["ThrottleInterval"] == 10
    assert "YOUR_ACCESS" not in blob.decode("utf-8", errors="replace")
    assert "OCTAGES_ORCH_REMOTE_AUD" not in env
    copied = render_launchd_plist(
        code_root=tmp_path,
        extra_env={"OCTAGES_ORCH_REMOTE_AUD": "not-a-committed-secret-test-tag", "PATH": "/bin"},
    )
    copied_env = plistlib.loads(copied)["EnvironmentVariables"]
    assert copied_env["OCTAGES_ORCH_REMOTE_AUD"] == "not-a-committed-secret-test-tag"
    assert copied_env["PATH"] == "/bin"


def test_refuse_if_foreign_listener_none_when_port_free() -> None:
    assert refuse_if_foreign_listener(1) is None or listener_pid(1) is not None


def test_is_octarel_listener_false_when_nothing_listens() -> None:
    assert is_octarel_listener(1) is False


def test_render_launchd_plist_honors_state_dir_and_wrapper(tmp_path: Path) -> None:
    wrapper = tmp_path / "octarel-dashboard-service.sh"
    wrapper.write_text("#!/bin/zsh\n", encoding="utf-8")
    state = tmp_path / "isolated-state"
    blob = render_launchd_plist(
        code_root=tmp_path / "code",
        extra_env={"OCTAREL_STATE_DIR": str(state)},
        wrapper_path=wrapper,
        label="com.octarel.cpx06.persist-test",
        port=8879,
        stdout_path=str(tmp_path / "out.log"),
        stderr_path=str(tmp_path / "err.log"),
    )
    data = plistlib.loads(blob)
    assert data["Label"] == "com.octarel.cpx06.persist-test"
    assert data["ProgramArguments"][1] == str(wrapper)
    assert data["EnvironmentVariables"]["OCTAREL_STATE_DIR"] == str(state)
    assert data["EnvironmentVariables"]["OCTAREL_DASHBOARD_PORT"] == "8879"
    assert data["StandardOutPath"] == str(tmp_path / "out.log")


def test_runtime_pid_path_is_on_the_boot_volume_support_dir() -> None:
    from scripts.agents.control_plane.service import runtime_pid_path, user_support_dir

    path = runtime_pid_path()
    assert path.parent == user_support_dir()
    assert path.name == "dashboard.pid"
    assert "Application Support" in str(path)


def test_wrapper_writes_pid_under_application_support() -> None:
    from tests.octarel_paths import OCTAREL_ROOT

    text = (OCTAREL_ROOT / "scripts" / "octarel-dashboard-service.sh").read_text(encoding="utf-8")
    assert "Application Support/Octarel" in text
    assert 'echo $$ > "${support_dir}/dashboard.pid"' in text
    assert "${state_dir}/dashboard.pid" not in text


def test_install_user_wrapper_copies_script(tmp_path: Path, monkeypatch) -> None:
    from scripts.agents.control_plane import service as svc

    monkeypatch.setattr(svc, "user_support_dir", lambda: tmp_path / "support")
    src_dir = tmp_path / "code" / "scripts"
    src_dir.mkdir(parents=True)
    (src_dir / "octarel-dashboard-service.sh").write_text("#!/bin/zsh\necho hi\n", encoding="utf-8")
    dest = install_user_wrapper(tmp_path / "code")
    assert dest.is_file()
    assert dest.stat().st_mode & 0o111
    assert dest.read_text(encoding="utf-8").startswith("#!/bin/zsh")
