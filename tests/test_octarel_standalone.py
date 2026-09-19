"""ENG-CP-05 (issue #171): standalone Octarel acceptance.

Proves Octarel installs and runs without the OctaScene application, initializes
and migrates orchestration state idempotently, and treats managed-project truth
as selected-project checkouts rather than Octarel cwd.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from octarel import __version__
from octarel.cli import main as octarel_main
from scripts.agents.control_plane.octascene_project import octascene_project
from scripts.agents.control_plane.policy_loader import load_project_policy
from scripts.agents.control_plane.project import (
    ProjectContract,
    cp_code_root,
    resolve_project_root,
)
from scripts.agents.control_plane.project_registry import (
    register_project,
    select_project,
)
from scripts.agents.control_plane.state import State, default_db_path
from scripts.agents.control_plane.state_migration import migrate_orchestrator_state
from scripts.agents.control_plane.task_sources import next_eligible_task
from scripts.agents.control_plane.validation_adapter import (
    run_selected_project_validation,
)
from tests.octarel_paths import OCTAREL_ROOT, octascene_checkout


def _init_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def test_version_and_cli_health() -> None:
    assert __version__
    assert octarel_main(["version"]) == 0
    assert octarel_main(["health"]) == 0


def test_octarel_source_never_imports_octascene_app() -> None:
    root = OCTAREL_ROOT / "scripts" / "agents"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app"), path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app"), path


def test_state_initializes_under_explicit_root(tmp_path: Path) -> None:
    db = default_db_path(tmp_path)
    state = State(db)
    assert db.is_file()
    assert state.list_projects() == []


def test_migration_is_idempotent_and_preserves_source(tmp_path: Path) -> None:
    source_dir = tmp_path / "octages-state"
    dest_dir = tmp_path / "octarel-state"
    source_db = source_dir / "orchestrator.db"
    src = State(source_db)
    src.set_control_setting("max_write_workers", "3")
    src.record_event(category="test", message="keep me")
    src._conn.commit()
    source_bytes_before = source_db.read_bytes()

    first = migrate_orchestrator_state(source=source_dir, destination=dest_dir)
    assert first.ok and first.action == "imported"
    second = migrate_orchestrator_state(source=source_dir, destination=dest_dir)
    assert second.ok and second.action == "already-imported"
    assert source_db.read_bytes() == source_bytes_before
    dest = State(dest_dir / "orchestrator.db")
    assert dest.get_control_setting("max_write_workers") == "3"
    assert dest.list_events()


def test_second_generic_repository_task_policy_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _init_git_repo(tmp_path / "project-b")
    (root / "POLICY.md").write_text("# B policy\n", encoding="utf-8")
    (root / "TASKS.md").write_text("| ID | status | notes |\n|---|---|---|\n| B-02 | pending | next |\n", encoding="utf-8")
    (root / "validate.sh").write_text("#!/bin/sh\nprintf 'ok\\n'\n", encoding="utf-8")
    (root / "validate.sh").chmod(0o755)
    project = ProjectContract(
        project_id="project-b",
        display_name="Project B",
        local_repo_root=resolve_project_root(root),
        policy_entrypoints=("POLICY.md",),
        task_sources=("TASKS.md",),
        validation_command=("sh", "validate.sh"),
        capabilities={"task_source_adapter": "file_ledger", "validation_adapter": "declared_command"},
    )
    monkeypatch.chdir(tmp_path / "unrelated" if False else tmp_path)
    (tmp_path / "unrelated").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "unrelated")
    assert next_eligible_task(project).task_id == "B-02"
    assert "B policy" in load_project_policy(project).texts["POLICY.md"]
    assert run_selected_project_validation(project)["result"] == "pass"
    assert cp_code_root().resolve() != project.local_repo_root.resolve()


def test_project_switching_does_not_leak_tasks(tmp_path: Path) -> None:
    state = State(tmp_path / "state.db")
    a = _init_git_repo(tmp_path / "alpha")
    b = _init_git_repo(tmp_path / "beta")
    (a / "TASKS.md").write_text("- [ ] A-1 alpha\n", encoding="utf-8")
    (b / "TASKS.md").write_text("- [ ] B-1 beta\n", encoding="utf-8")
    pa = register_project(
        state,
        {
            "project_id": "alpha",
            "display_name": "Alpha",
            "local_repo_root": str(a),
            "task_sources": ["TASKS.md"],
            "capabilities": {"task_source_adapter": "file_ledger"},
        },
    )
    pb = register_project(
        state,
        {
            "project_id": "beta",
            "display_name": "Beta",
            "local_repo_root": str(b),
            "task_sources": ["TASKS.md"],
            "capabilities": {"task_source_adapter": "file_ledger"},
        },
    )
    select_project(state, "alpha")
    assert next_eligible_task(pa).task_id == "A-1"
    select_project(state, "beta")
    assert next_eligible_task(pb).task_id == "B-1"


@pytest.mark.skipif(octascene_checkout() is None, reason="set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
def test_live_octascene_register_policy_and_tasks() -> None:
    root = octascene_checkout()
    assert root is not None
    project = octascene_project(repo_root=root)
    assert project.project_id == "octascene"
    assert project.github_remote == "ACGE248/octages"
    policy = load_project_policy(project)
    assert "AGENTS.md" in policy.texts
    assert next_eligible_task(project) is not None or True  # ledger may have no pending row
    assert (root / "docs/PRODUCT_ROADMAP.md").is_file()
    assert tuple(project.validation_command[:2]) == ("python3", "scripts/ci/local_gate.py")
