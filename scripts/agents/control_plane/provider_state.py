"""Provider state model, classifier, and the fixed catalog-only rows.

Every provider actually wired for execution comes from ENG-AGENT-01's
``workers.json`` registry (``claude-code``, ``grok-build``, ``codex-review``,
``opencode2-gemini-flash-lite``, and the ``antigravity-*`` workers today). This
module seeds one durable ``ProviderState`` row per registry worker, plus a
fixed, non-adapter, ``NOT_CONFIGURED`` catalog row for every execution system
the maintainer wants visible in the control center but that has no CLI or
credentials in this repository: GLM, DeepSeek, OmniRoute, and Cheaper
Inference. Catalog rows are never probed, never adapted, and are structurally
excluded from routing math (see ``routing.py``) rather than being present
with a zero weight. (OpenAI/Codex moved out of this catalog in
ENG-AGENT-02-S7, issue #97, once ``codex-review`` became a real,
subscription-authenticated registry worker -- see ``workers.json``.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Iterable

from ..registry import (
    REASON_AVAILABLE,
    REASON_CATALOG_ONLY,
    REASON_CLI_MISSING,
    REASON_DISABLED,
    REASON_LAUNCH_ENVIRONMENT_ERROR,
    REASON_NOT_AUTHENTICATED,
    Registry,
    Worker,
)
from .models import ProviderState, utc_now_iso

if TYPE_CHECKING:
    from .state import State

# Every state a real, registry-backed provider can occupy.
STATE_AVAILABLE = "AVAILABLE"
STATE_BUSY = "BUSY"
STATE_RATE_LIMITED = "RATE_LIMITED"
STATE_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
STATE_COOLING_DOWN = "COOLING_DOWN"
STATE_FAILED = "FAILED"
STATE_DISABLED = "DISABLED"
# Catalog-only: no adapter exists at all. Distinct from DISABLED, which means a
# real, executable registry worker that policy has turned off.
STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
# A hard budget (see control_plane/telemetry.py check_budget) tripped for this
# provider's API route. Distinct from QUOTA_EXHAUSTED (a provider-reported
# limit) and DISABLED (an operator choice unrelated to spend).
STATE_COST_BLOCKED = "COST_BLOCKED"
# ENG-AGENT-11 (issue #131): the local auth/session probe (and, when
# declared, the authoritative harmless-prompt fallback) never produced a
# truthful answer -- a launcher/sandbox visibility defect, not a provider-
# reported logout. Distinct from NOT_CONFIGURED, which covers both a
# genuinely missing CLI and a probe that actually received an explicit
# negative answer; this state exists so an operator (and routing) can tell
# the two apart instead of being told to sign in again for a session that is
# still valid.
STATE_LAUNCH_ENVIRONMENT_ERROR = "LAUNCH_ENVIRONMENT_ERROR"

PROVIDER_STATES = frozenset(
    {
        STATE_AVAILABLE,
        STATE_BUSY,
        STATE_RATE_LIMITED,
        STATE_QUOTA_EXHAUSTED,
        STATE_COOLING_DOWN,
        STATE_FAILED,
        STATE_DISABLED,
        STATE_NOT_CONFIGURED,
        STATE_COST_BLOCKED,
        STATE_LAUNCH_ENVIRONMENT_ERROR,
    }
)

# States eligible to receive new routed work right now.
ROUTABLE_STATES = frozenset({STATE_AVAILABLE, STATE_BUSY})

# States that structurally exclude a provider from any active-routing math,
# regardless of the mode. A provider in one of these is a row for display
# only.
NON_ROUTABLE_STATES = PROVIDER_STATES - ROUTABLE_STATES


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    provider: str
    execution_system: str = "none"
    cost_class: str = "catalog-only"


# Fixed, hand-maintained catalog of execution systems with no adapter in this
# repository today. Extending this list never makes an entry executable by
# itself — a real adapter, CLI, and ``workers.json`` route are required first.
CATALOG_ONLY_PROVIDERS: tuple[CatalogEntry, ...] = (
    CatalogEntry(name="glm", provider="Zhipu"),
    CatalogEntry(name="deepseek-catalog", provider="DeepSeek"),
    CatalogEntry(name="omniroute", provider="OmniRoute"),
    CatalogEntry(name="cheaper-inference", provider="CheaperInference"),
)

# Phrases in captured worker output/errors that reclassify a provider after a
# failed run. Order matters: more specific phrases are checked first.
_QUOTA_HINTS = (
    "quota exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "hard quota",
    "weekly limit",
    "usage limit",
)
_RATE_LIMIT_HINTS = ("rate limit", "rate_limit", "too many requests", "429")

_RESET_PATTERNS = (
    re.compile(r"\bresets?\s+(?:at\s+)?([^.;\n]+)", re.I),
    re.compile(r"\breset\s+window\s*[:=]\s*([^.;\n]+)", re.I),
)


def failure_attribution(
    *, worker: Worker | None, reason: str, provider_state: str | None = None
) -> dict[str, str | None]:
    """Return truthful route-specific failure facts without cross-provider inference."""

    clean = (reason or "").strip()[:800]
    reset = None
    for pattern in _RESET_PATTERNS:
        match = pattern.search(clean)
        if match:
            reset = match.group(1).strip()
            break
    category = {
        STATE_QUOTA_EXHAUSTED: "QUOTA",
        STATE_RATE_LIMITED: "RATE_LIMIT",
    }.get(provider_state) or {
        STATE_QUOTA_EXHAUSTED: "QUOTA",
        STATE_RATE_LIMITED: "RATE_LIMIT",
    }.get(classify_failure(clean))
    if category is None:
        lowered = clean.lower()
        if any(term in lowered for term in (
            "not authenticated", "not_authenticated", "login required", "cli unavailable", "cli_missing", "command not found"
        )):
            category = "AUTH_CLI"
        elif any(term in lowered for term in ("provider outage", "service unavailable", "temporarily unavailable")):
            category = "PROVIDER_OUTAGE"
        elif any(term in lowered for term in ("context window", "context length", "too many tokens")):
            category = "CONTEXT"
        elif any(term in lowered for term in ("secret", "policy", "safety", "worktree", "authorization")):
            category = "SAFETY_POLICY"
        else:
            category = "UNKNOWN"
    return {
        "worker_id": worker.name if worker else None,
        "execution_system": worker.execution_system if worker else None,
        "provider": worker.provider if worker else None,
        "model": worker.effective_model if worker else None,
        "category": category,
        "reason": clean or None,
        "reset": reset,
        "reset_source": "provider diagnostic" if reset else None,
    }


def seed_provider_states(registry: Registry) -> list[ProviderState]:
    """Build the initial provider-state rows: real registry workers + catalog."""

    states: list[ProviderState] = []
    for worker in registry.workers.values():
        states.append(_seed_registry_worker(worker))
    for entry in CATALOG_ONLY_PROVIDERS:
        states.append(
            ProviderState(
                name=entry.name,
                execution_system=entry.execution_system,
                provider=entry.provider,
                cost_class=entry.cost_class,
                state=STATE_NOT_CONFIGURED,
                configured=False,
                reason=REASON_CATALOG_ONLY,
            )
        )
    return states


def reconcile_provider_states(state: "State", registry: Registry) -> None:
    """Add any provider row the current registry/catalog knows about but the

    database does not yet have, and remove a row that no longer corresponds
    to any registry worker or catalog entry, without touching any other row
    that already exists.

    Seeding used to run only against an empty database (first-ever startup),
    so a worker added to ``workers.json`` later (e.g. ``codex-review``,
    ENG-AGENT-02-S7 issue #97) never appeared for a maintainer whose
    ``.orchestrator-state`` database already existed -- the Control Center
    kept showing OpenAI/Codex as the old catalog-only ``NOT_CONFIGURED`` row
    forever, even after a real adapter and CLI existed. The same defect runs
    in reverse when a name is *removed* (``openai-codex`` itself, once
    ``codex-review`` replaced it): without an explicit removal step, that
    stale row would persist forever in an already-existing database (Grok
    Build review, issue #97). ``registry.workers`` and
    ``CATALOG_ONLY_PROVIDERS`` are both fully deterministic, so any row whose
    name is in neither is unambiguously obsolete. Every other row is left
    exactly as it is so accumulated runtime state (BUSY, QUOTA_EXHAUSTED,
    COST_BLOCKED, consecutive_failures, an operator's ``provider_disable``,
    ...) survives a daemon restart untouched.
    """

    valid_names = set(registry.workers) | {entry.name for entry in CATALOG_ONLY_PROVIDERS}
    existing = {row.name for row in state.list_provider_states()}
    for provider in seed_provider_states(registry):
        if provider.name not in existing:
            state.upsert_provider_state(provider)
    for stale_name in existing - valid_names:
        state.delete_provider_state(stale_name)


def state_for_reason(reason: str) -> str:
    """The routing-relevant ``state`` bucket for one ``availability_reason()`` value.

    Shared by initial seeding and the explicit ``probe`` command so the two
    can never disagree about which reasons are routable (Grok Build review,
    issue #97: a probe that only ever *promoted* to ``AVAILABLE`` without
    ever demoting away from it left a provider stuck ``AVAILABLE`` after its
    auth session actually expired).
    """

    return {
        REASON_AVAILABLE: STATE_AVAILABLE,
        REASON_DISABLED: STATE_DISABLED,
        REASON_CLI_MISSING: STATE_NOT_CONFIGURED,
        REASON_NOT_AUTHENTICATED: STATE_NOT_CONFIGURED,
        REASON_LAUNCH_ENVIRONMENT_ERROR: STATE_LAUNCH_ENVIRONMENT_ERROR,
    }.get(reason, STATE_NOT_CONFIGURED)


def _seed_registry_worker(worker: Worker) -> ProviderState:
    # probe=False: seeding runs on every orchestrator command context build
    # (including `--dry-run`), so it must never spawn a subprocess just to
    # classify a worker. The fuller, probed truth (e.g. NOT_AUTHENTICATED)
    # is available on demand through the explicit `probe` command.
    reason = worker.availability_reason(probe=False)
    state = state_for_reason(reason)
    return ProviderState(
        name=worker.name,
        execution_system=worker.execution_system,
        provider=worker.provider,
        cost_class=worker.cost_class,
        state=state,
        configured=worker.enabled,
        reason=reason,
    )


def classify_failure(output_or_error: str) -> str:
    """Suggest the next provider state after a failed run, from captured text.

    Conservative: anything not clearly quota/rate-limit shaped is a plain
    ``FAILED`` rather than guessed as a transient condition, so a provider is
    never left permanently excluded by a misclassified one-off error.
    """

    lowered = (output_or_error or "").lower()
    if any(hint in lowered for hint in _QUOTA_HINTS):
        return STATE_QUOTA_EXHAUSTED
    if any(hint in lowered for hint in _RATE_LIMIT_HINTS):
        return STATE_RATE_LIMITED
    return STATE_FAILED


def next_state_after_success(_current: str) -> str:
    return STATE_AVAILABLE


def is_routable(provider: ProviderState) -> bool:
    """A provider can receive new routed work only when configured and live."""

    return provider.configured and provider.state in ROUTABLE_STATES


# ENG-AGENT-12 (issue #136): how long a persisted ProviderState's last probe
# may be trusted before a new launch must re-probe it rather than trusting a
# possibly-stale cached row. A provider that has never been probed
# (``last_probe_at`` is ``None``, e.g. right after seeding) is always stale.
PROVIDER_STATE_MAX_AGE_SECONDS = 300


def provider_state_age_seconds(provider: ProviderState, *, now: datetime | None = None) -> float | None:
    """Seconds since ``provider.last_probe_at`` was recorded, or ``None`` if never probed."""

    if not provider.last_probe_at:
        return None
    try:
        checked_at = datetime.fromisoformat(provider.last_probe_at)
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - checked_at).total_seconds())


def provider_state_freshness(provider: ProviderState, *, max_age_seconds: float = PROVIDER_STATE_MAX_AGE_SECONDS) -> str:
    """``FRESH``/``STALE``/``UNKNOWN`` for display -- never itself a routing input."""

    age = provider_state_age_seconds(provider)
    if age is None:
        return "UNKNOWN"
    return "FRESH" if age <= max_age_seconds else "STALE"


def provider_state_is_stale(provider: ProviderState, *, max_age_seconds: float = PROVIDER_STATE_MAX_AGE_SECONDS) -> bool:
    age = provider_state_age_seconds(provider)
    return age is None or age > max_age_seconds


def probe_and_update(state: "State", registry: Registry, name: str) -> ProviderState | None:
    """Run the existing non-billable CLI probe for ``name`` and persist the result.

    Shared by the explicit ``probe`` command (``commands.cmd_probe``) and the
    automatic pre-launch freshness refresh (``refresh_stale_routable_candidates``
    below) so the two can never disagree about how a probe result maps onto
    ``state``/``reason`` (``state_for_reason``). Never touches a
    ``COST_BLOCKED`` row: that is this control plane's own spend gate, not a
    CLI-availability fact, and is only ever released through the explicit
    ``provider_cost_clear`` command. Returns ``None`` without persisting
    anything when the name is not a real registry worker or has no existing
    provider-state row.
    """

    try:
        worker = registry.get(name)
    except Exception:
        return None
    provider = state.get_provider_state(name)
    if provider is None:
        return None
    reason = worker.availability_reason()
    provider.last_probe_at = utc_now_iso()
    if provider.configured and provider.state != STATE_COST_BLOCKED:
        provider.reason = reason
        # Demotes as readily as it promotes -- see cmd_probe's own comment
        # for why a probe must never only ever promote toward AVAILABLE.
        provider.state = state_for_reason(reason)
    state.upsert_provider_state(provider)
    return provider


def refresh_stale_routable_candidates(
    state: "State",
    registry: Registry,
    names: Iterable[str],
    *,
    max_age_seconds: float = PROVIDER_STATE_MAX_AGE_SECONDS,
) -> list[str]:
    """Re-probe every named provider that is both stale and currently routable.

    Called before a new launch admits a route candidate (``dispatch.managed_admit``)
    so a persisted ``AVAILABLE``/``BUSY`` row that is actually stale (e.g. the
    provider's auth session or CLI availability changed since the last probe)
    gets one non-billable freshness check before it can be selected. A row
    already in a non-routable state (``QUOTA_EXHAUSTED``, ``DISABLED``, ...)
    is deliberately left untouched here: it cannot regress into being wrongly
    selected by staying stale, and this probe cannot confirm quota/rate-limit
    recovery anyway (no locally installed tool exposes that; see
    ``telemetry.claude_quota_windows``) -- only an explicit operator ``probe``
    or a real subsequent run can promote it back to ``AVAILABLE``. Returns the
    names actually refreshed, for logging/testing.
    """

    refreshed: list[str] = []
    for name in names:
        provider = state.get_provider_state(name)
        if provider is None or provider.state not in ROUTABLE_STATES:
            continue
        if not provider_state_is_stale(provider, max_age_seconds=max_age_seconds):
            continue
        if probe_and_update(state, registry, name) is not None:
            refreshed.append(name)
    return refreshed
