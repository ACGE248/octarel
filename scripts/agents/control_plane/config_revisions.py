"""Revisioned, runtime-editable Control Plane configuration (ENG-PC-08).

This module is deliberately an explicit audit of the mixed ``control_settings``
table.  Adding a row to that table does not make it configuration and does not
make it revisionable.  New runtime settings must be deliberately added here,
with validation, before the revision API can persist or replay them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePath, PureWindowsPath
from typing import Any

from ..redaction import redact_text


class ConfigurationRevisionError(ValueError):
    """Base class for a refused configuration operation."""


class StaleConfigurationError(ConfigurationRevisionError):
    """The caller's predecessor is no longer the current revision."""


class InvalidConfigurationError(ConfigurationRevisionError):
    """A proposed snapshot is outside the runtime configuration contract."""


# These are the only values in control_settings owned by runtime operators.
# Everything else is a cache, internal marker, transient operation record, or
# repository-controlled input.  Keep this literal set easy to audit in review.
REVISIONABLE_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "held_task_ids",
        "max_global_workers",
        "max_heavy_workers",
        "max_provider_workers",
        "max_read_workers",
        "max_write_workers",
        "stop_after_current",
    }
)

CONFIGURATION_OWNERSHIP: dict[str, Any] = {
    "runtime_editable": sorted(REVISIONABLE_SETTING_KEYS),
    "repository_controlled": [
        {
            "source": "worker_registry",
            "path": "scripts/agents/workers.json",
            "reason": "worker definitions remain repository-controlled",
        },
        {
            "source": "canonical_policies",
            "path": ".agents/",
            "reason": "canonical policy files remain repository-controlled",
        },
    ],
    "excluded_control_settings": [
        {
            "keys": ["repository_health_cache", "worktree_status_cache"],
            "reason": "derived project-dependent caches",
        },
        {
            "keys": ["schema_version", "selected_project_id", "project migration markers"],
            "reason": "internal schema and project-selection markers",
        },
        {
            "keys": ["merge_prepare:<operation_id>"],
            "reason": "transient operation state that may contain operator paths",
        },
    ],
}

_ABSOLUTE_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ABSOLUTE_PATH_IN_TEXT_RE = re.compile(r"(?:^|[\s\"'=:(])(?:/[^\s\"',}\]]+|[A-Za-z]:[\\/][^\s\"',}\]]+)")


def _iter_strings(value: Any):  # noqa: ANN202 - small recursive iterator
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_strings(item)


def _contains_absolute_path(value: str) -> bool:
    candidates: list[Any] = [value]
    try:
        candidates.append(json.loads(value))
    except (json.JSONDecodeError, TypeError):
        pass
    for candidate in candidates:
        for text in _iter_strings(candidate):
            if PurePath(text).is_absolute() or PureWindowsPath(text).is_absolute():
                return True
            if _ABSOLUTE_WINDOWS_PATH_RE.match(text) or _ABSOLUTE_PATH_IN_TEXT_RE.search(text):
                return True
    return False


def _validate_safe_value(key: str, value: str) -> None:
    if redact_text(value) != value:
        raise InvalidConfigurationError(f"{key} contains secret-shaped data; configuration was not stored")
    if _contains_absolute_path(value):
        raise InvalidConfigurationError(f"{key} contains an absolute path; configuration was not stored")


def validate_revision_attribution(actor: str, reason: str) -> tuple[str, str]:
    """Validate immutable metadata with the same no-secret/no-path rule as values."""

    if not isinstance(actor, str) or not actor.strip():
        raise InvalidConfigurationError("configuration actor is required")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidConfigurationError("configuration reason is required")
    normalized_actor = actor.strip()
    normalized_reason = reason.strip()
    _validate_safe_value("configuration actor", normalized_actor)
    _validate_safe_value("configuration reason", normalized_reason)
    return normalized_actor, normalized_reason


def validate_configuration(settings: Mapping[str, str]) -> dict[str, str]:
    """Validate and normalize one complete runtime configuration snapshot."""

    unknown = sorted(set(settings) - REVISIONABLE_SETTING_KEYS)
    if unknown:
        raise InvalidConfigurationError(
            "settings are not runtime-revisionable: " + ", ".join(unknown)
        )

    normalized: dict[str, str] = {}
    for key, raw_value in settings.items():
        if not isinstance(raw_value, str):
            raise InvalidConfigurationError(f"{key} must be stored as a string")
        value = raw_value.strip() if key.startswith("max_") else raw_value
        _validate_safe_value(key, value)
        if key.startswith("max_"):
            try:
                count = int(value)
            except ValueError:
                raise InvalidConfigurationError(f"{key} must be an integer") from None
            if count < 1:
                raise InvalidConfigurationError(f"{key} must be at least 1")
            value = str(count)
        elif key == "stop_after_current" and value not in {"0", "1"}:
            raise InvalidConfigurationError("stop_after_current must be '0' or '1'")
        elif key == "held_task_ids":
            try:
                task_ids = json.loads(value)
            except json.JSONDecodeError:
                raise InvalidConfigurationError("held_task_ids must be a JSON array of strings") from None
            if not isinstance(task_ids, list) or any(
                not isinstance(task_id, str) or not task_id for task_id in task_ids
            ):
                raise InvalidConfigurationError("held_task_ids must be a JSON array of non-empty strings")
            if len(set(task_ids)) != len(task_ids):
                raise InvalidConfigurationError("held_task_ids must not contain duplicates")
            value = json.dumps(sorted(task_ids))
        normalized[key] = value
    return normalized


def validate_changes(changes: Mapping[str, str]) -> dict[str, str]:
    """Validate a non-empty partial update before merging it into a snapshot."""

    if not isinstance(changes, Mapping) or not changes:
        raise InvalidConfigurationError("at least one configuration change is required")
    return validate_configuration(changes)
