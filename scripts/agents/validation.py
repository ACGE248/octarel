"""Strict task-id and scope-path validation for the delegation tooling.

Every delegated run is keyed by a task id that becomes a directory name under
``.agent-output/``, and every caller-supplied scope path is resolved and checked
so a worker cannot be pointed at the secret store, the data tree, or anything
outside the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

# ``ENG-AGENT-01``, ``FND-08``, ``LFD-028`` ... uppercase segments joined by ``-``.
TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")

# Path segments a delegated worker must never be scoped to.
_FORBIDDEN_PARTS = frozenset(
    {
        ".aws",
        ".git",
        ".ssh",
        "data",
        ".venv",
        ".packaging-venv",
        "venv",
        "node_modules",
        "octages-ui-template",
    }
)
_FORBIDDEN_NAMES = frozenset({".env", ".netrc", ".npmrc", ".pypirc"})
# Case-insensitive markers that indicate a secret-bearing file.
_SECRET_MARKERS = ("secret", ".cloud_gpu", "credential", "launch.json")


class ValidationError(ValueError):
    """Raised when a task id or scope path fails validation."""


def validate_task_id(task_id: str) -> str:
    """Return ``task_id`` unchanged if it is a safe, well-formed reference."""

    if not task_id or not TASK_ID_RE.match(task_id):
        raise ValidationError(
            f"invalid task id {task_id!r}: expected uppercase segments joined by '-', e.g. 'ENG-AGENT-01'"
        )
    if len(task_id) > 64:
        raise ValidationError(f"invalid task id {task_id!r}: too long")
    return task_id


def validate_scope_path(raw: str, repo_root: Path, *, require_exists: bool = True) -> Path:
    """Resolve ``raw`` against ``repo_root`` and reject unsafe targets.

    Returns the resolved absolute path. Raises :class:`ValidationError` when the
    path escapes the repository, resolves to the repository root itself, names
    an ignored/vendored tree, or looks secret-bearing -- every one of those
    checks applies regardless of ``require_exists``. ``require_exists`` is
    ``True`` for every ordinary scoped worker (it must not be pointed at a
    path that isn't really there); a caller that only needs a path for a
    git pathspec -- never a filesystem read -- may pass ``False`` so a path a
    candidate diff deletes still resolves.
    """

    repo_root = repo_root.resolve()
    raw = (raw or "").strip()
    if not raw:
        raise ValidationError("empty scope path")
    if "\x00" in raw:
        raise ValidationError("scope path contains a null byte")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()

    try:
        rel = resolved.relative_to(repo_root)
    except ValueError:
        raise ValidationError(f"scope path {raw!r} is outside the repository") from None

    parts = rel.parts
    if not parts:
        raise ValidationError("scope path resolves to the repository root; narrow it")
    if require_exists and not resolved.exists():
        raise ValidationError(f"scope path {raw!r} does not exist")
    if any(part in _FORBIDDEN_PARTS for part in parts):
        raise ValidationError(f"scope path {raw!r} points into an ignored or vendored tree")
    if any(part in _FORBIDDEN_NAMES for part in parts):
        raise ValidationError(f"scope path {raw!r} names a protected file")
    lowered = rel.as_posix().lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise ValidationError(f"scope path {raw!r} looks secret-bearing; refusing")
    return resolved
