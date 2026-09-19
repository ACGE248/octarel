"""Adaptive routing modes A-E, restricted to configured/routable providers."""

from __future__ import annotations

import pytest

from scripts.agents.control_plane.models import ProviderState
from scripts.agents.control_plane.provider_state import (
    STATE_AVAILABLE,
    STATE_DISABLED,
    STATE_NOT_CONFIGURED,
)
from scripts.agents.control_plane.routing import (
    ROUTING_MODES,
    RoutingError,
    compute_routing,
)

ORDER = ("cheap", "mid", "premium", "catalog_only", "disabled")


def _providers() -> dict[str, ProviderState]:
    return {
        "cheap": ProviderState(
            name="cheap", execution_system="X", provider="P", cost_class="free-verified", state=STATE_AVAILABLE
        ),
        "mid": ProviderState(
            name="mid",
            execution_system="X",
            provider="P",
            cost_class="supplemental-configured",
            state=STATE_AVAILABLE,
        ),
        "premium": ProviderState(
            name="premium",
            execution_system="X",
            provider="P",
            cost_class="premium-subscription",
            state=STATE_AVAILABLE,
        ),
        "catalog_only": ProviderState(
            name="catalog_only",
            execution_system="none",
            provider="P",
            cost_class="catalog-only",
            state=STATE_NOT_CONFIGURED,
            configured=False,
        ),
        "disabled": ProviderState(
            name="disabled",
            execution_system="X",
            provider="P",
            cost_class="metered-configured",
            state=STATE_DISABLED,
        ),
    }


@pytest.mark.parametrize("mode", ROUTING_MODES)
def test_every_mode_sums_to_100_and_excludes_not_configured_and_disabled(mode):
    result = compute_routing(mode, _providers(), ORDER)
    assert abs(sum(result.percentages.values()) - 100.0) < 1e-6
    assert "catalog_only" not in result.percentages
    assert "disabled" not in result.percentages
    assert "catalog_only" not in result.order
    assert "disabled" not in result.order


def test_mode_a_single_primary_is_100_percent_to_first_routable():
    result = compute_routing("A", _providers(), ORDER)
    assert result.percentages == {"cheap": 100.0}


def test_mode_b_even_split_across_routable_providers():
    result = compute_routing("B", _providers(), ORDER)
    assert set(result.percentages) == {"cheap", "mid", "premium"}
    for share in result.percentages.values():
        assert abs(share - 100.0 / 3) < 0.01


def test_mode_c_cost_weighted_favors_cheaper_providers():
    result = compute_routing("C", _providers(), ORDER)
    assert result.percentages["cheap"] > result.percentages["mid"] > result.percentages["premium"]


def test_mode_d_capability_priority_decays_by_position():
    result = compute_routing("D", _providers(), ORDER)
    assert result.percentages["cheap"] > result.percentages["mid"] > result.percentages["premium"]


def test_mode_e_failover_chain_is_100_percent_to_first_but_exposes_full_order():
    result = compute_routing("E", _providers(), ORDER)
    assert result.percentages == {"cheap": 100.0}
    assert result.order == ("cheap", "mid", "premium")


def test_unknown_mode_raises():
    with pytest.raises(RoutingError):
        compute_routing("Z", _providers(), ORDER)


def test_no_routable_providers_returns_empty_result():
    providers = {
        "catalog_only": _providers()["catalog_only"],
        "disabled": _providers()["disabled"],
    }
    for mode in ROUTING_MODES:
        result = compute_routing(mode, providers, ("catalog_only", "disabled"))
        assert result.percentages == {}
        assert result.order == ()


def test_not_configured_provider_present_in_map_but_marked_configured_is_still_excluded():
    providers = _providers()
    # A provider that is technically AVAILABLE-state but not `configured` must
    # still be excluded — configuration, not just live state, gates routing.
    providers["unconfigured_available"] = ProviderState(
        name="unconfigured_available",
        execution_system="X",
        provider="P",
        cost_class="metered-configured",
        state=STATE_AVAILABLE,
        configured=False,
    )
    order = (*ORDER, "unconfigured_available")
    for mode in ROUTING_MODES:
        result = compute_routing(mode, providers, order)
        assert "unconfigured_available" not in result.percentages
