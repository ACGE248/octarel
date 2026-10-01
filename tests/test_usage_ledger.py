"""ENG-PC-05 scope A: durable, attempt-scoped usage accounting."""

from __future__ import annotations

import datetime as dt
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.agents.control_plane.models import TASK_SUCCEEDED, Task
from scripts.agents.control_plane.pricing import ModelPricing, PricingLookup
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.usage_ledger import (
    build_ledger_rows,
    reconcile_usage_governance,
    record_usage_attempts,
)
from scripts.agents.control_plane.usage_policy import finalize_route_attempt
from scripts.agents.control_plane.usage_telemetry import (
    BILLING_SUBSCRIPTION,
    BILLING_UNKNOWN,
    CLASS_DERIVED,
    CLASS_NOT_EXPOSED,
    CLASS_UNKNOWN,
    REASON_NO_CACHE_ACCOUNTING,
    REASON_NO_CONTEXT_LIMIT,
    build_aggregates,
)

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)
PRICE = ModelPricing(
    catalog_model_id="test/model",
    input_per_1k_usd=0.01,
    output_per_1k_usd=0.02,
    matched_by="test",
)


class FixedBook:
    def lookup(self, *, provider, model):
        return PricingLookup(PRICE)


def registry(*workers):
    return SimpleNamespace(workers={worker.name: worker for worker in workers})


def worker(name, provider, model, cost_class):
    return SimpleNamespace(
        name=name,
        provider=provider,
        effective_model=model,
        default_model=model,
        execution_system=f"{provider} CLI",
        cost_class=cost_class,
    )


def attempt(run_id, worker_name, *, tokens, cost_class, provider="Provider", model="model"):
    input_tokens, output_tokens = tokens
    return {
        "run_id": run_id,
        "worker": worker_name,
        "provider": provider,
        "model": model,
        "cost_class": cost_class,
        "status": "SUCCEEDED",
        "ended_at": NOW.isoformat(),
        "usage_recorded": True,
        "telemetry_quality": "exact",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def governance(*attempts, runbook_id="rb-1", task_id="task-1"):
    return {
        "runbook_id": runbook_id,
        "task_id": task_id,
        "telemetry_quality": "exact",
        "input_tokens": attempts[-1]["input_tokens"],
        "output_tokens": attempts[-1]["output_tokens"],
        "route_history": list(attempts),
        "updated_at": NOW.isoformat(),
    }


def test_fallback_records_every_worker_with_its_own_usage():
    state = State(":memory:")
    first = attempt("run-first", "worker-a", tokens=(100, 10), cost_class="metered-configured")
    second = attempt("run-second", "worker-b", tokens=(200, 20), cost_class="metered-configured")

    assert record_usage_attempts(state, governance(first, second), project_id="p") == 2
    entries = {row["worker"]: row for row in state.list_usage_ledger(project_id="p")}

    assert entries["worker-a"]["source_record"]["input_tokens"] == 100
    assert entries["worker-a"]["source_record"]["output_tokens"] == 10
    assert entries["worker-b"]["source_record"]["input_tokens"] == 200
    assert entries["worker-b"]["source_record"]["output_tokens"] == 20


def test_replay_and_unique_conflict_are_contained_and_ledger_is_structurally_immutable():
    state = State(":memory:")
    current = attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured")
    current["status"] = "RUNNING"
    row = governance(current)
    state.upsert_usage_governance(row)
    task = Task(
        id="task-1",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="worker-a",
        state=TASK_SUCCEEDED,
        result="PASS",
        runbook_id="rb-1",
        project_id="p",
    )

    assert finalize_route_attempt(state, task) is True
    assert finalize_route_attempt(state, task) is False
    stored = state.get_usage_governance("rb-1")
    assert record_usage_attempts(state, stored, project_id="p") == 0
    assert len(state.list_usage_ledger()) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute("DELETE FROM usage_ledger")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("source_attempt", '{"reported_cost_usd":999}'),
        ("source_record", '{"input_tokens":999}'),
        ("worker", "changed-worker"),
        ("effective_model", "changed-model"),
        ("occurred_at", "2099-01-01T00:00:00+00:00"),
    ],
    ids=["money", "tokens", "worker", "model", "time"],
)
def test_append_only_rejects_each_financial_fact_column(column, value):
    state = State(":memory:")
    record_usage_attempts(
        state,
        governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured")),
        project_id="p",
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute(f"UPDATE usage_ledger SET {column} = ?", (value,))
    state._conn.rollback()


def test_real_project_adoption_completes_attribution_and_makes_row_visible():
    from scripts.agents.control_plane.project_registry import (
        migrate_legacy_state_to_project,
    )

    state = State(":memory:")
    record = governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured"))

    assert record_usage_attempts(state, record) == 1
    assert len(state.list_usage_ledger()) == 1
    assert state.list_usage_ledger(project_id="project-a") == []

    result = migrate_legacy_state_to_project(state, "project-a")

    assert result["adopted_rows"]["usage_ledger"] == 1
    assert len(state.list_usage_ledger(project_id="project-a")) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute("UPDATE usage_ledger SET project_id = 'project-b'")
    state._conn.rollback()


def test_reconcile_completes_project_attribution_without_duplicating_run():
    state = State(":memory:")
    record = governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured"))
    state.upsert_usage_governance(record)

    assert reconcile_usage_governance(state) == 1
    assert state.list_usage_ledger()[0]["project_id"] is None
    state.upsert_task(
        Task(
            id="task-1",
            task_ref="ENG-PC-05",
            role="primary-implementation",
            worker="worker-a",
            project_id="project-a",
        )
    )

    assert reconcile_usage_governance(state) == 0
    rows = state.list_usage_ledger()
    assert len(rows) == 1
    assert rows[0]["project_id"] == "project-a"


def test_orphan_reconcile_under_two_selected_projects_keeps_one_run():
    state = State(":memory:")
    orphan = governance(
        attempt("orphan-run", "worker-a", tokens=(10, 2), cost_class="metered-configured"),
        task_id=None,
    )
    state.upsert_usage_governance(orphan)

    assert reconcile_usage_governance(state, project_id="project-a") == 1
    assert reconcile_usage_governance(state, project_id="project-b") == 0
    rows = state.list_usage_ledger()
    assert len(rows) == 1
    assert rows[0]["project_id"] == "project-a"


def test_empty_project_identity_is_normalized_and_cannot_complete_attribution():
    state = State(":memory:")
    record = governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured"))

    assert record_usage_attempts(state, record, project_id="  ") == 1
    assert state.list_usage_ledger()[0]["project_id"] is None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute("UPDATE usage_ledger SET project_id = ''")
    state._conn.rollback()


def test_deleted_runbook_resolves_project_from_task_and_retains_ledger():
    state = State(":memory:")
    record = governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured"))
    state.upsert_task(
        Task(
            id="task-1",
            task_ref="ENG-PC-05",
            role="primary-implementation",
            worker="worker-a",
            runbook_id="rb-1",
            project_id="project-a",
        )
    )
    state.upsert_usage_governance(record)

    state.delete_runbook("rb-1")
    assert state.get_runbook("rb-1") is None
    assert state.get_usage_governance("rb-1") is None
    assert record_usage_attempts(state, record) == 1

    rows = state.list_usage_ledger(project_id="project-a")
    assert len(rows) == 1
    state.delete_runbook("rb-1")
    assert state.list_usage_ledger(project_id="project-a") == rows


def test_project_filtered_read_and_project_audit_count_the_same_ledger_rows():
    state = State(":memory:")
    for index in range(2):
        record_usage_attempts(
            state,
            governance(
                attempt(
                    f"run-{index}",
                    "worker-a",
                    tokens=(10, 2),
                    cost_class="metered-configured",
                ),
                runbook_id=f"rb-{index}",
                task_id=f"task-{index}",
            ),
            project_id="project-a",
        )

    filtered = state.list_usage_ledger(project_id="project-a")
    audit = state.count_project_rows("project-a")

    assert audit["usage_ledger"] == len(filtered) == 2


def test_live_new_run_is_not_frozen_from_governance_unknown_placeholders():
    state = State(":memory:")
    live = {
        "run_id": "run-live",
        "worker": "worker-a",
        "status": "RUNNING",
    }
    record = {
        "runbook_id": "rb-live",
        "task_id": "task-live",
        "telemetry_quality": "unknown",
        "input_tokens": None,
        "output_tokens": None,
        "route_history": [live],
    }

    assert record_usage_attempts(state, record, project_id="p") == 0
    assert state.list_usage_ledger(project_id="p") == []


def test_legacy_run_without_token_evidence_is_attributed_with_unknown_evidence():
    state = State(":memory:")
    record = {
        "runbook_id": "rb-legacy-unknown",
        "task_id": "task-legacy-unknown",
        "telemetry_quality": "unknown",
        "input_tokens": None,
        "output_tokens": None,
        "route_history": [
            {
                "worker": "worker-a",
                "provider": "Provider",
                "model": "model",
                "reason": "legacy route did not expose token counts",
            }
        ],
        "updated_at": NOW.isoformat(),
    }

    assert record_usage_attempts(state, record, project_id="p") == 1
    entry = state.list_usage_ledger(project_id="p")[0]
    assert entry["run_id"] == "rb-legacy-unknown:attempt:1"
    assert entry["worker"] == "worker-a"
    assert entry["source_attempt"]["reason"] == "legacy route did not expose token counts"

    row = build_ledger_rows([entry], registry=registry(), pricing_book=FixedBook())[0]
    assert row["metrics"]["input_tokens"]["class"] == CLASS_UNKNOWN
    assert row["metrics"]["input_tokens"]["value"] is None
    assert row["metrics"]["output_tokens"]["class"] == CLASS_UNKNOWN
    assert row["metrics"]["actual_cost_usd"]["class"] == CLASS_UNKNOWN


def test_legacy_multiple_workers_keep_each_run_without_reusing_record_tokens():
    state = State(":memory:")
    record = {
        "runbook_id": "rb-legacy-multiple",
        "task_id": "task-legacy-multiple",
        "telemetry_quality": "exact",
        "input_tokens": 40,
        "output_tokens": 2,
        "route_history": [
            {"worker": "worker-a", "provider": "Provider", "model": "model"},
            {"worker": "worker-b", "provider": "Provider", "model": "model"},
        ],
        "updated_at": NOW.isoformat(),
    }

    assert record_usage_attempts(state, record, project_id="p") == 2
    entries = {entry["worker"]: entry for entry in state.list_usage_ledger(project_id="p")}
    assert set(entries) == {"worker-a", "worker-b"}
    earlier = build_ledger_rows([entries["worker-a"]], registry=registry(), pricing_book=FixedBook())[0]
    newest = build_ledger_rows([entries["worker-b"]], registry=registry(), pricing_book=FixedBook())[0]
    assert earlier["metrics"]["input_tokens"]["class"] == CLASS_UNKNOWN
    assert earlier["metrics"]["input_tokens"]["value"] is None
    assert newest["metrics"]["input_tokens"]["value"] == 40


def test_opening_old_database_collapses_project_keyed_duplicate_runs(tmp_path):
    database = tmp_path / "duplicates.db"
    state = State(database)
    state._conn.execute("DROP TRIGGER usage_ledger_no_update")
    state._conn.execute("DROP TRIGGER usage_ledger_no_delete")
    state._conn.execute("DROP INDEX idx_usage_ledger_run")
    state._conn.execute(
        "CREATE UNIQUE INDEX idx_usage_ledger_run ON usage_ledger (IFNULL(project_id, ''), run_id)"
    )
    payload = (
        "",
        None,
        "task-1",
        "rb-1",
        "run-1",
        None,
        "worker-a",
        "Provider",
        "model",
        NOW.isoformat(),
        "{}",
        "{}",
        NOW.isoformat(),
    )
    state._conn.execute(
        "INSERT INTO usage_ledger "
        "(project_id, program_ref, task_id, runbook_id, run_id, session_id, worker, provider, "
        "effective_model, occurred_at, source_record, source_attempt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    state._conn.execute(
        "INSERT INTO usage_ledger "
        "(project_id, program_ref, task_id, runbook_id, run_id, session_id, worker, provider, "
        "effective_model, occurred_at, source_record, source_attempt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("project-a", "ENG-PC", *payload[2:]),
    )
    state._conn.commit()
    state.close()

    with State(database) as reopened:
        rows = reopened.list_usage_ledger()
        assert len(rows) == 1
        assert rows[0]["project_id"] == "project-a"
        assert rows[0]["program_ref"] == "ENG-PC"
        index_sql = reopened._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_usage_ledger_run'"
        ).fetchone()["sql"]
        assert "project_id" not in index_sql


def test_opening_migrated_database_runs_no_usage_ledger_ddl(tmp_path, monkeypatch):
    database = tmp_path / "migrated.db"
    with State(database):
        pass

    statements = []
    migrate = State._migrate_usage_ledger

    def trace_migration(state):
        state._conn.set_trace_callback(statements.append)
        try:
            return migrate(state)
        finally:
            state._conn.set_trace_callback(None)

    monkeypatch.setattr(State, "_migrate_usage_ledger", trace_migration)
    with State(database):
        pass

    schema_ddl = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("ALTER ", "CREATE ", "DROP "))
    ]
    assert schema_ddl == []


def test_rows_survive_state_store_restart(tmp_path):
    db = tmp_path / "state.db"
    with State(db) as state:
        record_usage_attempts(
            state,
            governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured")),
            project_id="p",
        )

    with State(db) as reopened:
        assert reopened.list_usage_ledger(project_id="p")[0]["run_id"] == "run-1"


def test_event_emit_failure_cannot_undo_committed_usage(monkeypatch):
    state = State(":memory:")

    def fail_emit(*args, **kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(state, "record_run_event", fail_emit)
    record = governance(attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured"))

    assert record_usage_attempts(state, record, project_id="p") == 1
    assert state.list_usage_ledger(project_id="p")[0]["run_id"] == "run-1"


def test_ledger_failure_cannot_escape_route_finalization(monkeypatch):
    state = State(":memory:")
    current = attempt("run-1", "worker-a", tokens=(10, 2), cost_class="metered-configured")
    current["status"] = "RUNNING"
    state.upsert_usage_governance(governance(current))
    task = Task(
        id="task-1",
        task_ref="ENG-PC-05",
        role="primary-implementation",
        worker="worker-a",
        state=TASK_SUCCEEDED,
        result="PASS",
        runbook_id="rb-1",
        project_id="p",
    )

    monkeypatch.setattr(state, "append_usage_ledger", lambda row: (_ for _ in ()).throw(RuntimeError("bad row")))

    assert finalize_route_attempt(state, task) is True
    assert state.get_usage_governance("rb-1")["route_history"][0]["status"] == TASK_SUCCEEDED


def test_unknown_provenance_and_unclassified_cost_remain_unknown():
    state = State(":memory:")
    unknown = attempt("run-u", "mystery", tokens=(None, None), cost_class="")
    unknown["telemetry_quality"] = "unknown"
    record_usage_attempts(state, governance(unknown), project_id="p")

    row = build_ledger_rows(
        state.list_usage_ledger(project_id="p"), registry=registry(), pricing_book=FixedBook()
    )[0]
    assert row["metrics"]["input_tokens"]["class"] == CLASS_UNKNOWN
    assert row["billing"]["class"] == BILLING_UNKNOWN
    assert row["metrics"]["actual_cost_usd"]["class"] == CLASS_UNKNOWN
    assert row["metrics"]["actual_cost_usd"]["value"] is None


def test_registry_model_fallback_never_becomes_measured_provenance():
    state = State(":memory:")
    configured = worker("worker-a", "Provider", "configured-model", "premium-subscription")
    source = attempt("run-a", "worker-a", tokens=(10, 2), cost_class=configured.cost_class)
    source.pop("model")
    record_usage_attempts(state, governance(source), project_id="p", registry=registry(configured))

    row = build_ledger_rows(
        state.list_usage_ledger(project_id="p"), registry=registry(configured), pricing_book=FixedBook()
    )[0]
    assert row["attribution"]["model"] == "configured-model"
    assert row["attribution"]["model_class"] == CLASS_DERIVED


def test_subscription_estimate_is_visible_but_never_spend():
    state = State(":memory:")
    sub = worker("sub", "Provider", "model", "premium-subscription")
    record_usage_attempts(
        state,
        governance(attempt("run-sub", "sub", tokens=(1_000, 500), cost_class=sub.cost_class)),
        project_id="p",
    )
    row = build_ledger_rows(
        state.list_usage_ledger(project_id="p"), registry=registry(sub), pricing_book=FixedBook()
    )[0]
    today = build_aggregates([row], now=NOW)["windows"]["today"]

    assert row["billing"]["class"] == BILLING_SUBSCRIPTION
    assert row["metrics"]["estimated_api_equivalent_usd"]["class"] == CLASS_DERIVED
    assert row["metrics"]["estimated_api_equivalent_usd"]["value"] == pytest.approx(0.02)
    assert row["metrics"]["actual_cost_usd"]["value"] == 0.0
    assert today["subscription_equivalent_usd"]["value"] == pytest.approx(0.02)
    assert today["actual_api_spend_usd"]["value"] == 0.0


def test_filters_select_exact_attribution_and_totals():
    state = State(":memory:")
    a = worker("a", "P1", "m1", "metered-configured")
    b = worker("b", "P2", "m2", "metered-configured")
    record_usage_attempts(
        state,
        governance(attempt("run-a", "a", tokens=(1_000, 0), cost_class=a.cost_class, provider="P1", model="m1")),
        project_id="project-a",
    )
    record_usage_attempts(
        state,
        governance(
            attempt("run-b", "b", tokens=(2_000, 0), cost_class=b.cost_class, provider="P2", model="m2"),
            runbook_id="rb-2",
            task_id="task-2",
        ),
        project_id="project-b",
    )
    rows = build_ledger_rows(state.list_usage_ledger(), registry=registry(a, b), pricing_book=FixedBook())
    filtered = build_aggregates(
        rows,
        now=NOW,
        project_id="project-b",
        task_id="task-2",
        provider="P2",
        model="m2",
    )

    today = filtered["windows"]["today"]
    assert today["counts"]["rows"] == 1
    assert today["actual_api_spend_usd"]["value"] == pytest.approx(0.02)
    assert state.list_usage_ledger(project_id="project-b", task_id="task-2", provider="P2", model="m2")


def test_cache_categories_and_context_limit_remain_not_exposed_with_reasons():
    state = State(":memory:")
    sub = worker("sub", "Provider", "model", "premium-subscription")
    record_usage_attempts(
        state,
        governance(attempt("run-sub", "sub", tokens=(10, 2), cost_class=sub.cost_class)),
        project_id="p",
    )
    metrics = build_ledger_rows(
        state.list_usage_ledger(project_id="p"), registry=registry(sub), pricing_book=FixedBook()
    )[0]["metrics"]

    assert metrics["fresh_input_tokens"]["class"] == CLASS_NOT_EXPOSED
    assert metrics["fresh_input_tokens"]["reason"] == REASON_NO_CACHE_ACCOUNTING
    assert metrics["cache_read_tokens"]["class"] == CLASS_NOT_EXPOSED
    assert metrics["context_limit"]["class"] == CLASS_NOT_EXPOSED
    assert metrics["context_limit"]["reason"] == REASON_NO_CONTEXT_LIMIT


def test_append_only_guard_enumerates_every_ledger_column(tmp_path):
    """The narrow backfill exception must not silently widen when a column is added.

    ``usage_ledger_no_update`` permits exactly one mutation -- ``project_id``
    NULL -> non-NULL -- by asserting ``NEW.<col> IS OLD.<col>`` for every other
    column. That shape is correct but fragile: a column added to the table and
    not to the trigger becomes freely mutable during an attribution backfill,
    and nothing else in the suite would notice. The guarantee this design rests
    on is the guard's *narrowness*, so the guard needs a guard.

    Compares the live schema against the live trigger rather than a hardcoded
    list, so the check cannot drift out of date in the same commit that breaks it.
    """

    state = State(tmp_path / "ledger-schema.db")
    try:
        columns = [row["name"] for row in state._conn.execute("PRAGMA table_info(usage_ledger)")]
        trigger = state._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'usage_ledger_no_update'"
        ).fetchone()
        assert trigger is not None, "usage_ledger must carry its append-only update guard"
        sql = trigger["sql"]
        # project_id is the single deliberate exception; every other column must be pinned.
        unguarded = [
            name for name in columns if name != "project_id" and f"NEW.{name} IS OLD.{name}" not in sql
        ]
        assert unguarded == [], (
            "usage_ledger columns not pinned by usage_ledger_no_update, so they could be "
            f"rewritten during a project_id backfill: {unguarded}"
        )
        # IS, not =, because a NULL column compared with = yields NULL and the
        # WHEN clause would fall through to permitting the update.
        assert "NEW.project_id IS NOT NULL" in sql and "OLD.project_id IS NULL" in sql
    finally:
        state.close()
