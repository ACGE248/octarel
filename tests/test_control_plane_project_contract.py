"""ENG-CP-01 (issue #165): generic managed-project/repository contract.

Proves the extraction seam CPX-01 establishes: a standalone Control Plane
must be able to select a repository as a project and read that repository's
own policy/roadmap/task/validation declarations, without the Control Plane's
own code checkout or process ``cwd`` ever silently becoming the source of
selected-project truth. Also proves current OctaScene "next eligible task"
behavior is unchanged and that a minimal synthetic second project can be
represented with no OctaScene-specific assumptions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.agents.control_plane.octascene_project import (
    OCTASCENE_TASK_SOURCES,
    octascene_project,
    present_policy_entrypoints,
    present_roadmap_paths,
    present_task_sources,
)
from scripts.agents.control_plane.project import (
    ProjectContract,
    ProjectRootError,
    cp_code_root,
    resolve_project_root,
)
from scripts.agents.control_plane.quickstart import (
    VIDEO_EDITOR_LEDGER_RELATIVE,
    next_eligible_video_editor_task,
)


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("synthetic repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _this_octages_checkout() -> Path:
    from tests.octarel_paths import octascene_checkout

    live = octascene_checkout()
    if live is not None:
        return live
    pytest.skip("set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")


def test_cp_code_checkout_can_differ_from_selected_project_checkout(tmp_path: Path) -> None:
    synthetic = tmp_path / "other-repo"
    _init_git_repo(synthetic)

    project = ProjectContract(
        project_id="synthetic",
        display_name="Synthetic",
        local_repo_root=resolve_project_root(synthetic),
    )

    code_root = cp_code_root()
    assert project.local_repo_root.resolve() != code_root.resolve()


def test_selected_repository_truth_is_never_derived_from_cp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic = tmp_path / "cwd-independent-repo"
    _init_git_repo(synthetic)

    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    resolved = resolve_project_root(synthetic)
    assert resolved.resolve() == synthetic.resolve()

    # octascene_project(repo_root=...) must resolve the explicit root, never
    # whatever repository this process's cwd happens to sit inside.
    octascene_checkout = _this_octages_checkout()
    monkeypatch.chdir(unrelated_cwd)
    project = octascene_project(repo_root=octascene_checkout)
    assert project.local_repo_root.resolve() == octascene_checkout.resolve()


def test_octascene_adapter_resolves_current_policy_roadmap_and_task_sources() -> None:
    root = _this_octages_checkout()
    project = octascene_project(repo_root=root)

    assert project.project_id == "octascene"
    assert project.github_remote == "ACGE248/octages"
    assert "AGENTS.md" in present_policy_entrypoints(project)
    assert "docs/PRODUCT_ROADMAP.md" in present_roadmap_paths(project)
    assert str(VIDEO_EDITOR_LEDGER_RELATIVE) in present_task_sources(project)
    assert tuple(project.task_sources) == OCTASCENE_TASK_SOURCES
    assert project.validation_command[:2] == ("python3", "scripts/ci/local_gate.py")


def test_octascene_next_task_behavior_is_unchanged_through_the_adapter() -> None:
    root = _this_octages_checkout()
    project = octascene_project(repo_root=root)

    direct = next_eligible_video_editor_task(root)
    via_project = next_eligible_video_editor_task(project.local_repo_root)

    assert via_project == direct


def test_synthetic_second_repository_is_representable_without_octascene_assumptions(tmp_path: Path) -> None:
    synthetic = tmp_path / "project-b"
    _init_git_repo(synthetic)
    (synthetic / "TASKS.md").write_text("- [ ] first task\n", encoding="utf-8")
    subprocess.run(["git", "add", "TASKS.md"], cwd=synthetic, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add tasks"], cwd=synthetic, check=True)

    project = ProjectContract(
        project_id="project-b",
        display_name="Project B",
        local_repo_root=resolve_project_root(synthetic),
        default_branch="main",
        github_remote=None,
        policy_entrypoints=("CONTRIBUTING.md",),
        roadmap_paths=(),
        task_sources=("TASKS.md",),
        validation_command=("make", "test"),
        capabilities={"task_source_adapter": "markdown_checklist"},
    )

    as_dict = project.as_dict()
    assert as_dict["project_id"] == "project-b"
    assert as_dict["task_sources"] == ["TASKS.md"]
    # The generic contract has no field, default, or import referencing
    # OctaScene/Video-Editor concepts.
    assert "octascene" not in repr(project).lower()
    assert "video_editor" not in repr(project).lower()


@pytest.mark.parametrize(
    "candidate_factory",
    [
        lambda tmp_path: tmp_path / "does-not-exist",
        lambda tmp_path: _make_plain_dir(tmp_path / "not-a-git-repo"),
    ],
)
def test_invalid_or_non_git_roots_fail_safely(tmp_path: Path, candidate_factory) -> None:
    candidate = candidate_factory(tmp_path)
    with pytest.raises(ProjectRootError):
        resolve_project_root(candidate)


def test_outside_or_traversal_style_root_fails_safely(tmp_path: Path) -> None:
    with pytest.raises(ProjectRootError):
        resolve_project_root(tmp_path / ".." / ".." / "nonexistent-root-xyz")


def _make_plain_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_project_contract_carries_declarations_not_file_contents(tmp_path: Path) -> None:
    synthetic = tmp_path / "secret-bearing-repo"
    _init_git_repo(synthetic)
    secret_path = synthetic / ".env"
    secret_marker = "SUPER_SECRET_TOKEN_VALUE_12345"
    secret_path.write_text(f"TOKEN={secret_marker}\n", encoding="utf-8")

    project = ProjectContract(
        project_id="secret-bearing",
        display_name="Secret Bearing",
        local_repo_root=resolve_project_root(synthetic),
        policy_entrypoints=(".env",),
    )

    serialized = project.as_dict()
    for value in serialized.values():
        assert secret_marker not in repr(value)
    assert secret_marker not in repr(project)


def test_selected_project_is_valid_even_when_cp_env_points_elsewhere(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated-canonical-root"
    _init_git_repo(unrelated)
    monkeypatch.setenv("OCTAGES_ORCH_CANONICAL_REPO_ROOT", str(unrelated))

    octascene_checkout = _this_octages_checkout()
    project = octascene_project(repo_root=octascene_checkout)

    assert project.local_repo_root.resolve() == octascene_checkout.resolve()
    assert project.local_repo_root.resolve() != unrelated.resolve()
