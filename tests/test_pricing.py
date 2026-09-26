"""OCTAREL-UI-07 (issue #42): API-equivalent pricing from the OpenCode catalog.

The point of these tests is that a price is either resolved from the catalog
by an exact identifier or not produced at all. Every "close enough" match is
a fabricated number attached to the wrong model, and this module exists
specifically to refuse them.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.agents.control_plane import pricing
from scripts.agents.model_catalog import Catalog, CatalogModel

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)


def model(model_id: str, *, cost: dict | None) -> CatalogModel:
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


def catalog(*models: CatalogModel, refreshed_at: str | None = None) -> Catalog:
    return Catalog(
        status="ok",
        reason="",
        opencode_version="1.0",
        refreshed_at=refreshed_at or NOW.isoformat(),
        credentials_status="ok",
        models=tuple(models),
    )


# Real declared rates: $0.30 / $2.50 per *million* tokens for Gemini 2.5 Flash.
FLASH = model("google/gemini-2.5-flash", cost={"input": 0.3, "output": 2.5})
GROK = model("xai/grok-4.6", cost={"input": 3.0, "output": 15.0})
ZERO = model("opencode/big-pickle", cost={"input": 0, "output": 0})


def book(*models: CatalogModel, refreshed_at: str | None = None) -> pricing.PricingBook:
    return pricing.book_from_catalog(catalog(*models, refreshed_at=refreshed_at), now=NOW)


def test_catalog_millions_are_converted_to_the_per_1k_rates_cost_accounting_speaks():
    """The catalog quotes dollars per million; PricingSnapshot speaks per thousand."""

    found = book(FLASH).lookup(provider="Google", model="google/gemini-2.5-flash").pricing
    assert found is not None
    assert found.input_per_1k_usd == pytest.approx(0.0003)
    assert found.output_per_1k_usd == pytest.approx(0.0025)
    assert found.matched_by == pricing.MATCH_CATALOG_ID


def test_a_bare_model_name_resolves_only_through_its_own_provider():
    """``grok-4.6`` from an xAI worker is ``xai/grok-4.6`` -- and nothing else."""

    resolved = book(GROK).lookup(provider="xAI", model="grok-4.6")
    assert resolved.pricing is not None
    assert resolved.pricing.catalog_model_id == "xai/grok-4.6"
    assert resolved.pricing.matched_by == pricing.MATCH_PROVIDER_QUALIFIED

    # The same bare name under a provider that does not declare it stays unpriced,
    # rather than borrowing another provider's rate for a similarly named model.
    crossed = book(GROK).lookup(provider="Google", model="grok-4.6")
    assert crossed.pricing is None
    assert crossed.reason


def test_a_provider_the_catalog_does_not_carry_is_reported_not_guessed():
    """Anthropic and DeepSeek are absent from the catalog by construction."""

    result = book(FLASH).lookup(provider="Anthropic", model="claude-sonnet-5")
    assert result.pricing is None
    assert "Anthropic" in result.reason


def test_a_near_miss_model_name_never_resolves():
    result = book(FLASH).lookup(provider="Google", model="gemini-2.5-flash-lite")
    assert result.pricing is None
    assert "no entry" in result.reason


def test_an_already_qualified_id_is_never_requalified_under_another_provider():
    """``openai/gpt-x`` must not fall back to ``google/openai/gpt-x`` or similar."""

    result = book(FLASH).lookup(provider="Google", model="openai/gpt-nonexistent")
    assert result.pricing is None


def test_a_model_with_no_recorded_name_cannot_be_priced():
    result = book(FLASH).lookup(provider="Google", model=None)
    assert result.pricing is None
    assert "did not record which model" in result.reason


@pytest.mark.parametrize(
    "cost",
    [None, {}, {"input": 0.3}, {"input": "cheap", "output": 1}, {"input": -1, "output": 1}, {"input": True, "output": 1}],
)
def test_incomplete_or_nonsense_cost_metadata_yields_no_price(cost):
    result = book(model("google/odd", cost=cost)).lookup(provider="Google", model="google/odd")
    assert result.pricing is None
    assert result.reason


def test_a_genuinely_zero_rate_is_a_price_not_a_missing_one():
    """A free OpenCode Zen model really does cost $0; that is a fact, not a blank."""

    found = book(ZERO).lookup(provider="OpenCode Zen", model="opencode/big-pickle").pricing
    assert found is not None
    assert found.input_per_1k_usd == 0.0
    assert found.output_per_1k_usd == 0.0


def test_no_catalog_snapshot_means_no_prices_at_all():
    empty = pricing.book_from_catalog(None, now=NOW)
    assert empty.status == pricing.STATUS_UNAVAILABLE
    result = empty.lookup(provider="Google", model="google/gemini-2.5-flash")
    assert result.pricing is None
    assert "model-catalog refresh" in result.reason
    assert empty.provenance()["status"] == pricing.STATUS_UNAVAILABLE


def test_an_old_snapshot_still_prices_but_says_it_is_old():
    """Stale prices beat no prices, as long as the operator can tell they are stale."""

    old = (NOW - dt.timedelta(days=30)).isoformat()
    aged = book(FLASH, refreshed_at=old)
    assert aged.stale is True
    assert "may no longer be current" in aged.reason
    found = aged.lookup(provider="Google", model="google/gemini-2.5-flash").pricing
    assert found is not None and found.stale is True

    fresh = book(FLASH, refreshed_at=(NOW - dt.timedelta(hours=2)).isoformat())
    assert fresh.stale is False


@pytest.mark.parametrize(
    ("provider", "model"),
    [("Anthropic", "claude-sonnet-5"), ("DeepSeek", "deepseek/deepseek-chat")],
)
def test_every_provider_the_catalog_omits_is_reported_the_same_way(provider, model):
    """Both absent providers in workers.json, not just the one that is obvious.

    ``claude-code`` runs through the Claude CLI and ``deepseek-overflow``
    through a provider OpenCode does not carry, so neither can be priced.
    """

    result = book(FLASH, GROK).lookup(provider=provider, model=model)
    assert result.pricing is None
    assert result.reason


def test_the_stale_boundary_is_exactly_the_documented_threshold():
    """Seven days, deliberately not the catalog's 30-minute discovery window."""

    cutoff = pricing.PRICING_STALE_AFTER_SECONDS
    assert cutoff == 7 * 24 * 3600

    just_inside = book(FLASH, refreshed_at=(NOW - dt.timedelta(seconds=cutoff - 1)).isoformat())
    assert just_inside.stale is False

    just_outside = book(FLASH, refreshed_at=(NOW - dt.timedelta(seconds=cutoff + 1)).isoformat())
    assert just_outside.stale is True


def test_a_snapshot_with_no_refresh_time_is_not_called_stale():
    """Unknown age is not evidence of being old; it is simply unknown."""

    undated = pricing.book_from_catalog(catalog(FLASH, refreshed_at="not a date"), now=NOW)
    assert undated.age_seconds is None
    assert undated.stale is False
    assert undated.lookup(provider="Google", model="google/gemini-2.5-flash").pricing is not None


def test_pricing_provenance_is_snapshot_evidence_and_says_so():
    provenance = book(FLASH).provenance()
    assert provenance["source"] == pricing.SOURCE_OPENCODE_CATALOG
    assert provenance["refreshed_at"] == NOW.isoformat()
    assert provenance["fingerprint"]
    # A pricing snapshot describes advertised rates. It carries no billing
    # claim, so nothing in its provenance may look like one.
    assert "billing" not in {key.lower() for key in provenance}
