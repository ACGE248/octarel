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
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from .models import (
    PERMISSION_STANDARD,
    Event,
    ExecutionLease,
    ProviderState,
    Runbook,
    Task,
    WakeContribution,
    WakeRequest,
    WorktreeRecord,
    utc_now_iso,
)
from .run_events import RunEvent, normalize_run_event

STATE_DIRNAME = ".orchestrator-state"
DB_FILENAME = "orchestrator.db"

_T = TypeVar("_T")


@dataclass(frozen=True)
class WakeRecoveryOutcome:
    """Result of one ``State.recover_claimed_wakes`` sweep (ENG-PC-03).

    ``reset`` counts a stranded ``CLAIMED`` row that had no ``PENDING`` sibling for its
    (project_id, task_id, stage) key and so went straight back to ``PENDING``. ``merged``
    counts a stranded row whose key already had a ``PENDING`` sibling -- created by a
    fresh trigger that arrived while this row was ``CLAIMED`` -- so its contributions and
    ``coalesced_count`` were folded into that sibling instead of becoming a second
    ``PENDING`` row for the same key, which the partial unique index forbids. See
    ``recover_claimed_wakes`` for why both outcomes are needed.
    """

    reset: int = 0
    merged: int = 0

    @property
    def total(self) -> int:
        return self.reset + self.merged


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
    message TEXT NOT NULL,
    run_id TEXT,
    run_sequence INTEGER,
    event_class TEXT,
    event_type TEXT,
    source TEXT,
    provenance TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '{}'
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

-- ENG-PC-05 (issue #33): immutable, attempt-scoped usage evidence.  The
-- governance row above remains the mutable runbook policy/read-model source;
-- this table is the append-only accounting record.  Corrections are new runs,
-- never mutations of historical rows (enforced by the triggers below).
-- A pre-project record may initially lack attribution, but the update trigger
-- permits exactly NULL -> non-NULL project identity completion while keeping
-- every financial/evidentiary field immutable.
CREATE TABLE IF NOT EXISTS usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    program_ref TEXT,
    task_id TEXT,
    runbook_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT,
    worker TEXT NOT NULL,
    provider TEXT,
    effective_model TEXT,
    occurred_at TEXT,
    source_record TEXT NOT NULL,
    source_attempt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_ledger_run
    ON usage_ledger (run_id);
CREATE INDEX IF NOT EXISTS idx_usage_ledger_filters
    ON usage_ledger (project_id, task_id, provider, effective_model, occurred_at);
CREATE TRIGGER IF NOT EXISTS usage_ledger_no_update
    BEFORE UPDATE ON usage_ledger
    WHEN NOT (
        OLD.project_id IS NULL
        AND NEW.project_id IS NOT NULL
        AND LENGTH(TRIM(NEW.project_id)) > 0
        AND NEW.id IS OLD.id
        AND NEW.program_ref IS OLD.program_ref
        AND NEW.task_id IS OLD.task_id
        AND NEW.runbook_id IS OLD.runbook_id
        AND NEW.run_id IS OLD.run_id
        AND NEW.session_id IS OLD.session_id
        AND NEW.worker IS OLD.worker
        AND NEW.provider IS OLD.provider
        AND NEW.effective_model IS OLD.effective_model
        AND NEW.occurred_at IS OLD.occurred_at
        AND NEW.source_record IS OLD.source_record
        AND NEW.source_attempt IS OLD.source_attempt
        AND NEW.created_at IS OLD.created_at
    )
    BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS usage_ledger_no_delete
    BEFORE DELETE ON usage_ledger
    BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END;

-- ENG-PC-05 scope B: mutable policy configuration, deliberately separate
-- from the immutable accounting ledger above. ``project_id IS NULL`` means a
-- genuinely Control-Plane-global budget; a non-NULL value limits the policy
-- to one managed project. Scope hierarchy is resolved by usage_budgets.py.
CREATE TABLE IF NOT EXISTS usage_budgets (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    scope_type TEXT NOT NULL,
    scope_key TEXT,
    constraint_type TEXT NOT NULL,
    limit_value REAL NOT NULL,
    warning_fraction REAL NOT NULL DEFAULT 0.8,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_budgets_resolution
    ON usage_budgets (project_id, enabled, scope_type, scope_key, constraint_type);

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

-- ENG-PC-01 (issue #29): durable execution-ownership record, keyed by worktree
-- (the actually-contended resource) rather than by task, so two different tasks
-- racing for the same worktree are refused exactly like the same task racing
-- itself. ``generation`` is the compare-and-swap guard every acquisition/release
-- writes through; see ``execution_lease.py`` for the mechanism and
-- ``models.ExecutionLease`` for the invariant this composes with (not duplicates)
-- alongside the advancement lease, the ``.write-lock`` file and task intake claims.
CREATE TABLE IF NOT EXISTS execution_leases (
    worktree TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RELEASED',
    project_id TEXT,
    task_id TEXT,
    stable_task_id TEXT,
    runbook_id TEXT,
    worker TEXT,
    owner_host TEXT,
    owner_pid INTEGER,
    owner_pid_create_time REAL,
    acquired_at TEXT,
    heartbeat_at TEXT,
    released_at TEXT,
    release_reason TEXT,
    recovery_reason TEXT,
    conflict_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ENG-PC-03 (issue #31): durable wake queue. ``wake_queue`` is the current,
-- coalesced state of each pending/in-flight wake; ``wake_queue_contributions``
-- is the append-only, never-mutated record of every individual trigger that
-- asked for one (including duplicates), which is how coalescing retains
-- count, reasons and provenance instead of just a counter. See
-- ``models.WakeRequest``/``models.WakeContribution`` and ``wake_queue.py`` for
-- the policy layer (typed reasons, bounded retry/backoff, poisoning).
CREATE TABLE IF NOT EXISTS wake_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    task_id TEXT,
    stage TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reason TEXT NOT NULL,
    coalesced_count INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claimed_at TEXT,
    claimed_by TEXT,
    completed_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS wake_queue_contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wake_id INTEGER NOT NULL REFERENCES wake_queue(id),
    reason TEXT NOT NULL,
    source TEXT,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- The coalescing invariant: at most one PENDING wake per project/task/stage.
-- ``IFNULL(..., '')`` folds NULLs into a stable, comparable value so two
-- project-less or stage-less wakes for the same task still collide as
-- intended rather than each getting its own NULL-valued "unique" row (SQLite
-- unique indexes otherwise never treat two NULLs as equal).
CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_queue_pending_key
    ON wake_queue (IFNULL(project_id, ''), IFNULL(task_id, ''), IFNULL(stage, ''))
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_wake_queue_status ON wake_queue (status, created_at);
CREATE INDEX IF NOT EXISTS idx_wake_queue_contributions_wake ON wake_queue_contributions (wake_id, id);

-- ENG-PC-02 (issue #30): one active task-scoped native agent session per
-- project/task/worker. Opaque adapter state is confined to continuation_state;
-- ordinary read APIs below intentionally never select or return that column.
CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    provider TEXT NOT NULL,
    effective_model TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    tree_sha TEXT NOT NULL,
    permission_profile TEXT NOT NULL,
    capability TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    identity_fingerprint TEXT NOT NULL,
    continuation_state TEXT,
    continuation_count INTEGER NOT NULL DEFAULT 0,
    force_fresh_next INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    last_run_id TEXT,
    last_mode TEXT NOT NULL DEFAULT 'FRESH',
    reason TEXT NOT NULL,
    state_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_live_task_worker
    ON agent_sessions (project_id, task_id, worker)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_agent_sessions_identity
    ON agent_sessions (identity_fingerprint, active);

-- ENG-PC-06 (issue #34): delivery cursors contain identities and event
-- positions only. They deliberately do not cache policy, roadmap, task, or
-- event content; repository truth and the existing events table are reread for
-- every delivery. A cursor is owned by exactly one project/task/consumer.
CREATE TABLE IF NOT EXISTS context_cursors (
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    tree_sha TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    task_contract_digest TEXT NOT NULL,
    identity_fingerprint TEXT NOT NULL,
    bundle_identity TEXT NOT NULL,
    preserved_policy_identity TEXT NOT NULL,
    delivered_task_identities TEXT NOT NULL DEFAULT '{}',
    delivered_ancestry_identity TEXT NOT NULL DEFAULT '',
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_outcome TEXT NOT NULL DEFAULT 'DELIVERED',
    last_attempted_characters INTEGER NOT NULL DEFAULT 0,
    inspection_summary TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, task_id, consumer_id)
);

CREATE INDEX IF NOT EXISTS idx_context_cursors_identity
    ON context_cursors (identity_fingerprint);

CREATE TABLE IF NOT EXISTS overnight_sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_execution_leases_task ON execution_leases (task_id);
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
# ENG-PC-01 follow-up (Grok Build review): the spawn-in-flight flag that closes
# the window between winning the execution lease and durably recording the real
# worker subprocess's pid. See ``models.ExecutionLease.spawn_pending`` and
# ``execution_lease._classify_owner``. Same forward-compatible-migration pattern
# as the tuples above: an on-disk database from before this follow-up (the
# ``execution_leases`` table itself shipped only one commit earlier) keeps
# opening with no manual step.
_EXECUTION_LEASES_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("spawn_pending", "INTEGER NOT NULL DEFAULT 0"),
)
_AGENT_SESSIONS_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("state_reason", "TEXT"),
)
_CONTEXT_CURSORS_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("delivered_ancestry_identity", "TEXT NOT NULL DEFAULT ''"),
    ("last_outcome", "TEXT NOT NULL DEFAULT 'DELIVERED'"),
    ("last_attempted_characters", "INTEGER NOT NULL DEFAULT 0"),
    ("inspection_summary", "TEXT NOT NULL DEFAULT '{}'"),
)
_EVENTS_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT"),
    ("run_sequence", "INTEGER"),
    ("event_class", "TEXT"),
    ("event_type", "TEXT"),
    ("source", "TEXT"),
    ("provenance", "TEXT"),
    ("data", "TEXT NOT NULL DEFAULT '{}'"),
    ("evidence", "TEXT NOT NULL DEFAULT '{}'"),
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

# OCTAREL-UI-08 (issue #44) deliberately adds no column here. A CLI-reported
# per-run cost belongs to the route-history *attempt* that produced it, not to
# the runbook as a whole: the read model attributes a row to the newest
# attempt, so a record-level figure would be served against whichever worker
# ran last rather than the one that reported it. route_history is already
# durable JSON on this table, so the attempt carries it with no schema change.


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
# - ``wake_queue_contributions`` -- keyed by ``wake_id``; it inherits project
#   scope transitively from the ``wake_queue`` row it belongs to, exactly like
#   ``usage_governance`` above, rather than duplicating the column.
#
# ``usage_ledger`` is intentionally included below: unlike governance it owns
# a durable project identity so retained accounting remains attributable even
# after its mutable runbook and governance rows are deleted.
#
# ``usage_budgets`` is intentionally omitted. Its nullable ``project_id`` is
# part of the policy meaning: NULL is a truly global definition, while a value
# makes the definition project-specific. Adopting NULL rows into one project
# would silently narrow a global safety brake and is therefore forbidden.
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
    "execution_leases",
    "wake_queue",
    "agent_sessions",
    # Context cursors are included because every identity, event position, and
    # ancestry reference belongs to one selected project. Adoption supports a
    # hypothetical pre-project ENG-PC-06 database; current rows are NOT NULL
    # and every read/write additionally requires the exact project id.
    "context_cursors",
    "usage_ledger",
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
# project scoping; 3 adds ENG-PC-05's immutable usage ledger; 4 adds mutable
# hierarchical usage-budget definitions; 5 makes run identity global and keeps
# durable program attribution on each ledger row.
CURRENT_SCHEMA_VERSION = 5


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
        self._conn.execute("PRAGMA busy_timeout=5000")
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
            self._migrate_execution_leases_columns()
            self._migrate_agent_sessions_columns()
            self._migrate_context_cursors_columns()
            self._migrate_project_id_columns()
            self._migrate_events_columns()
            self._migrate_usage_ledger()
            self._migrate_usage_budget_columns()
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
    def _migrate_execution_leases_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(execution_leases)").fetchall()}
        for column, ddl in _EXECUTION_LEASES_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE execution_leases ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_agent_sessions_columns(self) -> None:
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(agent_sessions)").fetchall()}
        for column, ddl in _AGENT_SESSIONS_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE agent_sessions ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_context_cursors_columns(self) -> None:
        """Upgrade only an actually older cursor table.

        The early return is intentional: even idempotent DDL takes SQLite's
        schema lock, so healthy opens must not run ALTER statements.
        """

        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(context_cursors)").fetchall()
        }
        missing = [item for item in _CONTEXT_CURSORS_MIGRATED_COLUMNS if item[0] not in existing]
        if not missing:
            return
        for column, ddl in missing:
            self._conn.execute(f"ALTER TABLE context_cursors ADD COLUMN {column} {ddl}")

    @_serialized
    def _migrate_events_columns(self) -> None:
        """Add ENG-PC-04's envelope beside the legacy event fields.

        The partial expression index enforces one sequence position per
        project/run while leaving every untyped legacy row untouched.
        """

        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()}
        for column, ddl in _EVENTS_MIGRATED_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_sequence "
            "ON events (IFNULL(project_id, ''), run_id, run_sequence) "
            "WHERE run_id IS NOT NULL AND run_sequence IS NOT NULL"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_run_order "
            "ON events (project_id, run_id, run_sequence)"
        )

    @_serialized
    def _migrate_usage_ledger(self) -> None:
        """Upgrade run identity, durable program scope, and attribution guard.

        A legacy governance record can predate project selection, so refusing
        all updates would strand its promoted ledger row forever. Completing
        NULL project identity is not a financial rewrite. This trigger permits
        exactly that transition and compares every other column with SQLite's
        NULL-safe ``IS`` operator; money/token evidence in the source JSON,
        worker/model attribution, and timestamps remain append-only.

        ``run_id`` is globally unique: current writers generate a UUID per
        concrete launch, while legacy ids combine the globally unique runbook
        primary key with an attempt ordinal. Earlier ENG-PC-05 trees incorrectly
        included ``project_id`` in the unique key, so a later attribution could
        duplicate a run. During upgrade the oldest row remains canonical, gains
        a usable attribution from a duplicate when necessary, and every duplicate
        is removed before the run-only unique index is installed.
        """

        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(usage_ledger)")}
        index = next(
            (
                row
                for row in self._conn.execute("PRAGMA index_list(usage_ledger)")
                if row["name"] == "idx_usage_ledger_run"
            ),
            None,
        )
        index_columns = (
            [row["name"] for row in self._conn.execute("PRAGMA index_info(idx_usage_ledger_run)")]
            if index is not None
            else []
        )
        if (
            "program_ref" in columns
            and index is not None
            and index["unique"] == 1
            and index["partial"] == 0
            and index_columns == ["run_id"]
        ):
            # This migration deliberately has no schema-version write of its
            # own: the project-registry bootstrap owns that setting and may not
            # have run yet. The actual ledger schema is the durable migration
            # marker. Returning before any DROP/CREATE/ALTER is essential;
            # even idempotent DDL takes SQLite's schema lock and can make a
            # concurrent State opener fail with ``database is locked``.
            return

        if "program_ref" not in columns:
            self._conn.execute("ALTER TABLE usage_ledger ADD COLUMN program_ref TEXT")

        self._conn.execute("DROP TRIGGER IF EXISTS usage_ledger_no_update")
        self._conn.execute("DROP TRIGGER IF EXISTS usage_ledger_no_delete")
        self._conn.execute("DROP INDEX IF EXISTS idx_usage_ledger_run")

        # Empty text is not an identity. Normalize historical instances before
        # the stricter trigger is restored.
        self._conn.execute("UPDATE usage_ledger SET project_id = NULL WHERE TRIM(project_id) = ''")
        duplicates = self._conn.execute(
            "SELECT run_id FROM usage_ledger GROUP BY run_id HAVING COUNT(*) > 1"
        ).fetchall()
        for duplicate in duplicates:
            rows = self._conn.execute(
                "SELECT id, project_id, program_ref FROM usage_ledger WHERE run_id = ? ORDER BY id",
                (duplicate["run_id"],),
            ).fetchall()
            canonical = rows[0]
            project_id = canonical["project_id"]
            program_ref = canonical["program_ref"]
            if project_id is None:
                project_id = next((row["project_id"] for row in rows if row["project_id"]), None)
            if program_ref is None:
                program_ref = next((row["program_ref"] for row in rows if row["program_ref"]), None)
            self._conn.execute(
                "UPDATE usage_ledger SET project_id = ?, program_ref = ? WHERE id = ?",
                (project_id, program_ref, canonical["id"]),
            )
            self._conn.execute(
                "DELETE FROM usage_ledger WHERE run_id = ? AND id <> ?",
                (duplicate["run_id"], canonical["id"]),
            )

        # Best-effort backfill while the mutable task row still exists. New
        # writes always persist this value, so later task deletion cannot loosen
        # a program budget.
        task_rows = self._conn.execute(
            "SELECT usage_ledger.id, tasks.task_ref FROM usage_ledger "
            "JOIN tasks ON tasks.id = usage_ledger.task_id WHERE usage_ledger.program_ref IS NULL"
        ).fetchall()
        for row in task_rows:
            stable_ref = str(row["task_ref"] or "").split(maxsplit=1)[0]
            program_ref = stable_ref.rsplit("-", 1)[0] if "-" in stable_ref else stable_ref
            if program_ref:
                self._conn.execute(
                    "UPDATE usage_ledger SET program_ref = ? WHERE id = ?", (program_ref, row["id"])
                )

        self._conn.execute("CREATE UNIQUE INDEX idx_usage_ledger_run ON usage_ledger (run_id)")
        self._conn.execute(
            """
            CREATE TRIGGER usage_ledger_no_update
            BEFORE UPDATE ON usage_ledger
            WHEN NOT (
                OLD.project_id IS NULL
                AND NEW.project_id IS NOT NULL
                AND LENGTH(TRIM(NEW.project_id)) > 0
                AND NEW.id IS OLD.id
                AND NEW.program_ref IS OLD.program_ref
                AND NEW.task_id IS OLD.task_id
                AND NEW.runbook_id IS OLD.runbook_id
                AND NEW.run_id IS OLD.run_id
                AND NEW.session_id IS OLD.session_id
                AND NEW.worker IS OLD.worker
                AND NEW.provider IS OLD.provider
                AND NEW.effective_model IS OLD.effective_model
                AND NEW.occurred_at IS OLD.occurred_at
                AND NEW.source_record IS OLD.source_record
                AND NEW.source_attempt IS OLD.source_attempt
                AND NEW.created_at IS OLD.created_at
            )
            BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END
            """
        )
        self._conn.execute(
            "CREATE TRIGGER usage_ledger_no_delete BEFORE DELETE ON usage_ledger "
            "BEGIN SELECT RAISE(ABORT, 'usage_ledger is append-only'); END"
        )

    @_serialized
    def _migrate_usage_budget_columns(self) -> None:
        """Add scope-B columns without replacing mutable configuration."""

        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(usage_budgets)").fetchall()}
        columns = (
            ("project_id", "TEXT"),
            ("scope_type", "TEXT"),
            ("scope_key", "TEXT"),
            ("constraint_type", "TEXT"),
            ("limit_value", "REAL"),
            ("warning_fraction", "REAL NOT NULL DEFAULT 0.8"),
            ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        )
        for column, ddl in columns:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE usage_budgets ADD COLUMN {column} {ddl}")

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

        This is only the one-time migration of a pre-registry database, whose
        rows all came from its single implicit managed project. It must not be
        used as a general attribution mechanism for heterogeneous NULL rows;
        current writers resolve or retain per-run identity instead.

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
    def upsert_task(self, task: Task, *, updated_at: str | None = None) -> None:
        """Write ``task``, stamping ``updated_at`` with the current second.

        ``updated_at`` overrides that stamp and exists for deterministic
        seeding (OCTAREL-TEST-02, issue #45, upholding OCTAREL-TEST-01's shared-
        fixture contract).  An ordinary write means "this row was
        touched now", but a fixture that seeds several task references in one
        pass needs their *relative* recency to be intentional rather than a
        function of how long the seed happened to take: ``utc_now_iso()`` has
        one-second resolution, so a seed that crosses a wall-clock second
        silently reorders which reference looks most recent.
        """

        task.updated_at = updated_at or utc_now_iso()
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
    def record_run_event(self, event: RunEvent, *, repo_root: Path | None = None) -> Event:
        """Append one typed event and atomically allocate its per-run sequence.

        ``BEGIN IMMEDIATE`` serializes sequence allocation across independent
        :class:`State` instances/processes, not merely threads sharing this
        object.  The existing unique index is the final invariant backstop.
        """

        row = normalize_run_event(event, repo_root=repo_root)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            sequence_row = self._conn.execute(
                "SELECT COALESCE(MAX(run_sequence), 0) + 1 AS next_sequence "
                "FROM events WHERE run_id = ? AND project_id IS ?",
                (row["run_id"], row["project_id"]),
            ).fetchone()
            sequence = int(sequence_row["next_sequence"])
            cursor = self._conn.execute(
                "INSERT INTO events "
                "(ts, category, task_id, provider, level, message, project_id, run_id, run_sequence, "
                "event_class, event_type, source, provenance, data, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    utc_now_iso(),
                    row["category"],
                    row["task_id"],
                    row["provider"],
                    row["level"],
                    row["message"],
                    row["project_id"],
                    row["run_id"],
                    sequence,
                    row["event_class"],
                    row["event_type"],
                    row["source"],
                    row["provenance"],
                    json.dumps(row["data"], sort_keys=True, separators=(",", ":")),
                    json.dumps(row["evidence"], sort_keys=True, separators=(",", ":")),
                ),
            )
            stored = self._conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return Event.from_row(dict(stored))

    @_serialized
    def list_events(self, *, limit: int = 200, project_id: str | None = None) -> list[Event]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Event.from_row(dict(row)) for row in rows]

    @_serialized
    def get_event(self, event_id: int, *, project_id: str | None = None) -> Event | None:
        if project_id is not None:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ? AND project_id = ?", (event_id, project_id)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return Event.from_row(dict(row)) if row else None

    @_serialized
    def list_run_events(
        self,
        *,
        limit: int = 200,
        project_id: str | None = None,
        run_id: str | None = None,
        event_class: str | None = None,
        after_id: int | None = None,
        task_ids: Iterable[str] | None = None,
        ascending: bool = True,
    ) -> list[Event]:
        """Read the one events table as a chronological typed timeline.

        Legacy events are intentionally included.  The API projects their
        missing typed fields as honest UNKNOWN/NOT_REPORTED values rather than
        backfilling facts that were never recorded.
        """

        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if event_class is not None:
            clauses.append("COALESCE(event_class, category) = ?")
            values.append(event_class)
        if after_id is not None:
            clauses.append("id > ?")
            values.append(max(0, int(after_id)))
        if task_ids is not None:
            selected_task_ids = tuple(task_ids)
            if not selected_task_ids:
                return []
            clauses.append(f"task_id IN ({', '.join('?' for _ in selected_task_ids)})")
            values.extend(selected_task_ids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "ASC" if ascending else "DESC"
        values.append(max(1, min(int(limit), 1_000)))
        rows = self._conn.execute(
            f"SELECT * FROM events{where} ORDER BY id {direction} LIMIT ?", values
        ).fetchall()
        return [Event.from_row(dict(row)) for row in rows]

    # ------------------------------------------------------ context cursors

    @staticmethod
    def _context_cursor_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in (
            "bundle_identity",
            "preserved_policy_identity",
            "delivered_task_identities",
            "inspection_summary",
        ):
            item[key] = json.loads(item[key])
        return item

    @_serialized
    def get_context_cursor(
        self, *, project_id: str, task_id: str, consumer_id: str
    ) -> dict[str, Any] | None:
        """Return only the cursor owned by the exact project/task/consumer."""

        row = self._conn.execute(
            "SELECT * FROM context_cursors "
            "WHERE project_id = ? AND task_id = ? AND consumer_id = ?",
            (project_id, task_id, consumer_id),
        ).fetchone()
        return self._context_cursor_from_row(row) if row else None

    @_serialized
    def upsert_context_cursor(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist identity/event positions, never authoritative content."""

        now = utc_now_iso()
        payload = {
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "consumer_id": row["consumer_id"],
            "tree_sha": row["tree_sha"],
            "policy_digest": row["policy_digest"],
            "task_contract_digest": row["task_contract_digest"],
            "identity_fingerprint": row["identity_fingerprint"],
            "bundle_identity": json.dumps(
                row["bundle_identity"], sort_keys=True, separators=(",", ":")
            ),
            "preserved_policy_identity": json.dumps(
                row["preserved_policy_identity"], sort_keys=True, separators=(",", ":")
            ),
            "delivered_task_identities": json.dumps(
                row.get("delivered_task_identities", {}), sort_keys=True, separators=(",", ":")
            ),
            "delivered_ancestry_identity": row.get("delivered_ancestry_identity", ""),
            "last_event_id": int(row.get("last_event_id", 0)),
            "last_outcome": row.get("last_outcome", "DELIVERED"),
            "last_attempted_characters": int(row.get("last_attempted_characters", 0)),
            "inspection_summary": json.dumps(
                row.get("inspection_summary", {}), sort_keys=True, separators=(",", ":")
            ),
            "created_at": row.get("created_at") or now,
            "updated_at": now,
        }
        self._conn.execute(
            "INSERT INTO context_cursors "
            "(project_id, task_id, consumer_id, tree_sha, policy_digest, task_contract_digest, "
            "identity_fingerprint, bundle_identity, preserved_policy_identity, "
            "delivered_task_identities, delivered_ancestry_identity, last_event_id, "
            "last_outcome, last_attempted_characters, inspection_summary, created_at, updated_at) "
            "VALUES (:project_id, :task_id, :consumer_id, :tree_sha, :policy_digest, "
            ":task_contract_digest, :identity_fingerprint, :bundle_identity, "
            ":preserved_policy_identity, :delivered_task_identities, "
            ":delivered_ancestry_identity, :last_event_id, :last_outcome, "
            ":last_attempted_characters, :inspection_summary, :created_at, :updated_at) "
            "ON CONFLICT(project_id, task_id, consumer_id) DO UPDATE SET "
            "tree_sha=excluded.tree_sha, policy_digest=excluded.policy_digest, "
            "task_contract_digest=excluded.task_contract_digest, "
            "identity_fingerprint=excluded.identity_fingerprint, "
            "bundle_identity=excluded.bundle_identity, "
            "preserved_policy_identity=excluded.preserved_policy_identity, "
            "delivered_task_identities=excluded.delivered_task_identities, "
            "delivered_ancestry_identity=excluded.delivered_ancestry_identity, "
            "last_event_id=excluded.last_event_id, last_outcome=excluded.last_outcome, "
            "last_attempted_characters=excluded.last_attempted_characters, "
            "inspection_summary=excluded.inspection_summary, "
            "updated_at=excluded.updated_at",
            payload,
        )
        self._conn.commit()
        stored = self._conn.execute(
            "SELECT * FROM context_cursors "
            "WHERE project_id = ? AND task_id = ? AND consumer_id = ?",
            (payload["project_id"], payload["task_id"], payload["consumer_id"]),
        ).fetchone()
        if stored is None:  # pragma: no cover - insert/select share one serialized connection
            raise RuntimeError("context cursor write completed without a readable row")
        return self._context_cursor_from_row(stored)

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
        """Delete mutable runbook state while retaining attributed usage history.

        Attributed ledger rows contain their own ``project_id`` and are
        deliberately not cascaded, so historical spend remains visible through
        project-filtered reads after its runbook and governance row are gone.
        """

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

    # ---------------------------------------------------------- usage ledger

    @staticmethod
    def _usage_ledger_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_record"] = json.loads(item["source_record"])
        item["source_attempt"] = json.loads(item["source_attempt"])
        return item

    @_serialized
    def append_usage_ledger(self, row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Append one run exactly once, returning ``(stored_row, inserted)``.

        The plain unique index on ``run_id`` is the cross-process idempotency guard.
        A replay/conflict is an expected no-op and is contained here rather
        than leaking ``sqlite3.IntegrityError`` into the daemon.
        """

        payload = {
            "project_id": (str(row["project_id"]).strip() or None) if row.get("project_id") else None,
            "program_ref": row.get("program_ref"),
            "task_id": row.get("task_id"),
            "runbook_id": row["runbook_id"],
            "run_id": row["run_id"],
            "session_id": row.get("session_id"),
            "worker": row["worker"],
            "provider": row.get("provider"),
            "effective_model": row.get("effective_model"),
            "occurred_at": row.get("occurred_at"),
            "source_record": json.dumps(row["source_record"], sort_keys=True, separators=(",", ":")),
            "source_attempt": json.dumps(row["source_attempt"], sort_keys=True, separators=(",", ":")),
            "created_at": row.get("created_at") or utc_now_iso(),
        }
        try:
            cursor = self._conn.execute(
                "INSERT INTO usage_ledger "
                "(project_id, program_ref, task_id, runbook_id, run_id, session_id, worker, provider, "
                "effective_model, occurred_at, source_record, source_attempt, created_at) "
                "VALUES (:project_id, :program_ref, :task_id, :runbook_id, :run_id, :session_id, :worker, "
                ":provider, :effective_model, :occurred_at, :source_record, :source_attempt, :created_at) "
                "ON CONFLICT(run_id) DO NOTHING",
                payload,
            )
            inserted = cursor.rowcount == 1
            stored = self._conn.execute(
                "SELECT * FROM usage_ledger WHERE run_id = ?", (payload["run_id"],)
            ).fetchone()
            if stored is not None and stored["project_id"] is None and payload["project_id"] is not None:
                self._conn.execute(
                    "UPDATE usage_ledger SET project_id = ? WHERE run_id = ?",
                    (payload["project_id"], payload["run_id"]),
                )
                stored = self._conn.execute(
                    "SELECT * FROM usage_ledger WHERE run_id = ?", (payload["run_id"],)
                ).fetchone()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            stored = self._conn.execute(
                "SELECT * FROM usage_ledger WHERE run_id = ?", (payload["run_id"],)
            ).fetchone()
            if stored is None:
                raise
            return self._usage_ledger_from_row(stored), False
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        if stored is None:  # pragma: no cover - the insert/select are one serialized operation
            raise RuntimeError("usage ledger insert completed without a readable row")
        return self._usage_ledger_from_row(stored), inserted

    @_serialized
    def list_usage_ledger(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("provider", provider),
            ("effective_model", model),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM usage_ledger{where} ORDER BY occurred_at DESC, id DESC", values
        ).fetchall()
        return [self._usage_ledger_from_row(row) for row in rows]

    # --------------------------------------------------------- usage budgets

    @_serialized
    def upsert_usage_budget(self, budget: dict[str, Any]) -> dict[str, Any]:
        """Create or replace one mutable budget definition."""

        now = utc_now_iso()
        existing = self._conn.execute(
            "SELECT created_at FROM usage_budgets WHERE id = ?", (budget["id"],)
        ).fetchone()
        row = {
            "id": str(budget["id"]),
            "project_id": budget.get("project_id"),
            "scope_type": str(budget["scope_type"]),
            "scope_key": budget.get("scope_key"),
            "constraint_type": str(budget["constraint_type"]),
            "limit_value": float(budget["limit_value"]),
            "warning_fraction": float(budget.get("warning_fraction", 0.8)),
            "enabled": 1 if budget.get("enabled", True) else 0,
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        self._conn.execute(
            "INSERT INTO usage_budgets "
            "(id, project_id, scope_type, scope_key, constraint_type, limit_value, "
            "warning_fraction, enabled, created_at, updated_at) "
            "VALUES (:id, :project_id, :scope_type, :scope_key, :constraint_type, :limit_value, "
            ":warning_fraction, :enabled, :created_at, :updated_at) "
            "ON CONFLICT(id) DO UPDATE SET "
            "project_id=excluded.project_id, scope_type=excluded.scope_type, scope_key=excluded.scope_key, "
            "constraint_type=excluded.constraint_type, limit_value=excluded.limit_value, "
            "warning_fraction=excluded.warning_fraction, enabled=excluded.enabled, updated_at=excluded.updated_at",
            row,
        )
        self._conn.commit()
        return self.get_usage_budget(row["id"]) or row

    @_serialized
    def get_usage_budget(self, budget_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM usage_budgets WHERE id = ?", (budget_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    @_serialized
    def list_usage_budgets(
        self, *, project_id: str | None = None, include_global: bool = True, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            clauses.append("(project_id = ? OR project_id IS NULL)" if include_global else "project_id = ?")
            values.append(project_id)
        elif not include_global:
            clauses.append("project_id IS NOT NULL")
        if enabled_only:
            clauses.append("enabled = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM usage_budgets{where} ORDER BY scope_type, scope_key, constraint_type, id",
            values,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result

    @_serialized
    def delete_usage_budget(self, budget_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM usage_budgets WHERE id = ?", (budget_id,))
        self._conn.commit()
        return cursor.rowcount == 1

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

    # ------------------------------------------------------- execution leases
    # ENG-PC-01 (issue #29): every mutation below is a single guarded SQL
    # statement so the database's own atomicity -- not a Python read-then-write
    # -- is what decides a race between two processes. See ``execution_lease.py``
    # for the policy (staleness proof, retry-never semantics) built on top.

    @_serialized
    def get_execution_lease(self, worktree: str) -> ExecutionLease | None:
        row = self._conn.execute(
            "SELECT * FROM execution_leases WHERE worktree = ?", (worktree,)
        ).fetchone()
        return ExecutionLease.from_row(dict(row)) if row else None

    @_serialized
    def list_execution_leases(self, *, project_id: str | None = None) -> list[ExecutionLease]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM execution_leases WHERE project_id = ? ORDER BY worktree", (project_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM execution_leases ORDER BY worktree").fetchall()
        return [ExecutionLease.from_row(dict(row)) for row in rows]

    @_serialized
    def try_acquire_execution_lease(
        self,
        *,
        worktree: str,
        expected_generation: int,
        task_id: str | None,
        project_id: str | None,
        stable_task_id: str | None,
        runbook_id: str | None,
        worker: str | None,
        owner_host: str | None,
        owner_pid: int | None,
        owner_pid_create_time: float | None,
        reason: str | None,
    ) -> ExecutionLease | None:
        """Compare-and-swap acquisition. ``None`` means the caller lost the race.

        The bootstrap ``INSERT OR IGNORE`` only ever creates the initial
        ``generation = 0`` / ``RELEASED`` row for a worktree never seen before; it
        never touches an existing row, so it cannot itself race the CAS ``UPDATE``
        below in a way that changes who wins. The ``UPDATE ... WHERE worktree = ?
        AND generation = ?`` is the actual guard: it succeeds only when the caller's
        ``expected_generation`` still matches the durable row, exactly the same
        compare-and-swap shape as ``claim_pending_fallback``.
        """

        now = utc_now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO execution_leases (worktree, generation, status, created_at, updated_at) "
            "VALUES (?, 0, 'RELEASED', ?, ?)",
            (worktree, now, now),
        )
        cursor = self._conn.execute(
            "UPDATE execution_leases SET "
            "generation = generation + 1, status = 'ACQUIRED', "
            "project_id = ?, task_id = ?, stable_task_id = ?, runbook_id = ?, worker = ?, "
            "owner_host = ?, owner_pid = ?, owner_pid_create_time = ?, spawn_pending = 1, "
            "acquired_at = ?, heartbeat_at = ?, released_at = NULL, release_reason = NULL, "
            "recovery_reason = ?, conflict_reason = NULL, updated_at = ? "
            "WHERE worktree = ? AND generation = ?",
            (
                project_id, task_id, stable_task_id, runbook_id, worker,
                owner_host, owner_pid, owner_pid_create_time,
                now, now, reason, now,
                worktree, expected_generation,
            ),
        )
        if cursor.rowcount != 1:
            self._conn.commit()
            return None
        self._conn.commit()
        return self.get_execution_lease(worktree)

    @_serialized
    def update_execution_lease_pid(
        self, *, worktree: str, expected_generation: int, pid: int, pid_create_time: float | None
    ) -> bool:
        """Attach the real launched-subprocess pid once known (after ``Popen`` returns).

        Guarded by the same ``generation`` the acquisition returned, so a lease
        reclaimed from under an acquirer in the narrow window between winning the
        CAS and the subprocess actually spawning is never silently overwritten.

        Also clears ``spawn_pending``: this is the one durable write that proves a
        real worker subprocess pid now exists, so it is the only place that flag
        may be turned off (see ``execution_lease._classify_owner``). A ``False``
        return means the generation no longer matched -- the lease was reclaimed
        out from under the caller between acquisition and this call -- and the
        caller must treat the just-spawned child as unowned rather than proceed.
        """

        now = utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE execution_leases SET owner_pid = ?, owner_pid_create_time = ?, spawn_pending = 0, "
            "heartbeat_at = ?, updated_at = ? "
            "WHERE worktree = ? AND generation = ? AND status = 'ACQUIRED'",
            (pid, pid_create_time, now, now, worktree, expected_generation),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_serialized
    def release_execution_lease(self, *, worktree: str, expected_pid: int, reason: str) -> bool:
        """Graceful self-release: only the process whose pid is currently recorded may free it."""

        now = utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE execution_leases SET status = 'RELEASED', released_at = ?, release_reason = ?, updated_at = ? "
            "WHERE worktree = ? AND owner_pid = ? AND status = 'ACQUIRED'",
            (now, reason, now, worktree, expected_pid),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_serialized
    def record_execution_lease_conflict(self, *, worktree: str, reason: str) -> None:
        """Informational only: the last refusal reason observers see for this worktree.

        Never part of any CAS decision -- a conflicting acquirer never mutated the
        row's ownership, so this only annotates it for the Runs/Tasks inspector.
        """

        now = utc_now_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO execution_leases (worktree, generation, status, created_at, updated_at) "
            "VALUES (?, 0, 'RELEASED', ?, ?)",
            (worktree, now, now),
        )
        self._conn.execute(
            "UPDATE execution_leases SET conflict_reason = ?, updated_at = ? WHERE worktree = ?",
            (reason, now, worktree),
        )
        self._conn.commit()

    @_serialized
    def force_release_execution_lease(self, *, worktree: str, expected_generation: int, reason: str) -> bool:
        """Reclaim a lease whose owner an independent liveness check already proved dead.

        Never called on a timeout or executable-name match alone -- the caller
        (``execution_lease.reconcile_stale_leases``) must already have proven the
        recorded pid dead via ``recovery.pid_is_alive`` or a create-time mismatch.
        """

        now = utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE execution_leases SET status = 'RELEASED', released_at = ?, release_reason = ?, "
            "recovery_reason = ?, spawn_pending = 0, updated_at = ? "
            "WHERE worktree = ? AND generation = ? AND status = 'ACQUIRED'",
            (now, reason, reason, now, worktree, expected_generation),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_serialized
    def release_execution_lease_before_spawn(self, *, worktree: str, expected_generation: int, reason: str) -> bool:
        """Undo a just-won acquisition whose spawn attempt never produced a live worker.

        Distinct from ``release_execution_lease`` (a graceful self-release by the pid
        currently recorded as owner) and ``force_release_execution_lease`` (a reclaim
        that required independent proof the recorded owner is dead): this is neither.
        It is the acquiring process's own synchronous knowledge, in the same call stack
        that won the CAS, that ``Supervisor._spawn`` raised before any subprocess pid
        could ever be attached -- there is no owner-death proof to check because the
        acquirer is not dead, it is the one calling this. CAS'd on ``generation`` (the
        same guard every other mutation here uses) rather than ``owner_pid``, so a
        lease already reclaimed out from under this caller for an unrelated reason is
        never clobbered by a late cleanup call.
        """

        now = utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE execution_leases SET status = 'RELEASED', released_at = ?, release_reason = ?, "
            "spawn_pending = 0, updated_at = ? "
            "WHERE worktree = ? AND generation = ? AND status = 'ACQUIRED'",
            (now, reason, now, worktree, expected_generation),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    # ------------------------------------------------------------ wake queue
    # ENG-PC-03 (issue #31). Every mutation below is a single guarded SQL
    # statement, the same discipline as the execution-lease block above: the
    # database's own atomicity decides concurrent outcomes, never a Python
    # read-then-write. See ``wake_queue.py`` for the typed policy layer built
    # on top (reason validation, backoff, poisoning, run-event emission).

    @_serialized
    def enqueue_wake(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        stage: str | None,
        reason: str,
        source: str | None,
        provenance: str,
        run_id: str | None,
        max_attempts: int,
    ) -> WakeRequest:
        """Atomically create-or-coalesce one wake, and durably record this contribution.

        The ``INSERT ... ON CONFLICT (...) WHERE status = 'PENDING' DO UPDATE`` targets
        exactly the partial unique index declared in the schema: SQLite only resolves a
        conflict target against a partial index when the conflict clause's own ``WHERE``
        matches it verbatim, which is what makes a duplicate pending wake for the same
        project/task/stage a single atomic increment of the existing row rather than a
        second row or a lost update -- proven against real concurrent connections, not
        mocked, in ``tests/test_wake_queue.py``. The contribution row is inserted in the
        same transaction so ``coalesced_count`` and the provenance ledger never drift
        apart.
        """

        now = utc_now_iso()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            cursor = self._conn.execute(
                "INSERT INTO wake_queue "
                "(project_id, task_id, stage, status, reason, coalesced_count, attempts, "
                "max_attempts, run_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'PENDING', ?, 1, 0, ?, ?, ?, ?) "
                "ON CONFLICT (IFNULL(project_id, ''), IFNULL(task_id, ''), IFNULL(stage, '')) "
                "WHERE status = 'PENDING' "
                "DO UPDATE SET reason = excluded.reason, coalesced_count = coalesced_count + 1, "
                "updated_at = excluded.updated_at "
                "RETURNING id",
                (project_id, task_id, stage, reason, max_attempts, run_id, now, now),
            )
            wake_id = cursor.fetchone()["id"]
            self._conn.execute(
                "INSERT INTO wake_queue_contributions (wake_id, reason, source, provenance, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (wake_id, reason, source, provenance, now),
            )
            row = self._conn.execute("SELECT * FROM wake_queue WHERE id = ?", (wake_id,)).fetchone()
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return WakeRequest.from_row(dict(row))

    @_serialized
    def claim_next_wake(self, *, claimed_by: str, now: str | None = None) -> WakeRequest | None:
        """Claim the oldest due wake (``PENDING``, or ``FAILED`` whose backoff elapsed).

        One atomic ``UPDATE ... WHERE id = (SELECT ...) RETURNING`` -- the subquery and
        the write are the same SQLite statement, so no other writer can observe or claim
        the selected row between the pick and the claim. ``None`` means nothing is due;
        never blocks and never spins.
        """

        now = now or utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE wake_queue SET status = 'CLAIMED', claimed_at = ?, claimed_by = ?, "
            "attempts = attempts + 1, updated_at = ? "
            "WHERE id = ("
            "  SELECT id FROM wake_queue "
            "  WHERE status = 'PENDING' "
            "     OR (status = 'FAILED' AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?) "
            "  ORDER BY created_at ASC LIMIT 1"
            ") "
            "RETURNING *",
            (now, claimed_by, now, now),
        )
        row = cursor.fetchone()
        self._conn.commit()
        return WakeRequest.from_row(dict(row)) if row else None

    @_serialized
    def complete_wake(self, *, wake_id: int, now: str | None = None) -> WakeRequest | None:
        """CAS: only a currently-``CLAIMED`` row may be marked ``COMPLETED``."""

        now = now or utc_now_iso()
        cursor = self._conn.execute(
            "UPDATE wake_queue SET status = 'COMPLETED', completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'CLAIMED' "
            "RETURNING *",
            (now, now, wake_id),
        )
        row = cursor.fetchone()
        self._conn.commit()
        return WakeRequest.from_row(dict(row)) if row else None

    @_serialized
    def fail_wake(
        self, *, wake_id: int, error: str, next_attempt_at: str | None, now: str | None = None
    ) -> WakeRequest | None:
        """CAS: only a currently-``CLAIMED`` row may be marked ``FAILED``/``POISONED``.

        Poisoning is decided from the typed ``attempts``/``max_attempts`` columns read
        from this same row, never from ``error`` text -- ``error`` is stored only as an
        informational, operator-facing record. Only this process (the one holding the
        claim) may ever observe the row in ``CLAIMED``, so the read immediately before
        the guarded write is not a race: no other caller can claim, complete, or fail
        the same id while it is ``CLAIMED``.
        """

        now = now or utc_now_iso()
        current = self._conn.execute(
            "SELECT * FROM wake_queue WHERE id = ? AND status = 'CLAIMED'", (wake_id,)
        ).fetchone()
        if current is None:
            return None
        poisoned = int(current["attempts"]) >= int(current["max_attempts"])
        status = "POISONED" if poisoned else "FAILED"
        cursor = self._conn.execute(
            "UPDATE wake_queue SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'CLAIMED' "
            "RETURNING *",
            (status, error, None if poisoned else next_attempt_at, now, wake_id),
        )
        row = cursor.fetchone()
        self._conn.commit()
        return WakeRequest.from_row(dict(row)) if row else None

    @_serialized
    def recover_claimed_wakes(
        self, *, project_id: str | None = None, claimed_by: str | None = None
    ) -> WakeRecoveryOutcome:
        """Resolve every orphaned ``CLAIMED`` wake out of ``CLAIMED``. Identity-based,
        never a timeout-based guess -- used two ways:

        - at daemon startup (``project_id`` only, no ``claimed_by``): every row still
          ``CLAIMED`` belonged to a previous process instance that is now gone, so it is
          definitionally orphaned (see ``advancement_lease.daemon_authority_active`` --
          there is only ever one daemon authority, so there is no cross-process
          ownership question to prove the way execution leases must).
        - at the start of every ``wake_queue.drain_due`` pass within one still-running
          daemon (``claimed_by`` only, no restart involved): ``claimed_by`` is a single
          fixed identity per daemon (``orchestrator._cmd_run`` always claims as
          ``"daemon"``), so a row still ``CLAIMED`` under that identity when a new pass
          begins cannot belong to anyone else -- it is this same process's own prior
          ``claim()``/``complete()`` sequence, which must have raised before reaching
          ``COMPLETED``. Resetting it here lets the very next pass claim and retry it,
          recovering within one daemon lifetime instead of requiring a restart.

        A stranded row cannot always go straight back to ``PENDING``, though: coalescing
        only ever merges a fresh trigger into a row that is *currently* ``PENDING`` (see
        ``enqueue_wake``), so while this row sat ``CLAIMED`` a fresh trigger for the exact
        same (project_id, task_id, stage) key is free to -- and legitimately does --
        start its own new ``PENDING`` row for that key. A bare ``UPDATE ... SET status =
        'PENDING'`` on the stranded row would then collide with that sibling under
        ``idx_wake_queue_pending_key`` and raise ``sqlite3.IntegrityError``, which used to
        propagate out of this method into ``drain_due`` and daemon startup alike, turning
        one contended ``complete_wake`` into a permanent outage. Instead, each stranded
        row is checked against its own key before being touched: with no ``PENDING``
        sibling it is reset in place exactly as before; with one, the two rows represent
        the same ask, so the stranded row's contributions are re-parented onto the
        sibling, its ``coalesced_count`` is folded in, and the now-empty stranded row is
        deleted -- never a second ``PENDING`` row, and never a dropped reason, count or
        contribution. Every row is resolved inside the same transaction, so the
        at-most-one-``PENDING``-per-key invariant never has a window where it could be
        violated.
        """

        now = utc_now_iso()
        clauses = ["status = 'CLAIMED'"]
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        if claimed_by is not None:
            clauses.append("claimed_by = ?")
            values.append(claimed_by)
        where = " AND ".join(clauses)
        reset = 0
        merged = 0
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            stranded = self._conn.execute(f"SELECT * FROM wake_queue WHERE {where}", values).fetchall()
            for row in stranded:
                wake_id = row["id"]
                sibling = self._conn.execute(
                    "SELECT id FROM wake_queue WHERE status = 'PENDING' "
                    "AND IFNULL(project_id, '') = IFNULL(?, '') "
                    "AND IFNULL(task_id, '') = IFNULL(?, '') "
                    "AND IFNULL(stage, '') = IFNULL(?, '') "
                    "AND id != ?",
                    (row["project_id"], row["task_id"], row["stage"], wake_id),
                ).fetchone()
                if sibling is None:
                    self._conn.execute(
                        "UPDATE wake_queue SET status = 'PENDING', claimed_at = NULL, "
                        "claimed_by = NULL, updated_at = ? WHERE id = ?",
                        (now, wake_id),
                    )
                    reset += 1
                else:
                    sibling_id = sibling["id"]
                    self._conn.execute(
                        "UPDATE wake_queue_contributions SET wake_id = ? WHERE wake_id = ?",
                        (sibling_id, wake_id),
                    )
                    self._conn.execute(
                        "UPDATE wake_queue SET coalesced_count = coalesced_count + ?, updated_at = ? "
                        "WHERE id = ?",
                        (row["coalesced_count"], now, sibling_id),
                    )
                    self._conn.execute("DELETE FROM wake_queue WHERE id = ?", (wake_id,))
                    merged += 1
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return WakeRecoveryOutcome(reset=reset, merged=merged)

    @_serialized
    def get_wake(self, wake_id: int) -> WakeRequest | None:
        row = self._conn.execute("SELECT * FROM wake_queue WHERE id = ?", (wake_id,)).fetchone()
        return WakeRequest.from_row(dict(row)) if row else None

    @_serialized
    def list_wakes(
        self, *, project_id: str | None = None, status: str | None = None, limit: int = 500
    ) -> list[WakeRequest]:
        clauses: list[str] = []
        values: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM wake_queue {where} ORDER BY created_at ASC LIMIT ?", values
        ).fetchall()
        return [WakeRequest.from_row(dict(row)) for row in rows]

    @_serialized
    def list_wake_contributions(self, wake_id: int) -> list[WakeContribution]:
        rows = self._conn.execute(
            "SELECT * FROM wake_queue_contributions WHERE wake_id = ? ORDER BY id ASC", (wake_id,)
        ).fetchall()
        return [WakeContribution.from_row(dict(row)) for row in rows]

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

    # ----------------------------------------------------- agent sessions
    # ENG-PC-02 policy (compatibility, redaction screening, reasons) lives in
    # ``agent_session.py``. These primitives only provide atomic persistence.

    @staticmethod
    def _public_agent_session(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        """Return session metadata without the opaque native continuation state."""

        public = dict(row)
        public.pop("continuation_state", None)
        public["has_stored_state"] = bool(row["continuation_state"])
        public["force_fresh_next"] = bool(row["force_fresh_next"])
        public["active"] = bool(row["active"])
        return public

    @_serialized
    def get_active_agent_session(self, *, project_id: str, task_id: str, worker: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE project_id = ? AND task_id = ? AND worker = ? AND active = 1",
            (project_id, task_id, worker),
        ).fetchone()
        return self._public_agent_session(row) if row else None

    @_serialized
    def list_agent_sessions(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """List safe metadata only; opaque continuation state never crosses this boundary."""

        if project_id is None:
            rows = self._conn.execute(
                "SELECT * FROM agent_sessions ORDER BY last_activity_at DESC, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agent_sessions WHERE project_id = ? ORDER BY last_activity_at DESC, id",
                (project_id,),
            ).fetchall()
        return [self._public_agent_session(row) for row in rows]

    @_serialized
    def replace_active_agent_session(
        self,
        *,
        row: dict[str, Any],
        expected_session_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically replace the active row, returning ``(row, won)``.

        The expected id is a compare-and-swap guard. ``BEGIN IMMEDIATE`` and
        the partial unique index jointly decide concurrent creators. Any index
        conflict is contained and returned as ``won=False``; it never escapes
        into a daemon caller loop.
        """

        key = (row["project_id"], row["task_id"], row["worker"])
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._conn.execute(
                "SELECT * FROM agent_sessions WHERE project_id = ? AND task_id = ? AND worker = ? AND active = 1",
                key,
            ).fetchone()
            current_id = current["id"] if current else None
            if current_id != expected_session_id:
                self._conn.rollback()
                return (self._public_agent_session(current), False) if current else ({}, False)
            if current is not None:
                self._conn.execute("UPDATE agent_sessions SET active = 0 WHERE id = ? AND active = 1", (current_id,))
            columns = tuple(row)
            self._conn.execute(
                f"INSERT INTO agent_sessions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
            stored = self._conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (row["id"],)).fetchone()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            winner = self._conn.execute(
                "SELECT * FROM agent_sessions WHERE project_id = ? AND task_id = ? AND worker = ? AND active = 1",
                key,
            ).fetchone()
            return (self._public_agent_session(winner), False) if winner else ({}, False)
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()
        return self._public_agent_session(stored), True

    @_serialized
    def try_resume_agent_session(
        self,
        *,
        session_id: str,
        identity_fingerprint: str,
        now: str,
        run_id: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        """CAS an actual resume and increment its continuation count exactly once."""

        cursor = self._conn.execute(
            "UPDATE agent_sessions SET continuation_count = continuation_count + 1, "
            "last_activity_at = ?, last_run_id = ?, last_mode = 'RESUMED', reason = ? "
            "WHERE id = ? AND active = 1 AND identity_fingerprint = ? "
            "AND force_fresh_next = 0 AND continuation_state IS NOT NULL RETURNING *",
            (now, run_id, reason, session_id, identity_fingerprint),
        )
        row = cursor.fetchone()
        self._conn.commit()
        return self._public_agent_session(row) if row else None

    @_serialized
    def store_agent_session_state(
        self,
        *,
        session_id: str,
        continuation_state: str | None,
        now: str,
        run_id: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        cursor = self._conn.execute(
            "UPDATE agent_sessions SET continuation_state = ?, last_activity_at = ?, last_run_id = ?, state_reason = ? "
            "WHERE id = ? AND active = 1 RETURNING *",
            (continuation_state, now, run_id, reason, session_id),
        )
        row = cursor.fetchone()
        self._conn.commit()
        return self._public_agent_session(row) if row else None

    @_serialized
    def request_agent_session_fresh(self, *, project_id: str, task_id: str, worker: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE agent_sessions SET force_fresh_next = 1 "
            "WHERE project_id = ? AND task_id = ? AND worker = ? AND active = 1",
            (project_id, task_id, worker),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @_serialized
    def _agent_session_resume_state(self, *, session_id: str, identity_fingerprint: str) -> str | None:
        """Capability-gated internal read used only to build a native resume argv.

        Deliberately private: dashboards, manifests, list APIs, and generic
        callers receive metadata only.
        """

        row = self._conn.execute(
            "SELECT continuation_state FROM agent_sessions "
            "WHERE id = ? AND identity_fingerprint = ? AND active = 1 AND continuation_state IS NOT NULL",
            (session_id, identity_fingerprint),
        ).fetchone()
        return str(row["continuation_state"]) if row else None

    # ------------------------------------------------- overnight sessions

    @_serialized
    def get_overnight_session(self, session_id: str) -> dict[str, Any] | None:
        """Durable ENG-AO-05 continuous-advancement session (bounds, counters, stop reason)."""

        row = self._conn.execute("SELECT * FROM overnight_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._overnight_from_row(row) if row else None

    @_serialized
    def upsert_overnight_session(self, record: dict[str, Any]) -> None:
        now = utc_now_iso()
        self._conn.execute(
            "INSERT INTO overnight_sessions (session_id, project_id, state, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "state=excluded.state, payload=excluded.payload, updated_at=excluded.updated_at",
            (record["session_id"], record["project_id"], record["state"], json.dumps(record, sort_keys=True), now, now),
        )
        self._conn.commit()

    @_serialized
    def insert_overnight_session_if_none_live(self, record: dict[str, Any], live_states: Iterable[str]) -> bool:
        """Insert a new session unless its project already has a live one (atomic across processes)."""

        states = tuple(live_states)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            marks = ",".join("?" for _ in states)
            taken = self._conn.execute(
                f"SELECT 1 FROM overnight_sessions WHERE project_id = ? AND state IN ({marks}) LIMIT 1",
                (record["project_id"], *states),
            ).fetchone()
            if taken:
                self._conn.rollback()
                return False
            now = utc_now_iso()
            self._conn.execute(
                "INSERT INTO overnight_sessions (session_id, project_id, state, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (record["session_id"], record["project_id"], record["state"], json.dumps(record, sort_keys=True), now, now),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return True

    @_serialized
    def mutate_overnight_session(
        self, session_id: str, fn: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Atomic read-modify-write of one session under a cross-process write lock.

        The daemon and the dashboard/CLI are separate processes that both update a session;
        ``BEGIN IMMEDIATE`` makes each update see the other's latest committed record, so an
        operator's pause/stop can never be overwritten by a stale daemon copy (or vice versa).
        ``fn`` may raise to abort; the transaction is rolled back and the error propagates.
        """

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute("SELECT * FROM overnight_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                self._conn.rollback()
                return None
            record = fn(self._overnight_from_row(row))
            self._conn.execute(
                "UPDATE overnight_sessions SET state = ?, payload = ?, updated_at = ? WHERE session_id = ?",
                (record["state"], json.dumps(record, sort_keys=True), utc_now_iso(), session_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return record

    @_serialized
    def list_overnight_sessions(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM overnight_sessions WHERE project_id = ? ORDER BY created_at DESC, session_id", (project_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM overnight_sessions ORDER BY created_at DESC, session_id").fetchall()
        return [self._overnight_from_row(row) for row in rows]

    @staticmethod
    def _overnight_from_row(row: sqlite3.Row) -> dict[str, Any]:
        record = json.loads(row["payload"] or "{}")
        record["session_id"] = row["session_id"]
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
