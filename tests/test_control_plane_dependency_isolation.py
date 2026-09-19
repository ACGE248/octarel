"""ENG-CP-02 (issue #165): Control Plane dependency/runtime isolation.

Proves CPX-02's required outcomes: the Control Plane can be installed and
run without OctaScene's application dependencies, its generic code never
imports OctaScene application modules, a selected project's commands run
from that project's own worktree with a sanitized environment (never this
Control Plane process's own orchestration-only variables), and invalid or
missing project environments fail clearly rather than silently falling back
to the Control Plane's own cwd.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.agents.control_plane.octascene_project import octascene_project
from scripts.agents.control_plane.project import (
    ProjectContract,
    ProjectValidationError,
    cp_code_root,
    run_project_validation,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry
from scripts.agents.runner import (
    ENV_CANONICAL_REPO_ROOT,
    ORCHESTRATION_ONLY_ENV_VARS,
    sanitized_subprocess_env,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_ROOT = REPO_ROOT / "scripts" / "agents"

# Standard library modules used anywhere under scripts/agents today. Anything
# outside this set (and not "scripts", this package's own namespace) is a
# third-party dependency that must be declared in
# scripts/agents/requirements.txt -- CP's own dependency manifest.
_STDLIB_ALLOWLIST = {
    "__future__", "abc", "argparse", "asyncio", "atexit", "collections", "contextlib",
    "dataclasses", "datetime", "enum", "fcntl", "functools", "hashlib",
    "ipaddress", "json", "os", "pathlib", "plistlib", "pty", "re", "shlex", "shutil",
    "signal", "socket", "sqlite3", "struct", "subprocess", "sys", "tempfile",
    "termios", "threading", "time", "typing", "urllib", "uuid",
}
# PyJWT's import name ("jwt") differs from its distribution name.
_DECLARED_THIRD_PARTY = {"fastapi", "uvicorn", "jwt", "psutil", "httpx"}

# This package's own namespace -- never counted as a cross-package dependency.
_OWN_PACKAGE = {"scripts.agents"}

# ENG-CP-02 (issue #165): documented, temporary intra-repository compatibility
# dependencies on ``scripts.ci`` -- repository-generic CI/dev tooling (risk
# classification, the exact-tree local gate's own read-only history/verdict
# helpers, worktree environment preparation, and worker-preset healing), not
# OctaScene application/domain code, but still a module living outside
# ``scripts/agents``. An earlier version of this audit collapsed every
# ``scripts.*`` import into the bare name ``"scripts"`` and exempted it
# wholesale as "this package's own namespace" -- masking every one of these
# as if they were internal, when only ``scripts.agents`` actually is
# (independent-review finding, Grok/xAI, ENG-CP-02). ``acceptance.py``,
# ``provisioning.py``, ``telemetry.py``, ``operations.py``, and
# ``dashboard_api.py`` import ``scripts.ci.change_risk``/``local_gate``/
# ``runtime_paths``/``environment``; ``orchestrate.py`` imports
# ``scripts.ci.reviewer_presets`` with a same-named ``sys.path`` fallback
# (``from ci.reviewer_presets import ...``, seen here as the bare root
# ``ci``). A standalone Control Plane repository (CPX-05) will need
# ``scripts/ci`` either vendored alongside `scripts/agents` or kept as an
# explicit adapter dependency -- resolving that is out of this slice's scope;
# this audit only names it so it cannot silently grow unnoticed.
_DOCUMENTED_INTRA_REPO_DEPENDENCIES = {"scripts.ci", "ci"}


def _iter_python_files() -> list[Path]:
    return sorted(
        path
        for path in AGENTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and "dashboard-tests" not in path.parts
    )


def _import_root(dotted: str) -> str:
    """The distribution/package root of a dotted import, keeping ``scripts.<pkg>`` distinct.

    A bare top-level split (``"scripts.ci.change_risk".split(".")[0]`` ==
    ``"scripts"``) would collapse every sibling package under ``scripts/``
    (``scripts.agents``, ``scripts.ci``, ...) into one indistinguishable
    name, hiding real cross-package dependencies on ``scripts.ci`` behind
    this package's own ``scripts.agents`` namespace exemption. Keeping the
    first two dotted components for anything under ``scripts.`` is what lets
    the two be told apart.
    """

    parts = dotted.split(".")
    if parts[0] == "scripts" and len(parts) > 1:
        return f"scripts.{parts[1]}"
    return parts[0]


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(_import_root(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import -- never a package/distribution name
            if node.module:
                roots.add(_import_root(node.module))
    return roots


def test_control_plane_never_imports_octascene_app_modules() -> None:
    offenders = {
        str(path.relative_to(REPO_ROOT)): imports
        for path in _iter_python_files()
        if "app" in (imports := _import_roots(path))
    }
    assert offenders == {}


def test_control_plane_third_party_imports_are_all_declared() -> None:
    undeclared = {}
    for path in _iter_python_files():
        extra = (
            _import_roots(path)
            - _STDLIB_ALLOWLIST
            - _OWN_PACKAGE
            - _DECLARED_THIRD_PARTY
            - _DOCUMENTED_INTRA_REPO_DEPENDENCIES
        )
        if extra:
            undeclared[str(path.relative_to(REPO_ROOT))] = extra
    assert undeclared == {}


def test_documented_intra_repo_dependencies_are_exactly_scripts_ci() -> None:
    """Guards against the exact masking bug an independent review caught.

    If this ever starts failing because a *new* cross-package dependency
    root appears, that is real information (a new coupling to name and
    document) -- never something to silence by widening
    ``_DOCUMENTED_INTRA_REPO_DEPENDENCIES`` or reintroducing a blanket
    ``"scripts"`` exemption.
    """

    found_scripts_roots = {
        root
        for path in _iter_python_files()
        for root in _import_roots(path)
        if root.startswith("scripts.") or root == "ci"
    }
    assert found_scripts_roots <= _OWN_PACKAGE | _DOCUMENTED_INTRA_REPO_DEPENDENCIES


def test_control_plane_own_requirements_file_declares_the_same_third_party_set() -> None:
    manifest = (AGENTS_ROOT / "requirements.txt").read_text(encoding="utf-8")
    # Every declared third-party import has a corresponding manifest entry
    # (matched loosely by distribution-name prefix, since PyJWT's import
    # name and distribution name differ).
    assert "fastapi" in manifest.lower()
    assert "uvicorn" in manifest.lower()
    assert "pyjwt" in manifest.lower()
    assert "psutil" in manifest.lower()
    assert "httpx" in manifest.lower()


def test_control_plane_tests_run_without_octascene_app_conftest_bootstrap() -> None:
    """CP's own tests must not require ``tests/conftest.py``'s app bootstrap.

    That conftest unconditionally imports ``app.bootstrap.services`` and
    calls ``bootstrap_services()`` for every test collected under ``tests/``
    -- necessary for OctaScene application tests, but an unnecessary hidden
    OctaScene-app dependency for the Control Plane's own tests.
    ``--noconftest`` disables that ancestor conftest.py entirely; this CP
    test file has no fixture dependency on it, so it must still pass.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--noconftest",
            "-q",
            str(REPO_ROOT / "tests" / "test_control_plane_project_contract.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sanitized_subprocess_env_excludes_orchestration_only_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CANONICAL_REPO_ROOT, "/some/cp/canonical/root")
    monkeypatch.setenv("UNRELATED_TEST_VAR", "keep-me")

    env = sanitized_subprocess_env()

    assert ENV_CANONICAL_REPO_ROOT not in env
    assert env.get("UNRELATED_TEST_VAR") == "keep-me"
    assert set(ORCHESTRATION_ONLY_ENV_VARS) == {ENV_CANONICAL_REPO_ROOT}


def test_sanitized_subprocess_env_supports_extra_exclude() -> None:
    env = sanitized_subprocess_env({"KEEP": "1", "DROP_ME": "2"}, extra_exclude=("DROP_ME",))
    assert env == {"KEEP": "1"}


def test_supervisor_spawn_does_not_leak_canonical_repo_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CANONICAL_REPO_ROOT, str(tmp_path))
    registry = load_registry(REPO_ROOT / "scripts" / "agents" / "workers.json")
    state = State(tmp_path / "state.db")
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)

    process = supervisor._spawn(
        [
            sys.executable,
            "-c",
            "import os, sys; sys.stdout.write(os.environ.get('OCTAGES_ORCH_CANONICAL_REPO_ROOT', 'UNSET'))",
        ],
        cwd=tmp_path,
    )
    out, _ = process.communicate(timeout=10)

    assert out == "UNSET"


def test_run_project_validation_executes_in_selected_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "selected-project"
    project_root.mkdir()
    project = ProjectContract(
        project_id="project-b",
        display_name="Project B",
        local_repo_root=project_root,
        validation_command=(sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"),
    )

    exit_code, output = run_project_validation(project)

    assert exit_code == 0
    assert Path(output).resolve() == project_root.resolve()


def test_run_project_validation_does_not_inherit_canonical_repo_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CANONICAL_REPO_ROOT, str(tmp_path))
    project_root = tmp_path / "another-selected-project"
    project_root.mkdir()
    project = ProjectContract(
        project_id="project-c",
        display_name="Project C",
        local_repo_root=project_root,
        validation_command=(
            sys.executable,
            "-c",
            "import os, sys; sys.stdout.write(os.environ.get('OCTAGES_ORCH_CANONICAL_REPO_ROOT', 'UNSET'))",
        ),
    )

    exit_code, output = run_project_validation(project)

    assert exit_code == 0
    assert output == "UNSET"


def test_run_project_validation_fails_clearly_with_no_validation_command(tmp_path: Path) -> None:
    project = ProjectContract(project_id="p", display_name="P", local_repo_root=tmp_path)
    with pytest.raises(ProjectValidationError):
        run_project_validation(project)


def test_run_project_validation_honors_explicit_candidate_worktree(tmp_path: Path) -> None:
    project_root = tmp_path / "registered-root"
    candidate = tmp_path / "exact-candidate"
    project_root.mkdir()
    candidate.mkdir()
    project = ProjectContract(
        project_id="project-d",
        display_name="Project D",
        local_repo_root=project_root,
        validation_command=(sys.executable, "-c", "import os, sys; sys.stdout.write(os.getcwd())"),
    )
    exit_code, output = run_project_validation(project, worktree=candidate)
    assert exit_code == 0
    assert Path(output).resolve() == candidate.resolve()
    assert Path(output).resolve() != project_root.resolve()


def test_run_project_validation_fails_clearly_for_missing_project_root(tmp_path: Path) -> None:
    missing_root = tmp_path / "deleted-worktree"
    project = ProjectContract(
        project_id="p",
        display_name="P",
        local_repo_root=missing_root,
        validation_command=("true",),
    )
    with pytest.raises(ProjectValidationError):
        run_project_validation(project)


def test_octascene_compatibility_behavior_is_unchanged() -> None:
    """CPX-02 must not change what CPX-01's adapter reports for OctaScene."""

    from tests.octarel_paths import octascene_checkout

    live = octascene_checkout()
    if live is None:
        pytest.skip("set OCTAREL_OCTASCENE_ROOT to a real Octages checkout")
    project = octascene_project(repo_root=live)
    assert project.project_id == "octascene"
    assert project.local_repo_root.resolve() == live.resolve()
    assert project.validation_command[:2] == ("python3", "scripts/ci/local_gate.py")


def test_cp_code_root_is_independent_of_any_selected_project(tmp_path: Path) -> None:
    synthetic_root = tmp_path / "unrelated-project"
    synthetic_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=synthetic_root, check=True)

    project = ProjectContract(
        project_id="synthetic",
        display_name="Synthetic",
        local_repo_root=synthetic_root,
    )

    code_root = cp_code_root()

    assert code_root.resolve() != project.local_repo_root.resolve()
    assert code_root.resolve() == REPO_ROOT.resolve()
