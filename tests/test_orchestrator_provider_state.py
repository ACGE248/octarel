"""Provider-state seeding and failure classification (ENG-AGENT-02 Slice 1)."""

from __future__ import annotations

from scripts.agents.control_plane.models import ProviderState
from scripts.agents.control_plane.provider_state import (
    CATALOG_ONLY_PROVIDERS,
    NON_ROUTABLE_STATES,
    PROVIDER_STATES,
    STATE_AVAILABLE,
    STATE_COST_BLOCKED,
    STATE_DISABLED,
    STATE_FAILED,
    STATE_NOT_CONFIGURED,
    STATE_QUOTA_EXHAUSTED,
    STATE_RATE_LIMITED,
    classify_failure,
    failure_attribution,
    is_routable,
    next_state_after_success,
    reconcile_provider_states,
    seed_provider_states,
)
from scripts.agents.control_plane.state import State
from scripts.agents.registry import REASON_CATALOG_ONLY, load_registry


def test_seed_includes_one_row_per_registry_worker_and_every_catalog_entry():
    registry = load_registry()
    states = seed_provider_states(registry)
    names = {s.name for s in states}
    for worker_name in registry.workers:
        assert worker_name in names
    for entry in CATALOG_ONLY_PROVIDERS:
        assert entry.name in names
    assert len(states) == len(registry.workers) + len(CATALOG_ONLY_PROVIDERS)


def test_catalog_only_rows_are_not_configured_and_have_no_adapter():
    registry = load_registry()
    by_name = {s.name: s for s in seed_provider_states(registry)}
    for entry in CATALOG_ONLY_PROVIDERS:
        row = by_name[entry.name]
        assert row.state == STATE_NOT_CONFIGURED
        assert row.configured is False
        assert row.execution_system == "none"
        assert row.cost_class == "catalog-only"
        assert row.reason == REASON_CATALOG_ONLY


def test_openai_codex_is_no_longer_a_permanent_catalog_row():
    # ENG-AGENT-02-S7 (issue #97): a real, subscription-authenticated
    # "codex-review" worker now exists in workers.json, so OpenAI/Codex must
    # never again be pinned to a fixed NOT_CONFIGURED catalog entry -- doing
    # so would make the Control Center lie even after Codex became usable.
    assert "openai-codex" not in {entry.name for entry in CATALOG_ONLY_PROVIDERS}
    registry = load_registry()
    assert "codex-review" in registry.workers


def test_disabled_registry_route_is_disabled_not_not_configured():
    # deepseek-overflow is a real, registry-backed route that policy disables
    # by default; it is a distinct concept from the catalog-only NOT_CONFIGURED
    # "deepseek-catalog" row (no adapter at all).
    registry = load_registry()
    by_name = {s.name: s for s in seed_provider_states(registry)}
    assert by_name["deepseek-overflow"].state == STATE_DISABLED
    assert by_name["deepseek-overflow"].configured is False
    assert by_name["deepseek-catalog"].state == STATE_NOT_CONFIGURED


def test_enabled_registry_worker_seeds_available_or_not_configured_by_cli_presence():
    registry = load_registry()
    by_name = {s.name: s for s in seed_provider_states(registry)}
    for name, worker in registry.workers.items():
        if not worker.enabled:
            continue
        reason = worker.availability_reason(probe=False)
        expected = STATE_AVAILABLE if reason == "AVAILABLE" else STATE_NOT_CONFIGURED
        assert by_name[name].state == expected
        assert by_name[name].configured is True
        assert by_name[name].reason == reason


def test_reconcile_adds_only_missing_rows_and_leaves_existing_rows_untouched(tmp_path):
    registry = load_registry()
    state = State(tmp_path / "orchestrator.db")
    reconcile_provider_states(state, registry)
    assert {p.name for p in state.list_provider_states()} == {
        w for w in registry.workers
    } | {e.name for e in CATALOG_ONLY_PROVIDERS}

    # Simulate an operator action + accumulated runtime state on a real row,
    # then reconcile again (as every orchestrator startup now does): the
    # existing row must survive completely unchanged.
    claude = state.get_provider_state("claude-code")
    claude.state = STATE_DISABLED
    claude.consecutive_failures = 3
    state.upsert_provider_state(claude)

    reconcile_provider_states(state, registry)
    after = state.get_provider_state("claude-code")
    assert after.state == STATE_DISABLED
    assert after.consecutive_failures == 3


def test_reconcile_removes_a_row_no_longer_backed_by_the_registry_or_catalog(tmp_path):
    """Grok Build review (issue #97): the exact defect this slice fixes runs

    in reverse when a name is *removed* from the catalog (``openai-codex``,
    once ``codex-review`` replaced it) -- without an explicit removal step, a
    maintainer's already-existing database would show that stale row forever.
    """

    registry = load_registry()
    state = State(tmp_path / "orchestrator.db")
    reconcile_provider_states(state, registry)

    from scripts.agents.control_plane.models import ProviderState

    state.upsert_provider_state(
        ProviderState(
            name="openai-codex",
            execution_system="none",
            provider="OpenAI",
            cost_class="catalog-only",
            state=STATE_NOT_CONFIGURED,
            configured=False,
        )
    )
    assert state.get_provider_state("openai-codex") is not None

    reconcile_provider_states(state, registry)
    assert state.get_provider_state("openai-codex") is None
    # Every real row is untouched by the same reconcile call.
    assert {p.name for p in state.list_provider_states()} == {
        w for w in registry.workers
    } | {e.name for e in CATALOG_ONLY_PROVIDERS}


def test_reconcile_adds_a_worker_newly_introduced_to_an_existing_database(tmp_path):
    # Regression for the exact defect this fixes: a maintainer's
    # ``.orchestrator-state`` database created before "codex-review" existed
    # in workers.json must still pick it up on the next daemon start, rather
    # than requiring the database to be deleted.
    registry = load_registry()
    state = State(tmp_path / "orchestrator.db")
    reconcile_provider_states(state, registry)
    state._conn.execute("DELETE FROM provider_states WHERE name = 'codex-review'")
    state._conn.commit()
    assert state.get_provider_state("codex-review") is None

    reconcile_provider_states(state, registry)
    codex = state.get_provider_state("codex-review")
    assert codex is not None
    # Reconciliation seeds without probing (never spawns a subprocess).
    assert codex.reason == registry.get("codex-review").availability_reason(probe=False)


def test_classify_failure_quota_before_rate_limit_before_generic():
    assert classify_failure("Error: quota exceeded for this billing period") == STATE_QUOTA_EXHAUSTED
    assert classify_failure("HTTP 429 Too Many Requests: rate limit hit") == STATE_RATE_LIMITED
    assert classify_failure("segmentation fault") == STATE_FAILED
    assert classify_failure("") == STATE_FAILED


def test_claude_weekly_limit_attribution_never_implies_codex_usage():
    worker = load_registry().get("claude-code")
    facts = failure_attribution(
        worker=worker,
        reason="You've hit your weekly limit · resets 8pm (America/Toronto)",
    )
    assert facts == {
        "worker_id": "claude-code",
        "execution_system": "Claude Code",
        "provider": "Anthropic",
        "model": worker.default_model,
        "category": "QUOTA",
        "reason": "You've hit your weekly limit · resets 8pm (America/Toronto)",
        "reset": "8pm (America/Toronto)",
        "reset_source": "provider diagnostic",
    }
    assert "Codex" not in " ".join(str(value) for value in facts.values())


def test_known_provider_quota_state_is_authoritative_when_detail_has_no_quota_phrase():
    worker = load_registry().get("claude-code")
    facts = failure_attribution(
        worker=worker,
        reason="worker 'claude-code' is unavailable before launch: QUOTA_EXHAUSTED",
        provider_state=STATE_QUOTA_EXHAUSTED,
    )
    assert facts["category"] == "QUOTA"


def test_next_state_after_success_is_always_available():
    assert next_state_after_success(STATE_QUOTA_EXHAUSTED) == STATE_AVAILABLE
    assert next_state_after_success(STATE_FAILED) == STATE_AVAILABLE


def test_is_routable_excludes_not_configured_and_disabled_and_unconfigured():
    available = ProviderState(
        name="a", execution_system="x", provider="p", cost_class="free-verified", state=STATE_AVAILABLE
    )
    not_configured = ProviderState(
        name="b",
        execution_system="none",
        provider="p",
        cost_class="catalog-only",
        state=STATE_NOT_CONFIGURED,
        configured=False,
    )
    disabled = ProviderState(
        name="c", execution_system="x", provider="p", cost_class="metered-configured", state=STATE_DISABLED
    )
    configured_but_marked_unrouteable = ProviderState(
        name="d",
        execution_system="x",
        provider="p",
        cost_class="metered-configured",
        state=STATE_AVAILABLE,
        configured=False,
    )
    assert is_routable(available) is True
    assert is_routable(not_configured) is False
    assert is_routable(disabled) is False
    assert is_routable(configured_but_marked_unrouteable) is False


def test_cost_blocked_is_a_real_state_excluded_from_routing():
    assert STATE_COST_BLOCKED in PROVIDER_STATES
    assert STATE_COST_BLOCKED in NON_ROUTABLE_STATES
    cost_blocked = ProviderState(
        name="e", execution_system="x", provider="p", cost_class="metered-configured", state=STATE_COST_BLOCKED
    )
    assert is_routable(cost_blocked) is False
