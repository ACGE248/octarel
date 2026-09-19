"""ENG-CP-01 (issue #165): generic managed-project/repository contract.

The long-term architecture is a standalone Control Plane that selects and
manages a repository, where the repository supplies its own policy,
roadmap/task sources, validation contract, and Git/GitHub state. This module
introduces that contract while the Control Plane still lives inside
``ACGE248/octages``: :class:`ProjectContract` is the generic, repository-agnostic
declaration a selected project supplies, and :func:`resolve_project_root` /
:func:`cp_code_root` make the CP-code-checkout-vs-selected-project-checkout
distinction explicit rather than inferring either from process ``cwd``.

Nothing here duplicates OctaScene's actual policy/roadmap/task content --
a ``ProjectContract`` only carries *declarations* (stable id, display name,
a local checkout path, a default branch, relative paths, a validation
command, and free-form capability strings). The OctaScene-specific adapter
that fills these fields in from current repository truth lives in
``octascene_project.py``, and later CPX slices add adapters for arbitrary
repositories the same way -- no project-specific branching belongs in this
module or in scheduler/dashboard code that consumes a ``ProjectContract``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..runner import run_worker_process


class ProjectRootError(ValueError):
    """Raised when a configured project root is not a usable, distinct Git repository."""


class ProjectValidationError(RuntimeError):
    """Raised when a project's declared validation command cannot be run at all."""


@dataclass(frozen=True)
class ProjectContract:
    """A generic managed-project/repository declaration.

    Every field is a declaration the selected repository supplies about
    itself, not content the Control Plane copies or caches. Path-shaped
    fields (``policy_entrypoints``, ``roadmap_paths``, ``task_sources``) are
    relative to ``local_repo_root`` and are resolved fresh by callers on
    every read -- this contract never stores file contents.
    """

    project_id: str
    display_name: str
    local_repo_root: Path
    default_branch: str = "main"
    # "owner/repo" form, or None for a project with no configured GitHub remote.
    github_remote: str | None = None
    policy_entrypoints: tuple[str, ...] = ()
    roadmap_paths: tuple[str, ...] = ()
    task_sources: tuple[str, ...] = ()
    # An argv tuple (never a shell string) for the repository-owned validation
    # command, e.g. ("python3", "scripts/ci/local_gate.py", "--docs-reviewed").
    # Empty means the project declares no repository-owned validation command.
    validation_command: tuple[str, ...] = ()
    # Free-form adapter/capability facts (e.g. which task-source adapter
    # applies, worktree naming convention) -- string-to-string only, never a
    # place to smuggle file contents or secrets.
    capabilities: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "local_repo_root": str(self.local_repo_root),
            "default_branch": self.default_branch,
            "github_remote": self.github_remote,
            "policy_entrypoints": list(self.policy_entrypoints),
            "roadmap_paths": list(self.roadmap_paths),
            "task_sources": list(self.task_sources),
            "validation_command": list(self.validation_command),
            "capabilities": dict(self.capabilities),
        }


def resolve_project_root(candidate: str | Path) -> Path:
    """Validate that ``candidate`` names a real, usable Git repository root.

    Never trusts a path just because it exists on disk: this resolves
    through ``git rev-parse --show-toplevel`` run *inside* ``candidate`` (the
    same mechanism ``runner.repo_root``/``runner.canonical_repo_root`` use for
    OctaScene's own canonical-truth resolution), so a missing directory, a
    plain non-Git directory, or an otherwise-invalid path fails loudly with
    :class:`ProjectRootError` instead of silently falling back to some other
    root (the CP code checkout, cwd, or a previously selected project).
    """

    path = Path(candidate)
    if not path.exists() or not path.is_dir():
        raise ProjectRootError(f"project root does not exist or is not a directory: {candidate!r}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectRootError(f"could not resolve a Git root for {candidate!r}: {exc}") from exc
    if result.returncode != 0:
        raise ProjectRootError(f"{candidate!r} is not inside a Git work tree")
    out = (result.stdout or "").strip()
    if not out:
        raise ProjectRootError(f"{candidate!r} did not resolve to a Git root")
    return Path(out)


def cp_code_root() -> Path:
    """The Control Plane's own code checkout root, independent of any selected project.

    Resolved from this module's own file location (the same pattern
    ``dashboard_api.cp_process_status`` already uses for ``cp_code_root``),
    never from process ``cwd`` and never from a selected
    :class:`ProjectContract`'s ``local_repo_root``. ENG-AGENT-12 established
    that the two are allowed to differ (a dedicated CP development checkout
    scheduling against a separate canonical OctaScene checkout); this is the
    CP-code side of that split, reusable by any future adapter/UI code that
    needs to report or compare against it.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Path(__file__).resolve().parents[2]
    out = (result.stdout or "").strip()
    return Path(out) if result.returncode == 0 and out else Path(__file__).resolve().parents[2]


def present_relative_paths(local_repo_root: Path, relative_paths: tuple[str, ...]) -> tuple[str, ...]:
    """The subset of ``relative_paths`` that actually exist under ``local_repo_root`` right now.

    Existence is checked at call time, never cached, since repository truth
    (which policy/roadmap/task-source files exist) changes between commits
    and branches. Shared by any adapter's policy/roadmap/task-source
    resolution so that logic is not duplicated per project.
    """

    return tuple(p for p in relative_paths if (local_repo_root / p).exists())


def run_project_validation(
    project: ProjectContract,
    *,
    worktree: Path | str | None = None,
    timeout: float | None = 1800.0,
) -> tuple[int, str]:
    """Run ``project.validation_command`` from the exact candidate -- never CP cwd.

    ENG-CP-02 (issue #165) wired ``validation_command`` to real execution.
    ENG-CP-04 (issue #169) lets callers pass ``worktree`` so acceptance and
    merge-prep run the *selected project's* declared command from the exact
    candidate tree rather than always from ``local_repo_root``. Reuses
    ``runner.run_worker_process`` rather than a new subprocess call: it
    already runs with ``cwd=root`` (explicitly, regardless of this process's
    own cwd) and an environment reduced to a minimal allowlist
    (ENG-AGENT-01), which already excludes every Control-Plane-only
    orchestration variable (``runner.ORCHESTRATION_ONLY_ENV_VARS``).

    Fails clearly and immediately -- never by silently falling back to
    running in the Control Plane's own cwd, and never by substituting
    ``local_repo_root`` when an explicit ``worktree`` is missing -- when
    ``project`` declares no validation command, or when the target directory
    does not exist.
    """

    if not project.validation_command:
        raise ProjectValidationError(f"project {project.project_id!r} declares no validation_command")
    target = Path(worktree) if worktree is not None else project.local_repo_root
    if not target.exists() or not target.is_dir():
        raise ProjectValidationError(
            f"project {project.project_id!r} validation target does not exist or is not a directory: {target!r}"
        )
    return run_worker_process(list(project.validation_command), target, timeout=timeout)
