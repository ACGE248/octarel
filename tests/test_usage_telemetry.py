"""OCTAREL-UI-06 (issue #25): AO-style usage, context and cost telemetry.

These tests exist mainly to pin the *honesty* rules: a metric Octarel cannot
establish must be reported as unavailable, never estimated into a plausible
number, and a subscription-backed route must never be given a dollar figure.
"""

from __future__ import annotations

import pytest

from scripts.agents.control_plane.usage_telemetry import (
    CLASS_DERIVED,
    CLASS_MEASURED,
    CLASS_NOT_APPLICABLE,
    CLASS_NOT_EXPOSED,
    CLASS_UNKNOWN,
    WorkerFacts,
    build_row,
    unavailable_metrics,
)

SUBSCRIPTION = WorkerFacts("claude-code", "Anthropic", "Claude Code", "claude-sonnet-5", "premium-subscription")
FREE = WorkerFacts("opencode-free-tests", "OpenCode Zen", "OpenCode2", "free-model", "free-dynamic")
METERED = WorkerFacts("grok-build", "xAI", "Grok Build", "grok-4.6", "metered-configured")


def record(**over):
    base = {
        "runbook_id": "rb-1",
        "task_id": "t-1",
        "telemetry_quality": "unknown",
        "input_tokens": None,
        "output_tokens": None,
        "updated_at": "2026-09-26T02:00:00+00:00",
        "route_history": [],
    }
    base.update(over)
    return base


def test_exact_counts_are_measured_and_the_total_is_derived():
    row = build_row(
        record(telemetry_quality="exact", input_tokens=1000, output_tokens=250),
        facts=SUBSCRIPTION,
        project_id="p",
    )
    m = row["metrics"]
    assert m["input_tokens"]["class"] == CLASS_MEASURED
    assert m["output_tokens"]["class"] == CLASS_MEASURED
    assert m["total_tokens"]["value"] == 1250
    assert m["total_tokens"]["class"] == CLASS_DERIVED
    # A derived value must state how it was derived.
    assert m["total_tokens"]["formula"]


def test_an_estimated_count_is_never_presented_as_a_provider_measurement():
    row = build_row(
        record(telemetry_quality="estimated", input_tokens=900, output_tokens=100),
        facts=SUBSCRIPTION,
        project_id="p",
    )
    cell = row["metrics"]["input_tokens"]
    assert cell["class"] == CLASS_DERIVED
    assert "not a provider count" in cell["source"]


def test_missing_counts_report_unknown_rather_than_zero():
    m = build_row(record(), facts=SUBSCRIPTION, project_id="p")["metrics"]
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert m[key]["class"] == CLASS_UNKNOWN
        assert m[key]["value"] is None


@pytest.mark.parametrize("key", unavailable_metrics())
def test_metrics_this_stack_cannot_produce_are_not_exposed_with_a_reason(key):
    """Cache categories and context limits do not exist anywhere in the stack."""

    m = build_row(
        record(telemetry_quality="exact", input_tokens=10, output_tokens=5),
        facts=SUBSCRIPTION,
        project_id="p",
    )["metrics"]
    assert m[key]["class"] == CLASS_NOT_EXPOSED
    assert m[key]["value"] is None
    # Never silently blank: the operator is told why.
    assert m[key]["reason"]


def test_cache_hit_rate_is_never_approximated_even_with_exact_tokens():
    m = build_row(
        record(telemetry_quality="exact", input_tokens=100000, output_tokens=1000),
        facts=SUBSCRIPTION,
        project_id="p",
    )["metrics"]
    assert m["cache_hit_rate"]["value"] is None
    assert "never approximated" in m["cache_hit_rate"]["reason"]


@pytest.mark.parametrize("facts", [SUBSCRIPTION, FREE])
def test_a_subscription_or_free_route_never_gets_a_dollar_figure(facts):
    """Subscription usage is not API billing; $0.00 would imply pricing that does not apply."""

    cost = build_row(
        record(telemetry_quality="exact", input_tokens=500000, output_tokens=20000),
        facts=facts,
        project_id="p",
    )["metrics"]["cost_usd"]
    assert cost["value"] is None
    assert cost["class"] == CLASS_NOT_APPLICABLE
    assert cost["reason"]


def test_a_metered_route_without_pricing_reports_unknown_not_a_guess():
    cost = build_row(
        record(telemetry_quality="exact", input_tokens=1000, output_tokens=100),
        facts=METERED,
        project_id="p",
    )["metrics"]["cost_usd"]
    assert cost["value"] is None
    assert cost["class"] == CLASS_UNKNOWN
    assert "pricing" in cost["reason"]


def test_every_row_is_fully_attributable():
    row = build_row(record(), facts=SUBSCRIPTION, project_id="proj-a")
    a = row["attribution"]
    for field in ("project_id", "runbook_id", "task_id", "worker", "provider", "model", "recorded_at", "source"):
        assert field in a
    assert a["project_id"] == "proj-a"
    assert a["worker"] == "claude-code"


def test_duration_is_unknown_when_not_recorded():
    m = build_row(record(), facts=SUBSCRIPTION, project_id="p")["metrics"]
    assert m["duration_seconds"]["class"] == CLASS_UNKNOWN
    m2 = build_row(record(), facts=SUBSCRIPTION, project_id="p", duration_seconds=12.5)["metrics"]
    assert m2["duration_seconds"]["class"] == CLASS_MEASURED
    assert m2["duration_seconds"]["value"] == 12.5


def test_route_records_billability_from_cost_class():
    assert build_row(record(), facts=METERED, project_id="p")["route"]["billable"] is True
    assert build_row(record(), facts=SUBSCRIPTION, project_id="p")["route"]["billable"] is False
    assert build_row(record(), facts=FREE, project_id="p")["route"]["billable"] is False
