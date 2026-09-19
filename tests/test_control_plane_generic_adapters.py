"""ENG-CP-04 (issue #169): generic task/policy/validation adapters.

Proves the Control Plane can discover tasks, load policy, and run validation
for an arbitrary second repository without OctaScene-specific code paths, that
OctaScene next-eligible-task and validation declarations remain compatible,
and that process cwd / the Control Plane checkout never silently become the
selected project's truth.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.agents.control_plane.octascene_project import octascene_project
from scripts.agents.control_plane.policy_loader import (
    PolicyLoadError,
    load_project_policy,
)
from scripts.agents.control_plane.project import (
    ProjectContract,
    ProjectValidationError,
    cp_code_root,
    resolve_project_root,
    run_project_validation,
)
from scripts.agents.control_plane.quickstart import (
    list_quickstart_options,
    next_eligible_video_editor_task,
)
from scripts.agents.control_plane.task_sources import (
    ADAPTER_FILE_LEDGER,
    ADAPTER_GITHUB_ISSUES,
    ADAPTER_VIDEO_EDITOR_LEDGER,
    TaskSourceError,
    discover_tasks,
    next_eligible_task,
    parse_file_ledger,
    task_source_adapter_name,
)
from scripts.agents.control_plane.validation_adapter import (
    ADAPTER_DECLARED_COMMAND,
    ADAPTER_EXACT_TREE_LOCAL_GATE,
    ValidationDispatchError,
    run_selected_project_validation,
    validation_adapter_name,
)
from scripts.agents.policy import PolicyError, compose_policy_bundle
from scripts.agents.registry import load_registry
from tests.octarel_paths import OCTAREL_ROOT, octascene_checkout

REPO_ROOT = OCTAREL_ROOT


def _init_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("synthetic repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


def _make_second_repository(root: Path) -> ProjectContract:
    """Deterministic minimal second repository: no AGENTS.md, no OctaScene ledger."""

    _init_git_repo(root)
    (root / "POLICY.md").write_text("# Project B policy\nDo not assume AGENTS.md.\n", encoding="utf-8")
    (root / "TASKS.md").write_text(
        "| ID | status | notes |\n"
        "|---|---|---|\n"
        "| B-01 | complete | already done |\n"
        "| B-02 | pending | generic second-repo task |\n"
        "| B-03 | pending | later |\n",
        encoding="utf-8",
    )
    (root / "validate.sh").write_text("#!/bin/sh\nprintf 'ok from %s\\n' \"$(pwd)\"\n", encoding="utf-8")
    (root / "validate.sh").chmod(0o755)
    subprocess.run(["git", "add", "POLICY.md", "TASKS.md", "validate.sh"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "project-b sources"], cwd=root, check=True)
    return ProjectContract(
        project_id="project-b",
        display_name="Project B",
        local_repo_root=resolve_project_root(root),
        default_branch="main",
        github_remote="example/project-b",
        policy_entrypoints=("POLICY.md",),
        task_sources=("TASKS.md",),
        validation_command=("sh", "validate.sh"),
        capabilities={
            "task_source_adapter": ADAPTER_FILE_LEDGER,
            "validation_adapter": ADAPTER_DECLARED_COMMAND,
        },
    )


def test_file_ledger_adapter_discovers_table_and_checklist_tasks(tmp_path: Path) -> None:
    root = tmp_path / "ledger-repo"
    _init_git_repo(root)
    (root / "TASKS.md").write_text(
        "# Tasks\n\n"
        "| ID | status | notes |\n"
        "|---|---|---|\n"
        "| T-01 | complete | done |\n"
        "| T-02 | pending | next |\n"
        "\n- [ ] C-01 checklist pending\n- [x] C-02 checklist done\n",
        encoding="utf-8",
    )
    project = ProjectContract(
        project_id="ledger",
        display_name="Ledger",
        local_repo_root=root,
        task_sources=("TASKS.md",),
        capabilities={"task_source_adapter": "file_ledger"},
    )
    tasks = discover_tasks(project)
    assert [t.task_id for t in tasks] == ["T-01", "T-02", "C-01", "C-02"]
    assert next_eligible_task(project).task_id == "T-02"


def test_markdown_checklist_alias_uses_file_ledger(tmp_path: Path) -> None:
    root = tmp_path / "alias-repo"
    _init_git_repo(root)
    (root / "TODO.md").write_text("- [ ] ITEM-1 first\n", encoding="utf-8")
    project = ProjectContract(
        project_id="alias",
        display_name="Alias",
        local_repo_root=root,
        task_sources=("TODO.md",),
        capabilities={"task_source_adapter": "markdown_checklist"},
    )
    assert task_source_adapter_name(project) == ADAPTER_FILE_LEDGER
    assert next_eligible_task(project).task_id == "ITEM-1"


def test_github_issues_adapter_uses_injected_fetcher_not_live_api(tmp_path: Path) -> None:
    root = tmp_path / "gh-repo"
    _init_git_repo(root)
    project = ProjectContract(
        project_id="gh",
        display_name="GH",
        local_repo_root=root,
        github_remote="acme/tools",
        capabilities={"task_source_adapter": ADAPTER_GITHUB_ISSUES},
    )

    def fetch(_project: ProjectContract) -> list[dict]:
        return [
            {"number": 12, "title": "First open", "state": "open", "body": "do it"},
            {"number": 13, "title": "Second open", "state": "open", "body": ""},
        ]

    tasks = discover_tasks(project, github_issue_fetcher=fetch)
    assert [t.task_id for t in tasks] == ["#12", "#13"]
    nxt = next_eligible_task(project, github_issue_fetcher=fetch)
    assert nxt is not None
    assert nxt.task_id == "#12"
    assert nxt.source == ADAPTER_GITHUB_ISSUES
    assert nxt.source_ref == "acme/tools#12"


def test_github_issues_adapter_requires_remote(tmp_path: Path) -> None:
    root = tmp_path / "no-remote"
    _init_git_repo(root)
    project = ProjectContract(
        project_id="gh",
        display_name="GH",
        local_repo_root=root,
        capabilities={"task_source_adapter": ADAPTER_GITHUB_ISSUES},
    )
    with pytest.raises(TaskSourceError, match="github_remote"):
        discover_tasks(project, github_issue_fetcher=lambda _p: [])


def test_policy_loader_reads_declared_entrypoints_without_agents_md(tmp_path: Path) -> None:
    project = _make_second_repository(tmp_path / "project-b")
    loaded = load_project_policy(project)
    assert loaded.entrypoints == ("POLICY.md",)
    assert "Do not assume AGENTS.md" in loaded.texts["POLICY.md"]
    assert not (project.local_repo_root / "AGENTS.md").exists()


def test_missing_declared_policy_fails_clearly(tmp_path: Path) -> None:
    root = tmp_path / "missing-policy"
    _init_git_repo(root)
    project = ProjectContract(
        project_id="missing",
        display_name="Missing",
        local_repo_root=root,
        policy_entrypoints=("POLICY.md",),
    )
    with pytest.raises(PolicyLoadError, match="does not exist"):
        load_project_policy(project)


def test_empty_declared_policy_fails_clearly(tmp_path: Path) -> None:
    root = tmp_path / "empty-policy"
    _init_git_repo(root)
    (root / "POLICY.md").write_text("   \n", encoding="utf-8")
    project = ProjectContract(
        project_id="empty",
        display_name="Empty",
        local_repo_root=root,
        policy_entrypoints=("POLICY.md",),
    )
    with pytest.raises(PolicyLoadError, match="empty"):
        load_project_policy(project)


def test_undeclared_policy_entrypoints_fail_clearly(tmp_path: Path) -> None:
    root = tmp_path / "no-policy"
    _init_git_repo(root)
    project = ProjectContract(project_id="none", display_name="None", local_repo_root=root)
    with pytest.raises(PolicyLoadError, match="no policy_entrypoints"):
        load_project_policy(project)


def test_compose_policy_bundle_for_generic_project_does_not_require_agents_md(tmp_path: Path) -> None:
    project = _make_second_repository(tmp_path / "project-b")
    bundle = compose_policy_bundle(
        root=project.local_repo_root,
        registry=load_registry(),
        worker_name="opencode2-gemini-flash-lite",
        route_role="focused-tests",
        project=project,
    )
    assert bundle.manifest["policy_source"] == "selected-project-declared"
    assert bundle.manifest["universal_policy"]["path"] == "POLICY.md"
    assert "--- POLICY: POLICY.md ---" in bundle.prompt
    assert "AGENTS.md" not in bundle.prompt.split("--- POLICY: POLICY.md ---", 1)[0]


def test_compose_policy_bundle_fails_closed_for_invalid_project_policy(tmp_path: Path) -> None:
    root = tmp_path / "broken-policy"
    _init_git_repo(root)
    project = ProjectContract(
        project_id="broken",
        display_name="Broken",
        local_repo_root=root,
        policy_entrypoints=("MISSING.md",),
    )
    with pytest.raises(PolicyError, match="does not exist"):
        compose_policy_bundle(
            root=root,
            registry=load_registry(),
            worker_name="opencode2-gemini-flash-lite",
            route_role="focused-tests",
            project=project,
        )


def test_validation_dispatch_runs_declared_command_from_candidate_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    candidate = tmp_path / "candidate"
    project_root.mkdir()
    candidate.mkdir()
    (candidate / "marker.txt").write_text("candidate\n", encoding="utf-8")
    project = ProjectContract(
        project_id="p",
        display_name="P",
        local_repo_root=project_root,
        validation_command=(
            sys.executable,
            "-c",
            "import os, pathlib, sys; sys.stdout.write(pathlib.Path('marker.txt').read_text() if pathlib.Path('marker.txt').exists() else os.getcwd())",
        ),
        capabilities={"validation_adapter": ADAPTER_DECLARED_COMMAND},
    )
    unrelated = tmp_path / "cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    result = run_selected_project_validation(project, worktree=candidate)
    assert result["result"] == "pass"
    assert "candidate" in result["output"]
    code, output = run_project_validation(project, worktree=candidate)
    assert code == 0
    assert "candidate" in output


def test_validation_dispatch_does_not_fall_back_to_cp_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = ProjectContract(
        project_id="p",
        display_name="P",
        local_repo_root=project_root,
        validation_command=(sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"),
    )
    unrelated = tmp_path / "other-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    _code, output = run_project_validation(project)
    assert Path(output).resolve() == project_root.resolve()
    assert Path(output).resolve() != unrelated.resolve()


def test_explicit_missing_worktree_does_not_fall_back_to_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = ProjectContract(
        project_id="p",
        display_name="P",
        local_repo_root=project_root,
        validation_command=("true",),
    )
    with pytest.raises(ProjectValidationError, match="validation target"):
        run_project_validation(project, worktree=tmp_path / "missing-candidate")


def test_generic_second_repository_task_policy_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_second_repository(tmp_path / "project-b")
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    nxt = next_eligible_task(project)
    assert nxt is not None
    assert nxt.task_id == "B-02"
    assert nxt.source == ADAPTER_FILE_LEDGER
    loaded = load_project_policy(project)
    assert "POLICY.md" in loaded.texts
    result = run_selected_project_validation(project)
    assert result["result"] == "pass"
    assert str(project.local_repo_root) in result["output"]
    options = list_quickstart_options(project.local_repo_root, project=project)
    assert options[0]["key"] == "continue-next-task"
    assert options[0]["task_id"] == "B-02"
    assert all(item["key"] != "continue-video-editor" for item in options)


def test_octascene_compatibility_next_eligible_matches_current_ledger() -> None:
    root = octascene_checkout()
    if root is None:
        pytest.skip("set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
    project = octascene_project(repo_root=root)
    assert task_source_adapter_name(project) == ADAPTER_VIDEO_EDITOR_LEDGER
    assert validation_adapter_name(project) == ADAPTER_EXACT_TREE_LOCAL_GATE
    direct = next_eligible_video_editor_task(root)
    via_adapter = next_eligible_task(project)
    if direct is None:
        assert via_adapter is None
    else:
        assert via_adapter is not None
        assert via_adapter.task_id == direct.task_id
        assert via_adapter.status == direct.status
        assert via_adapter.notes == direct.notes
    options = list_quickstart_options(root, project=project)
    assert options[0]["key"] == "continue-video-editor"


def test_switching_back_to_octascene_does_not_leak_second_repo(tmp_path: Path) -> None:
    live = octascene_checkout()
    if live is None:
        pytest.skip("set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
    second = _make_second_repository(tmp_path / "project-b")
    octascene = octascene_project(repo_root=live)
    b_task = next_eligible_task(second)
    o_task = next_eligible_task(octascene)
    assert b_task is not None and b_task.task_id == "B-02"
    direct = next_eligible_video_editor_task(live)
    if direct is None:
        assert o_task is None
    else:
        assert o_task is not None
        assert o_task.task_id == direct.task_id
        assert o_task.task_id != b_task.task_id
    b_policy = load_project_policy(second)
    o_policy = load_project_policy(octascene)
    assert "POLICY.md" in b_policy.texts
    assert "AGENTS.md" in o_policy.texts
    assert "Do not assume AGENTS.md" not in o_policy.texts["AGENTS.md"]


def test_project_isolation_no_cross_project_task_or_policy_leakage(tmp_path: Path) -> None:
    a_root = tmp_path / "alpha"
    b_root = tmp_path / "beta"
    _init_git_repo(a_root)
    _init_git_repo(b_root)
    (a_root / "POLICY.md").write_text("alpha policy SECRET_A\n", encoding="utf-8")
    (b_root / "POLICY.md").write_text("beta policy SECRET_B\n", encoding="utf-8")
    (a_root / "TASKS.md").write_text("- [ ] A-99 alpha only\n", encoding="utf-8")
    (b_root / "TASKS.md").write_text("- [ ] B-99 beta only\n", encoding="utf-8")
    project_a = ProjectContract(
        project_id="alpha",
        display_name="Alpha",
        local_repo_root=a_root,
        policy_entrypoints=("POLICY.md",),
        task_sources=("TASKS.md",),
        capabilities={"task_source_adapter": "file_ledger"},
    )
    project_b = ProjectContract(
        project_id="beta",
        display_name="Beta",
        local_repo_root=b_root,
        policy_entrypoints=("POLICY.md",),
        task_sources=("TASKS.md",),
        capabilities={"task_source_adapter": "file_ledger"},
    )
    assert next_eligible_task(project_a).task_id == "A-99"
    assert next_eligible_task(project_b).task_id == "B-99"
    a_policy = load_project_policy(project_a)
    b_policy = load_project_policy(project_b)
    assert "SECRET_A" in a_policy.texts["POLICY.md"]
    assert "SECRET_B" not in a_policy.texts["POLICY.md"]
    assert "SECRET_B" in b_policy.texts["POLICY.md"]
    assert "SECRET_A" not in b_policy.texts["POLICY.md"]


def test_no_cp_cwd_fallback_for_task_or_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_second_repository(tmp_path / "project-b")
    decoy = tmp_path / "decoy-cwd"
    _init_git_repo(decoy)
    (decoy / "TASKS.md").write_text("- [ ] DECOY-1 should never be selected\n", encoding="utf-8")
    (decoy / "POLICY.md").write_text("decoy policy\n", encoding="utf-8")
    monkeypatch.chdir(decoy)
    assert next_eligible_task(project).task_id == "B-02"
    loaded = load_project_policy(project)
    assert "Do not assume AGENTS.md" in loaded.texts["POLICY.md"]
    assert "decoy policy" not in loaded.texts["POLICY.md"]
    assert project.local_repo_root.resolve() != Path.cwd().resolve()
    assert cp_code_root().resolve() != project.local_repo_root.resolve()


def test_parse_file_ledger_skips_prose_and_separators() -> None:
    text = (
        "# Heading | not a table\n"
        "| ID | status | notes |\n"
        "|---|---|---|\n"
        "| K-01 | pending | keep |\n"
        "mentions | pipes | in prose\n"
    )
    tasks = parse_file_ledger(text, source_ref="TASKS.md")
    assert [t.task_id for t in tasks] == ["K-01"]


def test_unknown_task_source_adapter_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "unknown"
    _init_git_repo(root)
    project = ProjectContract(
        project_id="u",
        display_name="U",
        local_repo_root=root,
        capabilities={"task_source_adapter": "linear"},
    )
    with pytest.raises(TaskSourceError, match="unknown task_source_adapter"):
        discover_tasks(project)


def test_unknown_validation_adapter_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "unknown-val"
    root.mkdir()
    project = ProjectContract(
        project_id="u",
        display_name="U",
        local_repo_root=root,
        validation_command=("true",),
        capabilities={"validation_adapter": "not-a-real-adapter"},
    )
    with pytest.raises(ValidationDispatchError, match="unknown validation_adapter"):
        run_selected_project_validation(project)
