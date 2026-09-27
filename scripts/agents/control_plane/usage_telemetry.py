"""OCTAREL-UI-06 (issue #25): AO-style model/session usage, context and cost.

Presents what a run actually consumed. The hard rule this module exists to
enforce is that a metric Octarel cannot establish is *said* to be unavailable
rather than estimated into something plausible.

Every metric is returned with a class:

``MEASURED``
    A real recorded value, with the source that produced it.
``DERIVED``
    Computed from measured values only, with its formula stated.
``UNKNOWN``
    Octarel should have this value but does not for this run.
``NOT_EXPOSED``
    Nothing in the current stack can produce it, with the reason.
``NOT_APPLICABLE``
    Meaningless for this route. Cost no longer uses this class: issue #42
    replaced "a subscription route has no dollar figure" with an explicit
    billing class plus two separate money fields, which says more and says it
    more precisely. The class stays in the vocabulary because it is part of
    the published contract and consumers already render it.

Inventory behind those classes, as of this change:

* Token counts come from the worker CLI's own ``modelUsage`` output, recorded
  into durable usage governance by the supervisor. Quality is recorded as
  exact, estimated or unknown and is never upgraded here.
* **No cache accounting exists anywhere in the stack.** Nothing records fresh
  input, cache reads, or cache writes, so the AO-style cache figures -- and the
  cache hit rate derived from them -- are ``NOT_EXPOSED``. A hit rate must come
  from authoritative token categories; it is never approximated.
* **No runtime reports an effective context limit.** The OpenCode catalog
  carries a per-model ``context_limit``, but that is a catalog fact about a
  model rather than the limit the active worker ran under, and the design
  specification forbids guessing a context window from a model name. Context
  utilization is therefore ``NOT_EXPOSED`` until a runtime reports it.
* Money is reported as **two separate fields** -- see below. Neither is ever
  folded into the other.

Estimated value versus actual cost (OCTAREL-UI-07, issue #42)
-------------------------------------------------------------

Subscription-backed work has a real API-equivalent value: the same tokens
bought on the public API would have cost something, and knowing roughly what
is useful. Presenting that number as *spend* would be a lie. So every row
carries two independent money cells, computed from different inputs, that are
never added together:

``estimated_api_equivalent_usd``
    What these tokens would have cost at public API rates, on any route.
    Always ``DERIVED`` -- an informational approximation, never an invoice,
    never a charge, and never accumulated into actual spend.
``actual_cost_usd``
    What this run actually added to a bill. ``$0.00`` on a subscription or
    free route *once that billing classification is established*, a computed
    figure on an API route with trustworthy evidence, and ``UNKNOWN`` when the
    billing class itself is unknown -- an unclassified route never reports
    ``$0.00``, because "we do not know" is not "free".

"Trustworthy evidence" for an actual charge means **exact** provider-reported
token counts plus a pricing snapshot. An ``ESTIMATED`` count is a local
character-length approximation: good enough to inform an explicitly
approximate equivalent value, not good enough to assert what someone was
charged. That asymmetry is why the two fields exist separately.

Billing class comes from the worker's ``cost_class``; rates come from a
pricing snapshot (``pricing.py``). They are tracked apart all the way to the
UI. A price says what a model advertises, a billing class says who paid, and
conflating the two is exactly how a subscription session gets mislabelled as
API spend.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from .pricing import ModelPricing
from .telemetry import (
    ROUTE_API,
    ROUTE_FREE,
    ROUTE_SUBSCRIPTION,
    ROUTE_UNKNOWN,
    SOURCE_WORKER_CLI,
    TOKEN_ESTIMATED,
    TOKEN_EXACT,
    PricingSnapshot,
    TokenUsage,
    compute_cost,
    execution_route_for_cost_class,
    usable_reported_cost,
)

CLASS_MEASURED = "MEASURED"
CLASS_DERIVED = "DERIVED"
CLASS_UNKNOWN = "UNKNOWN"
CLASS_NOT_EXPOSED = "NOT_EXPOSED"
CLASS_NOT_APPLICABLE = "NOT_APPLICABLE"

# Why the AO-style cache and context figures cannot be produced here. Stated
# once so every row gives the operator the same honest explanation.
REASON_NO_CACHE_ACCOUNTING = (
    "no worker CLI in this stack reports cache-category tokens, so fresh input "
    "and cache reads are not recorded"
)
REASON_NO_CONTEXT_LIMIT = (
    "the active worker runtime does not report an effective context limit; a "
    "limit is never inferred from a model name"
)

# ------------------------------------------------------------ billing class
#
# Who paid, as distinct from what the model costs. Derived from the worker's
# ``cost_class`` through the execution route that routing already uses, so
# there is no second classifier to drift out of step with it.

BILLING_SUBSCRIPTION = "SUBSCRIPTION_INCLUDED"
BILLING_API = "API_BILLED"
BILLING_FREE = "FREE_TIER"
BILLING_UNKNOWN = "UNKNOWN"

_ROUTE_BILLING: dict[str, str] = {
    ROUTE_SUBSCRIPTION: BILLING_SUBSCRIPTION,
    ROUTE_API: BILLING_API,
    ROUTE_FREE: BILLING_FREE,
    ROUTE_UNKNOWN: BILLING_UNKNOWN,
}

# The operator-facing wording. A free route says "free", never "included with
# a subscription": both cost nothing, but only one of them is covered by
# something the operator is paying for, and claiming a subscription covers a
# free-tier route would misdescribe what the account is buying.
BILLING_LABELS: dict[str, str] = {
    BILLING_SUBSCRIPTION: "Included with subscription",
    BILLING_API: "API-billed",
    BILLING_FREE: "Free tier",
    BILLING_UNKNOWN: "Billing not established",
}

BILLING_REASONS: dict[str, str] = {
    BILLING_SUBSCRIPTION: "subscription-backed CLI session: tokens are covered by the subscription",
    BILLING_API: "metered API route: tokens add to an API bill",
    BILLING_FREE: "free route: the provider charges nothing for these tokens",
    BILLING_UNKNOWN: (
        "no cost_class maps this route to a billing class, so whether these tokens "
        "were charged is unknown"
    ),
}


def billing_for_route(execution_route: str) -> str:
    return _ROUTE_BILLING.get(execution_route, BILLING_UNKNOWN)


# How an actual-cost figure was arrived at (OCTAREL-UI-08, issue #44). The
# metric class alone cannot carry this: a subscription route's $0.00 and an
# API route's tokens-times-pricing figure are both DERIVED, but one is an
# assertion the billing class licenses and the other is a reconstruction. The
# UI needs to say which without pattern-matching prose.
BASIS_REPORTED = "reported"          # the worker CLI stated this run's cost
BASIS_BILLING_CLASS = "billing-class"  # $0.00 established by the route itself
BASIS_TOKENS_AND_PRICING = "tokens-and-pricing"  # reconstructed from a snapshot


# What the estimated API-equivalent value may truthfully say about itself, per
# billing class. The subscription/free wording asserts the run was not billed;
# that is only sayable where the classification establishes it.
_EQUIVALENT_REASONS: dict[str, str] = {
    BILLING_SUBSCRIPTION: (
        "approximate API-equivalent value, not a charge: this run was not billed at these rates"
    ),
    BILLING_FREE: (
        "approximate API-equivalent value, not a charge: this run was not billed at these rates"
    ),
    BILLING_API: (
        "approximate API-equivalent value; the actual charge for this run is reported separately"
    ),
    BILLING_UNKNOWN: (
        "approximate API-equivalent value only; how this run was billed is not established, "
        "so whether it was charged at these rates is unknown"
    ),
}


def metric(
    value: Any = None,
    *,
    klass: str = CLASS_UNKNOWN,
    source: str | None = None,
    formula: str | None = None,
    reason: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    """One metric cell: its value, how it was established, and why if it wasn't."""

    return {
        "value": value,
        "class": klass,
        "source": source,
        "formula": formula,
        "reason": reason,
        "unit": unit,
    }


@dataclass(frozen=True)
class WorkerFacts:
    """The facts a usage row needs about the worker that ran.

    ``from_record`` marks whether ``model``/``cost_class`` came from what the
    run itself recorded, or were filled in from the live registry. The registry
    describes the worker *now*, which is not necessarily how it was configured
    when the run happened -- a pool worker resolves a different model per run,
    and any later edit to workers.json would silently relabel history. A
    registry-sourced value is therefore reported as DERIVED with that caveat
    rather than as a measurement of this run.
    """

    worker: str
    provider: str | None = None
    execution_system: str | None = None
    model: str | None = None
    cost_class: str | None = None
    model_from_record: bool = False
    cost_class_from_record: bool = False


def _token_metrics(record: dict[str, Any]) -> dict[str, Any]:
    """Token cells from a durable usage-governance record."""

    quality = str(record.get("telemetry_quality") or "unknown").lower()
    raw_in = record.get("input_tokens")
    raw_out = record.get("output_tokens")

    if quality == "exact":
        klass, source = CLASS_MEASURED, "worker CLI modelUsage"
    elif quality == "estimated":
        # Recorded as an explicit local approximation by the supervisor; it is
        # presented as DERIVED with that origin, never as a provider count.
        klass, source = CLASS_DERIVED, "local character-length approximation (not a provider count)"
    else:
        klass, source = CLASS_UNKNOWN, None

    known = isinstance(raw_in, int) and isinstance(raw_out, int) and klass != CLASS_UNKNOWN
    cells: dict[str, Any] = {
        "input_tokens": metric(
            raw_in if klass != CLASS_UNKNOWN else None,
            klass=klass if isinstance(raw_in, int) else CLASS_UNKNOWN,
            source=source,
            unit="tokens",
        ),
        "output_tokens": metric(
            raw_out if klass != CLASS_UNKNOWN else None,
            klass=klass if isinstance(raw_out, int) else CLASS_UNKNOWN,
            source=source,
            unit="tokens",
        ),
        "total_tokens": metric(
            (raw_in + raw_out) if known else None,
            klass=CLASS_DERIVED if known else CLASS_UNKNOWN,
            formula="input_tokens + output_tokens" if known else None,
            source=source if known else None,
            unit="tokens",
        ),
        # The AO cache panel has no counterpart here. Saying so is the point.
        "fresh_input_tokens": metric(klass=CLASS_NOT_EXPOSED, reason=REASON_NO_CACHE_ACCOUNTING, unit="tokens"),
        "cache_read_tokens": metric(klass=CLASS_NOT_EXPOSED, reason=REASON_NO_CACHE_ACCOUNTING, unit="tokens"),
        "cache_hit_rate": metric(
            klass=CLASS_NOT_EXPOSED,
            reason=(
                "a hit rate must be computed from authoritative cache token "
                "categories, which are not recorded; it is never approximated"
            ),
            unit="percent",
        ),
        "context_limit": metric(klass=CLASS_NOT_EXPOSED, reason=REASON_NO_CONTEXT_LIMIT, unit="tokens"),
        "context_used_percent": metric(
            klass=CLASS_NOT_EXPOSED,
            reason="requires an authoritative context limit, which is not reported",
            unit="percent",
        ),
    }
    return cells


def _countable(value: Any) -> int | None:
    """A token count fit to multiply by a price, or nothing.

    ``_token_metrics`` already refuses to display a non-integer count; the
    money path must be at least as strict, because it divides and multiplies
    rather than merely rendering. Independent review (Grok Build, issue #42)
    found that a value SQLite happily stores in an INTEGER column but which is
    not an ``int`` -- the column has affinity, not a constraint -- raised a
    ``TypeError`` out of the arithmetic and failed the whole endpoint, hiding
    every other run's usage because of one malformed record.

    A negative count is refused for a different reason: it is not a fabricated
    figure, it is a *wrong* one, and it would understate spend. Missing
    evidence is always the safer answer than a number that reads as money and
    is not.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _token_usage(record: dict[str, Any]) -> TokenUsage:
    quality = str(record.get("telemetry_quality") or "unknown").lower()
    mode = {"exact": TOKEN_EXACT, "estimated": TOKEN_ESTIMATED}.get(quality, "UNKNOWN")
    return TokenUsage(
        mode=mode,
        input_tokens=_countable(record.get("input_tokens")),
        output_tokens=_countable(record.get("output_tokens")),
    )


def _as_snapshot(pricing: ModelPricing, worker: str) -> PricingSnapshot:
    """Adapt a catalog-derived rate to the existing cost-accounting contract."""

    return PricingSnapshot(
        version=f"{pricing.source} @ {pricing.refreshed_at or 'unknown'}",
        worker=worker,
        model=pricing.catalog_model_id,
        input_per_1k_usd=pricing.input_per_1k_usd,
        output_per_1k_usd=pricing.output_per_1k_usd,
    )


def _equivalent_metric(
    record: dict[str, Any],
    billing_class: str,
    pricing: ModelPricing | None,
    pricing_reason: str | None,
) -> dict[str, Any]:
    """What these tokens would have cost at public API rates, on any route.

    Deliberately route-blind. The entire point of this cell is that a
    subscription-backed run *does* get a number here, so an operator can see
    the value of what the subscription covered. It is always ``DERIVED``: it
    is arithmetic over an advertised rate, never a record of a charge.
    """

    usage = _token_usage(record)
    if pricing is None:
        return metric(
            klass=CLASS_UNKNOWN,
            reason=pricing_reason or "no pricing is available for this model, so no equivalent value is estimated",
            unit="usd",
        )
    if usage.mode == "UNKNOWN" or usage.input_tokens is None or usage.output_tokens is None:
        return metric(
            klass=CLASS_UNKNOWN,
            reason="token counts are UNKNOWN for this run, so there is nothing to price",
            unit="usd",
        )

    usd = (usage.input_tokens / 1000.0) * pricing.input_per_1k_usd
    usd += (usage.output_tokens / 1000.0) * pricing.output_per_1k_usd
    basis = "provider-reported" if usage.mode == TOKEN_EXACT else "locally approximated"
    return metric(
        round(usd, 6),
        klass=CLASS_DERIVED,
        # Worded so the two money cells never read identically, even on an
        # API-billed row where the arithmetic happens to be the same.
        formula=(
            f"API-equivalent value: {basis} tokens x {pricing.source} rates "
            f"for {pricing.catalog_model_id}"
        ),
        source=(
            f"{pricing.source} snapshot {pricing.fingerprint or 'unknown'}"
            f" refreshed {pricing.refreshed_at or 'unknown'}"
        ),
        # Worded for the route that actually ran, in three cases rather than
        # two. On an API-billed row the run *was* billed at these rates, so a
        # blanket "not billed at these rates" is false. And where the billing
        # class was never established, claiming the run was not billed asserts
        # something unknown -- the same unsupported claim in the opposite
        # direction. Independent review (Grok Build, issue #42).
        reason=_EQUIVALENT_REASONS.get(billing_class, _EQUIVALENT_REASONS[BILLING_UNKNOWN]),
        unit="usd",
    )


def _with_basis(cell: dict[str, Any], basis: str) -> dict[str, Any]:
    """Tag an actual-cost cell with how its figure was arrived at."""

    cell["basis"] = basis
    return cell


def _actual_cost_metric(
    facts: WorkerFacts,
    record: dict[str, Any],
    billing_class: str,
    pricing: ModelPricing | None,
    pricing_reason: str | None,
) -> dict[str, Any]:
    """What this run actually added to a bill, or why that is not known.

    ``$0.00`` is an assertion, not a default. It is made only where the
    billing classification actually establishes that nothing was charged --
    a subscription-included or free route. An unclassified route reports
    ``UNKNOWN``; reporting zero there would turn missing evidence into a
    claim that the run was free.

    **Billing class is decided before any monetary evidence is consulted, and
    that ordering is load-bearing** (OCTAREL-UI-08, issue #44). A
    subscription-backed CLI may itself print a ``total_cost_usd`` -- the
    Claude CLI does, and it means "what this would have cost on the API", not
    "what you were charged". Reading that field before classifying the route
    would turn a subscription session into reported API spend, which is the
    exact misrepresentation this whole feature exists to prevent. Only an
    API-billed route ever reaches the evidence below.

    Within an API-billed route the precedence is strongest-evidence-first:

    1. a cost the worker CLI reported for this run -- ``MEASURED``;
    2. exact provider-reported tokens times a trusted pricing snapshot --
       ``DERIVED``;
    3. ``UNKNOWN``.

    A derived figure never overwrites a measured one.
    """

    provenance = (
        "billing class recorded by the run"
        if facts.cost_class_from_record
        else "billing class from the current registry configuration for this worker"
    )

    if billing_class == BILLING_SUBSCRIPTION:
        return _with_basis(
            metric(
                0.0,
                klass=CLASS_DERIVED,
                formula="subscription-included route: no per-token API charge",
                source=provenance,
                unit="usd",
            ),
            BASIS_BILLING_CLASS,
        )
    if billing_class == BILLING_FREE:
        return _with_basis(
            metric(
                0.0,
                klass=CLASS_DERIVED,
                formula="free route: the provider charges nothing for these tokens",
                source=provenance,
                unit="usd",
            ),
            BASIS_BILLING_CLASS,
        )
    if billing_class != BILLING_API:
        return metric(klass=CLASS_UNKNOWN, reason=BILLING_REASONS[BILLING_UNKNOWN], unit="usd")

    # API-billed. Strongest evidence first: what the worker itself reported
    # this run cost beats what a price list implies it should have cost.
    reported = usable_reported_cost(record.get("reported_cost_usd"))
    if reported is not None:
        return _with_basis(
            metric(
                reported,
                klass=CLASS_MEASURED,
                source=str(record.get("reported_cost_source") or SOURCE_WORKER_CLI),
                unit="usd",
            ),
            BASIS_REPORTED,
        )

    # No reported figure: fall back to reconstructing one, exactly as before.
    # A charge is only asserted from evidence strong enough to support it:
    # exact provider-reported counts plus a rate.
    usage = _token_usage(record)
    if usage.mode == TOKEN_ESTIMATED:
        return metric(
            klass=CLASS_UNKNOWN,
            reason=(
                "token counts are a local approximation rather than a provider count; "
                "an actual charge is never derived from an approximation (the estimated "
                "API-equivalent value is reported separately)"
            ),
            unit="usd",
        )
    if pricing is None:
        return metric(
            klass=CLASS_UNKNOWN,
            reason=pricing_reason or "API route but no pricing snapshot covers this model",
            unit="usd",
        )
    result = compute_cost(
        worker_cost_class=facts.cost_class or "",
        token_usage=usage,
        pricing=_as_snapshot(pricing, facts.worker),
    )
    if result.usd is None:
        return metric(klass=CLASS_UNKNOWN, reason=result.reason, unit="usd")
    return _with_basis(
        metric(
            result.usd,
            klass=CLASS_DERIVED,
            formula=(
                f"actual API charge: provider-reported tokens x {pricing.source} rates "
                f"for {pricing.catalog_model_id}"
            ),
            source=f"{provenance}; {result.reason}",
            unit="usd",
        ),
        BASIS_TOKENS_AND_PRICING,
    )


def _occurrence(record: dict[str, Any]) -> dict[str, Any]:
    """When this usage happened, for time-bounded aggregation.

    The strongest available evidence is the finalized route attempt's
    ``ended_at`` -- the moment the worker that produced these tokens stopped.
    Failing that, the governance record's ``updated_at`` is used, which is
    when the record was last written for any reason and so only approximates
    the run. A row with neither is excluded from every window rather than
    silently counted into the current one.

    Only the **latest** attempt may supply that timestamp, and only if it has
    one. Independent review (Grok Build, issue #42) found that scanning back
    through history for any ``ended_at`` let a still-running retry inherit the
    end time of the previous, failed attempt -- dating the current run's tokens
    to whenever the earlier one stopped, potentially in a different window.
    The tokens on this record belong to the attempt the row is attributed to,
    so the timestamp must come from that same attempt or not at all.
    """

    for attempt in reversed(record.get("route_history") or []):
        if not isinstance(attempt, dict) or not attempt.get("worker"):
            continue
        # The newest attempt with a worker is the one this row is attributed
        # to. If it has not ended, fall through -- never borrow an older one.
        if attempt.get("ended_at"):
            return {
                "value": str(attempt["ended_at"]),
                "class": CLASS_MEASURED,
                "source": "route attempt ended_at",
            }
        break
    updated = record.get("updated_at")
    if updated:
        return {
            "value": str(updated),
            "class": CLASS_DERIVED,
            "source": "usage record updated_at; the run's own end time was not recorded",
        }
    return {"value": None, "class": CLASS_UNKNOWN, "source": None}


def build_row(
    record: dict[str, Any],
    *,
    facts: WorkerFacts,
    project_id: str | None,
    duration_seconds: float | None = None,
    pricing: ModelPricing | None = None,
    pricing_reason: str | None = None,
) -> dict[str, Any]:
    """One attributable usage row for a runbook's durable usage record."""

    metrics = _token_metrics(record)
    metrics["duration_seconds"] = (
        metric(duration_seconds, klass=CLASS_MEASURED, source="orchestrator run timing", unit="seconds")
        if duration_seconds is not None
        else metric(klass=CLASS_UNKNOWN, reason="no recorded run duration", unit="seconds")
    )

    route = execution_route_for_cost_class(facts.cost_class or "")
    billing_class = billing_for_route(route)
    # Two cells, computed from different evidence, that are never summed
    # together. See the module docstring.
    metrics["estimated_api_equivalent_usd"] = _equivalent_metric(
        record, billing_class, pricing, pricing_reason
    )
    metrics["actual_cost_usd"] = _actual_cost_metric(
        facts, record, billing_class, pricing, pricing_reason
    )

    return {
        # Every row is attributable; figures from different workers are never
        # merged into one number without saying so.
        "attribution": {
            "project_id": project_id,
            "runbook_id": record.get("runbook_id"),
            "task_id": record.get("task_id"),
            "worker": facts.worker,
            "provider": facts.provider,
            "execution_system": facts.execution_system,
            "model": facts.model,
            # How the model attribution was established, so a registry-derived
            # label is never mistaken for what this run actually used.
            "model_class": (
                CLASS_MEASURED
                if facts.model_from_record
                else CLASS_DERIVED
                if facts.model
                else CLASS_UNKNOWN
            ),
            "model_source": (
                "recorded by the run"
                if facts.model_from_record
                else "current registry configuration for this worker, not recorded by the run"
                if facts.model
                else None
            ),
            "recorded_at": record.get("updated_at"),
            "source": "durable usage governance record",
        },
        "route": {
            "execution_route": route,
            "cost_class": facts.cost_class,
            "cost_class_class": (
                CLASS_MEASURED
                if facts.cost_class_from_record
                else CLASS_DERIVED
                if facts.cost_class
                else CLASS_UNKNOWN
            ),
            "billable": route == ROUTE_API,
            "history": record.get("route_history") or [],
            "escalation_state": record.get("escalation_state"),
            "escalation_reason": record.get("escalation_reason"),
        },
        # Who paid. Kept as its own block rather than folded into ``route`` so
        # that the UI, the aggregates and any later consumer all read the same
        # classification, and so it can never be confused with the pricing
        # provenance below.
        "billing": {
            "class": billing_class,
            "label": BILLING_LABELS[billing_class],
            "reason": BILLING_REASONS[billing_class],
            # How well established the classification itself is. A billing
            # class inferred from today's registry is weaker evidence than one
            # the run recorded, and an operator can see which they are reading.
            "established": (
                CLASS_MEASURED
                if facts.cost_class_from_record
                else CLASS_DERIVED
                if facts.cost_class
                else CLASS_UNKNOWN
            ),
            "source": (
                "cost_class recorded by the run"
                if facts.cost_class_from_record
                else "cost_class from the current registry configuration for this worker"
                if facts.cost_class
                else None
            ),
        },
        # Where the rates came from. A pricing snapshot is not billing
        # evidence: it says what a model advertises, never what was charged.
        "pricing": (
            pricing.as_dict()
            if pricing is not None
            else {
                "status": "unavailable",
                "catalog_model_id": None,
                "input_per_1k_usd": None,
                "output_per_1k_usd": None,
                "matched_by": None,
                "source": None,
                "refreshed_at": None,
                "fingerprint": None,
                "age_seconds": None,
                "stale": False,
                "reason": pricing_reason,
            }
        ),
        "occurred_at": _occurrence(record),
        "telemetry_quality": str(record.get("telemetry_quality") or "unknown"),
        "metrics": metrics,
    }


# ------------------------------------------------------------- aggregation

# The one rule the aggregates exist to enforce. Stated here, asserted in
# tests, and shown to the operator in the panel legend.
AGGREGATE_INVARIANT = (
    "Estimated API-equivalent value is never added to actual API spend. They are "
    "separate totals over separate evidence and answer different questions."
)

WINDOW_BASIS = (
    "UTC: today is the current calendar day, this week starts Monday, this month "
    "starts on the 1st"
)


def _parse_iso(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive timestamp from this stack is always UTC (``utc_now_iso``); saying
    # so explicitly beats letting it compare as local time.
    return parsed.replace(tzinfo=_dt.UTC) if parsed.tzinfo is None else parsed


def window_starts(now: _dt.datetime) -> dict[str, _dt.datetime]:
    """Start of the current UTC day, ISO week and calendar month."""

    moment = now.astimezone(_dt.UTC)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "today": day,
        "week": day - _dt.timedelta(days=day.weekday()),
        "month": day.replace(day=1),
    }


def _sum_cell(values: list[float], *, formula: str, rows: int) -> dict[str, Any]:
    """A window total, with how much of the window it actually covers.

    Three genuinely different situations, which earlier collapsed into one
    number (independent review, Grok Build, issue #42):

    * **No rows of this kind at all.** A total over zero rows is a truthful
      ``$0.00`` -- nothing of this kind happened.
    * **Rows existed but none could supply a figure.** ``UNKNOWN``. Reporting
      ``$0.00`` here would be the same defaulted zero the per-row path refuses:
      "three API-billed runs, $0.00 spend" reads as "they cost nothing" when it
      actually means "we could not price any of them".
    * **Some contributed.** A real partial total, carrying ``coverage`` so the
      shortfall can be shown on screen rather than hidden in a tooltip.
    """

    missing = rows - len(values)
    coverage = {"contributed": len(values), "rows": rows, "complete": missing == 0}

    if rows and not values:
        cell = metric(
            klass=CLASS_UNKNOWN,
            formula=formula,
            reason=(
                f"none of the {rows} row(s) in this window could supply this figure, "
                "so no total is asserted"
            ),
            unit="usd",
        )
        cell["coverage"] = coverage
        return cell

    cell = metric(
        round(sum(values), 6),
        klass=CLASS_DERIVED,
        formula=formula,
        source=f"{len(values)} of {rows} row(s) in this window contributed a figure",
        reason=(
            f"{missing} row(s) in this window could not supply this figure and are excluded"
            if missing
            else None
        ),
        unit="usd",
    )
    cell["coverage"] = coverage
    return cell


def build_aggregates(rows: list[dict[str, Any]], *, now: _dt.datetime | None = None) -> dict[str, Any]:
    """Per-window totals, with estimated value and actual spend kept apart.

    Three totals of *value* -- what the tokens would have been worth on the
    public API, split by who actually paid -- and exactly one total of
    *spend*, over API-billed rows only. Nothing crosses between them.

    A row whose occurrence time is unknown is counted nowhere and reported as
    excluded. Dropping it into the current window would inflate today's
    figures with work of unknown age.
    """

    moment = (now or _dt.datetime.now(_dt.UTC)).astimezone(_dt.UTC)
    starts = window_starts(moment)

    dated: list[tuple[_dt.datetime, dict[str, Any]]] = []
    undated = 0
    future = 0
    for row in rows:
        when = _parse_iso((row.get("occurred_at") or {}).get("value"))
        if when is None:
            undated += 1
            continue
        when = when.astimezone(_dt.UTC)
        if when > moment:
            # A timestamp after "now" means a clock skew somewhere. It belongs
            # in no window, but it was being dropped silently -- so the totals
            # were quietly incomplete with nothing saying so. Independent
            # review (Grok Build, issue #42).
            future += 1
            continue
        dated.append((when, row))

    def established(cell: dict[str, Any] | None) -> float | None:
        """A figure only counts toward a total if its own class established it."""

        if not cell or cell.get("class") not in (CLASS_DERIVED, CLASS_MEASURED):
            return None
        value = cell.get("value")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    buckets: dict[str, Any] = {}
    for name, start in starts.items():
        window = [row for when, row in dated if start <= when <= moment]
        by_billing: dict[str, list[dict[str, Any]]] = {
            BILLING_SUBSCRIPTION: [],
            BILLING_API: [],
            BILLING_FREE: [],
            BILLING_UNKNOWN: [],
        }
        for row in window:
            klass = (row.get("billing") or {}).get("class") or BILLING_UNKNOWN
            by_billing.setdefault(klass, []).append(row)

        def equivalents(group: list[dict[str, Any]]) -> list[float]:
            return [
                value
                for row in group
                if (value := established((row.get("metrics") or {}).get("estimated_api_equivalent_usd")))
                is not None
            ]

        sub_values = equivalents(by_billing[BILLING_SUBSCRIPTION])
        api_values = equivalents(by_billing[BILLING_API])
        free_values = equivalents(by_billing[BILLING_FREE])

        # Actual spend is drawn *only* from API-billed rows' actual-cost cells.
        # Subscription and free rows contribute a real $0.00 to spend, but they
        # are counted separately so the number reads as "what the API bill grew
        # by", not "the sum of everything that happened".
        spend_values = [
            value
            for row in by_billing[BILLING_API]
            if (value := established((row.get("metrics") or {}).get("actual_cost_usd"))) is not None
        ]

        buckets[name] = {
            "window": {"start": start.isoformat(), "end": moment.isoformat()},
            "subscription_equivalent_usd": _sum_cell(
                sub_values,
                formula="sum of estimated API-equivalent value over subscription-included rows",
                rows=len(by_billing[BILLING_SUBSCRIPTION]),
            ),
            "free_equivalent_usd": _sum_cell(
                free_values,
                formula="sum of estimated API-equivalent value over free-tier rows",
                rows=len(by_billing[BILLING_FREE]),
            ),
            "api_equivalent_usd": _sum_cell(
                api_values,
                formula="sum of estimated API-equivalent value over API-billed rows",
                rows=len(by_billing[BILLING_API]),
            ),
            "actual_api_spend_usd": _sum_cell(
                spend_values,
                formula="sum of actual cost over API-billed rows only",
                rows=len(by_billing[BILLING_API]),
            ),
            "counts": {
                "rows": len(window),
                "subscription_included": len(by_billing[BILLING_SUBSCRIPTION]),
                "api_billed": len(by_billing[BILLING_API]),
                "free_tier": len(by_billing[BILLING_FREE]),
                "billing_unknown": len(by_billing[BILLING_UNKNOWN]),
            },
        }

    return {
        "basis": WINDOW_BASIS,
        "invariant": AGGREGATE_INVARIANT,
        "generated_at": moment.isoformat(),
        "excluded_undated_rows": undated,
        "excluded_future_rows": future,
        "excluded_reason": (
            "; ".join(
                part
                for part in (
                    "rows with no recorded end time or record timestamp are counted in no window, "
                    "rather than being attributed to the current one"
                    if undated
                    else "",
                    f"{future} row(s) are timestamped after the current time (clock skew) and are "
                    "counted in no window"
                    if future
                    else "",
                )
                if part
            )
            or None
        ),
        "windows": buckets,
    }


def unavailable_metrics() -> list[str]:
    """Metric names this stack currently cannot produce, for the UI legend."""

    return [
        "fresh_input_tokens",
        "cache_read_tokens",
        "cache_hit_rate",
        "context_limit",
        "context_used_percent",
    ]


__all__ = [
    "AGGREGATE_INVARIANT",
    "BILLING_API",
    "BILLING_FREE",
    "BILLING_LABELS",
    "BILLING_REASONS",
    "BILLING_SUBSCRIPTION",
    "BILLING_UNKNOWN",
    "CLASS_DERIVED",
    "CLASS_MEASURED",
    "CLASS_NOT_APPLICABLE",
    "CLASS_NOT_EXPOSED",
    "CLASS_UNKNOWN",
    "REASON_NO_CACHE_ACCOUNTING",
    "REASON_NO_CONTEXT_LIMIT",
    "WINDOW_BASIS",
    "WorkerFacts",
    "billing_for_route",
    "build_aggregates",
    "build_row",
    "metric",
    "unavailable_metrics",
    "window_starts",
]
