"""OCTAREL-UI-06 (issue #25): AO-style usage, context and cost telemetry.

These tests exist mainly to pin the *honesty* rules: a metric Octarel cannot
establish must be reported as unavailable, never estimated into a plausible
number, and a subscription-backed route must never be given a dollar figure.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.agents.control_plane.pricing import ModelPricing
from scripts.agents.control_plane.usage_telemetry import (
    AGGREGATE_INVARIANT,
    BILLING_API,
    BILLING_FREE,
    BILLING_SUBSCRIPTION,
    BILLING_UNKNOWN,
    CLASS_DERIVED,
    CLASS_MEASURED,
    CLASS_NOT_APPLICABLE,
    CLASS_NOT_EXPOSED,
    CLASS_UNKNOWN,
    WorkerFacts,
    build_aggregates,
    build_row,
    unavailable_metrics,
    window_starts,
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


def test_a_metered_route_without_pricing_reports_unknown_not_a_guess():
    cost = build_row(
        record(telemetry_quality="exact", input_tokens=1000, output_tokens=100),
        facts=METERED,
        project_id="p",
    )["metrics"]["actual_cost_usd"]
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


# --------------------------------------------------------------------------
# OCTAREL-UI-07 (issue #42): approximate API-equivalent value, kept strictly
# apart from what anyone was actually charged.
#
# The rule these tests defend is asymmetric on purpose. Showing a
# subscription-backed run's API-equivalent value is useful; showing it as
# spend would be a lie. So the two figures come from different evidence, live
# in different fields, and are never summed together.


FLASH_RATES = ModelPricing(
    catalog_model_id="google/gemini-2.5-flash",
    input_per_1k_usd=0.0003,
    output_per_1k_usd=0.0025,
    matched_by="exact catalog id",
    refreshed_at="2026-09-26T08:00:00+00:00",
    fingerprint="abc123",
)

UNCLASSIFIED = WorkerFacts("mystery-worker", "Somebody", "Unknown", "some-model", "not-a-known-cost-class")


def money(facts, *, pricing=FLASH_RATES, reason=None, **over):
    base = {"telemetry_quality": "exact", "input_tokens": 100_000, "output_tokens": 10_000}
    base.update(over)
    return build_row(
        record(**base), facts=facts, project_id="p", pricing=pricing, pricing_reason=reason
    )["metrics"]


def test_subscription_usage_gets_an_equivalent_value_and_zero_actual_cost():
    """The headline case from issue #42: valuable to see, never spend."""

    m = money(SUBSCRIPTION)
    equivalent = m["estimated_api_equivalent_usd"]
    # 100K in at $0.0003/1K + 10K out at $0.0025/1K = $0.03 + $0.025
    assert equivalent["value"] == pytest.approx(0.055)
    assert equivalent["class"] == CLASS_DERIVED
    # It must announce itself as an approximation, not a charge.
    assert "not a charge" in equivalent["reason"]

    actual = m["actual_cost_usd"]
    assert actual["value"] == 0.0
    assert actual["class"] == CLASS_DERIVED
    assert "subscription" in actual["formula"]


def test_subscription_billing_is_labelled_as_included_never_as_spend():
    billing = build_row(record(), facts=SUBSCRIPTION, project_id="p")["billing"]
    assert billing["class"] == BILLING_SUBSCRIPTION
    assert billing["label"] == "Included with subscription"


def test_a_free_route_is_called_free_and_never_included_with_a_subscription():
    """Both cost nothing; only one of them is covered by something being paid for."""

    row = build_row(record(), facts=FREE, project_id="p")
    assert row["billing"]["class"] == BILLING_FREE
    assert row["billing"]["label"] == "Free tier"
    assert "subscription" not in row["billing"]["label"].lower()
    assert money(FREE)["actual_cost_usd"]["value"] == 0.0


def test_api_billed_usage_reports_a_real_cost_and_is_distinguishable():
    m = money(METERED)
    row = build_row(
        record(telemetry_quality="exact", input_tokens=100_000, output_tokens=10_000),
        facts=METERED,
        project_id="p",
        pricing=FLASH_RATES,
    )
    assert row["billing"]["class"] == BILLING_API
    assert row["billing"]["label"] == "API-billed"
    assert m["actual_cost_usd"]["value"] == pytest.approx(0.055)
    assert m["actual_cost_usd"]["class"] == CLASS_DERIVED


def test_equivalent_and_actual_stay_separate_fields_even_when_they_match():
    """Issue #42 requires two backend fields, not one value formatted twice."""

    m = money(METERED)
    assert m["estimated_api_equivalent_usd"]["value"] == m["actual_cost_usd"]["value"]
    # Same number, different provenance: one is what it would have cost, the
    # other is what it did cost, and they are established differently.
    assert m["estimated_api_equivalent_usd"]["formula"] != m["actual_cost_usd"]["formula"]
    assert m["estimated_api_equivalent_usd"] is not m["actual_cost_usd"]


def test_unknown_billing_never_reports_zero_cost():
    """"We do not know" is not "free". This is the field's most dangerous default."""

    row = build_row(
        record(telemetry_quality="exact", input_tokens=10_000, output_tokens=1_000),
        facts=UNCLASSIFIED,
        project_id="p",
        pricing=FLASH_RATES,
    )
    assert row["billing"]["class"] == BILLING_UNKNOWN
    actual = row["metrics"]["actual_cost_usd"]
    assert actual["value"] is None
    assert actual["class"] == CLASS_UNKNOWN
    # An equivalent value is still legitimate: it never claimed to be a charge.
    assert row["metrics"]["estimated_api_equivalent_usd"]["value"] is not None


def test_missing_pricing_makes_the_estimate_unavailable_rather_than_guessed():
    m = money(SUBSCRIPTION, pricing=None, reason="no entry in the catalog snapshot")
    estimate = m["estimated_api_equivalent_usd"]
    assert estimate["value"] is None
    assert estimate["class"] == CLASS_UNKNOWN
    assert estimate["reason"] == "no entry in the catalog snapshot"
    # The billing classification does not depend on pricing, so it survives.
    assert m["actual_cost_usd"]["value"] == 0.0


def test_missing_token_data_means_no_estimate_at_all():
    m = money(SUBSCRIPTION, telemetry_quality="unknown", input_tokens=None, output_tokens=None)
    estimate = m["estimated_api_equivalent_usd"]
    assert estimate["value"] is None
    assert estimate["class"] == CLASS_UNKNOWN
    assert "nothing to price" in estimate["reason"]


def test_an_approximate_token_count_values_but_never_bills():
    """A character-length approximation can inform an estimate, never a charge."""

    m = money(METERED, telemetry_quality="estimated")
    assert m["estimated_api_equivalent_usd"]["value"] == pytest.approx(0.055)
    assert "locally approximated" in m["estimated_api_equivalent_usd"]["formula"]

    actual = m["actual_cost_usd"]
    assert actual["value"] is None
    assert actual["class"] == CLASS_UNKNOWN
    assert "approximation" in actual["reason"]


def test_cost_no_longer_uses_not_applicable_for_a_subscription_route():
    """Issue #42 deliberately replaced that answer with a better one.

    The old model said a subscription route had no meaningful dollar figure.
    It now says two more precise things instead: an approximate value, and a
    zero incremental charge with the billing class that establishes it.
    """

    m = money(SUBSCRIPTION)
    assert m["actual_cost_usd"]["class"] != CLASS_NOT_APPLICABLE
    assert m["estimated_api_equivalent_usd"]["class"] != CLASS_NOT_APPLICABLE


def test_billing_class_recorded_by_the_run_outranks_the_current_registry():
    """History must not be relabelled by a later workers.json edit."""

    from_record = WorkerFacts(
        "grok-build", "xAI", "Grok Build", "grok-4.6", "metered-configured", cost_class_from_record=True
    )
    recorded = build_row(record(), facts=from_record, project_id="p")["billing"]
    assert recorded["established"] == CLASS_MEASURED
    assert "recorded by the run" in recorded["source"]

    inferred = build_row(record(), facts=METERED, project_id="p")["billing"]
    assert inferred["established"] == CLASS_DERIVED
    assert "current registry" in inferred["source"]


def test_a_row_with_no_cost_class_at_all_reports_billing_as_unestablished():
    nameless = WorkerFacts("UNKNOWN", None, None, None, None)
    billing = build_row(record(), facts=nameless, project_id="p")["billing"]
    assert billing["class"] == BILLING_UNKNOWN
    assert billing["established"] == CLASS_UNKNOWN
    assert billing["source"] is None


def test_pricing_provenance_travels_with_the_row_and_is_not_billing_evidence():
    row = build_row(record(), facts=SUBSCRIPTION, project_id="p", pricing=FLASH_RATES)
    assert row["pricing"]["catalog_model_id"] == "google/gemini-2.5-flash"
    assert row["pricing"]["refreshed_at"] == "2026-09-26T08:00:00+00:00"
    # Separate blocks: what a model advertises vs. who paid for this run.
    assert row["pricing"]["status"] == "ok"
    assert row["billing"]["class"] == BILLING_SUBSCRIPTION
    assert "cost_class" not in row["pricing"]


def test_an_unpriced_row_still_carries_the_reason_it_could_not_be_priced():
    row = build_row(record(), facts=SUBSCRIPTION, project_id="p", pricing=None, pricing_reason="why not")
    assert row["pricing"]["status"] == "unavailable"
    assert row["pricing"]["reason"] == "why not"


# ------------------------------------------------------------- aggregation

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)  # a Saturday


def dated_row(facts, when, **over):
    """One row whose occurrence time is the finalized attempt's end time."""

    base = {"telemetry_quality": "exact", "input_tokens": 100_000, "output_tokens": 10_000}
    base.update(over)
    return build_row(
        record(route_history=[{"worker": facts.worker, "ended_at": when.isoformat()}], **base),
        facts=facts,
        project_id="p",
        pricing=FLASH_RATES,
    )


def test_windows_are_the_current_utc_day_iso_week_and_calendar_month():
    starts = window_starts(NOW)
    assert starts["today"] == dt.datetime(2026, 9, 26, tzinfo=dt.UTC)
    # 2026-09-26 is a Saturday, so the ISO week began Monday the 21st.
    assert starts["week"] == dt.datetime(2026, 9, 21, tzinfo=dt.UTC)
    assert starts["month"] == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def test_a_row_lands_in_exactly_the_windows_that_contain_it():
    rows = [
        dated_row(SUBSCRIPTION, NOW - dt.timedelta(hours=1)),  # today, week, month
        dated_row(SUBSCRIPTION, NOW - dt.timedelta(days=2)),  # week, month
        dated_row(SUBSCRIPTION, NOW - dt.timedelta(days=10)),  # month only
        dated_row(SUBSCRIPTION, NOW - dt.timedelta(days=60)),  # none
    ]
    windows = build_aggregates(rows, now=NOW)["windows"]
    assert windows["today"]["counts"]["rows"] == 1
    assert windows["week"]["counts"]["rows"] == 2
    assert windows["month"]["counts"]["rows"] == 3


def test_a_row_exactly_on_a_window_boundary_is_inside_it():
    """Boundaries are inclusive at the start; an off-by-one here silently drops usage."""

    starts = window_starts(NOW)
    rows = [dated_row(SUBSCRIPTION, starts["today"])]
    assert build_aggregates(rows, now=NOW)["windows"]["today"]["counts"]["rows"] == 1

    just_before = [dated_row(SUBSCRIPTION, starts["today"] - dt.timedelta(microseconds=1))]
    assert build_aggregates(just_before, now=NOW)["windows"]["today"]["counts"]["rows"] == 0
    assert build_aggregates(just_before, now=NOW)["windows"]["week"]["counts"]["rows"] == 1


def test_subscription_value_is_never_added_to_actual_api_spend():
    """The single most important property in this file."""

    rows = [
        dated_row(SUBSCRIPTION, NOW),  # $0.055 of value, $0.00 of spend
        dated_row(METERED, NOW),  # $0.055 of value, $0.055 of spend
        dated_row(FREE, NOW),  # $0.055 of value, $0.00 of spend
    ]
    today = build_aggregates(rows, now=NOW)["windows"]["today"]

    assert today["subscription_equivalent_usd"]["value"] == pytest.approx(0.055)
    assert today["free_equivalent_usd"]["value"] == pytest.approx(0.055)
    assert today["api_equivalent_usd"]["value"] == pytest.approx(0.055)
    # Spend counts the metered row and nothing else. If subscription or free
    # value ever leaked in, this would be 0.11 or 0.165.
    assert today["actual_api_spend_usd"]["value"] == pytest.approx(0.055)
    assert "API-billed rows only" in today["actual_api_spend_usd"]["formula"]


def test_the_separation_invariant_is_stated_in_the_payload():
    aggregates = build_aggregates([], now=NOW)
    assert aggregates["invariant"] == AGGREGATE_INVARIANT
    assert "never added" in AGGREGATE_INVARIANT


def test_an_undated_row_is_counted_in_no_window_and_reported_as_excluded():
    """Attributing usage of unknown age to today would inflate today's figures."""

    undated = build_row(
        record(telemetry_quality="exact", input_tokens=1, output_tokens=1, updated_at=None),
        facts=SUBSCRIPTION,
        project_id="p",
        pricing=FLASH_RATES,
    )
    aggregates = build_aggregates([undated], now=NOW)
    assert aggregates["excluded_undated_rows"] == 1
    assert aggregates["excluded_reason"]
    for window in aggregates["windows"].values():
        assert window["counts"]["rows"] == 0


def test_an_unpriced_row_is_excluded_from_totals_and_the_shortfall_is_visible():
    """A small total must not be readable as "little was used"."""

    priced = dated_row(SUBSCRIPTION, NOW)
    unpriced = build_row(
        record(
            telemetry_quality="exact",
            input_tokens=999_999,
            output_tokens=999_999,
            route_history=[{"worker": "claude-code", "ended_at": NOW.isoformat()}],
        ),
        facts=SUBSCRIPTION,
        project_id="p",
        pricing=None,
        pricing_reason="Anthropic is not carried by the catalog",
    )
    today = build_aggregates([priced, unpriced], now=NOW)["windows"]["today"]
    cell = today["subscription_equivalent_usd"]
    assert cell["value"] == pytest.approx(0.055)
    assert "1 of 2" in cell["source"]
    assert "1 row(s)" in cell["reason"]


def test_rows_are_bucketed_by_billing_class_not_by_whether_a_number_exists():
    rows = [dated_row(SUBSCRIPTION, NOW), dated_row(METERED, NOW), dated_row(FREE, NOW)]
    counts = build_aggregates(rows, now=NOW)["windows"]["today"]["counts"]
    assert counts["subscription_included"] == 1
    assert counts["api_billed"] == 1
    assert counts["free_tier"] == 1
    assert counts["billing_unknown"] == 0


def test_an_empty_window_totals_a_truthful_zero():
    today = build_aggregates([], now=NOW)["windows"]["today"]
    assert today["actual_api_spend_usd"]["value"] == 0.0
    assert today["actual_api_spend_usd"]["class"] == CLASS_DERIVED
    assert today["counts"]["rows"] == 0


def test_the_run_end_time_outranks_the_record_timestamp():
    """``updated_at`` moves whenever the record is rewritten; the run's end does not."""

    row = build_row(
        record(
            updated_at="2026-09-26T11:00:00+00:00",
            route_history=[{"worker": "claude-code", "ended_at": "2026-09-20T09:00:00+00:00"}],
        ),
        facts=SUBSCRIPTION,
        project_id="p",
    )
    assert row["occurred_at"]["value"] == "2026-09-20T09:00:00+00:00"
    assert row["occurred_at"]["class"] == CLASS_MEASURED

    fallback = build_row(record(updated_at="2026-09-26T11:00:00+00:00"), facts=SUBSCRIPTION, project_id="p")
    assert fallback["occurred_at"]["value"] == "2026-09-26T11:00:00+00:00"
    assert fallback["occurred_at"]["class"] == CLASS_DERIVED


def test_a_naive_timestamp_is_read_as_utc_rather_than_local_time():
    """``utc_now_iso`` output is UTC; comparing it as local time would misbucket it."""

    row = build_row(
        record(route_history=[{"worker": "claude-code", "ended_at": "2026-09-26T11:00:00"}]),
        facts=SUBSCRIPTION,
        project_id="p",
        pricing=FLASH_RATES,
    )
    assert build_aggregates([row], now=NOW)["windows"]["today"]["counts"]["rows"] == 1


def test_an_unparseable_timestamp_is_excluded_rather_than_assumed():
    row = build_row(
        record(route_history=[{"worker": "claude-code", "ended_at": "not a date"}]),
        facts=SUBSCRIPTION,
        project_id="p",
    )
    assert build_aggregates([row], now=NOW)["excluded_undated_rows"] == 1
