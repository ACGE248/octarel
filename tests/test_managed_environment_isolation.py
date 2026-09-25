"""ENG-AO-09 (issue #21): managed-project Python environment isolation.

The real failure: Octarel ran from its own ``.venv`` while the daemon dispatched OctaScene's exact-tree gate for a
sibling worktree. The gate inherited Octarel's interpreter and died on ``ModuleNotFoundError: No module named
'PIL'``, a module only the managed project's environment has.

These tests reproduce that with real virtual environments: an "Octarel venv" that is *active* (``sys.prefix``,
``VIRTUAL_ENV`` and ``PATH`` all point at it) and a managed project whose own ``.venv`` alone provides
``managedonly`` (the PIL stand-in). Nothing is mocked at the subprocess boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from scripts.agents.control_plane import managed_environment as me
from scripts.agents.control_plane import provisioning
from scripts.agents.control_plane.acceptance import _managed_environment_evidence
from scripts.agents.control_plane.managed_environment import (
    ManagedEnvironmentError,
    isolated_environment,
    resolve_managed_environment,
)
from scripts.agents.control_plane.models import Task
from scripts.agents.control_plane.project import (
    ProjectContract,
    ProjectValidationError,
    run_project_validation,
)
from scripts.agents.control_plane.project_registry import register_project
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.validation_adapter import (
    ADAPTER_DECLARED_COMMAND,
    ADAPTER_EXACT_TREE_LOCAL_GATE,
    run_selected_project_validation,
)
from scripts.agents.registry import load_registry

# Stand-in for the managed project's third-party dependency (PIL in the VE-BRIDGE-02 incident).
MANAGED_ONLY = "managedonly"

FAKE_GATE = """\
import json, os, pathlib, sys
pathlib.Path(os.environ["GATE_REPORT"]).write_text(json.dumps({
    "executable": sys.executable,
    "prefix": sys.prefix,
    "virtual_env": os.environ.get("VIRTUAL_ENV"),
    "path_head": os.environ["PATH"].split(os.pathsep)[0],
    "argv": sys.argv[1:],
}))
import managedonly  # noqa: F401  -- the PIL stand-in: only the managed project's venv has it
"""


def _site_packages(python: Path) -> Path:
    out = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def _make_venv(path: Path, *, modules: tuple[str, ...]) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=True).create(path)
    python = path / "bin" / "python"
    site = _site_packages(python)
    for name in modules:
        (site / f"{name}.py").write_text("", encoding="utf-8")
    return python


@pytest.fixture(scope="module")
def venvs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("venvs")
    return {
        # Octarel's own environment: has the gate's tools but NOT the managed project's dependency.
        "octarel": _make_venv(root / "octarel-venv", modules=("pytest", "ruff")),
        "managed": _make_venv(root / "managed-venv", modules=("pytest", "ruff", MANAGED_ONLY)),
        "no_ruff": _make_venv(root / "no-ruff-venv", modules=("pytest", MANAGED_ONLY)),
    }


@pytest.fixture
def octarel_active(monkeypatch: pytest.MonkeyPatch, venvs: dict[str, Path]) -> Path:
    """Simulate Octarel launched from its own ``.venv``: prefix, VIRTUAL_ENV and PATH all name it."""

    home = venvs["octarel"].parent.parent
    monkeypatch.setattr(sys, "prefix", str(home))
    monkeypatch.setenv("VIRTUAL_ENV", str(home))
    monkeypatch.setenv("PATH", f"{home / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(home / "leak"))
    return home


@pytest.fixture
def gate_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    report = tmp_path / "gate-report.json"
    monkeypatch.setenv("GATE_REPORT", str(report))
    return report


def _project(tmp_path: Path, venvs: dict[str, Path], *, venv_key: str | None = "managed", **capabilities: str):
    root = tmp_path / "ManagedProject"
    root.mkdir()
    if venv_key is not None:
        (root / ".venv").symlink_to(venvs[venv_key].parent.parent)
    return ProjectContract(
        project_id="managed-app",
        display_name="Managed App",
        local_repo_root=root,
        validation_command=("python3", "scripts/ci/local_gate.py", "--docs-reviewed"),
        capabilities={"validation_adapter": ADAPTER_EXACT_TREE_LOCAL_GATE, **capabilities},
    )


def _fresh_sibling_worktree(project: ProjectContract) -> Path:
    """A newly created sibling worktree: the gate script exists, there is no ``.venv`` of any kind."""

    worktree = project.local_repo_root.parent / "ManagedProject-ve-bridge-02"
    (worktree / "scripts" / "ci").mkdir(parents=True)
    (worktree / "scripts" / "ci" / "local_gate.py").write_text(FAKE_GATE, encoding="utf-8")
    return worktree


def _tree_listing(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# --------------------------------------------------------------------------- the reproduction


def test_ambient_octarel_venv_cannot_import_the_managed_dependency(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    """Control: this is the pre-fix behaviour -- the ambient ``python3`` is Octarel's and lacks the module."""

    worktree = _fresh_sibling_worktree(_project(tmp_path, venvs))
    ambient = subprocess.run(
        ["python3", "-c", f"import {MANAGED_ONLY}"], cwd=worktree, capture_output=True, text=True, check=False
    )
    assert ambient.returncode != 0
    assert "ModuleNotFoundError" in ambient.stderr


def test_exact_tree_gate_runs_a_fresh_sibling_worktree_under_the_managed_project_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs)
    worktree = _fresh_sibling_worktree(project)
    before = _tree_listing(worktree)
    assert not (worktree / ".venv").exists() and not (worktree / ".venv").is_symlink()

    result = run_selected_project_validation(
        project, worktree=worktree, gate_kwargs={"docs_reviewed": True, "review_provider": "Google"}
    )

    assert result["result"] == "pass", result["output"]
    report = json.loads(gate_report.read_text(encoding="utf-8"))
    managed_python = project.local_repo_root / ".venv" / "bin" / "python"
    # The test's project ``.venv`` is a link to the real venv; Python reports a linked venv by its real path.
    assert Path(report["executable"]).resolve().parent == managed_python.resolve().parent
    assert Path(report["executable"]).name == "python"
    assert Path(report["prefix"]).resolve() == venvs["managed"].parent.parent.resolve()
    assert Path(report["virtual_env"]).resolve() == venvs["managed"].parent.parent.resolve()
    assert Path(report["path_head"]) == managed_python.parent
    assert "--independent-review-provider" in report["argv"]
    evidence = result["managed_environment"]
    assert evidence["source"] == me.SOURCE_PROJECT
    assert evidence["interpreter"] == str(managed_python)
    assert evidence["required_modules"] == {"pytest": True, "ruff": True}
    assert result["python"] == str(managed_python)
    # Source-tree evidence is untouched: no .venv link (or anything else) was written into the worktree.
    assert _tree_listing(worktree) == before
    assert not (worktree / ".venv").exists() and not (worktree / ".venv").is_symlink()


def test_octarel_active_environment_never_reaches_the_gate_child(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs)
    worktree = _fresh_sibling_worktree(project)

    run_selected_project_validation(project, worktree=worktree)

    report = json.loads(gate_report.read_text(encoding="utf-8"))
    assert str(octarel_active) not in report["prefix"]
    assert str(octarel_active / "bin") != report["path_head"]


def test_worktree_local_environment_is_preferred_over_the_project_canonical_one(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key=None)
    worktree = _fresh_sibling_worktree(project)
    (worktree / ".venv").symlink_to(venvs["managed"].parent.parent)

    result = run_selected_project_validation(project, worktree=worktree)

    assert result["result"] == "pass", result["output"]
    assert result["managed_environment"]["source"] == me.SOURCE_WORKTREE
    assert result["python"] == str(worktree / ".venv" / "bin" / "python")


def test_declared_interpreter_capability_selects_the_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(
        tmp_path, venvs, venv_key=None, python_interpreter=str(venvs["managed"])
    )
    worktree = _fresh_sibling_worktree(project)

    result = run_selected_project_validation(project, worktree=worktree)

    assert result["result"] == "pass", result["output"]
    assert result["managed_environment"]["source"] == me.SOURCE_DECLARED


# --------------------------------------------------------------------------- fail closed


def _assert_gate_not_started(result: dict, gate_report: Path) -> None:
    assert result["result"] == "fail"
    assert result["exit_code"] is None
    assert result["python"] is None
    assert not gate_report.exists(), "the gate must not run when the managed environment is unusable"
    assert result["prerequisite_failures"] and result["prerequisite_failures"][0].startswith(
        "managed-project Python environment:"
    )
    assert result["managed_environment"]["status"] == "UNAVAILABLE"


def test_missing_environment_fails_closed_and_never_falls_back_to_octarel(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key=None)
    worktree = _fresh_sibling_worktree(project)

    result = run_selected_project_validation(project, worktree=worktree)

    _assert_gate_not_started(result, gate_report)
    reason = result["prerequisite_failures"][0]
    assert "no Python environment found" in reason
    assert str(project.local_repo_root / ".venv" / "bin" / "python") in reason
    assert "never used as a fallback" in reason
    assert [s["source"] for s in result["managed_environment"]["searched"]] == [me.SOURCE_WORKTREE, me.SOURCE_PROJECT]


def test_environment_missing_a_required_module_fails_closed_with_the_module_named(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key="no_ruff")
    worktree = _fresh_sibling_worktree(project)

    result = run_selected_project_validation(project, worktree=worktree)

    _assert_gate_not_started(result, gate_report)
    assert "missing required module(s): ruff" in result["prerequisite_failures"][0]
    assert result["managed_environment"]["required_modules"] == {"pytest": True, "ruff": False}
    assert result["managed_environment"]["interpreter"].endswith(".venv/bin/python")


def test_declared_interpreter_that_is_missing_does_not_fall_through_to_the_canonical_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key="managed", python_interpreter=str(tmp_path / "nope" / "python"))
    worktree = _fresh_sibling_worktree(project)

    result = run_selected_project_validation(project, worktree=worktree)

    _assert_gate_not_started(result, gate_report)
    assert "declared managed-project Python interpreter does not exist" in result["prerequisite_failures"][0]
    assert result["managed_environment"]["source"] == me.SOURCE_DECLARED
    assert result["managed_environment"]["searched"] == [
        {"source": me.SOURCE_DECLARED, "path": str(tmp_path / "nope" / "python")}
    ]


def test_dangling_worktree_environment_link_is_an_inconsistency_not_something_to_skip(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key="managed")
    worktree = _fresh_sibling_worktree(project)
    (worktree / ".venv").symlink_to(tmp_path / "moved-away-venv")

    result = run_selected_project_validation(project, worktree=worktree)

    _assert_gate_not_started(result, gate_report)
    assert "present but not an executable file" in result["prerequisite_failures"][0]
    assert result["managed_environment"]["source"] == me.SOURCE_WORKTREE


def test_managed_interpreter_that_is_octarels_own_active_environment_is_rejected(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, gate_report: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key="octarel")
    worktree = _fresh_sibling_worktree(project)

    result = run_selected_project_validation(project, worktree=worktree)

    _assert_gate_not_started(result, gate_report)
    assert "Octarel's own active environment" in result["prerequisite_failures"][0]


def test_octarel_managing_its_own_checkout_may_use_its_own_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, venvs, venv_key="octarel")
    monkeypatch.setattr(me, "cp_code_root", lambda: project.local_repo_root)

    environment = resolve_managed_environment(project, required_modules=("pytest",))

    assert environment.interpreter == project.local_repo_root / ".venv" / "bin" / "python"


def test_unrunnable_interpreter_fails_its_probe(tmp_path: Path, venvs: dict[str, Path], octarel_active: Path) -> None:
    project = _project(tmp_path, venvs, venv_key=None)
    fake = tmp_path / "fake-python"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    project = ProjectContract(
        project_id=project.project_id, display_name="x", local_repo_root=project.local_repo_root,
        capabilities={"python_interpreter": str(fake)},
    )

    with pytest.raises(ManagedEnvironmentError) as raised:
        resolve_managed_environment(project)

    assert raised.value.kind == me.KIND_INCONSISTENT
    assert "failed its health probe (exit 3)" in raised.value.reason
    assert "boom" in raised.value.reason


# --------------------------------------------------------------------------- environment construction


def test_isolated_environment_removes_octarel_environment_and_leads_with_the_managed_one(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _project(tmp_path, venvs)
    environment = resolve_managed_environment(project)
    base = {"PATH": os.pathsep.join([str(octarel_active / "bin"), "/usr/bin", "/bin"]), "HOME": "/h",
            "VIRTUAL_ENV": str(octarel_active), "PYTHONPATH": "x", "PYTHONHOME": "y", "KEEP": "1"}

    env = isolated_environment(base, environment)

    assert env["PATH"].split(os.pathsep) == [str(environment.bin_dir), "/usr/bin", "/bin"]
    assert Path(env["VIRTUAL_ENV"]).resolve() == venvs["managed"].parent.parent.resolve()
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["HOME"] == "/h" and env["KEEP"] == "1"


def test_isolated_environment_without_a_managed_environment_only_removes_octarels(octarel_active: Path) -> None:
    base = {"PATH": os.pathsep.join([str(octarel_active / "bin"), "/usr/bin"]), "VIRTUAL_ENV": str(octarel_active)}

    env = isolated_environment(base, None)

    assert env["PATH"] == "/usr/bin"
    assert "VIRTUAL_ENV" not in env


# --------------------------------------------------------------------------- declared-command projects


def _declared_project(tmp_path: Path, venvs: dict[str, Path], command: tuple[str, ...], **kw) -> ProjectContract:
    project = _project(tmp_path, venvs, **kw)
    return ProjectContract(
        project_id=project.project_id, display_name="Managed App", local_repo_root=project.local_repo_root,
        validation_command=command,
        capabilities={**project.capabilities, "validation_adapter": ADAPTER_DECLARED_COMMAND},
    )


def test_declared_validation_command_resolves_python_inside_the_managed_project(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _declared_project(tmp_path, venvs, ("python3", "-c", f"import {MANAGED_ONLY}; print('ok')"))

    code, output = run_project_validation(project, worktree=project.local_repo_root)

    assert (code, output.strip()) == (0, "ok")


def test_declared_validation_command_without_any_project_environment_never_gets_octarels(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _declared_project(
        tmp_path, venvs, ("python3", "-c", "import sys; print(sys.prefix)"), venv_key=None
    )

    code, output = run_project_validation(project, worktree=project.local_repo_root)

    assert code == 0
    assert Path(output.strip()).resolve() != octarel_active.resolve()


def test_declared_validation_command_with_a_broken_declared_environment_is_not_started(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    marker = tmp_path / "ran"
    project = _declared_project(
        tmp_path, venvs, (sys.executable, "-c", f"open({str(marker)!r}, 'w')"),
        venv_key=None, python_interpreter=str(tmp_path / "missing" / "python"),
    )

    with pytest.raises(ProjectValidationError, match="declared managed-project Python interpreter does not exist"):
        run_project_validation(project, worktree=project.local_repo_root)
    assert not marker.exists()


# --------------------------------------------------------------------------- worker launch + readiness + evidence


def _supervisor_with_project(tmp_path: Path, venvs: dict[str, Path], **kw) -> tuple[Supervisor, Task, ProjectContract]:
    project = _project(tmp_path, venvs, **kw)
    subprocess.run(["git", "init", "-q", str(project.local_repo_root)], check=True)
    state = State(":memory:")
    register_project(
        state,
        {"project_id": project.project_id, "display_name": "Managed App", "local_repo_root": str(project.local_repo_root),
         "validation_command": list(project.validation_command)},
    )
    supervisor = Supervisor(registry=load_registry(), repo_root=project.local_repo_root, state=state)
    task = Task(id="T-1", worker="claude-code", role="primary-implementation", task_ref="X-1",
                worktree=str(project.local_repo_root), project_id=project.project_id)
    return supervisor, task, project


def test_worker_launch_environment_leads_with_the_managed_project_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    supervisor, task, project = _supervisor_with_project(tmp_path, venvs)

    env = supervisor._child_environment(task)

    assert env["PATH"].split(os.pathsep)[0] == str(project.local_repo_root / ".venv" / "bin")
    assert str(octarel_active / "bin") not in env["PATH"].split(os.pathsep)
    assert "PYTHONPATH" not in env


def test_worker_launch_with_an_unresolvable_environment_still_removes_octarels_and_records_why(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    supervisor, task, _project_contract = _supervisor_with_project(tmp_path, venvs, venv_key=None)

    env = supervisor._child_environment(task)

    assert str(octarel_active / "bin") not in env["PATH"].split(os.pathsep)
    events = [e for e in supervisor.state.list_events(limit=20) if e.category == "supervisor"]
    assert any("managed Python environment unavailable" in e.message for e in events)


def test_worker_launch_for_a_task_without_a_project_is_unchanged(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    supervisor, task, _project_contract = _supervisor_with_project(tmp_path, venvs)
    task.project_id = None

    env = supervisor._child_environment(task)

    assert env["PATH"].split(os.pathsep)[0] == str(octarel_active / "bin")


def test_fresh_worktree_readiness_inspects_the_managed_interpreter_not_octarels(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _project(tmp_path, venvs)
    worktree = _fresh_sibling_worktree(project)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)

    readiness = provisioning._inspect_managed_worktree(
        worktree, project, require_frontend=False, require_browser=False, create_local_dirs=False,
        hydrate_offline=False,
    )

    managed_python = str(project.local_repo_root / ".venv" / "bin" / "python")
    assert readiness["status"] == "READY", readiness["failures"]
    assert readiness["python"] == managed_python
    assert readiness["managed_environment"]["interpreter"] == managed_python


def test_fresh_worktree_readiness_is_blocked_when_the_managed_environment_is_unavailable(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key=None)
    worktree = _fresh_sibling_worktree(project)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)

    readiness = provisioning._inspect_managed_worktree(
        worktree, project, require_frontend=False, require_browser=False, create_local_dirs=False,
        hydrate_offline=False,
    )

    assert readiness["status"] == "BLOCKED"
    assert any(f.startswith("managed-project Python environment:") for f in readiness["failures"])
    assert readiness["managed_environment"]["status"] == "UNAVAILABLE"
    assert readiness["fingerprint"]


def test_acceptance_evidence_records_the_managed_environment_for_pass_and_fail() -> None:
    facts = {"status": "READY", "interpreter": "/p/.venv/bin/python"}

    assert _managed_environment_evidence({"managed_environment": facts}) == {"managed_environment": facts}
    assert _managed_environment_evidence({"result": "pass"}) == {}


# --------------------------------------------------------------------------- app lifecycle + terminal


def _app_manager(project: ProjectContract | None, root: Path):
    from types import SimpleNamespace

    from scripts.agents.control_plane.operations import AppLifecycleManager

    return AppLifecycleManager(SimpleNamespace(state=State(":memory:"), selected_project=project, repo_root=root))


def test_managed_app_launch_runs_under_the_project_environment(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _project(tmp_path, venvs)

    env = _app_manager(project, project.local_repo_root)._child_environment()

    assert env["PATH"].split(os.pathsep)[0] == str(project.local_repo_root / ".venv" / "bin")
    assert str(octarel_active / "bin") not in env["PATH"].split(os.pathsep)
    assert "PYTHONPATH" not in env


def test_managed_app_launch_without_a_project_environment_still_drops_octarels(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    project = _project(tmp_path, venvs, venv_key=None)

    env = _app_manager(project, project.local_repo_root)._child_environment()

    assert str(octarel_active / "bin") not in env["PATH"].split(os.pathsep)
    assert "VIRTUAL_ENV" not in env


def test_terminal_environment_does_not_leak_octarels_virtualenv(
    tmp_path: Path, venvs: dict[str, Path], octarel_active: Path
) -> None:
    from scripts.agents.control_plane.dashboard_api import _terminal_child_env

    bare = tmp_path / "bare"
    bare.mkdir()
    assert str(octarel_active / "bin") not in _terminal_child_env(bare)["PATH"].split(os.pathsep)

    project = _project(tmp_path, venvs)
    with_env = _terminal_child_env(project.local_repo_root)
    assert with_env["PATH"].split(os.pathsep)[0] == str(project.local_repo_root.resolve() / ".venv" / "bin")
    assert str(octarel_active / "bin") not in with_env["PATH"].split(os.pathsep)
