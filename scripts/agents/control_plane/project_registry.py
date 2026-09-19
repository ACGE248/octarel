"""ENG-CP-03 (issue #165): the durable managed-project registry and selection.

CPX-01 introduced :class:`~.project.ProjectContract` -- the generic declaration
a managed repository supplies about itself -- and CPX-02 wired its
``validation_command`` to real execution in the project's own root. Both were
built fresh per process from a single hard-coded OctaScene adapter call. This
module makes that registry *durable and plural*: several projects can be
registered, exactly one is selected at a time, and every project-scoped Control
Plane record is associated with the project it belongs to.

What this module deliberately does **not** do is become a second source of
truth. A registry row stores only *where and how to obtain* repository truth --
a checkout path, a GitHub remote, a default branch, relative policy/roadmap/
task-source paths, and an argv validation command. It never stores roadmap
content, task status, ledger state, policy text, or any other product fact.
Those are read fresh from the managed repository on every request, exactly as
they were before this slice (see ``docs/engineering/ENG-CP-03.md``).

The OctaScene-specific declarations stay in ``octascene_project.py``, which
remains the only file in the Control Plane that knows what OctaScene is. This
module treats it as one adapter among future others, calling it in exactly two
places: the deterministic auto-migration that adopts an existing installation's
state, and the equally deterministic default selection when a registry is
empty.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .octascene_project import (
    OCTASCENE_PROJECT_ID,
    _looks_like_octascene,
    octascene_project,
)
from .project import (
    ProjectContract,
    ProjectRootError,
    cp_code_root,
    resolve_project_root,
)
from .state import CURRENT_SCHEMA_VERSION, State

SELECTED_PROJECT_SETTING = "selected_project_id"
MIGRATION_MARKER_SETTING = "legacy_project_migration"

# A project id is used verbatim as a ``control_settings`` key suffix and as a
# stable identifier in API paths, so it is deliberately restricted to an
# unambiguous, filesystem- and URL-safe shape rather than accepting arbitrary
# text that would need escaping at every use site.
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# "owner/repo", the same form ``ProjectContract.github_remote`` documents.
GITHUB_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

# Candidate files the Add Project detector looks for. These are *hints offered
# to the operator*, never silently-applied assumptions: detection results are
# shown before saving, and a repository matching none of them is still
# registrable with empty declarations.
POLICY_CANDIDATES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".agents/README.md")
ROADMAP_CANDIDATES: tuple[str, ...] = (
    "docs/PRODUCT_ROADMAP.md",
    "docs/ROADMAP.md",
    "ROADMAP.md",
    "docs/roadmap.md",
)
TASK_SOURCE_CANDIDATES: tuple[str, ...] = ("TASKS.md", "docs/TASKS.md", "TODO.md")


class ProjectRegistryError(ValueError):
    """Base class for registry errors. Always carries an actionable message."""


class DuplicateProjectError(ProjectRegistryError):
    """Raised when registering an id (or repository root) already registered."""


class UnknownProjectError(ProjectRegistryError):
    """Raised when addressing a project id that is not registered."""


class ProjectConfigError(ProjectRegistryError):
    """Raised when a submitted project configuration is structurally unsafe/invalid."""


# --------------------------------------------------------------------- encoding


def contract_to_row(project: ProjectContract, *, enabled: bool = True) -> dict[str, Any]:
    """Encode a :class:`ProjectContract` into a ``projects`` table row."""

    return {
        "project_id": project.project_id,
        "display_name": project.display_name,
        "local_repo_root": str(project.local_repo_root),
        "default_branch": project.default_branch,
        "github_remote": project.github_remote,
        "policy_entrypoints": json.dumps(list(project.policy_entrypoints)),
        "roadmap_paths": json.dumps(list(project.roadmap_paths)),
        "task_sources": json.dumps(list(project.task_sources)),
        "validation_command": json.dumps(list(project.validation_command)),
        "capabilities": json.dumps(dict(project.capabilities), sort_keys=True),
        "enabled": 1 if enabled else 0,
    }


def row_to_contract(row: dict[str, Any]) -> ProjectContract:
    """Decode a ``projects`` row back into the one CPX-01 contract type.

    Returning a :class:`ProjectContract` rather than a registry-specific object
    is the point: every downstream consumer (validation execution, the
    dashboard, future adapters) keeps working against the single generic
    representation CPX-01 established instead of a parallel one.
    """

    return ProjectContract(
        project_id=row["project_id"],
        display_name=row["display_name"],
        local_repo_root=Path(row["local_repo_root"]),
        default_branch=row["default_branch"] or "main",
        github_remote=row["github_remote"] or None,
        policy_entrypoints=tuple(json.loads(row["policy_entrypoints"] or "[]")),
        roadmap_paths=tuple(json.loads(row["roadmap_paths"] or "[]")),
        task_sources=tuple(json.loads(row["task_sources"] or "[]")),
        validation_command=tuple(json.loads(row["validation_command"] or "[]")),
        capabilities=dict(json.loads(row["capabilities"] or "{}")),
    )


def row_to_api_dict(row: dict[str, Any]) -> dict[str, Any]:
    """The JSON shape the dashboard consumes: the contract plus registry metadata."""

    contract = row_to_contract(row)
    body = contract.as_dict()
    body.update(
        {
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )
    return body


# ------------------------------------------------------------------ validation


def _validate_relative_paths(values: Any, field: str, root: Path) -> tuple[str, ...]:
    """Every declared path must be repository-relative and stay inside the root.

    Rejects absolute paths, ``..`` traversal, and anything that resolves outside
    ``root`` even via symlink -- the Control Plane must never be talked into
    reading a policy/roadmap/task file from outside the selected project.
    Existence is *not* required: a repository may legitimately declare a path
    that only exists on some branches, and CPX-01's ``present_relative_paths``
    already resolves existence fresh at read time.
    """

    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ProjectConfigError(f"{field} must be a list of repository-relative paths, not {type(values).__name__}")
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ProjectConfigError(f"{field} entries must be non-empty strings")
        value = raw.strip()
        candidate = Path(value)
        if candidate.is_absolute():
            raise ProjectConfigError(
                f"{field} entry {value!r} must be relative to the repository root, not an absolute path"
            )
        if ".." in candidate.parts:
            raise ProjectConfigError(f"{field} entry {value!r} must not traverse outside the repository with '..'")
        # Resolve against the root to catch symlink-based escapes as well as
        # any traversal shape the textual check above could miss.
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise ProjectConfigError(f"{field} entry {value!r} resolves outside the repository root {resolved_root}")
        cleaned.append(value)
    return tuple(cleaned)


def _validate_validation_command(values: Any) -> tuple[str, ...]:
    """The validation command must be an argv list, never a shell string.

    CPX-02 executes this through ``runner.run_worker_process`` without a shell,
    so accepting a single string here would silently turn into a one-element
    argv that cannot run, and encouraging a shell string would invite command
    injection through registry data.
    """

    if values is None:
        return ()
    if isinstance(values, str):
        raise ProjectConfigError(
            "validation_command must be an argv list (e.g. [\"make\", \"test\"]), not a shell string -- "
            "it is executed without a shell"
        )
    if not isinstance(values, (list, tuple)):
        raise ProjectConfigError(f"validation_command must be a list, not {type(values).__name__}")
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ProjectConfigError("validation_command entries must be non-empty strings")
        cleaned.append(raw)
    return tuple(cleaned)


def _validate_capabilities(values: Any) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ProjectConfigError(f"capabilities must be an object of string->string, not {type(values).__name__}")
    cleaned: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ProjectConfigError("capabilities keys and values must both be strings")
        cleaned[key] = value
    return cleaned


def validate_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.match(project_id):
        raise ProjectConfigError(
            f"project_id {project_id!r} must be 1-64 characters of lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    return project_id


def validate_github_remote(remote: Any) -> str | None:
    if remote in (None, ""):
        return None
    if not isinstance(remote, str) or not GITHUB_REMOTE_PATTERN.match(remote.strip()):
        raise ProjectConfigError(
            f"github_remote {remote!r} must be in 'owner/repo' form (e.g. 'ACGE248/octages') or empty"
        )
    return remote.strip()


def build_contract(payload: dict[str, Any]) -> ProjectContract:
    """Validate a submitted configuration and build a :class:`ProjectContract`.

    Raises :class:`ProjectConfigError` / :class:`~.project.ProjectRootError`
    with a message naming the specific field and the specific problem, never a
    generic "invalid project" -- the operator has to be able to act on it
    directly from the Add Project dialog.
    """

    project_id = validate_project_id(payload.get("project_id"))
    raw_root = payload.get("local_repo_root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ProjectConfigError("local_repo_root is required and must be a path to a local Git checkout")
    # resolve_project_root (CPX-01) is the single mechanism that decides what a
    # usable project root is. It fails loudly for a missing directory or a
    # non-Git directory and never falls back to cwd or the CP code checkout.
    root = resolve_project_root(raw_root.strip())

    display_name = payload.get("display_name") or project_id
    if not isinstance(display_name, str) or not display_name.strip():
        raise ProjectConfigError("display_name must be a non-empty string")

    default_branch = payload.get("default_branch") or "main"
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise ProjectConfigError("default_branch must be a non-empty branch name")

    return ProjectContract(
        project_id=project_id,
        display_name=display_name.strip(),
        local_repo_root=root,
        default_branch=default_branch.strip(),
        github_remote=validate_github_remote(payload.get("github_remote")),
        policy_entrypoints=_validate_relative_paths(payload.get("policy_entrypoints"), "policy_entrypoints", root),
        roadmap_paths=_validate_relative_paths(payload.get("roadmap_paths"), "roadmap_paths", root),
        task_sources=_validate_relative_paths(payload.get("task_sources"), "task_sources", root),
        validation_command=_validate_validation_command(payload.get("validation_command")),
        capabilities=_validate_capabilities(payload.get("capabilities")),
    )


def validate_project(project: ProjectContract) -> dict[str, Any]:
    """Re-check a registered project against the filesystem right now.

    Returns a structured report rather than raising, so the Control Center can
    show a project as "registered but currently broken" (a deleted or moved
    checkout, a remote that no longer matches) without the whole registry
    failing to load.
    """

    problems: list[str] = []
    warnings: list[str] = []
    root_ok = False
    resolved_root: str | None = None
    try:
        resolved = resolve_project_root(project.local_repo_root)
        resolved_root = str(resolved)
        root_ok = True
        if resolved.resolve() != project.local_repo_root.resolve():
            warnings.append(
                f"registered root {project.local_repo_root} resolves to the Git root {resolved}; "
                "consider re-registering with the resolved path"
            )
    except ProjectRootError as exc:
        problems.append(str(exc))

    if root_ok:
        for field_name, values in (
            ("policy_entrypoints", project.policy_entrypoints),
            ("roadmap_paths", project.roadmap_paths),
            ("task_sources", project.task_sources),
        ):
            try:
                _validate_relative_paths(list(values), field_name, project.local_repo_root)
            except ProjectConfigError as exc:
                problems.append(str(exc))
            missing = [p for p in values if not (project.local_repo_root / p).exists()]
            if missing:
                warnings.append(f"{field_name} declared but not present on the current branch: {', '.join(missing)}")

        observed = detect_git_remote(project.local_repo_root)
        if project.github_remote and observed and observed != project.github_remote:
            warnings.append(
                f"configured github_remote {project.github_remote!r} does not match the checkout's "
                f"origin remote {observed!r}"
            )

        # CP-code-root / project-root separation (ENG-AGENT-12, CPX-01). The two
        # are *allowed* to coincide -- the Control Plane still physically lives
        # inside the OctaScene repository until CPX-05 -- so this is reported as
        # an explicit observation, never an error and never an inference: a
        # project root is only ever what the operator registered.
        try:
            same_as_cp = project.local_repo_root.resolve() == cp_code_root().resolve()
        except OSError:  # pragma: no cover - resolution failure is itself reported above
            same_as_cp = False
    else:
        same_as_cp = False

    if not project.validation_command:
        warnings.append("no validation_command declared; repository-owned validation cannot be run for this project")

    return {
        "project_id": project.project_id,
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "resolved_root": resolved_root,
        "is_cp_code_root": same_as_cp,
        "cp_code_root": str(cp_code_root()),
    }


# ------------------------------------------------------------------- detection


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def detect_git_remote(root: Path) -> str | None:
    """The checkout's ``origin`` remote normalized to ``owner/repo``, if any.

    Handles both SSH (``git@github.com:owner/repo.git``) and HTTPS
    (``https://github.com/owner/repo.git``) forms. Returns ``None`` -- never a
    guess -- for a repository with no origin or a remote shape this does not
    recognize; a project with no detectable remote is still perfectly
    registrable.
    """

    url = _git_output(root, "remote", "get-url", "origin")
    if not url:
        return None
    candidate = url.removesuffix(".git")
    if "@" in candidate and ":" in candidate.split("@", 1)[1]:
        candidate = candidate.split("@", 1)[1].split(":", 1)[1]
    elif "://" in candidate:
        path = candidate.split("://", 1)[1]
        parts = path.split("/", 1)
        candidate = parts[1] if len(parts) == 2 else ""
    candidate = candidate.strip("/")
    return candidate if GITHUB_REMOTE_PATTERN.match(candidate) else None


def detect_default_branch(root: Path) -> str:
    """The repository's own default branch, falling back to its current branch.

    Prefers ``origin/HEAD`` (what the remote itself declares) and falls back to
    the currently checked-out branch, then to ``main``. Never assumes ``main``
    for a repository that says otherwise.
    """

    head = _git_output(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        return head.split("/", 1)[1]
    current = _git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current and current != "HEAD":
        return current
    return "main"


def detect_project(candidate: str | Path) -> dict[str, Any]:
    """Inspect a local folder and report what can be detected about it.

    Used by the Add Project flow to show detected values *before* saving.
    Everything here is a suggestion derived from generic Git/filesystem facts --
    there is no OctaScene-specific branch anywhere in this function, and a plain
    repository with nothing but ``.git`` detects successfully with empty
    suggestion lists.
    """

    root = resolve_project_root(candidate)
    present = lambda names: [n for n in names if (root / n).exists()]  # noqa: E731 - local, single-use
    suggested_id = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-_") or "project"
    validation_command: list[str] = []
    if (root / "Makefile").exists():
        validation_command = ["make", "test"]
    elif (root / "package.json").exists():
        validation_command = ["npm", "test"]
    elif (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
        validation_command = ["python3", "-m", "pytest"]

    return {
        "local_repo_root": str(root),
        "suggested_project_id": suggested_id[:64],
        "suggested_display_name": root.name,
        "github_remote": detect_git_remote(root),
        "default_branch": detect_default_branch(root),
        "policy_entrypoints": present(POLICY_CANDIDATES),
        "roadmap_paths": present(ROADMAP_CANDIDATES),
        "task_sources": present(TASK_SOURCE_CANDIDATES),
        "validation_command": validation_command,
        "suggested_task_source_adapter": (
            "file_ledger" if present(TASK_SOURCE_CANDIDATES) else ("github_issues" if detect_git_remote(root) else "")
        ),
        "is_cp_code_root": root.resolve() == cp_code_root().resolve(),
    }


# -------------------------------------------------------------------- registry


def list_projects(state: State, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    return [row_to_api_dict(row) for row in state.list_projects(enabled_only=enabled_only)]


def get_project(state: State, project_id: str) -> ProjectContract:
    row = state.get_project(project_id)
    if row is None:
        raise UnknownProjectError(f"no project registered with id {project_id!r}")
    return row_to_contract(row)


def register_project(state: State, payload: dict[str, Any], *, enabled: bool = True) -> ProjectContract:
    """Validate and durably register a new managed project."""

    contract = build_contract(payload)
    if state.get_project(contract.project_id) is not None:
        raise DuplicateProjectError(
            f"a project with id {contract.project_id!r} is already registered; "
            "choose a different id or edit the existing project"
        )
    target = contract.local_repo_root.resolve()
    for row in state.list_projects():
        if Path(row["local_repo_root"]).resolve() == target:
            raise DuplicateProjectError(
                f"repository {target} is already registered as project {row['project_id']!r} "
                f"({row['display_name']})"
            )
    state.upsert_project(contract_to_row(contract, enabled=enabled))
    state.record_event(
        category="project_registry",
        message=f"registered project {contract.project_id!r} ({contract.display_name}) at {target}",
        project_id=contract.project_id,
    )
    return contract


def update_project(state: State, project_id: str, changes: dict[str, Any]) -> ProjectContract:
    """Edit a registered project's configuration.

    The project's id is immutable: it is the key every project-scoped record
    already references, so "renaming" it would orphan that history. Display name
    and every declaration are freely editable.
    """

    existing = state.get_project(project_id)
    if existing is None:
        raise UnknownProjectError(f"no project registered with id {project_id!r}")
    if "project_id" in changes and changes["project_id"] != project_id:
        raise ProjectConfigError(
            "project_id is immutable because existing tasks, runbooks, worktrees and history reference it; "
            "register a new project instead"
        )
    merged = row_to_contract(existing).as_dict()
    merged.update({k: v for k, v in changes.items() if k != "project_id"})
    merged["project_id"] = project_id
    contract = build_contract(merged)

    target = contract.local_repo_root.resolve()
    for row in state.list_projects():
        if row["project_id"] != project_id and Path(row["local_repo_root"]).resolve() == target:
            raise DuplicateProjectError(
                f"repository {target} is already registered as project {row['project_id']!r}"
            )

    row = contract_to_row(contract, enabled=bool(existing["enabled"]))
    if "enabled" in changes:
        row["enabled"] = 1 if changes["enabled"] else 0
    row["created_at"] = existing["created_at"]
    state.upsert_project(row)
    state.record_event(
        category="project_registry",
        message=f"updated project {project_id!r} configuration",
        project_id=project_id,
    )
    return contract


def set_project_enabled(state: State, project_id: str, enabled: bool) -> ProjectContract:
    """Disable (or re-enable) a project without touching its data or history."""

    return update_project(state, project_id, {"enabled": bool(enabled)})


def remove_project(state: State, project_id: str, *, confirm: bool = False) -> dict[str, Any]:
    """Remove a project from the registry. Never touches the repository.

    Explicitly does not, and cannot, delete the repository, its branches, its
    worktrees, or any product file -- this only deletes one row from the Control
    Plane's own ``projects`` table. Historical Control Plane evidence (tasks,
    runbooks, events, operations) is deliberately **retained** with its original
    ``project_id`` so the record of what ran against that repository survives;
    the returned counts report exactly what was preserved.

    ``confirm`` must be passed explicitly: this is a destructive CP-state
    action, and the same client-tamper-proof confirm gate the existing
    destructive command verbs use applies here.
    """

    row = state.get_project(project_id)
    if row is None:
        raise UnknownProjectError(f"no project registered with id {project_id!r}")
    if not confirm:
        raise ProjectConfigError(
            f"removing project {project_id!r} is a destructive Control Plane state change and requires confirmation"
        )
    if project_id == selected_project_id(state):
        remaining = [p for p in state.list_projects() if p["project_id"] != project_id]
        if not remaining:
            raise ProjectConfigError(
                f"project {project_id!r} is the only registered project and is currently selected; "
                "register another project before removing it"
            )
        select_project(state, remaining[0]["project_id"])

    preserved = state.count_project_rows(project_id)
    state.delete_project(project_id)
    state.record_event(
        category="project_registry",
        message=(
            f"removed project {project_id!r} from the registry; repository at {row['local_repo_root']} "
            f"untouched, {sum(preserved.values())} historical Control Plane records preserved"
        ),
        project_id=project_id,
    )
    return {"removed": project_id, "preserved_records": preserved, "repository_untouched": row["local_repo_root"]}


# ------------------------------------------------------------------- selection


def selected_project_id(state: State) -> str | None:
    return state.get_control_setting(SELECTED_PROJECT_SETTING)


def select_project(state: State, project_id: str) -> ProjectContract:
    """Make ``project_id`` the selected project.

    Selection is pure Control Plane state: it changes what the operator is
    looking at and what subsequent work is scoped to. It never checks out a
    branch, never runs Git in either repository, and therefore cannot mutate any
    managed repository.
    """

    row = state.get_project(project_id)
    if row is None:
        raise UnknownProjectError(f"no project registered with id {project_id!r}")
    if not row["enabled"]:
        raise ProjectConfigError(f"project {project_id!r} is disabled; enable it before selecting it")
    state.set_control_setting(SELECTED_PROJECT_SETTING, project_id)
    state.record_event(
        category="project_registry", message=f"selected project {project_id!r}", project_id=project_id
    )
    return row_to_contract(row)


def selected_project(state: State) -> ProjectContract | None:
    """The currently selected project, or ``None`` when the registry is empty.

    Falls back to the single registered project when nothing is explicitly
    selected (the common case immediately after auto-migration), but never
    invents a project and never falls back to the Control Plane's own checkout.
    """

    current = selected_project_id(state)
    if current:
        row = state.get_project(current)
        if row is not None and row["enabled"]:
            return row_to_contract(row)
    enabled = state.list_projects(enabled_only=True)
    if len(enabled) == 1:
        return row_to_contract(enabled[0])
    return None


# ------------------------------------------------------------------- migration


def migrate_legacy_state_to_project(state: State, project_id: str) -> dict[str, Any]:
    """Adopt every pre-ENG-CP-03 record into ``project_id``. Idempotent.

    A legacy installation's rows carry ``project_id IS NULL`` because they were
    written before the Control Plane knew about projects. Every one of them
    belongs, by definition, to the single project that installation was managing
    -- so they are adopted wholesale rather than being dropped, hidden, or
    recreated. Running this twice is a no-op: the second run finds no NULL rows.

    The legacy unprefixed project-dependent caches are moved to their
    per-project keys at the same time, so an existing installation's cached
    repository health and worktree snapshot stay visible under the project they
    actually describe instead of silently disappearing.
    """

    adopted = state.adopt_unscoped_rows(project_id)
    from .state import PROJECT_SCOPED_SETTING_KEYS, project_scoped_setting_key

    moved: list[str] = []
    for key in sorted(PROJECT_SCOPED_SETTING_KEYS):
        scoped_key = project_scoped_setting_key(key, project_id)
        legacy_value = state.get_control_setting(key)
        if legacy_value is not None and state.get_control_setting(scoped_key) is None:
            state.set_control_setting(scoped_key, legacy_value)
            moved.append(key)

    result = {"project_id": project_id, "adopted_rows": adopted, "moved_settings": moved}
    if sum(adopted.values()) or moved:
        state.record_event(
            category="project_registry",
            message=(
                f"migrated {sum(adopted.values())} legacy Control Plane records into project {project_id!r}"
            ),
            project_id=project_id,
        )
    state.set_control_setting(MIGRATION_MARKER_SETTING, json.dumps(result, sort_keys=True))
    return result


def ensure_octascene_project(state: State, repo_root: Path | str | None = None) -> ProjectContract | None:
    """Register OctaScene as a managed project if it is not registered yet.

    This is the auto-migration requirement: an existing installation must
    recognize its existing managed project without the operator recreating any
    configuration. The declarations come from ``octascene_project()`` -- the
    same CPX-01 compatibility adapter, resolving the same
    ``runner.canonical_repo_root()`` truth -- so nothing about what OctaScene is
    gets restated here.

    Deterministic and idempotent: once a row with the OctaScene project id
    exists, this returns it unchanged and never overwrites operator edits to it.
    Returns ``None`` (rather than raising) when the canonical OctaScene checkout
    cannot be resolved at all, so a Control Plane pointed at a missing or moved
    checkout still starts and can register a project through the UI.
    """

    existing = state.get_project(OCTASCENE_PROJECT_ID)
    if existing is not None:
        existing_root = Path(existing["local_repo_root"])
        # CPX-07: a leftover bootstrap that pointed "octascene" at the Octarel
        # code checkout is not a managed project. Drop it so health/select
        # cannot treat Octarel as OctaScene.
        try:
            self_registered = existing_root.resolve() == cp_code_root().resolve()
        except OSError:
            self_registered = False
        if self_registered and not _looks_like_octascene(existing_root):
            state.delete_project(OCTASCENE_PROJECT_ID)
            if selected_project_id(state) == OCTASCENE_PROJECT_ID:
                state.set_control_setting(SELECTED_PROJECT_SETTING, "")
        else:
            return row_to_contract(existing)
    try:
        candidate = Path(repo_root) if repo_root is not None else None
        if candidate is not None and not _looks_like_octascene(candidate):
            # Standalone Octarel code root must never be auto-registered as OctaScene.
            return None
        contract = octascene_project(repo_root)
    except ProjectRootError:
        return None
    state.upsert_project(contract_to_row(contract, enabled=True))
    state.record_event(
        category="project_registry",
        message=(
            f"auto-migrated existing installation: registered {contract.display_name} "
            f"({contract.github_remote}) at {contract.local_repo_root} as the first managed project"
        ),
        project_id=contract.project_id,
    )
    return contract


def bootstrap_registry(state: State, repo_root: Path | str | None = None) -> dict[str, Any]:
    """The one deterministic, idempotent registry startup path.

    Called once when a Control Plane process builds its command context. In
    order: ensure OctaScene is registered (auto-migration), adopt any legacy
    unscoped records into it, select a project if none is selected, and record
    the schema version. Every step is individually idempotent, so this is safe
    to run on every start, and a restart mid-way simply completes the remainder
    on the next run.
    """

    octascene = ensure_octascene_project(state, repo_root)
    migration: dict[str, Any] | None = None
    if octascene is not None and state.schema_version() < CURRENT_SCHEMA_VERSION:
        # Legacy adoption runs exactly once, gated on the persisted schema
        # version rather than re-running blindly on every start. An independent
        # review (Grok/xAI) noted that an unconditional re-run would attribute
        # *any* later ``project_id IS NULL`` row to OctaScene -- including work
        # that ran while a different project was selected. After this one-time
        # migration, a NULL row is a bug in a writer, not something to silently
        # relabel; every writer is now stamped at creation instead.
        migration = migrate_legacy_state_to_project(state, octascene.project_id)

    current = selected_project_id(state)
    if current is None or state.get_project(current) is None:
        candidates = state.list_projects(enabled_only=True)
        if candidates:
            preferred = next(
                (row for row in candidates if row["project_id"] == OCTASCENE_PROJECT_ID), candidates[0]
            )
            select_project(state, preferred["project_id"])

    # Only record the new schema version once legacy adoption has actually run.
    # An independent review (Grok/xAI) caught that bumping it unconditionally
    # meant a start where the OctaScene checkout could not be resolved (the
    # documented "still start so the operator can fix it" path) would mark the
    # database as migrated without adopting anything -- stranding every legacy
    # row as invisible for the life of the database, because the now-gated
    # adoption would never run again. Leaving the version alone lets the next
    # successful start complete the migration.
    if octascene is not None:
        state.set_schema_version(CURRENT_SCHEMA_VERSION)
    return {
        "octascene_project_id": octascene.project_id if octascene else None,
        "selected_project_id": selected_project_id(state),
        "migration": migration,
        "schema_version": state.schema_version(),
    }
