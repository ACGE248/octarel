"""Typed, safe envelope for append-only execution events (ENG-PC-04).

This is deliberately a contract over the existing ``events`` table, not a
second history store.  Large output remains in the existing ``.agent-output``
tree; an event may retain only validated repository-relative pointers to files
that already exist there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..redaction import redact_text

EVENT_CLASSES = frozenset(
    {
        "lifecycle",
        "wake",
        "route",
        "lease",
        "session",
        "process",
        "adapter_tool",
        "checkpoint",
        "usage",
        "review",
        "gate",
        "pr_merge",
        "wait_attention",
        "approval",
    }
)

# Reuse the usage-telemetry provenance vocabulary.  NOT_REPORTED is a truthful
# event/data status, not a new provenance class.
PROVENANCE_CLASSES = frozenset({"MEASURED", "DERIVED", "UNKNOWN", "NOT_EXPOSED"})
STATUS_NOT_REPORTED = "NOT_REPORTED"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_HOST_PATH_RE = re.compile(r"(?<![A-Za-z0-9_:])(?:file://)?/(?:[^\s,;]+)")
_WINDOWS_HOST_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s,;]+")
_MAX_MESSAGE_CHARS = 2_000
_MAX_DATA_CHARS = 16_000


@dataclass(frozen=True)
class RunEvent:
    """One typed event request; sequence and timestamp are assigned by SQLite."""

    run_id: str
    event_class: str
    event_type: str
    source: str
    provenance: str
    message: str
    # Optional legacy category retained for existing consumers while
    # ``event_class`` supplies the normalized RunEvent domain.
    category: str | None = None
    task_id: str | None = None
    provider: str | None = None
    level: str = "info"
    project_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, str] = field(default_factory=dict)


def _token(name: str, value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{name} must be a non-empty stable token")
    return value


def _optional_token(name: str, value: str | None) -> str | None:
    return _token(name, value) if value is not None else None


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        clean = sanitize_text(value)
        # Host-layout disclosure is never useful structured event data.  Paths
        # belong only in the separately validated evidence mapping.
        if clean.startswith(("/", "file://")) or _WINDOWS_ABSOLUTE_RE.match(clean):
            return "[absolute path omitted]"
        return clean
    raise ValueError(f"run event data contains unsupported value type {type(value).__name__}")


def sanitize_text(value: str) -> str:
    """Redact credentials and replace host-absolute paths wherever they occur."""

    clean = redact_text(value)
    clean = _HOST_PATH_RE.sub("[absolute path omitted]", clean)
    return _WINDOWS_HOST_PATH_RE.sub("[absolute path omitted]", clean)


def sanitize_data(value: Any) -> Any:
    """Redact a JSON-shaped value and remove absolute host paths recursively."""

    if isinstance(value, Mapping):
        cleaned = {sanitize_text(str(key)): sanitize_data(item) for key, item in value.items()}
    elif isinstance(value, (list, tuple)):
        cleaned = [sanitize_data(item) for item in value]
    else:
        cleaned = _safe_scalar(value)
    if len(json.dumps(cleaned, sort_keys=True, separators=(",", ":"))) > _MAX_DATA_CHARS:
        raise ValueError("run event data exceeds the safe structured-summary limit")
    return cleaned


def validate_evidence_pointers(repo_root: Path, pointers: Mapping[str, str]) -> dict[str, str]:
    """Return existing, regular ``.agent-output`` pointers or fail closed.

    Resolution is performed before persistence, including a resolved-parent
    containment check, so ``..`` and symlinks cannot turn a relative pointer
    into arbitrary host filesystem access.
    """

    root = Path(repo_root).resolve()
    evidence_root = (root / ".agent-output").resolve()
    validated: dict[str, str] = {}
    for raw_key, raw_path in pointers.items():
        key = _token("evidence key", str(raw_key))
        if not isinstance(raw_path, str):
            raise ValueError(f"evidence pointer {key!r} must be a string")
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != ".agent-output":
            raise ValueError(f"evidence pointer {key!r} must stay under .agent-output")
        candidate = (root / Path(*path.parts)).resolve()
        try:
            candidate.relative_to(evidence_root)
        except ValueError:
            raise ValueError(f"evidence pointer {key!r} escapes .agent-output") from None
        if not candidate.is_file():
            raise ValueError(f"evidence pointer {key!r} does not resolve to an existing file")
        validated[key] = path.as_posix()
    return validated


def normalize_run_event(event: RunEvent, *, repo_root: Path | None) -> dict[str, Any]:
    """Validate/redact an envelope into the exact database payload."""

    event_class = _token("event_class", event.event_class)
    if event_class not in EVENT_CLASSES:
        raise ValueError(f"unsupported run event class {event_class!r}")
    provenance = _token("provenance", event.provenance)
    if provenance not in PROVENANCE_CLASSES:
        raise ValueError(f"unsupported run event provenance {provenance!r}")
    if event.evidence and repo_root is None:
        raise ValueError("repo_root is required when a run event carries evidence pointers")
    message = sanitize_text(str(event.message)).strip()[:_MAX_MESSAGE_CHARS]
    if not message:
        raise ValueError("run event message must not be empty")
    if event.level not in {"info", "warning", "error"}:
        raise ValueError("run event level must be info, warning, or error")
    return {
        "run_id": _token("run_id", event.run_id),
        "event_class": event_class,
        "category": _optional_token("category", event.category) or event_class,
        "event_type": _token("event_type", event.event_type),
        "source": _token("source", event.source),
        "provenance": provenance,
        "message": message,
        "task_id": _optional_token("task_id", event.task_id),
        "provider": _optional_token("provider", event.provider),
        "level": event.level,
        "project_id": _optional_token("project_id", event.project_id),
        "data": sanitize_data(event.data),
        "evidence": validate_evidence_pointers(repo_root, event.evidence) if event.evidence else {},
    }
