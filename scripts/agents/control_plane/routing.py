"""Adaptive routing: five modes computed only over configured, routable providers.

A provider that is ``NOT_CONFIGURED`` or ``DISABLED`` (see ``provider_state.py``)
is structurally excluded from every mode below — it never appears as a key in
the returned percentage mapping, rather than appearing with a ``0`` weight.
This matters for the maintainer's routing-percentage safety rule: OpenAI/Codex,
GLM, DeepSeek, OmniRoute, and Cheaper Inference (catalog-only, no adapter) can
never receive traffic no matter which mode is selected.

Percentages always sum to 100 (float) across the returned providers, rounding
applied only for display; callers that need exact reproducibility should treat
the returned mapping as the source of truth rather than re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ProviderState
from .provider_state import is_routable

# Mode A: single primary. All traffic to the first (cheapest/most preferred)
# routable provider in the given preference order; used when determinism
# matters more than diversification (e.g. a single write-capable worker).
MODE_SINGLE_PRIMARY = "A"

# Mode B: even split. Equal weight across every routable provider for a role.
MODE_EVEN_SPLIT = "B"

# Mode C: cost-weighted. Weight favors the cheapest cost class; free/verified
# providers receive the largest share, premium-subscription the smallest.
MODE_COST_WEIGHTED = "C"

# Mode D: capability-priority. Weight decays by position in the supplied
# preference order (first gets the largest share) rather than being uniform.
MODE_CAPABILITY_PRIORITY = "D"

# Mode E: failover chain. Same 100/0 shape as Mode A but the caller receives
# the full ordered fallback chain (not just the primary) so a scheduler can
# advance to the next provider without recomputing routing after a failure.
MODE_FAILOVER_CHAIN = "E"

ROUTING_MODES = (
    MODE_SINGLE_PRIMARY,
    MODE_EVEN_SPLIT,
    MODE_COST_WEIGHTED,
    MODE_CAPABILITY_PRIORITY,
    MODE_FAILOVER_CHAIN,
)

# Lower rank = cheaper = larger share under Mode C. Anything unlisted (should
# not happen for a routable provider) falls back to the most expensive rank.
_COST_CLASS_RANK = {
    "free-verified": 0,
    "supplemental-configured": 1,
    "free-dynamic": 1,
    "metered-configured": 2,
    "premium-subscription": 3,
    "optional-overflow": 4,
}


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingResult:
    mode: str
    percentages: dict[str, float]
    order: tuple[str, ...]  # preference order actually used (routable subset)


def _routable_in_order(providers: dict[str, ProviderState], order: list[str] | tuple[str, ...]) -> list[str]:
    return [name for name in order if name in providers and is_routable(providers[name])]


def compute_routing(
    mode: str,
    providers: dict[str, ProviderState],
    preference_order: list[str] | tuple[str, ...],
) -> RoutingResult:
    """Compute routing percentages for ``mode`` across routable providers only.

    ``providers`` maps name -> ProviderState (the full known set, including
    NOT_CONFIGURED/DISABLED rows for display elsewhere). ``preference_order``
    is typically a registry route (cheapest-capable-first).
    """

    if mode not in ROUTING_MODES:
        raise RoutingError(f"unknown routing mode {mode!r}; expected one of {ROUTING_MODES}")

    routable = _routable_in_order(providers, preference_order)
    if not routable:
        return RoutingResult(mode=mode, percentages={}, order=())

    if mode == MODE_SINGLE_PRIMARY:
        percentages = {routable[0]: 100.0}
    elif mode == MODE_EVEN_SPLIT:
        share = round(100.0 / len(routable), 4)
        percentages = {name: share for name in routable}
        _rebalance_to_100(percentages, routable[0])
    elif mode == MODE_COST_WEIGHTED:
        weights = [1.0 / (1 + _COST_CLASS_RANK.get(providers[name].cost_class, 4)) for name in routable]
        total = sum(weights)
        percentages = {name: round(100.0 * w / total, 4) for name, w in zip(routable, weights)}
        _rebalance_to_100(percentages, routable[0])
    elif mode == MODE_CAPABILITY_PRIORITY:
        # Geometric decay by position: 1, 1/2, 1/4, ... normalized to 100.
        weights = [0.5**i for i in range(len(routable))]
        total = sum(weights)
        percentages = {name: round(100.0 * w / total, 4) for name, w in zip(routable, weights)}
        _rebalance_to_100(percentages, routable[0])
    elif mode == MODE_FAILOVER_CHAIN:
        percentages = {routable[0]: 100.0}
    else:  # pragma: no cover - guarded by the membership check above
        raise RoutingError(f"unhandled routing mode {mode!r}")

    return RoutingResult(mode=mode, percentages=percentages, order=tuple(routable))


def _rebalance_to_100(percentages: dict[str, float], anchor: str) -> None:
    """Absorb float rounding drift into ``anchor`` so the total is exactly 100."""

    drift = round(100.0 - sum(percentages.values()), 4)
    if drift:
        percentages[anchor] = round(percentages[anchor] + drift, 4)
