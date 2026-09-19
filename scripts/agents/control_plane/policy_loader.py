"""ENG-CP-04 (issue #169): generic selected-project policy loader.

Loads the policy entrypoints the selected :class:`~.project.ProjectContract`
declares, from that project's checkout (or an explicit candidate/worktree).
Generic Control Plane code must not assume ``AGENTS.md`` exists. Missing or
invalid declared policy fails clearly. Process ``cwd`` and the Control Plane
code checkout are never silent fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .project import ProjectContract, cp_code_root

_SENSITIVE_PARTS = frozenset({"data", ".env", "credentials", "secrets"})


class PolicyLoadError(ValueError):
    """Declared project policy is missing, escapes the repository, or is invalid."""


@dataclass(frozen=True)
class LoadedProjectPolicy:
    """Declared policy files read fresh from the selected project's tree."""

    project_id: str
    root: Path
    entrypoints: tuple[str, ...]
    texts: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "root": str(self.root),
            "entrypoints": list(self.entrypoints),
            "paths": list(self.texts),
        }


def _safe_declared_file(root: Path, raw: str) -> tuple[str, Path]:
    normalized = PurePosixPath(raw.replace("\\", "/")).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or normalized.startswith("../"):
        raise PolicyLoadError(f"invalid declared policy path {raw!r}")
    if ".." in PurePosixPath(normalized).parts:
        raise PolicyLoadError(f"declared policy path escapes the repository: {raw!r}")
    if any(part.lower() in _SENSITIVE_PARTS or part.lower().startswith(".env") for part in PurePosixPath(normalized).parts):
        raise PolicyLoadError(f"sensitive declared policy path is forbidden: {raw!r}")
    path = (root / normalized).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PolicyLoadError(f"declared policy path escapes the repository: {raw!r}")
    if not path.is_file():
        raise PolicyLoadError(f"declared policy does not exist: {normalized} (root={root})")
    return normalized, path


def load_project_policy(
    project: ProjectContract,
    *,
    root: Path | None = None,
) -> LoadedProjectPolicy:
    """Read every declared ``policy_entrypoints`` file from ``root``.

    ``root`` defaults to ``project.local_repo_root``. Callers pass a candidate
    worktree when policy must be loaded from the exact tree under test. This
    never consults process ``cwd`` and never substitutes the Control Plane
    code checkout for a missing project file.
    """

    source_root = Path(root) if root is not None else project.local_repo_root
    if not source_root.exists() or not source_root.is_dir():
        raise PolicyLoadError(
            f"project {project.project_id!r} policy root does not exist or is not a directory: {source_root!r}"
        )
    if not project.policy_entrypoints:
        raise PolicyLoadError(
            f"project {project.project_id!r} declares no policy_entrypoints"
        )
    texts: dict[str, str] = {}
    for relative in project.policy_entrypoints:
        normalized, path = _safe_declared_file(source_root, relative)
        try:
            texts[normalized] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyLoadError(
                f"project {project.project_id!r} could not read declared policy {normalized}: {exc}"
            ) from exc
        if not texts[normalized].strip():
            raise PolicyLoadError(
                f"project {project.project_id!r} declared policy {normalized} is empty"
            )
    return LoadedProjectPolicy(
        project_id=project.project_id,
        root=source_root,
        entrypoints=tuple(texts),
        texts=texts,
    )


def orchestration_policy_root() -> Path:
    """Where Control Plane role/workflow/provider adapter files live.

    Distinct from selected-project policy. Generic repositories may have no
    ``.agents/`` tree; worker/role composition still has to come from the
    Control Plane checkout. This is never a fallback for a *declared*
    project policy file.
    """

    return cp_code_root()
