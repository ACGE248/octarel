"""ENG-CP-05 (issue #171): idempotent import of Octages-hosted orchestration state.

Copies the existing ``.orchestrator-state/orchestrator.db`` (and sidecar WAL/SHM
plus ``reports/``) into standalone Octarel state. Never deletes or moves the
source. Restart-safe: a second run against the same source snapshot is a no-op.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .state import DB_FILENAME, STATE_DIRNAME

MIGRATION_SOURCE_SHA_SETTING = "octarel_migration_source_sha"
MIGRATION_SOURCE_PATH_SETTING = "octarel_migration_source_path"


class StateMigrationError(RuntimeError):
    """Migration could not proceed without risking history loss."""


@dataclass
class MigrationReport:
    ok: bool
    action: str
    source: str
    destination: str
    source_sha256: str | None = None
    tables: dict[str, int] = field(default_factory=dict)
    destination_tables: dict[str, int] = field(default_factory=dict)
    copied_sidecars: list[str] = field(default_factory=list)
    reason: str = ""

    def as_text(self) -> str:
        lines = [
            f"ok={self.ok}",
            f"action={self.action}",
            f"source={self.source}",
            f"destination={self.destination}",
            f"source_sha256={self.source_sha256 or ''}",
            f"reason={self.reason}",
        ]
        if self.tables:
            lines.append("tables=" + ",".join(f"{k}:{v}" for k, v in sorted(self.tables.items())))
        if self.destination_tables:
            lines.append(
                "destination_tables="
                + ",".join(f"{k}:{v}" for k, v in sorted(self.destination_tables.items()))
            )
        if self.copied_sidecars:
            lines.append("sidecars=" + ",".join(self.copied_sidecars))
        return "\n".join(lines)


def _resolve_source_db(source: Path) -> Path:
    path = source.expanduser().resolve()
    if path.is_dir():
        candidate = path / DB_FILENAME
        if not candidate.is_file():
            raise StateMigrationError(f"source directory has no {DB_FILENAME}: {path}")
        return candidate
    if path.is_file():
        return path
    raise StateMigrationError(f"source does not exist: {source}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}
    finally:
        conn.close()


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM control_settings WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _history_nonempty(counts: dict[str, int]) -> bool:
    """True when destination already holds operator history we must not clobber."""

    return any(int(counts.get(name, 0) or 0) for name in ("tasks", "runbooks"))


def migrate_orchestrator_state(
    *,
    source: Path,
    destination: Path,
    dry_run: bool = False,
    replace_unmigrated_destination: bool = False,
) -> MigrationReport:
    """Import ``source`` into ``destination`` using SQLite backup.

    ``destination`` is an Octarel ``.orchestrator-state`` directory. The source
    tree is never modified. If the destination already contains a successful
    import of this exact source snapshot, the call is a no-op.

    A destination that was initialized by a CPX-05 empty/bootstrap database
    (no tasks/runbooks, no prior migration SHA) may be replaced when
    ``replace_unmigrated_destination`` is true. A destination that already
    holds a *different* imported snapshot is always refused.
    """

    source_db = _resolve_source_db(source)
    dest_dir = destination.expanduser().resolve()
    dest_db = dest_dir / DB_FILENAME
    # Hash a WAL-consistent copy, not the raw file bytes (which omit WAL).
    with tempfile.NamedTemporaryFile(prefix="octarel-migrate-", suffix=".db", delete=False) as handle:
        tmp_copy = Path(handle.name)
    try:
        src_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
        tmp_conn = sqlite3.connect(str(tmp_copy))
        try:
            src_conn.backup(tmp_conn)
            tmp_conn.commit()
        finally:
            tmp_conn.close()
            src_conn.close()
        source_sha = _file_sha256(tmp_copy)
        tables = _table_counts(tmp_copy)
    finally:
        tmp_copy.unlink(missing_ok=True)

    if dest_db.is_file():
        dest_conn = sqlite3.connect(str(dest_db))
        try:
            previous = _setting(dest_conn, MIGRATION_SOURCE_SHA_SETTING)
        except sqlite3.Error:
            previous = None
        finally:
            dest_conn.close()
        if previous == source_sha:
            return MigrationReport(
                ok=True,
                action="already-imported",
                source=str(source_db),
                destination=str(dest_db),
                source_sha256=source_sha,
                tables=tables,
                destination_tables=_table_counts(dest_db),
                reason="destination already holds this exact source snapshot",
            )
        dest_conn = sqlite3.connect(str(dest_db))
        try:
            dest_counts = {
                row[0]: int(dest_conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0])
                for row in dest_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        except sqlite3.Error:
            dest_counts = {}
        finally:
            dest_conn.close()
        nonempty = {name: count for name, count in dest_counts.items() if count and name != "control_settings"}
        if nonempty and previous and previous != source_sha:
            raise StateMigrationError(
                "destination already contains a different imported snapshot; "
                "refusing to overwrite existing Octarel history"
            )
        if nonempty and previous is None and _history_nonempty(dest_counts) and not replace_unmigrated_destination:
            raise StateMigrationError(
                "destination already contains tasks or runbooks without a recorded migration SHA; "
                "pass replace_unmigrated_destination to adopt Octages state over this database"
            )

    if dry_run:
        return MigrationReport(
            ok=True,
            action="would-import",
            source=str(source_db),
            destination=str(dest_db),
            source_sha256=source_sha,
            tables=tables,
            reason="dry-run; source was not modified",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(source_db))
    dest_conn = sqlite3.connect(str(dest_db))
    try:
        src_conn.backup(dest_conn)
        dest_conn.execute(
            "INSERT INTO control_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (MIGRATION_SOURCE_SHA_SETTING, source_sha),
        )
        dest_conn.execute(
            "INSERT INTO control_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (MIGRATION_SOURCE_PATH_SETTING, str(source_db)),
        )
        dest_conn.commit()
    finally:
        dest_conn.close()
        src_conn.close()

    copied: list[str] = []
    source_dir = source_db.parent
    reports = source_dir / "reports"
    if reports.is_dir():
        dest_reports = dest_dir / "reports"
        if dest_reports.exists():
            shutil.rmtree(dest_reports)
        shutil.copytree(reports, dest_reports)
        copied.append("reports/")

    dest_tables = _table_counts(dest_db)
    return MigrationReport(
        ok=True,
        action="imported",
        source=str(source_db),
        destination=str(dest_db),
        source_sha256=source_sha,
        tables=tables,
        destination_tables=dest_tables,
        copied_sidecars=copied,
        reason="sqlite backup completed; source left in place",
    )


def default_octarel_state_dir(code_root: Path) -> Path:
    env = os.environ.get("OCTAREL_STATE_DIR")
    if env:
        return Path(env)
    return code_root / STATE_DIRNAME
