"""Read-only Agent Activity evidence for the Control Center worker viewer (OCTAREL-UI-01).

Reuses the existing ENG-AGENT-01 ``.agent-output/<task>/<worker>/<run_id>/`` evidence
tree and the existing redaction module. This module never executes a worker, never
opens a shell, and never reads any path outside ``.agent-output/<task>/<worker>/<run_id>/``
for a task/worker/run_id triple that the caller has already confirmed is in scope.

Security properties (see AGENTS.md / OCTAREL-UI-01):
- No arbitrary filesystem reads: every path is built from a validated slug and checked
  to still resolve inside the expected ``.agent-output`` subtree before any read.
- Redaction-on-read as defense in depth, even though the evidence on disk was already
  redacted at write time by :mod:`scripts.agents.redaction`.
- Bounded reads: log output is tailed to a byte cap, never loaded/streamed in full.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..redaction import redact_text

_AGENT_OUTPUT_DIRNAME = ".agent-output"

# Conservative slug: matches how task ids, worker names, and run ids are actually
# generated elsewhere in this codebase. Rejects '/', '..', and anything else that
# could escape the intended directory.
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]*[A-Za-z0-9])?$")

MAX_OUTPUT_BYTES = 200_000  # bounded history: never load/return more than this
MAX_SUMMARY_BYTES = 50_000


class UnsafeIdentifier(ValueError):
    """Raised when a task/worker/run_id segment cannot be a safe path component."""


def _validate_slug(value: str, *, field: str) -> str:
    if not value or not _SLUG_RE.match(value) or ".." in value:
        raise UnsafeIdentifier(f"invalid {field}: {value!r}")
    return value


def _agent_output_root(repo_root: Path) -> Path:
    return repo_root / _AGENT_OUTPUT_DIRNAME


def _run_dir(repo_root: Path, task_id: str, worker: str, run_id: str) -> Path:
    """Resolve the on-disk run directory, refusing anything that escapes the tree."""

    _validate_slug(task_id, field="task_id")
    _validate_slug(worker, field="worker")
    _validate_slug(run_id, field="run_id")
    root = _agent_output_root(repo_root).resolve()
    candidate = (root / task_id / worker / run_id).resolve()
    if candidate != root and root not in candidate.parents:
        # Defense in depth: even a passing slug should never resolve outside root.
        raise UnsafeIdentifier(f"resolved path escapes .agent-output: {candidate}")
    return candidate


@dataclass
class AttemptSummary:
    run_id: str
    result: str | None
    started_at: str | None
    finished_at: str | None
    exit_status: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_status": self.exit_status,
        }


def list_attempts(repo_root: Path, task_id: str, worker: str) -> list[dict[str, Any]]:
    """Every recorded attempt (run_id) for one task/worker pair, most recent first.

    Distinguishes same-role cards by run_id, per OCTAREL-UI-01. Returns ``[]`` for an
    unknown or not-yet-run task/worker pair rather than raising -- an empty attempt
    list is a normal, honest state, not an error.
    """

    _validate_slug(task_id, field="task_id")
    _validate_slug(worker, field="worker")
    worker_dir = _agent_output_root(repo_root) / task_id / worker
    if not worker_dir.is_dir():
        return []
    attempts: list[AttemptSummary] = []
    for run_dir in sorted(p for p in worker_dir.iterdir() if p.is_dir()):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        attempts.append(
            AttemptSummary(
                run_id=run_dir.name,
                result=data.get("result"),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                exit_status=data.get("exit_status"),
            )
        )
    attempts.sort(key=lambda a: a.started_at or "", reverse=True)
    return [a.to_dict() for a in attempts]


def _tail_text(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    """Read up to the last ``max_bytes`` of a text file. Returns (text, truncated)."""

    size = path.stat().st_size
    truncated = size > max_bytes
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - max_bytes)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        # Drop a possibly-partial first line so the tail starts cleanly.
        text = text.split("\n", 1)[-1]
    return text, truncated


def read_attempt(repo_root: Path, task_id: str, worker: str, run_id: str) -> dict[str, Any]:
    """Details + evidence + bounded live/historical output for one attempt.

    Reuses the existing manifest.json / summary.md / log file this worker's
    delegated run already wrote (see scripts/agents/manifest.py and orchestrate.py).
    Invents no new execution or logging architecture.
    """

    # Resolve once so relpaths computed from resolved run/log paths stay relative to it
    # even when the checkout sits behind a symlink (macOS /var -> /private/var temp roots).
    repo_root = Path(repo_root).resolve()
    run_dir = _run_dir(repo_root, task_id, worker, run_id)
    if not run_dir.is_dir():
        return {
            "task": task_id,
            "worker": worker,
            "run_id": run_id,
            "status": "missing",
            "details": None,
            "evidence": None,
            "output": {"status": "missing", "content": "", "truncated": False, "size": 0},
        }

    manifest_path = run_dir / "manifest.json"
    details: dict[str, Any] | None = None
    manifest_data: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest_data = {}
    if manifest_data:
        planned = manifest_data.get("planned") or {}
        actual = manifest_data.get("actual") or {}
        details = {
            "task": manifest_data.get("task"),
            "role": manifest_data.get("role"),
            "worker": manifest_data.get("worker"),
            "run_id": run_id,
            "planned": {
                "execution_system": planned.get("execution_system"),
                "provider": planned.get("provider"),
                "model": planned.get("model"),
                "intensity": planned.get("intensity"),
            },
            "actual": {
                "execution_system": actual.get("execution_system"),
                "provider": actual.get("provider"),
                "model": actual.get("model"),
                "intensity": actual.get("intensity"),
            },
            "requested_command": manifest_data.get("requested_command") or [],
            "result": manifest_data.get("result"),
            "exit_status": manifest_data.get("exit_status"),
            "duration_seconds": manifest_data.get("duration_seconds"),
            "started_at": manifest_data.get("started_at"),
            "finished_at": manifest_data.get("finished_at"),
            "candidate_tree_sha": manifest_data.get("candidate_tree_sha"),
            # Already redacted at write time (manifest.py); redact again on read
            # as defense in depth in case an older manifest predates a redaction
            # rule, or a future writer regresses it.
            "notes": [redact_text(str(n)) for n in (manifest_data.get("notes") or [])],
        }

    summary_text: str | None = None
    summary_path = run_dir / "summary.md"
    if summary_path.is_file():
        raw_summary, _ = _tail_text(summary_path, max_bytes=MAX_SUMMARY_BYTES)
        summary_text = redact_text(raw_summary)

    evidence = {
        "summary": summary_text,
        "manifest_relpath": str(manifest_path.relative_to(repo_root)) if manifest_path.is_file() else None,
        "log_relpath": None,
        "candidate_tree_sha": manifest_data.get("candidate_tree_sha"),
        "files_changed": [redact_text(p) for p in (manifest_data.get("files_changed") or [])],
        "tests_or_checks": [redact_text(t) for t in (manifest_data.get("tests_or_checks") or [])],
    }

    output_status = "missing"
    output_content = ""
    output_truncated = False
    output_size = 0
    log_path: Path | None = None
    # The manifest records its own log path (see manifest.write_artifacts: the
    # actual file lives at "<run_dir>/logs/run.log", not directly in run_dir).
    # Trust that recorded path only after confirming it still resolves inside
    # this attempt's own run_dir -- defense in depth against a hand-edited or
    # forged manifest pointing elsewhere on disk.
    manifest_log_rel = (manifest_data.get("paths") or {}).get("log")
    if manifest_log_rel:
        candidate = (repo_root / manifest_log_rel).resolve()
        try:
            candidate.relative_to(run_dir.resolve())
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            log_path = candidate
    if log_path is None:
        # Fall back to a direct glob for any manifest that predates the
        # logs/run.log convention or omits "paths" entirely.
        log_candidates = sorted(run_dir.glob("*.log")) or sorted(run_dir.glob("logs/*.log"))
        log_path = log_candidates[0] if log_candidates else None
    if log_path is not None:
        evidence["log_relpath"] = str(log_path.relative_to(repo_root))
        output_size = log_path.stat().st_size
        raw_text, output_truncated = _tail_text(log_path, max_bytes=MAX_OUTPUT_BYTES)
        output_content = redact_text(raw_text)
        output_status = "available" if output_content.strip() else "empty"

    return {
        "task": task_id,
        "worker": worker,
        "run_id": run_id,
        "status": "ok",
        "details": details,
        "evidence": evidence,
        "output": {
            "status": output_status,
            "content": output_content,
            "truncated": output_truncated,
            "size": output_size,
        },
    }
