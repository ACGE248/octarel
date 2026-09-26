"""OCTAREL-UI-07 (issue #42): API-equivalent pricing, and where it came from.

This module answers exactly one question -- *what would a thousand tokens of
this model have cost on the public API?* -- and it answers it only from a
source the repository already maintains.

**The authoritative source is the OpenCode model catalog**
(``scripts/agents/model_catalog.py``, snapshot ``catalog.json``). OpenCode
declares each model's ``cost`` alongside its context limit and capabilities,
and ``model_catalog.classify_cost`` already treats those declared numbers as
the authority for whether a route is metered or free. Introducing a second,
hand-entered price table beside it would create a competing source of truth
that could silently disagree with the one routing already trusts. There is no
new pricing file in this change.

Units. The catalog's ``cost.input`` / ``cost.output`` are US dollars per
**million** tokens -- ``google/gemini-2.5-flash`` declares ``input: 0.3``
against a published rate of $0.30 per million input tokens, and
``google/gemini-2.5-flash-lite`` declares ``input: 0.1`` against $0.10 per
million. :class:`ModelPricing` converts once, to the per-1K rates
``telemetry.PricingSnapshot`` already speaks, so the conversion lives in one
place rather than at every call site.

What this module deliberately will not do:

* **No name matching.** A model resolves by exact catalog id, or by its
  provider-qualified id when the worker records a bare model name. Nothing
  else. A near-miss produces no price at all, because a plausible-looking
  price attached to the wrong model is worse than an honest blank.
* **No inferred provider.** The worker-provider-to-catalog-provider map below
  is hand-maintained for the same reason ``telemetry._COST_CLASS_ROUTES`` is:
  an unrecognised provider resolves to nothing rather than guessing.
* **No network.** Pricing is read from the cached snapshot on disk. Refreshing
  the catalog is ``model_catalog``'s job and runs a local ``opencode`` status
  command; nothing here spawns a process or contacts a provider.

Two providers in ``workers.json`` are absent from the catalog by construction:
Anthropic (``claude-code`` runs through the Claude CLI, not OpenCode) and
DeepSeek. Their runs report an unavailable estimate with that reason. That is
the intended outcome, not a gap to paper over.

A pricing snapshot is **not** billing evidence. It says what a model is
advertised to cost; it says nothing about how a particular run was actually
paid for. Billing class comes from the worker's ``cost_class`` and travels
separately all the way to the UI -- see ``usage_telemetry``.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import model_catalog

# A worker's ``workers.json`` provider name -> the catalog's ``provider_id``.
# Hand-maintained on purpose: a provider missing from this map yields no price
# rather than a guessed one. Anthropic and DeepSeek are absent because the
# OpenCode catalog does not carry them, not because they were overlooked.
_PROVIDER_CATALOG_IDS: dict[str, str] = {
    "Google": "google",
    "OpenAI": "openai",
    "xAI": "xai",
    "OpenCode Zen": "opencode",
}

MATCH_CATALOG_ID = "exact catalog id"
MATCH_PROVIDER_QUALIFIED = "provider-qualified catalog id"

SOURCE_OPENCODE_CATALOG = "OpenCode model catalog"

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"

# How old a snapshot may be before a price quoted from it carries a caveat.
#
# Deliberately *not* ``model_catalog.CATALOG_MAX_AGE_SECONDS`` (30 minutes).
# That threshold governs when routing should re-discover a model's
# availability and capability, which change often. Published per-token rates
# change on the order of months, so reusing the discovery window would mark
# nearly every snapshot stale and the flag would carry no information. A week
# is the point at which a quoted rate deserves to be read with suspicion.
PRICING_STALE_AFTER_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class ModelPricing:
    """Per-1K-token rates for one model, plus the snapshot they came from."""

    catalog_model_id: str
    input_per_1k_usd: float
    output_per_1k_usd: float
    matched_by: str
    source: str = SOURCE_OPENCODE_CATALOG
    refreshed_at: str | None = None
    fingerprint: str | None = None
    age_seconds: float | None = None
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": STATUS_OK,
            "catalog_model_id": self.catalog_model_id,
            "input_per_1k_usd": self.input_per_1k_usd,
            "output_per_1k_usd": self.output_per_1k_usd,
            "matched_by": self.matched_by,
            "source": self.source,
            "refreshed_at": self.refreshed_at,
            "fingerprint": self.fingerprint,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "reason": None,
        }


@dataclass(frozen=True)
class PricingLookup:
    """The outcome of asking for one model's price, resolved or not."""

    pricing: ModelPricing | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.pricing is not None:
            return self.pricing.as_dict()
        return {
            "status": STATUS_UNAVAILABLE,
            "catalog_model_id": None,
            "input_per_1k_usd": None,
            "output_per_1k_usd": None,
            "matched_by": None,
            "source": SOURCE_OPENCODE_CATALOG,
            "refreshed_at": None,
            "fingerprint": None,
            "age_seconds": None,
            "stale": False,
            "reason": self.reason,
        }


def _rates(cost: Any) -> tuple[float, float] | None:
    """The declared per-million input/output rates, or nothing.

    Only the two top-level fields ``model_catalog.classify_cost`` itself
    requires are read. Tiered and cache rates exist in the catalog but apply
    under conditions no run records, so using them would mean assuming which
    tier applied -- exactly the guess this module refuses to make.
    """

    if not isinstance(cost, dict):
        return None
    raw_in = cost.get("input")
    raw_out = cost.get("output")
    if isinstance(raw_in, bool) or isinstance(raw_out, bool):
        return None
    if not isinstance(raw_in, (int, float)) or not isinstance(raw_out, (int, float)):
        return None
    if raw_in < 0 or raw_out < 0:
        return None
    return float(raw_in), float(raw_out)


@dataclass(frozen=True)
class PricingBook:
    """Every price a catalog snapshot can supply, and the snapshot's provenance."""

    status: str
    reason: str
    models: dict[str, dict[str, Any]]
    refreshed_at: str | None = None
    fingerprint: str | None = None
    age_seconds: float | None = None
    stale: bool = False

    def provenance(self) -> dict[str, Any]:
        """Snapshot-level facts for the UI legend. Never billing evidence."""

        return {
            "status": self.status,
            "reason": self.reason,
            "source": SOURCE_OPENCODE_CATALOG,
            "refreshed_at": self.refreshed_at,
            "fingerprint": self.fingerprint,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "model_count": len(self.models),
        }

    def _entry(self, provider: str | None, model: str | None) -> tuple[str, str] | None:
        """Resolve ``(catalog_id, matched_by)`` by exact id only."""

        if not model:
            return None
        if model in self.models:
            return model, MATCH_CATALOG_ID
        if "/" in model:
            # Already provider-qualified and still unmatched. Re-qualifying it
            # under a different provider would be a guess.
            return None
        provider_id = _PROVIDER_CATALOG_IDS.get(provider or "")
        if not provider_id:
            return None
        qualified = f"{provider_id}/{model}"
        if qualified in self.models:
            return qualified, MATCH_PROVIDER_QUALIFIED
        return None

    def lookup(self, *, provider: str | None, model: str | None) -> PricingLookup:
        """Price one model, or say precisely why it could not be priced."""

        if self.status != STATUS_OK:
            return PricingLookup(None, self.reason)
        if not model:
            return PricingLookup(None, "this run did not record which model ran, so it cannot be priced")

        match = self._entry(provider, model)
        if match is None:
            if "/" not in model and not _PROVIDER_CATALOG_IDS.get(provider or ""):
                return PricingLookup(
                    None,
                    f"provider {provider or 'UNKNOWN'!s} is not carried by the {SOURCE_OPENCODE_CATALOG}, "
                    "and a model name is never matched across providers",
                )
            return PricingLookup(
                None, f"model {model!r} has no entry in the {SOURCE_OPENCODE_CATALOG} snapshot"
            )

        catalog_id, matched_by = match
        rates = _rates(self.models[catalog_id].get("cost"))
        if rates is None:
            return PricingLookup(
                None, f"the catalog entry for {catalog_id!r} declares no complete numeric cost metadata"
            )
        per_million_in, per_million_out = rates
        return PricingLookup(
            ModelPricing(
                catalog_model_id=catalog_id,
                # The catalog quotes dollars per million tokens; PricingSnapshot
                # and compute_cost speak per thousand. Converted once, here.
                input_per_1k_usd=per_million_in / 1000.0,
                output_per_1k_usd=per_million_out / 1000.0,
                matched_by=matched_by,
                refreshed_at=self.refreshed_at,
                fingerprint=self.fingerprint,
                age_seconds=self.age_seconds,
                stale=self.stale,
            )
        )


def unavailable_book(reason: str) -> PricingBook:
    return PricingBook(status=STATUS_UNAVAILABLE, reason=reason, models={})


def book_from_catalog(
    catalog: model_catalog.Catalog | None, *, now: _dt.datetime | None = None
) -> PricingBook:
    """Build a book from an already-loaded catalog snapshot."""

    if catalog is None:
        return unavailable_book(
            "no OpenCode catalog snapshot is cached; run the model-catalog refresh to establish pricing"
        )
    age = model_catalog.catalog_age_seconds(catalog, now)
    stale = age is not None and age > PRICING_STALE_AFTER_SECONDS
    models = {
        entry.id: {"cost": entry.cost, "context_limit": entry.context_limit} for entry in catalog.models
    }
    if not models:
        return unavailable_book("the cached OpenCode catalog snapshot declares no models")
    return PricingBook(
        status=STATUS_OK,
        # A stale snapshot is still the best evidence available and is used --
        # but it says so, so an operator can tell a current price from an old one.
        reason=(
            f"catalog snapshot is older than {PRICING_STALE_AFTER_SECONDS // 86400} days; "
            "prices may no longer be current"
        )
        if stale
        else "cached OpenCode catalog snapshot",
        models=models,
        refreshed_at=catalog.refreshed_at,
        fingerprint=catalog.fingerprint,
        age_seconds=age,
        stale=stale,
    )


def load_pricing_book(
    *, state_dir: Path | None = None, now: _dt.datetime | None = None
) -> PricingBook:
    """Read the cached catalog snapshot. Never discovers, never spawns."""

    return book_from_catalog(model_catalog.load_catalog(state_dir), now=now)


__all__ = [
    "MATCH_CATALOG_ID",
    "MATCH_PROVIDER_QUALIFIED",
    "PRICING_STALE_AFTER_SECONDS",
    "SOURCE_OPENCODE_CATALOG",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "ModelPricing",
    "PricingBook",
    "PricingLookup",
    "book_from_catalog",
    "load_pricing_book",
    "unavailable_book",
]
