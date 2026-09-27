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


# ------------------------------------------------- persistence (attempt-scoped)


def governance(state, runbook_id, history):
    state.upsert_usage_governance(
        {
            "runbook_id": runbook_id,
            "classification": "routine",
            "codex_policy": "conserve",
            "route_history": history,
        }
    )


def test_a_reported_cost_round_trips_on_the_attempt_that_produced_it():
    """Stored with the attempt, so it travels with its own worker.

    Independent review (Grok Build) found a record-level column misattributing
    across a fallback: the figure outlived the attempt that reported it and was
    served as the next worker's spend. route_history is already durable JSON,
    so the attempt carries it with no schema change.
    """

    state = State(":memory:")
    governance(state, "rb-1", [
        {"worker": "grok-build", "reported_cost_usd": 0.69791188,
         "reported_cost_source": SOURCE_WORKER_CLI},
    ])
    attempt = state.get_usage_governance("rb-1")["route_history"][-1]
    assert attempt["reported_cost_usd"] == pytest.approx(0.69791188)
    assert attempt["reported_cost_source"] == SOURCE_WORKER_CLI


def test_no_schema_column_was_added_for_the_reported_cost():
    """Two homes for one fact is the drift risk this deliberately avoids."""

    state = State(":memory:")
    columns = {row[1] for row in state._conn.execute("PRAGMA table_info(usage_governance)")}
    assert "reported_cost_usd" not in columns
    assert "reported_cost_source" not in columns


# ------------------------------------------------------------- capture


def test_the_supervisor_records_the_cost_alongside_the_token_counts(tmp_path):
    """Read from the same structured payload the token counts come from."""

    from scripts.agents.control_plane.models import Task

    registry = load_registry()
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-s", "classification": "routine", "codex_policy": "conserve",
         "route_history": [{"worker": "grok-build", "status": "RUNNING"}]}
    )
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(id="t-s", task_ref="X", role="primary-implementation", worker="grok-build",
                runbook_id="rb-s", command=("grok", "--single", "x"))

    supervisor._record_usage(task, payload(**{REPORTED_COST_FIELD: 0.69791188}))

    stored = state.get_usage_governance("rb-s")
    attempt = stored["route_history"][-1]
    assert attempt["worker"] == "grok-build"
    assert attempt["reported_cost_usd"] == pytest.approx(0.69791188)
    assert attempt["reported_cost_source"] == SOURCE_WORKER_CLI
    # The token counts it has always captured are unaffected.
    assert stored["telemetry_quality"] == "exact"
    assert stored["input_tokens"] == 5


def test_a_later_pass_without_the_field_never_erases_a_captured_cost(tmp_path):
    """Demoting a measured run to the derived path would lose real evidence."""

    from scripts.agents.control_plane.models import Task

    registry = load_registry()
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-k", "classification": "routine", "codex_policy": "conserve",
         "route_history": [{"worker": "grok-build", "status": "RUNNING"}]}
    )
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(id="t-k", task_ref="X", role="primary-implementation", worker="grok-build",
                runbook_id="rb-k", command=("grok",))

    supervisor._record_usage(task, payload(**{REPORTED_COST_FIELD: 0.5}))
    supervisor._record_usage(task, payload())  # same attempt, output without the field

    attempt = state.get_usage_governance("rb-k")["route_history"][-1]
    assert attempt["reported_cost_usd"] == pytest.approx(0.5)


def test_a_cost_is_written_to_the_running_attempt_not_an_older_finished_one(tmp_path):
    """The figure must land on the worker that produced it, mid-fallback."""

    from scripts.agents.control_plane.models import Task

    registry = load_registry()
    state = State(":memory:")
    state.upsert_usage_governance(
        {"runbook_id": "rb-fb2", "classification": "routine", "codex_policy": "conserve",
         "route_history": [
             {"worker": "claude-code", "status": "FAILED"},
             {"worker": "grok-build", "status": "RUNNING"},
         ]}
    )
    supervisor = Supervisor(registry=registry, repo_root=tmp_path, state=state)
    task = Task(id="t-fb2", task_ref="X", role="primary-implementation", worker="grok-build",
                runbook_id="rb-fb2", command=("grok",))

    supervisor._record_usage(task, payload(**{REPORTED_COST_FIELD: 0.33}))

    history = state.get_usage_governance("rb-fb2")["route_history"]
    assert "reported_cost_usd" not in history[0], "the failed attempt must not acquire a cost"
    assert history[1]["reported_cost_usd"] == pytest.approx(0.33)


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
    """A fallback run's cost belongs to the replacement that reported it.

    Pricing is deliberately unavailable, so a MEASURED figure can only come
    from the attempt's recorded cost -- there is no derived path to confuse it
    with. The failed attempt carries a *different* figure, so a row serving the
    wrong one would be visible in the value rather than merely plausible.
    """

    ctx.state.upsert_usage_governance(
        {
            "runbook_id": "rb-fb",
            "classification": "routine",
            "codex_policy": "conserve",
            "telemetry_quality": "exact",
            "input_tokens": 1000,
            "output_tokens": 100,
            "route_history": [
                {"worker": "claude-code", "provider": "Anthropic", "status": "FAILED",
                 "cost_class": "premium-subscription",
                 "reported_cost_usd": 0.99, "reported_cost_source": SOURCE_WORKER_CLI},
                {"worker": "grok-build", "provider": "xAI", "model": "grok-4.6",
                 "cost_class": "metered-configured", "status": "SUCCEEDED",
                 "reported_cost_usd": 0.42, "reported_cost_source": SOURCE_WORKER_CLI},
            ],
        }
    )
    app = create_app(ctx, roadmap_path=tmp_path / "R.md", pricing_loader=lambda: FIXED_BOOK)
    row = TestClient(app).get("/api/usage-telemetry").json()["rows"][0]

    assert row["attribution"]["worker"] == "grok-build"
    assert row["billing"]["class"] == "API_BILLED"
    cost = row["metrics"]["actual_cost_usd"]
    assert cost["value"] == pytest.approx(0.42), "must be the replacement's own figure"
    assert cost["value"] != pytest.approx(0.99), "never the failed attempt's figure"
    assert cost["class"] == "MEASURED"
    assert cost["basis"] == "reported"


def test_a_subscription_attempts_figure_never_becomes_a_later_workers_api_spend(ctx, tmp_path):
    """The blocker this design exists to make impossible.

    A subscription CLI's total_cost_usd means "what this would have cost on
    the API", not a charge. With the figure stored per-runbook it outlived the
    attempt that reported it: once a fallback to an API-billed worker became
    the attributed attempt, that value was served as the replacement's
    MEASURED spend -- someone else's number, on the wrong worker, summed into
    actual API spend. Binding it to the attempt makes that unrepresentable.
    """

    ctx.state.upsert_usage_governance(
        {
            "runbook_id": "rb-cross",
            "classification": "routine",
            "codex_policy": "conserve",
            "telemetry_quality": "exact",
            "input_tokens": 1000,
            "output_tokens": 100,
            "route_history": [
                {"worker": "claude-code", "provider": "Anthropic",
                 "cost_class": "premium-subscription", "status": "FAILED",
                 "reported_cost_usd": 0.51, "reported_cost_source": SOURCE_WORKER_CLI},
                # The replacement reported nothing of its own.
                {"worker": "grok-build", "provider": "xAI", "model": "grok-4.6",
                 "cost_class": "metered-configured", "status": "SUCCEEDED"},
            ],
        }
    )
    app = create_app(ctx, roadmap_path=tmp_path / "R.md", pricing_loader=lambda: FIXED_BOOK)
    body = TestClient(app).get("/api/usage-telemetry").json()
    row = body["rows"][0]

    assert row["attribution"]["worker"] == "grok-build"
    cost = row["metrics"]["actual_cost_usd"]
    assert cost["value"] is None, "the subscription attempt's figure must not appear here"
    assert cost["class"] == "UNKNOWN"
    # ...and it must not reach the spend aggregate either.
    spend = body["aggregates"]["windows"]["month"]["actual_api_spend_usd"]
    assert spend["value"] != pytest.approx(0.51)


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
