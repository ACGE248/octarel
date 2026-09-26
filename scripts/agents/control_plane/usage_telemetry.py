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
    Meaningless for this route -- a subscription-backed CLI invocation has no
    per-call dollar cost, so reporting ``$0.00`` would imply API pricing that
    does not apply.

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
* Dollar cost is only ever produced for an API route with both a pricing
  snapshot and known token counts; subscription and free routes report
  ``NOT_APPLICABLE`` with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .telemetry import (
    ROUTE_API,
    ROUTE_FREE,
    ROUTE_SUBSCRIPTION,
    TOKEN_ESTIMATED,
    TOKEN_EXACT,
    TokenUsage,
    compute_cost,
    execution_route_for_cost_class,
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
    """The registry facts a usage row needs, resolved by the caller."""

    worker: str
    provider: str | None = None
    execution_system: str | None = None
    model: str | None = None
    cost_class: str | None = None


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


def _cost_metric(facts: WorkerFacts, record: dict[str, Any]) -> dict[str, Any]:
    """Dollar cost, or the reason there is no meaningful dollar figure."""

    quality = str(record.get("telemetry_quality") or "unknown").lower()
    mode = {"exact": TOKEN_EXACT, "estimated": TOKEN_ESTIMATED}.get(quality, "UNKNOWN")
    usage = TokenUsage(
        mode=mode,
        input_tokens=record.get("input_tokens"),
        output_tokens=record.get("output_tokens"),
    )
    result = compute_cost(
        worker_cost_class=facts.cost_class or "",
        token_usage=usage,
        # No pricing snapshot is configured in this deployment; compute_cost
        # returns None with that reason rather than inventing a rate.
        pricing=None,
    )
    if result.usd is not None:
        return metric(result.usd, klass=CLASS_DERIVED, formula="tokens x pricing snapshot", unit="usd")

    route = result.route
    if route in (ROUTE_SUBSCRIPTION, ROUTE_FREE):
        # Never imply API pricing for a subscription-backed or free CLI route.
        return metric(klass=CLASS_NOT_APPLICABLE, reason=result.reason, unit="usd")
    return metric(klass=CLASS_UNKNOWN, reason=result.reason, unit="usd")


def build_row(
    record: dict[str, Any],
    *,
    facts: WorkerFacts,
    project_id: str | None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """One attributable usage row for a runbook's durable usage record."""

    metrics = _token_metrics(record)
    metrics["duration_seconds"] = (
        metric(duration_seconds, klass=CLASS_MEASURED, source="orchestrator run timing", unit="seconds")
        if duration_seconds is not None
        else metric(klass=CLASS_UNKNOWN, reason="no recorded run duration", unit="seconds")
    )
    metrics["cost_usd"] = _cost_metric(facts, record)

    route = execution_route_for_cost_class(facts.cost_class or "")
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
            "recorded_at": record.get("updated_at"),
            "source": "durable usage governance record",
        },
        "route": {
            "execution_route": route,
            "cost_class": facts.cost_class,
            "billable": route == ROUTE_API,
            "history": record.get("route_history") or [],
            "escalation_state": record.get("escalation_state"),
            "escalation_reason": record.get("escalation_reason"),
        },
        "telemetry_quality": str(record.get("telemetry_quality") or "unknown"),
        "metrics": metrics,
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
    "CLASS_DERIVED",
    "CLASS_MEASURED",
    "CLASS_NOT_APPLICABLE",
    "CLASS_NOT_EXPOSED",
    "CLASS_UNKNOWN",
    "REASON_NO_CACHE_ACCOUNTING",
    "REASON_NO_CONTEXT_LIMIT",
    "WorkerFacts",
    "build_row",
    "metric",
    "unavailable_metrics",
]
