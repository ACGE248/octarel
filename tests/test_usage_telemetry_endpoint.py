"""OCTAREL-UI-07 (issue #42): ``/api/usage-telemetry`` end to end.

The unit tests next door pin the read model's honesty rules. These pin the
join the endpoint performs on top of it -- which worker a run is attributed
to, which model that worker's usage is priced at, and whether a later edit to
``workers.json`` can quietly relabel what a past run cost.

Nothing here contacts a provider. Pricing is injected through ``create_app``'s
``pricing_loader`` seam, so these assertions do not depend on whatever catalog
snapshot happens to be cached on the machine running them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane import pricing as _pricing
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.model_catalog import Catalog, CatalogModel
from scripts.agents.registry import load_registry

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)


def _model(model_id: str, cost: dict) -> CatalogModel:
    provider_id, _, bare = model_id.partition("/")
    return CatalogModel(
        id=model_id,
        provider_id=provider_id,
        model_id=bare,
        display_name=bare,
        family="",
        status="active",
        api_url="",
        context_limit=200000,
        output_limit=8192,
        cost=cost,
        credential="api",
        cost_class="metered",
        cost_reason="",
        capable=True,
        capability_reasons=(),
    )


FIXED_BOOK = _pricing.book_from_catalog(
    Catalog(
        status="ok",
        reason="",
        opencode_version="1.0",
        refreshed_at=NOW.isoformat(),
        credentials_status="ok",
        models=(
            # Rates chosen to be distinct so a row priced at the wrong model is
            # obvious from its value alone.
            _model("xai/grok-4.6", {"input": 1.0, "output": 1.0}),
            _model("openai/gpt-5.6-sol", {"input": 10.0, "output": 10.0}),
        ),
    ),
    now=NOW,
)


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    return CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


@pytest.fixture()
def client(ctx: CommandContext, tmp_path: Path) -> TestClient:
    app = create_app(ctx, roadmap_path=tmp_path / "ROADMAP.md", pricing_loader=lambda: FIXED_BOOK)
    return TestClient(app)


def seed(ctx: CommandContext, runbook_id: str, route_history: list[dict], **over) -> None:
    record = {
        "runbook_id": runbook_id,
        "task_id": f"task-{runbook_id}",
        "classification": "routine",
        "codex_policy": "conserve",
        "telemetry_quality": "exact",
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "route_history": route_history,
    }
    record.update(over)
    ctx.state.upsert_usage_governance(record)


def row_for(client: TestClient, runbook_id: str) -> dict:
    body = client.get("/api/usage-telemetry").json()
    return next(row for row in body["rows"] if row["attribution"]["runbook_id"] == runbook_id)


def test_a_fallback_run_is_priced_at_the_worker_that_actually_executed(ctx, client):
    """The run preferred Grok, fell back to Codex, and must be reported as Codex.

    Pricing follows attribution: 1M input tokens at the Codex model's $10/M
    is $10.00. If the endpoint had kept the originally preferred worker, the
    same tokens would have come out as $1.00 -- so the value proves which
    worker the row was actually attributed to.
    """

    seed(
        ctx,
        "rb-fallback",
        [
            {"worker": "grok-build", "provider": "xAI", "model": "grok-4.6", "status": "FAILED"},
            {
                "worker": "codex-build",
                "provider": "OpenAI",
                "model": "gpt-5.6-sol",
                "cost_class": "premium-subscription",
                "status": "RUNNING",
                "from_worker": "grok-build",
            },
        ],
    )
    row = row_for(client, "rb-fallback")

    assert row["attribution"]["worker"] == "codex-build"
    assert row["attribution"]["provider"] == "OpenAI"
    assert row["pricing"]["catalog_model_id"] == "openai/gpt-5.6-sol"
    assert row["metrics"]["estimated_api_equivalent_usd"]["value"] == pytest.approx(10.0)
    # ...and the billing class of the worker that ran, not the one that failed.
    assert row["billing"]["class"] == "SUBSCRIPTION_INCLUDED"
    assert row["metrics"]["actual_cost_usd"]["value"] == 0.0


def test_a_recorded_billing_class_is_preferred_over_the_current_registry(ctx, client):
    """History must survive a later ``workers.json`` edit.

    ``grok-build`` is configured ``metered-configured`` today. A run that
    recorded itself as subscription-backed keeps that classification, marked
    as evidence the run itself supplied.
    """

    seed(
        ctx,
        "rb-historical",
        [
            {
                "worker": "grok-build",
                "provider": "xAI",
                "model": "grok-4.6",
                "cost_class": "premium-subscription",
            }
        ],
    )
    row = row_for(client, "rb-historical")

    assert row["billing"]["class"] == "SUBSCRIPTION_INCLUDED"
    assert row["billing"]["established"] == "MEASURED"
    assert "recorded by the run" in row["billing"]["source"]
    assert row["route"]["cost_class"] == "premium-subscription"


def test_a_run_that_recorded_no_billing_class_is_marked_as_inferred(ctx, client):
    """Filling the gap from the registry is allowed; passing it off as fact is not."""

    seed(ctx, "rb-inferred", [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6"}])
    row = row_for(client, "rb-inferred")

    # grok-build is metered in the shipped registry, so the row is API-billed...
    assert row["billing"]["class"] == "API_BILLED"
    # ...but the classification is flagged as derived from today's config.
    assert row["billing"]["established"] == "DERIVED"
    assert "current registry" in row["billing"]["source"]


def test_a_model_the_catalog_does_not_carry_reports_an_unavailable_estimate(ctx, client):
    """Anthropic is absent from the OpenCode catalog by construction."""

    seed(
        ctx,
        "rb-claude",
        [{"worker": "claude-code", "provider": "Anthropic", "model": "claude-sonnet-5"}],
    )
    row = row_for(client, "rb-claude")

    assert row["pricing"]["status"] == "unavailable"
    assert "Anthropic" in row["pricing"]["reason"]
    estimate = row["metrics"]["estimated_api_equivalent_usd"]
    assert estimate["value"] is None
    assert estimate["class"] == "UNKNOWN"
    # Billing does not depend on pricing: the subscription fact still holds.
    assert row["billing"]["class"] == "SUBSCRIPTION_INCLUDED"
    assert row["metrics"]["actual_cost_usd"]["value"] == 0.0


def test_the_payload_separates_pricing_provenance_from_billing_evidence(ctx, client):
    seed(ctx, "rb-any", [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6"}])
    body = client.get("/api/usage-telemetry").json()

    source = body["pricing_source"]
    assert source["source"] == _pricing.SOURCE_OPENCODE_CATALOG
    assert source["refreshed_at"] == NOW.isoformat()
    assert source["status"] == "ok"
    # Snapshot provenance is about rates. It makes no claim about who paid.
    assert "billing" not in source


def test_aggregates_ship_with_the_rows_and_state_their_own_invariant(ctx, client):
    seed(
        ctx,
        "rb-agg",
        [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6", "cost_class": "metered-configured"}],
    )
    aggregates = client.get("/api/usage-telemetry").json()["aggregates"]

    assert set(aggregates["windows"]) == {"today", "week", "month"}
    assert "never added" in aggregates["invariant"]
    for window in aggregates["windows"].values():
        assert "actual_api_spend_usd" in window
        assert "subscription_equivalent_usd" in window


def test_the_endpoint_never_reads_the_machines_own_catalog_when_one_is_injected(ctx, tmp_path):
    """The seam exists so money figures in tests are not machine-dependent."""

    calls: list[int] = []

    def loader() -> _pricing.PricingBook:
        calls.append(1)
        return _pricing.unavailable_book("injected: no pricing")

    app = create_app(ctx, roadmap_path=tmp_path / "ROADMAP.md", pricing_loader=loader)
    seed(ctx, "rb-seam", [{"worker": "grok-build", "provider": "xAI", "model": "grok-4.6"}])
    body = TestClient(app).get("/api/usage-telemetry").json()

    assert calls, "the injected loader must be the only pricing source"
    assert body["pricing_source"]["status"] == "unavailable"
    assert body["rows"][0]["pricing"]["reason"] == "injected: no pricing"
