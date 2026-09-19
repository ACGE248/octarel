"""ENG-CP-06 / CPX-06: canonical-runtime cutover helpers.

Snapshot embedded Octages orchestration state and standalone Octarel state
without deleting either, record before/after counts, and emit the operator
rollback procedure. Machine-specific paths are arguments, never committed
defaults.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .state import DB_FILENAME, STATE_DIRNAME
from .state_migration import (
    _file_sha256,
    _resolve_source_db,
    _table_counts,
    migrate_orchestrator_state,
)

RUNTIME_KIND = "octarel"
RUNTIME_IDENTITY_FILENAME = "canonical-runtime.json"
CHECKPOINT_COUNTS_FILENAME = "counts.json"
CHECKPOINT_META_FILENAME = "checkpoint.json"
ROLLBACK_FILENAME = "ROLLBACK.md"

HISTORY_TABLES = (
    "tasks",
    "runbooks",
    "events",
    "worktrees",
    "provider_states",
    "operations",
    "terminal_commands",
    "usage_governance",
    "task_intake_claims",
    "dispatch_decisions",
    "projects",
    "control_settings",
)

LAUNCHD_DASHBOARD_LABEL = "com.octascene.orchestrator-dashboard"
LAUNCHD_TUNNEL_LABEL = "com.octascene.orchestrator-tunnel"
DEFAULT_DASHBOARD_PORT = 8877


@dataclass
class CheckpointReport:
    ok: bool
    checkpoint_dir: str
    created_at: str
    octages_db: str | None = None
    octarel_db: str | None = None
    octages_sha256: str | None = None
    octarel_sha256: str | None = None
    octages_tables: dict[str, int] = field(default_factory=dict)
    octarel_tables: dict[str, int] = field(default_factory=dict)
    copied: list[str] = field(default_factory=list)
    reason: str = ""

    def as_text(self) -> str:
        lines = [
            f"ok={self.ok}",
            f"checkpoint_dir={self.checkpoint_dir}",
            f"created_at={self.created_at}",
            f"octages_db={self.octages_db or ''}",
            f"octarel_db={self.octarel_db or ''}",
            f"octages_sha256={self.octages_sha256 or ''}",
            f"octarel_sha256={self.octarel_sha256 or ''}",
            f"reason={self.reason}",
        ]
        if self.octages_tables:
            lines.append("octages_tables=" + _fmt_tables(self.octages_tables))
        if self.octarel_tables:
            lines.append("octarel_tables=" + _fmt_tables(self.octarel_tables))
        if self.copied:
            lines.append("copied=" + ",".join(self.copied))
        return "\n".join(lines)


def _fmt_tables(tables: dict[str, int]) -> str:
    return ",".join(f"{k}:{v}" for k, v in sorted(tables.items()))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sqlite_consistent_copy(source_db: Path, dest_db: Path) -> None:
    """Copy ``source_db`` (including WAL) via the SQLite backup API."""

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest_db))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def snapshot_state_tree(source: Path, dest_dir: Path) -> dict[str, str]:
    """Snapshot a ``.orchestrator-state`` directory. Never modifies ``source``."""

    source_db = _resolve_source_db(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_db = dest_dir / DB_FILENAME
    sqlite_consistent_copy(source_db, dest_db)
    copied = {"db": str(dest_db)}
    reports = source_db.parent / "reports"
    if reports.is_dir():
        dest_reports = dest_dir / "reports"
        if dest_reports.exists():
            shutil.rmtree(dest_reports)
        shutil.copytree(reports, dest_reports)
        copied["reports"] = str(dest_reports)
    return copied


def write_cutover_checkpoint(
    *,
    destination: Path,
    octages_state: Path | None,
    octarel_state: Path | None,
    extra: dict[str, str] | None = None,
) -> CheckpointReport:
    """Write a deterministic pre-cutover checkpoint. Never deletes sources."""

    created = _utc_now()
    dest = destination.expanduser().resolve() / created
    dest.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    octages_tables: dict[str, int] = {}
    octarel_tables: dict[str, int] = {}
    octages_sha = None
    octarel_sha = None
    octages_db_out = None
    octarel_db_out = None

    if octages_state is not None:
        octages_dir = dest / "octages-state"
        snapshot_state_tree(octages_state, octages_dir)
        octages_db_out = str(octages_dir / DB_FILENAME)
        octages_sha = _file_sha256(Path(octages_db_out))
        octages_tables = _table_counts(Path(octages_db_out))
        copied.append("octages-state/")

    if octarel_state is not None:
        try:
            _resolve_source_db(octarel_state)
        except Exception:
            octarel_state = None
        else:
            octarel_dir = dest / "octarel-state-before"
            snapshot_state_tree(octarel_state, octarel_dir)
            octarel_db_out = str(octarel_dir / DB_FILENAME)
            octarel_sha = _file_sha256(Path(octarel_db_out))
            octarel_tables = _table_counts(Path(octarel_db_out))
            copied.append("octarel-state-before/")

    meta = {
        "created_at": created,
        "octages_state": str(octages_state) if octages_state else None,
        "octarel_state": str(octarel_state) if octarel_state else None,
        "octages_sha256": octages_sha,
        "octarel_sha256": octarel_sha,
        "extra": extra or {},
    }
    (dest / CHECKPOINT_META_FILENAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (dest / CHECKPOINT_COUNTS_FILENAME).write_text(
        json.dumps({"octages": octages_tables, "octarel_before": octarel_tables}, indent=2) + "\n",
        encoding="utf-8",
    )
    (dest / ROLLBACK_FILENAME).write_text(rollback_procedure_text(), encoding="utf-8")
    copied.extend([CHECKPOINT_META_FILENAME, CHECKPOINT_COUNTS_FILENAME, ROLLBACK_FILENAME])

    return CheckpointReport(
        ok=True,
        checkpoint_dir=str(dest),
        created_at=created,
        octages_db=octages_db_out,
        octarel_db=octarel_db_out,
        octages_sha256=octages_sha,
        octarel_sha256=octarel_sha,
        octages_tables=octages_tables,
        octarel_tables=octarel_tables,
        copied=copied,
        reason="pre-cutover checkpoint written; sources left in place",
    )


def adopt_octages_state(
    *,
    source: Path,
    destination: Path,
    dry_run: bool = False,
) -> object:
    """Import latest embedded state into Octarel. Source is never deleted."""

    return migrate_orchestrator_state(
        source=source,
        destination=destination,
        dry_run=dry_run,
        replace_unmigrated_destination=True,
    )


def runtime_identity_path(state_dir: Path) -> Path:
    return state_dir / RUNTIME_IDENTITY_FILENAME


def write_runtime_identity(
    *,
    state_dir: Path,
    code_root: Path,
    pid: int,
    port: int = DEFAULT_DASHBOARD_PORT,
    selected_project_id: str | None = None,
    selected_project_root: str | None = None,
) -> Path:
    payload = {
        "runtime": RUNTIME_KIND,
        "pid": pid,
        "port": port,
        "code_root": str(code_root),
        "state_dir": str(state_dir),
        "selected_project_id": selected_project_id,
        "selected_project_root": selected_project_root,
        "cwd": os.getcwd(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path = runtime_identity_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def read_runtime_identity(state_dir: Path) -> dict | None:
    path = runtime_identity_path(state_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_runtime_identity(state_dir: Path) -> None:
    path = runtime_identity_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def meaningful_history_counts(tables: dict[str, int]) -> dict[str, int]:
    keys = ("tasks", "runbooks", "events", "worktrees", "dispatch_decisions", "task_intake_claims", "projects")
    return {key: int(tables.get(key, 0)) for key in keys}


def rollback_procedure_text() -> str:
    return """# CPX-06 rollback — restore embedded Octages Control Center

Do this if standalone Octarel is unhealthy on port 8877 or if
https://dev.octascene.com is partially operational after cutover.
Do not leave the tunnel pointing at a dead origin.

The Cloudflare Tunnel (`com.octascene.orchestrator-tunnel`) stays running.
It already forwards `dev.octascene.com` to `http://127.0.0.1:8877`. Rollback
swaps which local process owns that port. Cloudflare Access is unchanged.

## 1. Stop standalone Octarel on 8877

```sh
launchctl bootout gui/$(id -u)/com.octascene.orchestrator-dashboard
```

If the process was started in a terminal instead of launchd:

```sh
# identify the listener, then terminate only that PID
lsof -nP -iTCP:8877 -sTCP:LISTEN
```

Confirm port 8877 is free before starting the embedded daemon.

## 2. Restore the pre-cutover launchd plist

The cutover checkpoint copies the live dashboard plist. Restore it:

```sh
cp "$CHECKPOINT/launchd/com.octascene.orchestrator-dashboard.plist" \\
   ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist
```

If no checkpoint copy exists, the known-good embedded command is:

```
WorkingDirectory = <octages-checkout>
Program = <octages-checkout>/.venv/bin/python -m scripts.agents.orchestrator dashboard --host 127.0.0.1 --port 8877
```

Embedded fallback also requires:

```
export OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK=1
```

(the launchd EnvironmentVariables dict must include that key after CPX-06).

## 3. Verify

```sh
curl -fsS http://127.0.0.1:8877/api/cp-status
# cp_code_root should be the Octages checkout, not Octarel
open https://dev.octascene.com
```

Cloudflare Access login must still appear for unauthenticated visitors.

## 4. Leave Octarel state in place

Do not delete Octarel `.orchestrator-state/` or the original Octages
`.orchestrator-state/`. Rollback is a process/plist swap, not a data move.

## 5. Re-cutover later

After fixing Octarel, repeat the documented cutover: snapshot, stop embedded,
migrate (idempotent), start Octarel, confirm Access + health, then audit UI.
"""


def default_checkpoint_root(code_root: Path) -> Path:
    env = os.environ.get("OCTAREL_CUTOVER_CHECKPOINT_DIR")
    if env:
        return Path(env)
    return code_root / STATE_DIRNAME / "cutover-checkpoints"
