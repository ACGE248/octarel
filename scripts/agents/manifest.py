"""Machine-readable manifest, human summary, and compact stdout pointer.

Detailed evidence goes to files under
``.agent-output/<TASK-ID>/<worker>/<RUN-ID>/`` so terminal truncation cannot lose
it and retries cannot overwrite it. Stdout gets only a short pointer block.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact_command, redact_text

# Terminal states a delegated run can end in.
RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_UNSUPPORTED = "UNSUPPORTED"
RESULT_BLOCKED = "BLOCKED"
RESULT_DRY_RUN = "DRY_RUN"

MANIFEST_VERSION = 2


@dataclass
class RunRecord:
    """Everything known about one delegated worker invocation."""

    task: str
    role: str
    worker: str
    planned_execution_system: str
    planned_provider: str
    planned_model: str
    planned_intensity: str
    requested_command: list[str]
    why_this_worker: str = ""
    actual_execution_system: str = ""
    actual_provider: str = ""
    actual_model: str = ""
    actual_intensity: str = ""
    result: str = RESULT_BLOCKED
    exit_status: int | None = None
    duration_seconds: float | None = None
    started_at: str = ""
    finished_at: str = ""
    files_changed: list[str] | None = None
    tests_or_checks: list[str] | None = None
    notes: list[str] | None = None
    candidate_tree_sha: str | None = None
    policy_manifest: dict[str, Any] | None = None

    def to_manifest(self, *, paths: dict[str, str]) -> dict[str, Any]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "task": self.task,
            "role": self.role,
            "worker": self.worker,
            "planned": {
                "execution_system": self.planned_execution_system,
                "provider": self.planned_provider,
                "model": self.planned_model,
                "intensity": self.planned_intensity,
                "why_this_worker": redact_text(self.why_this_worker),
            },
            "requested_command": redact_command(self.requested_command),
            "actual": {
                "execution_system": self.actual_execution_system,
                "provider": self.actual_provider,
                "model": self.actual_model,
                "intensity": self.actual_intensity,
            },
            "result": self.result,
            "exit_status": self.exit_status,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "files_changed": sorted(redact_text(path) for path in (self.files_changed or [])),
            "tests_or_checks": [redact_text(check) for check in (self.tests_or_checks or [])],
            "notes": [redact_text(note) for note in (self.notes or [])],
            "candidate_tree_sha": self.candidate_tree_sha,
            "policy_manifest": self.policy_manifest or {},
            "paths": paths,
            "redaction_applied": True,
            "ci_invocation_allowed": False,
        }


def compact_pointer(manifest: dict[str, Any]) -> str:
    """The short block written to stdout so CLI truncation cannot lose evidence."""

    actual = manifest["actual"]
    paths = manifest["paths"]
    lines = [
        f"TASK: {manifest['task']}",
        f"ROLE: {manifest['role']}",
        f"STATUS: {manifest['result']}",
        f"SYSTEM: {actual['execution_system'] or '(not run)'}",
        f"PROVIDER: {actual['provider'] or '(not run)'}",
        f"MODEL: {actual['model'] or '(not run)'}",
        f"EXIT: {manifest['exit_status'] if manifest['exit_status'] is not None else 'n/a'}",
        f"SUMMARY: {paths['summary']}",
        f"MANIFEST: {paths['manifest']}",
        f"LOG: {paths['log']}",
    ]
    return "\n".join(lines)


def render_summary(manifest: dict[str, Any]) -> str:
    planned = manifest["planned"]
    actual = manifest["actual"]
    md = [
        f"# Delegation summary — {manifest['task']} / {manifest['worker']}",
        "",
        f"- **Result:** {manifest['result']}",
        f"- **Role:** {manifest['role']}",
        f"- **Exit status:** {manifest['exit_status']}",
        f"- **Duration (s):** {manifest['duration_seconds']}",
        f"- **Started:** {manifest['started_at']}",
        f"- **Finished:** {manifest['finished_at']}",
        "",
        "## Planned",
        f"- System / provider / model: {planned['execution_system']} / {planned['provider']} / "
        f"{planned['model'] or '(unset)'}",
        f"- Intensity: {planned['intensity']}",
        f"- Why this worker: {planned['why_this_worker'] or '(not recorded)'}",
        "",
        "## Actual",
        f"- System / provider / model: {actual['execution_system']} / {actual['provider']} / "
        f"{actual['model'] or '(unset)'}",
        f"- Intensity: {actual['intensity']}",
        "",
        "## Requested command (redacted)",
        "```",
        " ".join(manifest["requested_command"]),
        "```",
        "",
        "## Files changed",
    ]
    changed = manifest["files_changed"]
    md += [f"- `{path}`" for path in changed] if changed else ["- (none)"]
    md += ["", "## Tests / checks"]
    checks = manifest["tests_or_checks"]
    md += [f"- {item}" for item in checks] if checks else ["- (none recorded)"]
    md += ["", "## Notes"]
    notes = manifest["notes"]
    md += [f"- {redact_text(note)}" for note in notes] if notes else ["- (none)"]
    md += ["", f"Full log: `{manifest['paths']['log']}`", ""]
    return "\n".join(md)


def write_artifacts(worker_dir: Path, record: RunRecord, *, log_text: str, repo_root: Path) -> dict[str, str]:
    """Write ``manifest.json``, ``summary.md`` and ``logs/run.log``.

    All paths in the returned mapping are repository-relative for compact
    display. ``log_text`` is redacted before it is written.
    """

    logs_dir = worker_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "run.log"
    log_path.write_text(redact_text(log_text or ""), encoding="utf-8")

    manifest_path = worker_dir / "manifest.json"
    summary_path = worker_dir / "summary.md"

    def _rel(path: Path) -> str:
        try:
            return path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:  # pragma: no cover - artifacts always live in the repo
            return str(path)

    paths = {
        "manifest": _rel(manifest_path),
        "summary": _rel(summary_path),
        "log": _rel(log_path),
    }
    manifest = record.to_manifest(paths=paths)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(render_summary(manifest), encoding="utf-8")
    return paths
