from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.agents.control_plane import runbooks
from scripts.agents.control_plane.commands import cmd_usage_override
from scripts.agents.control_plane.models import ProviderState, Runbook
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.usage_policy import (
    ARCHITECTURE_HIGH_RISK,
    HARD_DEBUGGING,
    MECHANICAL,
    ROUTINE,
    build_context_manifest,
    classify_failure,
    classify_task,
    codex_allowed,
    fallback_for_failure,
    new_usage_record,
    provider_diverse_review,
    route_for_role,
    should_escalate,
)
from scripts.agents.policy import compose_policy_bundle
from scripts.agents.registry import REASON_AVAILABLE, Registry, load_registry


def test_classification_is_deterministic_and_role_first() -> None:
    assert classify_task(role="focused-tests", changed_paths=["app/core/jobs.py"]) == MECHANICAL
    assert classify_task(role="primary-implementation", changed_paths=["README.md"]) == ROUTINE
    assert classify_task(role="primary-implementation", changed_paths=["scripts/agents/runner.py"]) == ARCHITECTURE_HIGH_RISK
    assert classify_task(role="primary-implementation", prior_failures=2) == HARD_DEBUGGING


def test_conserve_requires_explicit_eligibility_and_hard_work() -> None:
    assert codex_allowed(policy="conserve", classification=HARD_DEBUGGING, auto_eligible=False,
                         invocations=0, max_invocations=1)[0] is False
    assert codex_allowed(policy="conserve", classification=ROUTINE, auto_eligible=True,
                         invocations=0, max_invocations=1)[0] is False
    assert codex_allowed(policy="conserve", classification=HARD_DEBUGGING, auto_eligible=True,
                         invocations=0, max_invocations=1)[0] is True
    assert codex_allowed(policy="unrestricted", classification=ROUTINE, auto_eligible=True,
                         invocations=1, max_invocations=1)[0] is False


def test_quota_rate_and_context_failures_never_escalate() -> None:
    for message, expected in [
        ("quota exhausted", "quota"), ("429 too many requests", "rate-limit"),
        ("RATE_LIMITED", "rate-limit"), ("CLI_MISSING", "auth-cli"),
        ("NOT_AUTHENTICATED", "auth-cli"), ("context window exceeded", "context-limit"),
    ]:
        failure = classify_failure(message)
        assert failure == expected
        assert should_escalate(failure=failure, attempts_at_tier=3, stronger_tier_available=True)[0] is False


def test_context_manifest_excludes_sensitive_paths_and_contents() -> None:
    manifest = build_context_manifest(
        paths=["app/main.py", ".env", "config/credentials.json", "docs/guide.md"],
        excerpts={"app/main.py": "x" * 5000, ".env": "SECRET=value"},
        test_output="y" * 9000,
    )
    assert manifest["paths"] == ["app/main.py", "docs/guide.md"]
    assert manifest["secret_content_included"] is False
    assert manifest["excerpts"] == [{"path": "app/main.py", "characters": 4000, "truncated": True}]
    assert manifest["test_output_characters"] == 8000


def test_usage_governance_survives_state_restart(tmp_path) -> None:
    db = tmp_path / "state.db"
    record = new_usage_record(
        runbook_id="RB-1", task_id="T-1", classification=HARD_DEBUGGING,
        codex_policy="balanced", codex_auto_eligible=True, max_codex_invocations=2,
        context_manifest={
            "paths": ["app/main.py"],
            "policy_manifest": {
                "role": "IMPLEMENTER",
                "workflow": "IMPLEMENT",
                "actual_worker": "codex-build",
                "actual_provider": "openai",
                "fallback_reason": "quota",
                "read_write_mode": "write",
            },
        },
    )
    record["route_history"] = [
        {"worker": "claude-code", "provider": "anthropic"},
        {"worker": "codex-build", "provider": "openai", "fallback_reason": "quota"},
    ]
    with State(db) as state:
        state.upsert_usage_governance(record)
    with State(db) as state:
        restored = state.get_usage_governance("RB-1")
    assert restored is not None
    assert restored["codex_policy"] == "balanced"
    assert restored["route_history"][-1] == {
        "worker": "codex-build", "provider": "openai", "fallback_reason": "quota"
    }
    assert restored["context_manifest"]["policy_manifest"]["role"] == "IMPLEMENTER"
    assert restored["context_manifest"]["policy_manifest"]["fallback_reason"] == "quota"
    assert restored["telemetry_quality"] == "unknown"


def _available_registry_states(registry):
    return {
        name: ProviderState(name, worker.execution_system, worker.provider, worker.cost_class, "AVAILABLE")
        for name, worker in registry.workers.items()
    }


def test_mechanical_avoids_codex_and_routine_prefers_claude() -> None:
    registry = load_registry()
    states = _available_registry_states(registry)
    mechanical = route_for_role(
        registry=registry, provider_states=states, role="mechanical-testing", classification=MECHANICAL,
        codex_policy="unrestricted", codex_auto_eligible=True, codex_invocations=0,
        max_codex_invocations=3,
    )
    routine = route_for_role(
        registry=registry, provider_states=states, role="primary-implementation", classification=ROUTINE,
        codex_policy="conserve", codex_auto_eligible=False, codex_invocations=0,
        max_codex_invocations=1,
    )
    assert mechanical.worker == "opencode2-gemini-flash-lite"
    assert routine.worker == "claude-code"


def test_unavailable_provider_blocks_without_silent_premium_fallback() -> None:
    registry = load_registry()
    decision = route_for_role(
        registry=registry, provider_states={}, role="primary-implementation", classification=ROUTINE,
        codex_policy="unrestricted", codex_auto_eligible=True, codex_invocations=0,
        max_codex_invocations=3,
    )
    assert decision.worker is None
    assert "unavailable" in decision.reason


def _implementation_manifest(registry, tmp_path):
    return compose_policy_bundle(
        root=tmp_path,
        registry=registry,
        worker_name="claude-code",
        route_role="primary-implementation",
        workflow="IMPLEMENT",
    ).manifest


@pytest.mark.parametrize(
    "failure",
    [
        "secret exposure detected",
        "not authorized for this repository",
        "unsafe worktree lock",
        "acceptance contract mismatch",
        "provider spend approval required",
        "CC-required safeguard failed",
        "production readiness gate failed",
        "implementation failure",
    ],
)
def test_nonrecoverable_failures_never_hop_providers(tmp_path, failure) -> None:
    registry = load_registry()
    decision = fallback_for_failure(
        root=tmp_path,
        registry=registry,
        provider_states=_available_registry_states(registry),
        route_role="primary-implementation",
        failed_worker="claude-code",
        failure_message=failure,
        original_policy_manifest=_implementation_manifest(registry, tmp_path),
        classification=ROUTINE,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        codex_invocations=0,
        max_codex_invocations=1,
        worker_availability={name: REASON_AVAILABLE for name in registry.workers},
    )
    assert decision.worker is None
    assert decision.status in {"BLOCKED", "ESCALATION_REQUIRED"}


def test_codex_budget_exhaustion_selects_next_eligible_provider(tmp_path) -> None:
    registry = load_registry()
    decision = fallback_for_failure(
        root=tmp_path,
        registry=registry,
        provider_states=_available_registry_states(registry),
        route_role="primary-implementation",
        failed_worker="claude-code",
        failure_message="weekly quota exhausted",
        original_policy_manifest=_implementation_manifest(registry, tmp_path),
        classification=ROUTINE,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        codex_invocations=1,
        max_codex_invocations=1,
        worker_availability={name: REASON_AVAILABLE for name in registry.workers},
    )
    assert decision.worker == "grok-build"
    assert "Codex invocation limit reached" in " ".join(decision.eligibility_reasons)


def test_no_eligible_provider_reports_each_blocked_route(tmp_path) -> None:
    registry = load_registry()
    decision = fallback_for_failure(
        root=tmp_path,
        registry=registry,
        provider_states={},
        route_role="primary-implementation",
        failed_worker="claude-code",
        failure_message="service unavailable",
        original_policy_manifest=_implementation_manifest(registry, tmp_path),
        classification=ROUTINE,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        codex_invocations=0,
        max_codex_invocations=1,
        worker_availability={name: REASON_AVAILABLE for name in registry.workers},
    )
    assert decision.worker is None
    assert decision.alternatives_considered == ("codex-build", "grok-build")
    assert "provider state is unavailable" in decision.reason


def test_api_billing_route_is_never_eligible_for_automatic_fallback(tmp_path) -> None:
    original = load_registry()
    billed = replace(original.get("codex-build"), allow_api_billing=True)
    registry = Registry(
        workers={**original.workers, "codex-build": billed},
        routes={**original.routes, "primary-implementation": ("claude-code", "codex-build")},
        intensities=original.intensities,
    )
    decision = fallback_for_failure(
        root=tmp_path,
        registry=registry,
        provider_states=_available_registry_states(registry),
        route_role="primary-implementation",
        failed_worker="claude-code",
        failure_message="quota exhausted",
        original_policy_manifest=_implementation_manifest(original, tmp_path),
        classification=ROUTINE,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        codex_invocations=0,
        max_codex_invocations=1,
        worker_availability={"codex-build": REASON_AVAILABLE},
    )
    assert decision.worker is None
    assert "API billing is forbidden" in decision.reason


def test_provider_diverse_review_skips_implementation_provider() -> None:
    registry = load_registry()
    states = _available_registry_states(registry)
    google = route_for_role(
        registry=registry, provider_states=states, role="diff-review", classification=ROUTINE,
        codex_policy="conserve", codex_auto_eligible=False, codex_invocations=0,
        max_codex_invocations=1,
    )
    assert provider_diverse_review("Anthropic", [google]).provider == "Google"


def test_overnight_defaults_conserve_and_operator_override_is_audited(tmp_path) -> None:
    runbook = Runbook(
        id="RB-overnight", name="Overnight", preset="overnight-development", objective="work",
        source_ref="ENG-1", branch="feature", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=480,
    )
    assert runbook.codex_policy == "conserve"
    assert runbook.codex_auto_eligible is False
    with State(tmp_path / "state.db") as state:
        state.upsert_runbook(runbook)
        state.upsert_usage_governance(new_usage_record(
            runbook_id=runbook.id, task_id=None, classification=ROUTINE, codex_policy="conserve",
            codex_auto_eligible=False, max_codex_invocations=1,
        ))
        exhausted = state.get_usage_governance(runbook.id)
        exhausted["codex_invocations"] = 1
        state.upsert_usage_governance(exhausted)
        result = cmd_usage_override(SimpleNamespace(state=state), runbook_id=runbook.id, reason="hard integration")
        restored = state.get_usage_governance(runbook.id)
        events = state.list_events()
    assert result.ok is True
    assert restored is not None and restored["codex_policy"] == "unrestricted"
    assert restored["max_codex_invocations"] == 2
    assert restored["escalation_state"] == "operator-override"
    assert any("premium Codex override" in event.message for event in events)


def test_draft_override_creates_visible_usage_record(tmp_path) -> None:
    runbook = Runbook(
        id="RB-draft", name="Draft", preset="overnight-development", objective="work",
        source_ref="ENG-1", branch="feature", worktree=str(tmp_path), parent_worker="claude-code",
        max_duration_minutes=480,
    )
    with State(tmp_path / "state.db") as state:
        state.upsert_runbook(runbook)
        cmd_usage_override(SimpleNamespace(state=state), runbook_id=runbook.id, reason="manual architecture review")
        usage = state.get_usage_governance(runbook.id)
    assert usage is not None
    assert usage["codex_policy"] == "unrestricted"
    assert usage["escalation_state"] == "operator-override"


def test_draft_policy_edit_synchronizes_existing_usage_record(tmp_path) -> None:
    registry = load_registry()
    with State(tmp_path / "state.db") as state:
        runbook = runbooks.create_runbook(
            state=state, registry=registry, name="Draft", preset="finish-pr", source_ref="ENG-1",
            branch="feature", worktree=str(tmp_path),
        )
        state.upsert_usage_governance(new_usage_record(
            runbook_id=runbook.id, task_id=None, classification=ROUTINE, codex_policy="conserve",
            codex_auto_eligible=False, max_codex_invocations=1,
        ))
        runbooks.update_runbook(
            state=state, registry=registry, runbook_id=runbook.id, codex_policy="balanced",
            codex_auto_eligible=True, max_codex_invocations=3,
        )
        usage = state.get_usage_governance(runbook.id)
    assert usage is not None
    assert (usage["codex_policy"], usage["codex_auto_eligible"], usage["max_codex_invocations"]) == (
        "balanced", True, 3,
    )
