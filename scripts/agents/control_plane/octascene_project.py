"""ENG-CP-01 (issue #165): OctaScene compatibility adapter for :mod:`project`.

Resolves current ``ACGE248/octages`` truth -- ``AGENTS.md``,
``docs/PRODUCT_ROADMAP.md``, the Video Editor ledger/commands docs, and the
repository-owned local gate -- into a generic :class:`ProjectContract`. This
module only *points at* those repository-owned sources by relative path; it
never copies, parses beyond existence-checking, or duplicates their content
into Control Plane code or state. Actual ledger parsing continues to live in
``quickstart.py``, which this adapter reuses rather than re-implementing.

Later CPX slices add adapters for arbitrary repositories the same way (see
``docs/engineering/ENG-CP-01.md``); this file is deliberately the only place
OctaScene-specific declarations (paths, validation command, capabilities)
are written down.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import runner
from .project import (
    ProjectContract,
    ProjectRootError,
    present_relative_paths,
    resolve_project_root,
)
from .quickstart import VIDEO_EDITOR_COMMANDS_RELATIVE, VIDEO_EDITOR_LEDGER_RELATIVE

# Operator-local OctaScene checkout. Never a committed machine path.
ENV_OCTASCENE_ROOT = "OCTAREL_OCTASCENE_ROOT"

OCTASCENE_PROJECT_ID = "octascene"
OCTASCENE_DISPLAY_NAME = "OctaScene"
OCTASCENE_DEFAULT_BRANCH = "main"
OCTASCENE_GITHUB_REMOTE = "ACGE248/octages"
OCTASCENE_POLICY_ENTRYPOINTS: tuple[str, ...] = ("AGENTS.md",)
OCTASCENE_ROADMAP_PATHS: tuple[str, ...] = ("docs/PRODUCT_ROADMAP.md",)
OCTASCENE_TASK_SOURCES: tuple[str, ...] = (str(VIDEO_EDITOR_LEDGER_RELATIVE), VIDEO_EDITOR_COMMANDS_RELATIVE)
OCTASCENE_VALIDATION_COMMAND: tuple[str, ...] = ("python3", "scripts/ci/local_gate.py", "--docs-reviewed")
OCTASCENE_CAPABILITIES: dict[str, str] = {
    "task_source_adapter": "video_editor_ledger",
    "validation_adapter": "exact_tree_local_gate",
    "worktree_convention": "sibling-directory",
    "validation_authority": "repository-owned-exact-tree-local-gate",
    "app_lifecycle_command": "make run",
    "app_lifecycle_port": "8765",
    "policy_missing_worktree_fallback": "controller-checkout",
}


def octascene_project(repo_root: Path | str | None = None) -> ProjectContract:
    """Build the OctaScene :class:`ProjectContract` from current repository truth.

    ``repo_root`` defaults to ``runner.canonical_repo_root()`` -- the same
    configured canonical checkout every other scheduling-relevant read in
    this Control Plane resolves against (ENG-AGENT-12) -- never this
    process's own ``cwd`` and never the Control Plane code checkout. Passing
    an explicit ``repo_root`` (e.g. a dedicated task worktree) keeps every
    other OctaScene declaration identical while pointing at that checkout.
    """

    if repo_root is not None:
        root = resolve_project_root(repo_root)
    else:
        configured = os.environ.get(ENV_OCTASCENE_ROOT)
        if configured:
            root = resolve_project_root(configured)
        else:
            try:
                candidate = resolve_project_root(runner.canonical_repo_root())
            except ProjectRootError:
                raise
            if not _looks_like_octascene(candidate):
                raise ProjectRootError(
                    "OctaScene checkout is not this process's git root; set OCTAREL_OCTASCENE_ROOT "
                    "or pass repo_root to octascene_project()"
                )
            root = candidate
    return ProjectContract(
        project_id=OCTASCENE_PROJECT_ID,
        display_name=OCTASCENE_DISPLAY_NAME,
        local_repo_root=root,
        default_branch=OCTASCENE_DEFAULT_BRANCH,
        github_remote=OCTASCENE_GITHUB_REMOTE,
        policy_entrypoints=OCTASCENE_POLICY_ENTRYPOINTS,
        roadmap_paths=OCTASCENE_ROADMAP_PATHS,
        task_sources=OCTASCENE_TASK_SOURCES,
        validation_command=OCTASCENE_VALIDATION_COMMAND,
        capabilities=dict(OCTASCENE_CAPABILITIES),
    )


def _looks_like_octascene(root: Path) -> bool:
    return (
        (root / "AGENTS.md").is_file()
        and (root / "docs/PRODUCT_ROADMAP.md").is_file()
        and (root / VIDEO_EDITOR_LEDGER_RELATIVE).is_file()
    )


def present_policy_entrypoints(project: ProjectContract) -> tuple[str, ...]:
    return present_relative_paths(project.local_repo_root, project.policy_entrypoints)


def present_roadmap_paths(project: ProjectContract) -> tuple[str, ...]:
    return present_relative_paths(project.local_repo_root, project.roadmap_paths)


def present_task_sources(project: ProjectContract) -> tuple[str, ...]:
    return present_relative_paths(project.local_repo_root, project.task_sources)


def next_eligible_octascene_task(project: ProjectContract | None = None, repo_root: Path | str | None = None):
    """Current next-eligible Video Editor task, via the dedicated ledger helper.

    ENG-CP-04: generic CP code must call
    :func:`task_sources.next_eligible_task` rather than this function. This
    remains the OctaScene compatibility path so existing next-eligible
    behavior stays equivalent to :func:`quickstart.next_eligible_video_editor_task`.
    """

    from .quickstart import next_eligible_video_editor_task

    root = project.local_repo_root if project is not None else repo_root
    if root is None:
        root = octascene_project().local_repo_root
    return next_eligible_video_editor_task(Path(root))


#: Modules the OctaScene exact-tree gate's Python phases need in the managed interpreter (its own
#: ``scripts/ci/environment.py`` preflight requires the same two).
OCTASCENE_GATE_PYTHON_MODULES: tuple[str, ...] = ("pytest", "ruff")


def run_octascene_exact_tree_gate(worktree: Path | str, *, project: ProjectContract, **gate_kwargs):
    """OctaScene compatibility: run the selected checkout's own local gate.

    Standalone Octarel must not import Octages ``scripts.ci.local_gate`` as a
    library. The OctaScene ``validation_command`` is executed in ``worktree``.

    ENG-AO-09 (issue #21): the gate runs under the *managed project's* Python environment
    (:mod:`managed_environment`), never Octarel's active interpreter -- a fresh sibling worktree needs no
    ``.venv`` link, and OctaScene's own ``resolve_python`` then lands on that interpreter. When the
    environment is unavailable or inconsistent the gate is not started and the result fails closed with the
    reason in ``prerequisite_failures`` and the probe facts in ``managed_environment``.
    """

    import subprocess

    from .managed_environment import (
        ManagedEnvironmentError,
        isolated_environment,
        resolve_managed_environment,
    )

    target = Path(worktree)
    try:
        environment = resolve_managed_environment(
            project, worktree=target, required_modules=OCTASCENE_GATE_PYTHON_MODULES
        )
    except ManagedEnvironmentError as exc:
        return {
            "result": "fail",
            "exit_code": None,
            "output": exc.reason,
            "ready": False,
            "adapter": "exact_tree_local_gate",
            "worktree": str(target),
            "python": None,
            "evidence_path": None,
            "prerequisite_failures": [f"managed-project Python environment: {exc.reason}"],
            "managed_environment": exc.evidence,
        }
    argv = list(OCTASCENE_VALIDATION_COMMAND)
    if argv and argv[0] in {"python", "python3"}:
        argv[0] = str(environment.interpreter)
    if gate_kwargs.get("docs_reviewed", True) and "--docs-reviewed" not in argv:
        argv.append("--docs-reviewed")
    provider = gate_kwargs.get("review_provider")
    evidence = gate_kwargs.get("review_evidence")
    if provider:
        argv.extend(["--independent-review-provider", str(provider)])
    if evidence:
        argv.extend(["--independent-review-evidence", str(evidence)])
    if gate_kwargs.get("dry_run"):
        argv.append("--dry-run")
    result = subprocess.run(
        argv,
        cwd=str(target),
        capture_output=True,
        text=True,
        check=False,
        env=isolated_environment(os.environ, environment),
    )
    stdout = result.stdout or ""
    passed = result.returncode == 0
    return {
        "result": "pass" if passed else "fail",
        "exit_code": result.returncode,
        "output": stdout + (result.stderr or ""),
        "ready": passed,
        "adapter": "exact_tree_local_gate",
        "worktree": str(target),
        "python": str(environment.interpreter),
        "managed_environment": environment.as_evidence(),
    }
