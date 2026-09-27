"""OCTAREL-UI-08 (issue #44): capturing a worker CLI's own per-run cost.

Three layers, tested where each one actually lives: reading the figure out of
structured CLI output, persisting it with the run's evidence, and serving it
through the endpoint with the attribution and provenance the UI needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import pricing as _pricing
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.control_plane.telemetry import (
    REPORTED_COST_FIELD,
    SOURCE_WORKER_CLI,
    extract_reported_cost,
    usable_reported_cost,
)
from scripts.agents.registry import load_registry

# ------------------------------------------------------------ extraction


def payload(**fields) -> str:
    return json.dumps({"text": "done", "modelUsage": {"m": {"inputTokens": 5, "outputTokens": 2}}, **fields})


def test_a_structured_cost_field_is_read_with_its_source():
    """The shape both the Grok and Claude CLIs already emit."""

    found = extract_reported_cost(payload(**{REPORTED_COST_FIELD: 0.69791188}))
    assert found.usd == pytest.approx(0.69791188)
    assert found.source == SOURCE_WORKER_CLI


def test_output_with_no_cost_field_reports_nothing():
    """A CLI that does not state a cost must not acquire invented support."""

    assert extract_reported_cost(payload()).usd is None


@pytest.mark.parametrize(
    "raw", ["", "not json", "[]", '"a string"', "null", '{"total_cost_usd": null}']
)
def test_unparseable_or_non_object_output_yields_nothing(raw):
    assert extract_reported_cost(raw).usd is None


@pytest.mark.parametrize(
    "bad", [-1, float("nan"), float("inf"), float("-inf"), True, False, "0.5", [], {}]
)
def test_a_field_that_cannot_stand_as_money_is_refused(bad):
    """Wrong evidence is worse than missing evidence where money is concerned."""

    assert extract_reported_cost(payload(**{REPORTED_COST_FIELD: bad})).usd is None
    assert usable_reported_cost(bad) is None


def test_a_reported_zero_is_a_real_figure():
    found = extract_reported_cost(payload(**{REPORTED_COST_FIELD: 0}))
    assert found.usd == 0.0
    assert found.source == SOURCE_WORKER_CLI


def test_human_readable_prose_is_never_scraped():
    """A number in free text is a guess wearing a number's clothing."""

    assert extract_reported_cost("Total cost: $0.69 for this run").usd is None


# ------------------------------------------------------------ persistence


def test_a_reported_cost_round_trips_through_usage_governance():
    """Persisted with the run so history never depends on today's catalog."""

    state = State(":memory:")
    state.upsert_usage_governance(
        {
            "runbook_id": "rb-1",
            "classification": "routine",
            "codex_policy": "conserve",
            "reported_cost_usd": 0.69791188,
            "reported_cost_source": SOURCE_WORKER_CLI,
        }
    )
    stored = state.get_usage_governance("rb-1")
    assert stored["reported_cost_usd"] == pytest.approx(0.69791188)
    assert stored["reported_cost_source"] == SOURCE_WORKER_CLI


@pytest.mark.parametrize("bad", [-1, float("nan"), "0.5", True])
def test_the_store_refuses_a_value_that_cannot_stand_as_money(bad):
    """Validated at the boundary, so a bad figure never reaches the column."""

    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-bad", "classification": "routine", "codex_policy": "conserve",
         "reported_cost_usd": bad, "reported_cost_source": SOURCE_WORKER_CLI}
    )
    stored = state.get_usage_governance("rb-bad")
    assert stored["reported_cost_usd"] is None
    # The source is dropped with it rather than left describing nothing.
    assert stored["reported_cost_source"] is None


def test_a_record_written_without_a_cost_simply_has_none():
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-none", "classification": "routine", "codex_policy": "conserve"}
    )
    assert state.get_usage_governance("rb-none")["reported_cost_usd"] is None


def test_an_existing_database_gains_the_columns_without_a_manual_step():
    """A pre-#44 on-disk database must keep opening."""

    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.sqlite3"
        # A usage_governance table exactly as it shipped before this change.
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE usage_governance (runbook_id TEXT PRIMARY KEY, task_id TEXT, "
            "classification TEXT NOT NULL, codex_policy TEXT NOT NULL, "
            "codex_auto_eligible INTEGER NOT NULL DEFAULT 0, "
            "max_codex_invocations INTEGER NOT NULL DEFAULT 1, "
            "codex_invocations INTEGER NOT NULL DEFAULT 0, "
            "telemetry_quality TEXT NOT NULL DEFAULT 'unknown', input_tokens INTEGER, "
            "output_tokens INTEGER, escalation_state TEXT NOT NULL DEFAULT 'none', "
            "escalation_reason TEXT, route_history TEXT NOT NULL DEFAULT '[]', "
            "escalation_history TEXT NOT NULL DEFAULT '[]', "
            "context_manifest TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO usage_governance (runbook_id, classification, codex_policy, updated_at) "
            "VALUES ('legacy', 'routine', 'conserve', '2026-09-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        state = State(str(db))
        legacy = state.get_usage_governance("legacy")
        assert legacy is not None
        assert legacy["reported_cost_usd"] is None
        # ...and the migrated database accepts a new one.
        legacy["reported_cost_usd"] = 0.25
        legacy["reported_cost_source"] = SOURCE_WORKER_CLI
        state.upsert_usage_governance(legacy)
        assert state.get_usage_governance("legacy")["reported_cost_usd"] == pytest.approx(0.25)


# ------------------------------------------------------------- capture


def test_the_supervisor_records_the_cost_alongside_the_token_counts(tmp_path):
    """Read from the same structured payload the token counts come from."""

    from scripts.agents.control_plane.models import Task

    registry = load_registry()
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-s", "classification": "routine", "codex_policy": "conserve"}
    )
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(id="t-s", task_ref="X", role="primary-implementation", worker="grok-build",
                runbook_id="rb-s", command=("grok", "--single", "x"))

    supervisor._record_usage(task, payload(**{REPORTED_COST_FIELD: 0.69791188}))

    stored = state.get_usage_governance("rb-s")
    assert stored["reported_cost_usd"] == pytest.approx(0.69791188)
    assert stored["reported_cost_source"] == SOURCE_WORKER_CLI
    # The token counts it has always captured are unaffected.
    assert stored["telemetry_quality"] == "exact"
    assert stored["input_tokens"] == 5


def test_a_later_pass_without_the_field_never_erases_a_captured_cost(tmp_path):
    """Demoting a measured run to the derived path would lose real evidence."""

    from scripts.agents.control_plane.models import Task

    registry = load_registry()
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-k", "classification": "routine", "codex_policy": "conserve"}
    )
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(id="t-k", task_ref="X", role="primary-implementation", worker="grok-build",
                runbook_id="rb-k", command=("grok",))

    supervisor._record_usage(task, payload(**{REPORTED_COST_FIELD: 0.5}))
    supervisor._record_usage(task, payload())  # same run, output without the field

    assert state.get_usage_governance("rb-k")["reported_cost_usd"] == pytest.approx(0.5)


# -------------------------------------------------------------- endpoint


FIXED_BOOK = _pricing.unavailable_book("no pricing in this test")


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    return CommandContext(
        state=state, registry=registry, scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


def test_the_endpoint_attributes_a_reported_cost_to_the_worker_that_ran(ctx, tmp_path):
    """A fallback run's cost belongs to the replacement, not the worker that failed.

    Pricing is deliberately unavailable here, so a MEASURED figure can only
    come from the recorded cost -- there is no derived path to confuse it with.
    """

    ctx.state.upsert_usage_governance(
        {
            "runbook_id": "rb-fb",
            "classification": "routine",
            "codex_policy": "conserve",
            "telemetry_quality": "exact",
            "input_tokens": 1000,
            "output_tokens": 100,
            "reported_cost_usd": 0.42,
            "reported_cost_source": SOURCE_WORKER_CLI,
            "route_history": [
                {"worker": "claude-code", "provider": "Anthropic", "status": "FAILED"},
                {"worker": "grok-build", "provider": "xAI", "model": "grok-4.6",
                 "cost_class": "metered-configured", "status": "SUCCEEDED"},
            ],
        }
    )
    app = create_app(ctx, roadmap_path=tmp_path / "R.md", pricing_loader=lambda: FIXED_BOOK)
    row = TestClient(app).get("/api/usage-telemetry").json()["rows"][0]

    assert row["attribution"]["worker"] == "grok-build"
    assert row["billing"]["class"] == "API_BILLED"
    cost = row["metrics"]["actual_cost_usd"]
    assert cost["value"] == pytest.approx(0.42)
    assert cost["class"] == "MEASURED"
    assert cost["basis"] == "reported"


def test_the_endpoint_keeps_a_subscription_row_at_zero_despite_a_reported_cost(ctx, tmp_path):
    ctx.state.upsert_usage_governance(
        {
            "runbook_id": "rb-sub",
            "classification": "routine",
            "codex_policy": "conserve",
            "telemetry_quality": "exact",
            "input_tokens": 1000,
            "output_tokens": 100,
            "reported_cost_usd": 9.99,
            "reported_cost_source": SOURCE_WORKER_CLI,
            "route_history": [
                {"worker": "claude-code", "provider": "Anthropic",
                 "cost_class": "premium-subscription", "status": "SUCCEEDED"},
            ],
        }
    )
    app = create_app(ctx, roadmap_path=tmp_path / "R.md", pricing_loader=lambda: FIXED_BOOK)
    row = TestClient(app).get("/api/usage-telemetry").json()["rows"][0]

    assert row["billing"]["label"] == "Included with subscription"
    assert row["metrics"]["actual_cost_usd"]["value"] == 0.0
    assert row["metrics"]["actual_cost_usd"]["basis"] == "billing-class"
