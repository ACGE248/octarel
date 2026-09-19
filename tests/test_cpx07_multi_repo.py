"""ENG-CP-07 (issue #175): two-repository acceptance without OctaScene code paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from octarel.cli import main as octarel_main
from scripts.agents.control_plane.policy_loader import load_project_policy
from scripts.agents.control_plane.project import cp_code_root
from scripts.agents.control_plane.project_registry import (
    register_project,
    remove_project,
    select_project,
    selected_project,
)
from scripts.agents.control_plane.recovery import discover_git_worktrees
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.task_sources import next_eligible_task
from scripts.agents.control_plane.validation_adapter import (
    run_selected_project_validation,
)
from tests.octarel_paths import octascene_checkout


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_generic_repo(root: Path, *, task_id: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "POLICY.md").write_text(f"# {root.name} policy\n", encoding="utf-8")
    (root / "TASKS.md").write_text(
        f"| ID | status | notes |\n|---|---|---|\n| {task_id} | pending | next |\n",
        encoding="utf-8",
    )
    (root / "validate.sh").write_text("#!/bin/sh\nprintf 'ok\\n'\n", encoding="utf-8")
    (root / "validate.sh").chmod(0o755)
    _git(root, "add", "POLICY.md", "TASKS.md", "validate.sh")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_generic_and_second_repo_switch_without_octascene_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OCTAREL_OCTASCENE_ROOT", raising=False)
    monkeypatch.delenv("OCTAGES_ORCH_CANONICAL_REPO_ROOT", raising=False)
    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    state = State(tmp_path / "state" / "orchestrator.db")
    alpha = _init_generic_repo(tmp_path / "alpha", task_id="A-1")
    beta = _init_generic_repo(tmp_path / "beta", task_id="B-1")
    extra = tmp_path / "alpha-wt"
    _git(alpha, "worktree", "add", "-q", str(extra), "HEAD")
    _git(alpha, "branch", "feature-a")

    pa = register_project(
        state,
        {
            "project_id": "alpha",
            "display_name": "Alpha",
            "local_repo_root": str(alpha),
            "policy_entrypoints": ["POLICY.md"],
            "task_sources": ["TASKS.md"],
            "validation_command": ["sh", "validate.sh"],
            "capabilities": {
                "task_source_adapter": "file_ledger",
                "validation_adapter": "declared_command",
            },
        },
    )
    pb = register_project(
        state,
        {
            "project_id": "beta",
            "display_name": "Beta",
            "local_repo_root": str(beta),
            "policy_entrypoints": ["POLICY.md"],
            "task_sources": ["TASKS.md"],
            "validation_command": ["sh", "validate.sh"],
            "capabilities": {
                "task_source_adapter": "file_ledger",
                "validation_adapter": "declared_command",
            },
        },
    )
    select_project(state, "alpha")
    assert next_eligible_task(pa).task_id == "A-1"
    assert "alpha policy" in load_project_policy(pa).texts["POLICY.md"]
    assert run_selected_project_validation(pa)["result"] == "pass"
    trees = discover_git_worktrees(alpha)
    assert len(trees) >= 2
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=alpha,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "feature-a" in branches
    state.record_event(category="test", message="alpha-only", project_id="alpha")
    state.record_event(category="test", message="beta-only", project_id="beta")
    alpha_events = [e.message for e in state.list_events(project_id="alpha")]
    beta_events = [e.message for e in state.list_events(project_id="beta")]
    assert "alpha-only" in alpha_events
    assert "beta-only" not in alpha_events
    assert "beta-only" in beta_events
    assert "alpha-only" not in beta_events

    select_project(state, "beta")
    assert selected_project(state).project_id == "beta"
    assert next_eligible_task(pb).task_id == "B-1"
    assert run_selected_project_validation(pb)["result"] == "pass"
    assert cp_code_root().resolve() != pb.local_repo_root.resolve()
    assert cp_code_root().resolve() != pa.local_repo_root.resolve()

    remove_project(state, "beta", confirm=True)
    assert state.get_project("beta") is None
    assert (beta / "TASKS.md").is_file()
    readded = register_project(
        state,
        {
            "project_id": "beta",
            "display_name": "Beta",
            "local_repo_root": str(beta),
            "task_sources": ["TASKS.md"],
            "capabilities": {"task_source_adapter": "file_ledger"},
        },
    )
    select_project(state, "beta")
    assert next_eligible_task(readded).task_id == "B-1"


@pytest.mark.skipif(octascene_checkout() is None, reason="set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
def test_octascene_and_generic_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.agents.control_plane.octascene_project import octascene_project
    from scripts.agents.control_plane.policy_loader import (
        load_project_policy as load_policy,
    )

    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path / "state"))
    state = State(tmp_path / "state" / "orchestrator.db")
    oct_root = octascene_checkout()
    assert oct_root is not None
    generic = _init_generic_repo(tmp_path / "generic", task_id="G-01")
    oct = octascene_project(repo_root=oct_root)
    register_project(
        state,
        {
            "project_id": oct.project_id,
            "display_name": oct.display_name,
            "local_repo_root": str(oct.local_repo_root),
            "github_remote": oct.github_remote,
            "policy_entrypoints": list(oct.policy_entrypoints),
            "roadmap_paths": list(oct.roadmap_paths),
            "task_sources": list(oct.task_sources),
            "validation_command": list(oct.validation_command),
            "capabilities": dict(oct.capabilities),
        },
    )
    generic_contract = register_project(
        state,
        {
            "project_id": "generic",
            "display_name": "Generic",
            "local_repo_root": str(generic),
            "policy_entrypoints": ["POLICY.md"],
            "task_sources": ["TASKS.md"],
            "validation_command": ["sh", "validate.sh"],
            "capabilities": {
                "task_source_adapter": "file_ledger",
                "validation_adapter": "declared_command",
            },
        },
    )
    select_project(state, "octascene")
    policy = load_policy(oct)
    assert "AGENTS.md" in policy.texts
    assert (oct.local_repo_root / "docs/PRODUCT_ROADMAP.md").is_file()
    select_project(state, "generic")
    assert next_eligible_task(generic_contract).task_id == "G-01"
    assert "AGENTS.md" not in load_policy(generic_contract).texts
    assert octarel_main(["project", "list"]) == 0
