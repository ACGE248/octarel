"""ENG-CP-07 (issue #175): project CLI, provider status, canonicalize helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from octarel.cli import main as octarel_main
from scripts.agents.control_plane.project_registry import (
    bootstrap_registry,
    selected_project,
)
from scripts.agents.control_plane.service import (
    canonical_state_dir,
    canonicalize_state_dir,
    state_dir_is_canonical,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.state_migration import migrate_orchestrator_state


def _init_repo(root: Path) -> Path:
    import subprocess

    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_project_cli_add_select_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OCTAREL_OCTASCENE_ROOT", raising=False)
    repo = _init_repo(tmp_path / "proj")
    (repo / "TASKS.md").write_text("| ID | status | notes |\n|---|---|---|\n| T-1 | pending | next |\n", encoding="utf-8")
    other = _init_repo(tmp_path / "other")
    assert octarel_main(["project", "add", "--id", "proj", "--path", str(repo), "--name", "Proj", "--tasks", "TASKS.md"]) == 0
    assert octarel_main(["project", "add", "--id", "other", "--path", str(other), "--name", "Other"]) == 0
    assert octarel_main(["project", "select", "proj"]) == 0
    assert octarel_main(["project", "list"]) == 0
    listed = capsys.readouterr().out
    assert "proj" in listed
    assert octarel_main(["project", "remove", "proj", "--confirm"]) == 0


def test_project_cli_add_declares_the_managed_python_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agents.control_plane.project_registry import get_project
    from scripts.agents.control_plane.state import State as CpState

    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("OCTAREL_OCTASCENE_ROOT", raising=False)
    repo = _init_repo(tmp_path / "proj")
    interpreter = tmp_path / "envs" / "bin" / "python"
    assert octarel_main(["project", "add", "--id", "proj", "--path", str(repo), "--python", str(interpreter)]) == 0
    assert octarel_main(["project", "add", "--id", "plain", "--path", str(_init_repo(tmp_path / "plain"))]) == 0

    state = CpState(tmp_path / "state" / "orchestrator.db")
    assert get_project(state, "proj").capabilities["python_interpreter"] == str(interpreter)
    assert "python_interpreter" not in get_project(state, "plain").capabilities


def test_providers_status_is_local_and_never_enables_billing(capsys: pytest.CaptureFixture[str]) -> None:
    assert octarel_main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "claude-code" in out
    assert "allow_api_billing=false" in out
    assert "deepseek-overflow" in out


def test_canonicalize_copies_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "live"
    dest = tmp_path / "canonical"
    src = State(source / "orchestrator.db")
    src.set_control_setting("max_write_workers", "5")
    src.record_event(category="keep", message="history")
    src._conn.commit()
    before = (source / "orchestrator.db").read_bytes()
    report = canonicalize_state_dir(source=source, destination=dest)
    assert report.ok
    assert (source / "orchestrator.db").read_bytes() == before
    dest_state = State(dest / "orchestrator.db")
    assert dest_state.get_control_setting("max_write_workers") == "5"
    assert state_dir_is_canonical(tmp_path, canonical_state_dir(tmp_path))
    second = migrate_orchestrator_state(source=source, destination=dest)
    assert second.action == "already-imported"


def test_bootstrap_drops_octarel_self_registered_as_octascene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agents.control_plane.project import cp_code_root

    monkeypatch.delenv("OCTAREL_OCTASCENE_ROOT", raising=False)
    state = State(tmp_path / "orchestrator.db")
    state.upsert_project(
        {
            "project_id": "octascene",
            "display_name": "OctaScene",
            "local_repo_root": str(cp_code_root()),
            "default_branch": "main",
            "github_remote": "ACGE248/octages",
            "policy_entrypoints": "[]",
            "roadmap_paths": "[]",
            "task_sources": "[]",
            "validation_command": "[]",
            "capabilities": "{}",
            "enabled": 1,
        }
    )
    state.set_control_setting("selected_project_id", "octascene")
    bootstrap_registry(state, None)
    project = selected_project(state)
    if project is not None:
        assert project.local_repo_root.resolve() != cp_code_root().resolve() or project.project_id != "octascene"
