"""SQLite-backed durable store for the orchestrator daemon.

Chosen over hand-rolled JSON+locking so the dashboard can read state safely
while the daemon writes. This database is Octarel-owned orchestration state
under the git-ignored, root-anchored ``.orchestrator-state/`` directory,
distinct from ENG-AGENT-01's ``.agent-output/`` audit-evidence tree and from
every managed project's own product database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .models import (
    PERMISSION_STANDARD,
    Event,
    ProviderState,
    Runbook,
    Task,
    WorktreeRecord,
    utc_now_iso,
)

STATE_DIRNAME = ".orchestrator-state"
DB_FILENAME = "orchestrator.db"

_T = TypeVar("_T")


def _serialized(method: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize access to the one SQLite connection shared by API threads."""

    @wraps(method)
    def wrapper(self: "State", *args: Any, **kwargs: Any) -> _T:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    worker TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'write',
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    dependencies TEXT NOT NULL DEFAULT '[]',
    pid INTEGER,
    worktree TEXT,
    command TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_states (
    name TEXT PRIMARY KEY,
    execution_system TEXT NOT NULL,
    provider TEXT NOT NULL,
    cost_class TEXT NOT NULL,
    state TEXT NOT NULL,
    configured INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_probe_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    path TEXT PRIMARY KEY,
    branch TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    lock_holder TEXT,
    managed INTEGER NOT NULL DEFAULT 0,
    discovered_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    task_id TEXT,
    provider TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS terminal_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    branch TEXT,
    command TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS control_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ENG-CP-03 (issue #165): the durable managed-project registry. Every column
-- is a *declaration* about where/how to obtain repository truth -- the same
-- fields CPX-01's ``ProjectContract`` already defines, persisted rather than
-- rebuilt per process. Deliberately NOT a copy of any repository's roadmap,
-- task status, or policy content: those stay in the managed repository and are
-- resolved fresh on every read (see ``docs/engineering/ENG-CP-03.md``).
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    local_repo_root TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    github_remote TEXT,
    policy_entrypoints TEXT NOT NULL DEFAULT '[]',
    roadmap_paths TEXT NOT NULL DEFAULT '[]',
    task_sources TEXT NOT NULL DEFAULT '[]',
    validation_command TEXT NOT NULL DEFAULT '[]',
    capabilities TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runbooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preset TEXT NOT NULL,
    objective TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    parent_worker TEXT NOT NULL,
    max_duration_minutes INTEGER NOT NULL,
    worker_routes TEXT NOT NULL DEFAULT '{}',
    concurrency TEXT NOT NULL DEFAULT '{}',
    safety_profile TEXT NOT NULL DEFAULT '{}',
    checkpoint_policy TEXT NOT NULL DEFAULT 'after_each_green_slice',
    stop_conditions TEXT NOT NULL DEFAULT '[]',
    permission_profile TEXT NOT NULL DEFAULT 'standard',
    codex_policy TEXT NOT NULL DEFAULT 'conserve',
    codex_auto_eligible INTEGER NOT NULL DEFAULT 0,
    max_codex_invocations INTEGER NOT NULL DEFAULT 1,
    phases TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    deadline_at TEXT,
    ended_at TEXT,
    recovery_note TEXT,
    report_markdown TEXT
);

CREATE TABLE IF NOT EXISTS usage_governance (
    runbook_id TEXT PRIMARY KEY,
    task_id TEXT,
    classification TEXT NOT NULL,
    codex_policy TEXT NOT NULL,
    codex_auto_eligible INTEGER NOT NULL DEFAULT 0,
    max_codex_invocations INTEGER NOT NULL DEFAULT 1,
    codex_invocations INTEGER NOT NULL DEFAULT 0,
    telemetry_quality TEXT NOT NULL DEFAULT 'unknown',
    input_tokens INTEGER,
    output_tokens INTEGER,
    escalation_state TEXT NOT NULL DEFAULT 'none',
    escalation_reason TEXT,
    route_history TEXT NOT NULL DEFAULT '[]',
    escalation_history TEXT NOT NULL DEFAULT '[]',
    context_manifest TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_intake_claims (
    stable_task_id TEXT PRIMARY KEY,
    owner_ref TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    stable_task_id TEXT NOT NULL,
    owner_ref TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    selected_worker TEXT,
    alternatives TEXT NOT NULL DEFAULT '[]',
    wave INTEGER,
    scores TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advancements (
    runbook_id TEXT PRIMARY KEY,
    project_id TEXT,
    state TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_enabled ON projects (enabled, project_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks (state);
CREATE INDEX IF NOT EXISTS idx_runbooks_status ON runbooks (status);
CREATE INDEX IF NOT EXISTS idx_operations_updated ON operations (updated_at);
CREATE INDEX IF NOT EXISTS idx_terminal_commands_ts ON terminal_commands (ts);
CREATE INDEX IF NOT EXISTS idx_dispatch_task ON dispatch_decisions (task_id, id);
"""

# Columns added after the original ``tasks`` schema shipped. Applied with
# ``ALTER TABLE ... ADD COLUMN`` (guarded by ``PRAGMA table_info``, since
# SQLite has no ``ADD COLUMN IF NOT EXISTS``) so an existing on-disk database
# from before ENG-AGENT-02-S5 keeps opening with no manual migration step.
_TASKS_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("launch_mode", "TEXT NOT NULL DEFAULT 'delegated'"),
    ("timeout_seconds", "INTEGER"),
    ("runbook_id", "TEXT"),
    ("permission_profile", f"TEXT NOT NULL DEFAULT '{PERMISSION_STANDARD}'"),
    ("fallback_reason", "TEXT"),
    ("failed_worker_id", "TEXT"),
    ("failure_execution_system", "TEXT"),
    ("failure_provider", "TEXT"),
    ("failure_model", "TEXT"),
    ("failure_category", "TEXT"),
    ("failure_reason_sanitized", "TEXT"),
    ("failure_reset", "TEXT"),
    ("failure_reset_source", "TEXT"),
    ("fallback_alternatives", "TEXT NOT NULL DEFAULT '[]'"),
    ("fallback_selected_worker", "TEXT"),
    ("fallback_automatic", "INTEGER"),
    ("stale_recovered", "INTEGER NOT NULL DEFAULT 0"),
    ("owner_ref", "TEXT"),
    ("required_capability", "TEXT"),
    ("changed_paths", "TEXT NOT NULL DEFAULT '[]'"),
    ("integration_base", "TEXT"),
    ("avoid_provider", "TEXT"),
    ("codex_policy", "TEXT NOT NULL DEFAULT 'conserve'"),
    ("codex_auto_eligible", "INTEGER NOT NULL DEFAULT 0"),
    ("max_codex_invocations", "INTEGER NOT NULL DEFAULT 1"),
    ("admission_state", "TEXT NOT NULL DEFAULT 'PENDING'"),
    ("admission_reason", "TEXT"),
    ("dependency_wave", "INTEGER"),
    ("selected_worker_reason", "TEXT"),
    ("selected_provider", "TEXT"),
    ("selection_alternatives", "TEXT NOT NULL DEFAULT '[]'"),
)

# Same forward-compatible-migration pattern as ``_TASKS_MIGRATED_COLUMNS``:
# an existing on-disk ``.orchestrator-state`` database from before
# ENG-AGENT-02-S7 (issue #97) keeps opening with no manual step. Pre-existing
# rows get the neutral default below (harmless: `reason` is read alongside
# `state`, which those rows already carry a correct value for); a brand-new
# row added later by ``provider_state.reconcile_provider_states`` always
# carries its own freshly computed reason.
_PROVIDER_STATES_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (("reason", "TEXT NOT NULL DEFAULT 'AVAILABLE'"),)
_WORKTREES_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("managed", "INTEGER NOT NULL DEFAULT 0"),
    ("discovered_at", "TEXT"),
    ("stale_lock", "INTEGER NOT NULL DEFAULT 0"),
    ("stale_lock_holder", "TEXT"),
    # ENG-AGENT-14 (issue #140): identifies an auto-provisioned review checkout.
    ("review_repository", "TEXT"),
    ("review_pr", "INTEGER"),
    ("review_head_sha", "TEXT"),
)
_RUNBOOKS_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("codex_policy", "TEXT NOT NULL DEFAULT 'conserve'"),
    ("codex_auto_eligible", "INTEGER NOT NULL DEFAULT 0"),
    ("max_codex_invocations", "INTEGER NOT NULL DEFAULT 1"),
    # ENG-AGENT-13 (issue #138): the acceptance pipeline that continues a
    # runbook from successful implementation through Test/Review/Checkpoint/PR
    # readiness before it may report terminal SUCCEEDED. Existing on-disk
    # databases keep opening with no manual step; pre-existing rows default to
    # PENDING/{} exactly like a runbook that has not started acceptance yet.
    ("acceptance_stage", "TEXT NOT NULL DEFAULT 'PENDING'"),
    ("acceptance_evidence", "TEXT NOT NULL DEFAULT '{}'"),
    ("stop_after_current_requested", "INTEGER NOT NULL DEFAULT 0"),
)


# ENG-CP-03 (issue #165): the tables that carry per-project records and so gain
# a ``project_id``. Everything omitted here is deliberately *global* Control
# Plane configuration rather than project truth:
#
# - ``provider_states``  -- provider/worker definitions and health are a
#   property of the Control Plane's own configured providers, identical no
#   matter which project is selected (spec: "provider definitions should remain
#   global unless it genuinely belongs to a project").
# - ``control_settings`` -- a mixed key/value store. Genuinely global keys
#   (``max_write_workers``, ``stop_after_current``) stay unprefixed; the
#   *derived, project-dependent caches* stored there are namespaced per project
#   through :func:`project_scoped_setting_key` instead of a table column, so a
#   Project A repository-health snapshot can never render as Project B's.
# - ``usage_governance`` -- keyed by ``runbook_id``; it inherits project scope
#   transitively from the runbook it belongs to rather than duplicating it.
#
# This is an explicit audit, not a blanket "add project_id to everything".
_PROJECT_SCOPED_TABLES: tuple[str, ...] = (
    "tasks",
    "worktrees",
    "events",
    "runbooks",
    "operations",
    "terminal_commands",
    "dispatch_decisions",
    "task_intake_claims",
)

# ``control_settings`` keys that are derived, project-dependent caches rather
# than global CP configuration. Namespaced per project on read/write, and
# migrated from their legacy unprefixed form exactly once (see
# ``project_registry.migrate_legacy_state_to_project``).
PROJECT_SCOPED_SETTING_KEYS: frozenset[str] = frozenset(
    {"repository_health_cache", "worktree_status_cache"}
)

SCHEMA_VERSION_SETTING = "schema_version"
# Bumped whenever a migration step below changes the on-disk shape. 1 is the
# implicit pre-ENG-CP-03 single-project schema; 2 adds the project registry and
# project scoping.
CURRENT_SCHEMA_VERSION = 2


def project_scoped_setting_key(key: str, project_id: str | None) -> str:
    """The ``control_settings`` key holding ``key`` for ``project_id``.

    Global keys (anything not in :data:`PROJECT_SCOPED_SETTING_KEYS`) and a
    ``None`` project are returned unchanged, so genuinely global Control Plane
    configuration keeps exactly the key it has always had on disk.
    """

    if project_id is None or key not in PROJECT_SCOPED_SETTING_KEYS:
        return key
    return f"{key}:{project_id}"


def default_state_dir(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME


def default_db_path(repo_root: Path) -> Path:
    return default_state_dir(repo_root) / DB_FILENAME


def resolve_standalone_db_path() -> Path | None:
    """Octarel-owned SQLite path from ``OCTAREL_STATE_DIR`` / ``OCTAREL_CODE_ROOT``.

    ``OCTAREL_STATE_DIR`` is the directory that *contains* ``orchestrator.db``
    (or the db file itself). It is never nested again under ``.orchestrator-state``.
    ``OCTAREL_CODE_ROOT`` falls back to ``<code-root>/.orchestrator-state/orchestrator.db``.
    """

    import os

    env_dir = os.environ.get("OCTAREL_STATE_DIR")
    if env_dir:
        path = Path(env_dir)
        return path if path.is_file() else path / DB_FILENAME
    env_code = os.environ.get("OCTAREL_CODE_ROOT")
    if env_code:
        return default_db_path(Path(env_code))
    return None


class State:
    """Thin, explicit persistence layer. No ORM."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        # ENG-CP-03 (issue #165): every schema migration runs inside one
        # explicit transaction so a failure part-way through rolls the whole
        # step back rather than leaving a half-migrated database on disk. SQLite
        # makes DDL (including ``ALTER TABLE ... ADD COLUMN``) transactional, so
        # this is a real all-or-nothing guarantee, not a best-effort one.
        try:
            self._conn.execute("BEGIN")
            self._migrate_tasks_columns()
            self._migrate_provider_states_columns()
            self._migrate_worktrees_columns()
            self._migrate_runbooks_columns()
            self._migrate_project_id_columns()
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @_serialized
    def _migrate_tasks_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for column, ddl in _TASKS_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_provider_states_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(provider_states)").fetchall()}
        for column, ddl in _PROVIDER_STATES_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE provider_states ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_worktrees_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(worktrees)").fetchall()}
        for column, ddl in _WORKTREES_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE worktrees ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_runbooks_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(runbooks)").fetchall()}
        for column, ddl in _RUNBOOKS_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE runbooks ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_project_id_columns(self) -> None:
        """Add the nullable ``project_id`` column to each project-scoped table.

        ENG-CP-03 (issue #165). Deliberately nullable with no default: a NULL
        ``project_id`` means "a legacy row written before this Control Plane
        knew about projects", which
        ``project_registry.migrate_legacy_state_to_project`` then adopts into
        the auto-migrated OctaScene project. Defaulting the column to a literal
        ``'octascene'`` here instead would hard-code one managed project's id
        into the generic persistence layer -- exactly the OctaScene-specific
        coupling this program exists to remove.
        """

        for table in _PROJECT_SCOPED_TABLES:
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "project_id" not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")

    # --------------------------------------------------------------- projects

    @_serialized
    def upsert_project(self, row: dict[str, Any]) -> None:
        """Insert or replace one registry row. Callers supply already-encoded values."""

        payload = dict(row)
        payload["updated_at"] = utc_now_iso()
        payload.setdefault("created_at", payload["updated_at"])
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{key}" for key in payload)
        updates = ", ".join(f"{key}=excluded.{key}" for key in payload if key not in {"project_id", "created_at"})
        self._conn.execute(
            f"INSERT INTO projects ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(project_id) DO UPDATE SET {updates}",
            payload,
        )
        self._conn.commit()

    @_serialized
    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    @_serialized
    def list_projects(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM projects"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY display_name COLLATE NOCASE ASC, project_id ASC"
        return [dict(row) for row in self._conn.execute(sql).fetchall()]

    @_serialized
    def delete_project(self, project_id: str) -> None:
        """Remove only the registry row.

        ENG-CP-03: deliberately does **not** cascade into tasks, runbooks,
        events, worktrees, or any other project-scoped evidence. Removing a
        project from the Control Plane must never destroy the historical record
        of what ran against it (nor, obviously, anything in the repository
        itself) -- ``project_registry.remove_project`` is the caller that
        enforces this and records the removal as an auditable event.
        """

        self._conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._conn.commit()

    @_serialized
    def count_project_rows(self, project_id: str) -> dict[str, int]:
        """How many rows in each project-scoped table belong to ``project_id``."""

        counts: dict[str, int] = {}
        for table in _PROJECT_SCOPED_TABLES:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project_id,)
            ).fetchone()
            counts[table] = int(row["n"]) if row else 0
        return counts

    @_serialized
    def adopt_unscoped_rows(self, project_id: str) -> dict[str, int]:
        """Assign every legacy ``project_id IS NULL`` row to ``project_id``.

        Idempotent by construction: a second run matches nothing because the
        first already set every previously-NULL row. Restart-safe for the same
        reason -- there is no intermediate state a crash could leave behind
        beyond "some rows adopted", which the next run simply completes.
        """

        adopted: dict[str, int] = {}
        try:
            self._conn.execute("BEGIN")
            for table in _PROJECT_SCOPED_TABLES:
                cursor = self._conn.execute(
                    f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL", (project_id,)
                )
                adopted[table] = int(cursor.rowcount or 0)
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return adopted

    @_serialized
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- tasks

    @_serialized
    def upsert_task(self, task: Task) -> None:
        task.updated_at = utc_now_iso()
        row = task.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "id")
        self._conn.execute(
            f"INSERT INTO tasks ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()

    @_serialized
    def get_task(self, task_id: str) -> Task | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(dict(row)) if row else None

    @_serialized
    def list_tasks(self, *, state: str | None = None, project_id: str | None = None) -> list[Task]:
        """List tasks, optionally filtered by lifecycle ``state`` and/or project.

        ENG-CP-03: ``project_id=None`` means "every project" and is what the
        daemon's own whole-Control-Plane reconciliation passes; the dashboard
        always passes the selected project so Project A's tasks can never
        render as Project B's.
        """

        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY priority DESC, created_at ASC", tuple(params)
        ).fetchall()
        return [Task.from_row(dict(row)) for row in rows]

    @_serialized
    def claim_pending_fallback(self, task_id: str) -> bool:
        """Atomically reserve the one automatic fallback decision for ``task_id``.

        ``fallback_automatic`` is deliberately tri-state: NULL means a
        recoverable failure still needs a decision, false means that decision
        is claimed/resolved without a launched replacement, and true means an
        automatic replacement was selected.  The compare-and-set prevents the
        dashboard request and its background reconcile tick from launching the
        same replacement concurrently.  A process crash after the claim fails
        closed instead of duplicating an uncounted attempt after restart.
        """

        cursor = self._conn.execute(
            "UPDATE tasks SET fallback_automatic = 0, updated_at = ? "
            "WHERE id = ? AND state = 'FAILED' AND fallback_automatic IS NULL",
            (utc_now_iso(), task_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_serialized
    def delete_task(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()

    # ------------------------------------------------------- provider states

    @_serialized
    def upsert_provider_state(self, provider: ProviderState) -> None:
        provider.updated_at = utc_now_iso()
        row = provider.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "name")
        self._conn.execute(
            f"INSERT INTO provider_states ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(name) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()

    @_serialized
    def get_provider_state(self, name: str) -> ProviderState | None:
        row = self._conn.execute("SELECT * FROM provider_states WHERE name = ?", (name,)).fetchone()
        return ProviderState.from_row(dict(row)) if row else None

    @_serialized
    def list_provider_states(self) -> list[ProviderState]:
        rows = self._conn.execute("SELECT * FROM provider_states ORDER BY name ASC").fetchall()
        return [ProviderState.from_row(dict(row)) for row in rows]

    @_serialized
    def delete_provider_state(self, name: str) -> None:
        self._conn.execute("DELETE FROM provider_states WHERE name = ?", (name,))
        self._conn.commit()

    # ------------------------------------------------------------ worktrees

    @_serialized
    def upsert_worktree(self, worktree: WorktreeRecord) -> None:
        worktree.updated_at = utc_now_iso()
        existing = self._conn.execute(
            "SELECT project_id FROM worktrees WHERE path = ?", (worktree.path,)
        ).fetchone()
        if existing is not None:
            owner = existing["project_id"]
            incoming = worktree.project_id
            # Fourth independent review (Grok/xAI): COALESCE(excluded, stored)
            # still prefers a non-NULL incoming id, so adopt_worktree could
            # re-stamp another project's row and wipe its managed/review fields.
            # An unstamped observation (incoming is None) continues to keep the
            # stored owner; a different non-NULL owner is refused outright.
            if owner is not None and incoming is not None and owner != incoming:
                raise ValueError(
                    f"worktree {worktree.path} is already owned by project {owner!r}; "
                    f"refusing to re-stamp it as {incoming!r}"
                )
        row = worktree.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        # ENG-CP-03: never let an unstamped record (e.g. one straight out of
        # `discover_git_worktrees`, which only observes git-visible facts) null
        # out a stored project_id on conflict.
        updates = ", ".join(
            "project_id=COALESCE(excluded.project_id, worktrees.project_id)"
            if key == "project_id"
            else f"{key}=excluded.{key}"
            for key in row
            if key != "path"
        )
        self._conn.execute(
            f"INSERT INTO worktrees ({columns}) VALUES ({placeholders}) ON CONFLICT(path) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()

    @_serialized
    def list_worktrees(self, *, project_id: str | None = None) -> list[WorktreeRecord]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM worktrees WHERE project_id = ? ORDER BY path ASC", (project_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM worktrees ORDER BY path ASC").fetchall()
        return [WorktreeRecord.from_row(dict(row)) for row in rows]

    @_serialized
    def replace_worktrees(self, worktrees: Iterable[WorktreeRecord], *, project_id: str | None = None) -> None:
        """Overwrite one project's worktree rows with a freshly observed snapshot.

        ENG-CP-03: a worktree snapshot is only ever observed for a single
        repository at a time, so this **never** deletes another project's rows.
        With a ``project_id`` it replaces that project's rows; without one it
        replaces only the unscoped (``project_id IS NULL``) rows.

        There is deliberately no whole-table delete left in this method. An
        independent review (Grok/xAI) showed that keeping one for the
        "no project selected" case re-created the original blocker through
        every fallback path -- every project disabled, a registry listing
        failure, or a bootstrap that registered nothing would each wipe every
        project's worktree rows and null the migrated stamps. Deleting only
        NULL-project rows is byte-for-byte the pre-ENG-CP-03 behavior on a
        database that has no projects (where every row is NULL) while being
        non-destructive on one that does.
        """

        existing = {Path(w.path).resolve(): w for w in self.list_worktrees(project_id=project_id)}
        # Paths already owned by a *different* project. Two registered roots can
        # be worktrees of the same underlying Git repository, in which case
        # `git worktree list` reports both and a scoped replace for project A
        # would otherwise re-stamp project B's row to A (ON CONFLICT(path)) and
        # drop its managed/review fields. Observed paths owned by someone else
        # are skipped entirely (independent review, Grok/xAI).
        owned_elsewhere = {
            Path(w.path).resolve()
            for w in self.list_worktrees()
            if w.project_id is not None and w.project_id != project_id
        }
        observed = [w for w in worktrees if Path(w.path).resolve() not in owned_elsewhere]
        # ENG-CP-03: delete + reinsert in ONE transaction. Committing the delete
        # first (independent review, Grok/xAI) left a crash window in which the
        # worktree table was observably empty.
        try:
            self._conn.execute("BEGIN")
            if project_id is not None:
                self._conn.execute("DELETE FROM worktrees WHERE project_id = ?", (project_id,))
            else:
                self._conn.execute("DELETE FROM worktrees WHERE project_id IS NULL")
            for worktree in observed:
                prior = existing.get(Path(worktree.path).resolve())
                if project_id is not None:
                    worktree.project_id = project_id
                elif prior is not None:
                    # An unscoped observation must not strip an existing stamp:
                    # a freshly discovered record carries no project, and
                    # ON CONFLICT(path) would otherwise overwrite the stored
                    # project_id with NULL (independent review, Grok/xAI).
                    worktree.project_id = prior.project_id
                if prior:
                    worktree.managed = prior.managed
                    worktree.discovered_at = prior.discovered_at
                    # ENG-AGENT-14 (issue #140): discover_git_worktrees() only
                    # ever observes git-visible facts (path/branch/lock) -- a
                    # fresh snapshot must not silently erase which worktree was
                    # auto-provisioned for which exact PR/head review.
                    worktree.review_repository = prior.review_repository
                    worktree.review_pr = prior.review_pr
                    worktree.review_head_sha = prior.review_head_sha
                worktree.updated_at = utc_now_iso()
                row = worktree.to_row()
                columns = ", ".join(row)
                placeholders = ", ".join(f":{key}" for key in row)
                updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "path")
                self._conn.execute(
                    f"INSERT INTO worktrees ({columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT(path) DO UPDATE SET {updates}",
                    row,
                )
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ------------------------------------------------------------- operations

    @_serialized
    def upsert_operation(self, operation_id: str, *, kind: str, action: str, target: str, state: str, stage: str, message: str, project_id: str | None = None) -> None:
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO operations (id, kind, action, target, state, stage, message, created_at, updated_at, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            # ENG-CP-03: project_id is in the conflict-update list too. Omitting
            # it (independent review, Grok/xAI) left a row first inserted before
            # a project was selected permanently NULL, so it stayed invisible to
            # project-scoped reads even after later updates carried a project.
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, stage=excluded.stage, message=excluded.message, "
            "updated_at=excluded.updated_at, project_id=COALESCE(excluded.project_id, operations.project_id)",
            (operation_id, kind, action, target, state, stage, message, now, now, project_id),
        )
        self._conn.commit()

    @_serialized
    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        return dict(row) if row else None

    @_serialized
    def list_operations(self, *, limit: int = 100, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM operations WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM operations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def record_terminal_command(self, *, actor: str, session_id: str, cwd: str, branch: str | None, command: str, exit_code: int | None = None, duration_seconds: float | None = None, project_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO terminal_commands (ts, actor, session_id, cwd, branch, command, exit_code, duration_seconds, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), actor, session_id, cwd, branch, command, exit_code, duration_seconds, project_id),
        )
        self._conn.commit()

    @_serialized
    def list_terminal_commands(self, *, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
        safe_limit = 20 if limit <= 20 else 50 if limit <= 50 else 100
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM terminal_commands WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, safe_limit),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM terminal_commands ORDER BY id DESC LIMIT ?", (safe_limit,)).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def clear_terminal_commands(self, *, project_id: str | None = None) -> None:
        """Clear terminal history, for one project when ``project_id`` is given.

        ENG-CP-03: scoping the delete keeps "clear history" an action on the
        project the operator is actually looking at rather than a silent
        cross-project wipe.
        """

        # Mirrors ``replace_worktrees``: with no project named this clears only
        # the unscoped rows rather than every project's history, so a context
        # with nothing selected can never wipe another project's activity.
        if project_id is not None:
            self._conn.execute("DELETE FROM terminal_commands WHERE project_id = ?", (project_id,))
        else:
            self._conn.execute("DELETE FROM terminal_commands WHERE project_id IS NULL")
        self._conn.commit()

    # ---------------------------------------------------------------- events

    @_serialized
    def record_event(
        self,
        *,
        category: str,
        message: str,
        task_id: str | None = None,
        provider: str | None = None,
        level: str = "info",
        project_id: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, category, task_id, provider, level, message, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), category, task_id, provider, level, message, project_id),
        )
        self._conn.commit()

    @_serialized
    def list_events(self, *, limit: int = 200, project_id: str | None = None) -> list[Event]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event.from_row(dict(row)) for row in rows]

    # --------------------------------------------------------------- runbooks

    @_serialized
    def upsert_runbook(self, runbook: Runbook) -> None:
        runbook.updated_at = utc_now_iso()
        row = runbook.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "id")
        self._conn.execute(
            f"INSERT INTO runbooks ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()

    @_serialized
    def get_runbook(self, runbook_id: str) -> Runbook | None:
        row = self._conn.execute("SELECT * FROM runbooks WHERE id = ?", (runbook_id,)).fetchone()
        return Runbook.from_row(dict(row)) if row else None

    @_serialized
    def list_runbooks(self, *, project_id: str | None = None) -> list[Runbook]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM runbooks WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM runbooks ORDER BY created_at DESC").fetchall()
        return [Runbook.from_row(dict(row)) for row in rows]

    @_serialized
    def delete_runbook(self, runbook_id: str) -> None:
        self._conn.execute("DELETE FROM usage_governance WHERE runbook_id = ?", (runbook_id,))
        self._conn.execute("DELETE FROM runbooks WHERE id = ?", (runbook_id,))
        self._conn.commit()

    # ------------------------------------------------------ usage governance

    @_serialized
    def upsert_usage_governance(self, record: dict[str, Any]) -> None:
        row = {
            "runbook_id": record["runbook_id"],
            "task_id": record.get("task_id"),
            "classification": record.get("classification", "routine"),
            "codex_policy": record.get("codex_policy", "conserve"),
            "codex_auto_eligible": 1 if record.get("codex_auto_eligible") else 0,
            "max_codex_invocations": int(record.get("max_codex_invocations", 1)),
            "codex_invocations": int(record.get("codex_invocations", 0)),
            "telemetry_quality": record.get("telemetry_quality", "unknown"),
            "input_tokens": record.get("input_tokens"),
            "output_tokens": record.get("output_tokens"),
            "escalation_state": record.get("escalation_state", "none"),
            "escalation_reason": record.get("escalation_reason"),
            "route_history": json.dumps(record.get("route_history", []), sort_keys=True),
            "escalation_history": json.dumps(record.get("escalation_history", []), sort_keys=True),
            "context_manifest": json.dumps(record.get("context_manifest", {}), sort_keys=True),
            "updated_at": utc_now_iso(),
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "runbook_id")
        self._conn.execute(
            f"INSERT INTO usage_governance ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(runbook_id) DO UPDATE SET {updates}", row
        )
        self._conn.commit()

    @_serialized
    def get_usage_governance(self, runbook_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM usage_governance WHERE runbook_id = ?", (runbook_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["codex_auto_eligible"] = bool(item["codex_auto_eligible"])
        for key in ("route_history", "escalation_history", "context_manifest"):
            item[key] = json.loads(item[key])
        return item

    @_serialized
    def list_usage_governance(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT runbook_id FROM usage_governance ORDER BY updated_at DESC").fetchall()
        return [item for row in rows if (item := self.get_usage_governance(row["runbook_id"]))]

    # ------------------------------------------------------ managed dispatch

    @_serialized
    def claim_task_identity(
        self, *, stable_task_id: str, owner_ref: str, source: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Atomically claim a stable task id without relabelling prior evidence."""

        now = utc_now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO task_intake_claims "
            "(stable_task_id, owner_ref, source, created_at, updated_at, project_id) VALUES (?, ?, ?, ?, ?, ?)",
            (stable_task_id, owner_ref, source, now, now, project_id),
        )
        row = self._conn.execute(
            "SELECT * FROM task_intake_claims WHERE stable_task_id = ?", (stable_task_id,)
        ).fetchone()
        if row is not None and row["owner_ref"] == owner_ref:
            # ENG-CP-03: also backfill project_id when the existing claim was
            # written before the registry existed. Only ever fills a NULL --
            # never relabels a claim that already belongs to a project.
            self._conn.execute(
                "UPDATE task_intake_claims SET updated_at = ?, "
                "project_id = COALESCE(project_id, ?) WHERE stable_task_id = ?",
                (now, project_id, stable_task_id),
            )
            # Re-read so the caller sees the backfilled project_id rather than
            # the pre-update snapshot (independent review, Grok/xAI).
            row = self._conn.execute(
                "SELECT * FROM task_intake_claims WHERE stable_task_id = ?", (stable_task_id,)
            ).fetchone()
        self._conn.commit()
        return dict(row) if row is not None else {}

    @_serialized
    def list_task_identity_claims(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM task_intake_claims ORDER BY stable_task_id").fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def record_dispatch_decision(
        self,
        *,
        task_id: str,
        stable_task_id: str,
        owner_ref: str,
        outcome: str,
        reason: str,
        selected_worker: str | None = None,
        alternatives: Iterable[str] = (),
        wave: int | None = None,
        scores: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO dispatch_decisions "
            "(task_id, stable_task_id, owner_ref, outcome, reason, selected_worker, alternatives, wave, scores, created_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                stable_task_id,
                owner_ref,
                outcome,
                reason,
                selected_worker,
                json.dumps(list(alternatives)),
                wave,
                json.dumps(scores or {}, sort_keys=True),
                utc_now_iso(),
                project_id,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    @_serialized
    def list_dispatch_decisions(self, *, limit: int = 200, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_decisions WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["alternatives"] = json.loads(item["alternatives"] or "[]")
            item["scores"] = json.loads(item["scores"] or "{}")
            result.append(item)
        return result

    # ---------------------------------------------------------- advancement

    @_serialized
    def get_advancement(self, runbook_id: str) -> dict[str, Any] | None:
        """Durable OCTAREL-OPS-02 advancement record for one completed runbook."""

        row = self._conn.execute("SELECT * FROM advancements WHERE runbook_id = ?", (runbook_id,)).fetchone()
        return self._advancement_from_row(row) if row else None

    @_serialized
    def upsert_advancement(self, record: dict[str, Any]) -> None:
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO advancements (runbook_id, project_id, state, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(runbook_id) DO UPDATE SET "
            "project_id=excluded.project_id, state=excluded.state, payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (
                record["runbook_id"],
                record.get("project_id"),
                record["state"],
                json.dumps(record, sort_keys=True),
                now,
                now,
            ),
        )
        self._conn.commit()

    @_serialized
    def list_advancements(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM advancements WHERE project_id = ? ORDER BY updated_at DESC, runbook_id", (project_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM advancements ORDER BY updated_at DESC, runbook_id").fetchall()
        return [self._advancement_from_row(row) for row in rows]

    @staticmethod
    def _advancement_from_row(row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(row["payload"] or "{}")
        record["runbook_id"] = row["runbook_id"]
        record["project_id"] = row["project_id"]
        record["state"] = row["state"]
        record["updated_at"] = row["updated_at"]
        return record

    # ------------------------------------------------------- control settings

    @_serialized
    def set_control_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO control_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    @_serialized
    def get_control_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM control_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_project_setting(self, key: str, value: str, project_id: str | None) -> None:
        """Write a project-dependent derived cache under its per-project key."""

        self.set_control_setting(project_scoped_setting_key(key, project_id), value)

    def get_project_setting(self, key: str, default: str | None = None, *, project_id: str | None = None) -> str | None:
        """Read a project-dependent derived cache.

        Never falls back to the legacy unprefixed key: doing so would show one
        project's cached repository health or worktree snapshot under another
        project that simply has no cache yet. A project with no cache reports
        ``default`` (i.e. "not computed yet"), which the caller renders as empty
        rather than as someone else's data.
        """

        return self.get_control_setting(project_scoped_setting_key(key, project_id), default)

    @_serialized
    def schema_version(self) -> int:
        raw = self.get_control_setting(SCHEMA_VERSION_SETTING)
        if raw is None:
            return 1
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 1

    @_serialized
    def set_schema_version(self, version: int) -> None:
        self.set_control_setting(SCHEMA_VERSION_SETTING, str(int(version)))
