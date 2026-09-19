from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from scripts.agents.control_plane.models import ProviderState
from scripts.agents.control_plane.usage_policy import (
    MECHANICAL,
    ROUTINE,
    fallback_for_failure,
)
from scripts.agents.policy import (
    PolicyError,
    compose_policy_bundle,
    validate_registry_policies,
)
from scripts.agents.redaction import PLACEHOLDER, redact_text
from scripts.agents.registry import (
    REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE,
    Registry,
    load_registry,
)
from scripts.agents.validation import ValidationError, validate_scope_path
from scripts.ci.change_risk import classify, requires_release_artifacts

ROOT = Path(__file__).resolve().parents[1]


def _available_states(registry):
    return {
        name: ProviderState(name, worker.execution_system, worker.provider, worker.cost_class, "AVAILABLE")
        for name, worker in registry.workers.items()
    }


def test_canonical_policy_tree_and_root_claude_bootstrap_are_minimal() -> None:
    required = {
        ".agents/README.md",
        *{f".agents/core/{name}.md" for name in ("SECURITY", "GIT_WORKTREES", "TESTING", "DOCUMENTATION", "COST_AND_PROVIDER_SAFETY")},
        *{f".agents/roles/{name}.md" for name in ("ORCHESTRATOR", "IMPLEMENTER", "TESTER", "REVIEWER", "RESEARCHER")},
        *{f".agents/providers/{name}.md" for name in ("CLAUDE", "CODEX_OPENAI", "GEMINI", "ANTIGRAVITY", "GROK", "DEEPSEEK")},
        *{f".agents/workflows/{name}.md" for name in ("IMPLEMENT", "TEST_AND_FIX", "REVIEW", "PROVIDER_INTEGRATION", "UI_AUDIT")},
    }
    assert all((ROOT / path).is_file() for path in required)
    bootstrap = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert len(bootstrap) < 1800
    assert "not an\nauthoritative" in bootstrap
    assert ".agents/providers/CLAUDE.md" in bootstrap
    for heading in ("## Architecture", "## Commands", "## Data model", "## UI conventions"):
        assert heading not in bootstrap


def test_policy_composition_is_deterministic_scoped_and_provider_explicit() -> None:
    registry = load_registry()
    first = compose_policy_bundle(
        root=ROOT,
        registry=registry,
        worker_name="opencode2-gemini-flash-lite",
        route_role="focused-tests",
        contracts=("docs/engineering/ENG-AGENT-04.md",),
        acceptance_criteria="Run the policy tests.",
    )
    second = compose_policy_bundle(
        root=ROOT,
        registry=registry,
        worker_name="opencode2-gemini-flash-lite",
        route_role="focused-tests",
        contracts=("docs/engineering/ENG-AGENT-04.md",),
        acceptance_criteria="Run the policy tests.",
    )
    assert first == second
    assert first.manifest["role"] == "TESTER"
    assert first.manifest["workflow"] == "TEST_AND_FIX"
    assert first.manifest["provider_policy"] == ".agents/providers/GEMINI.md"
    assert first.manifest["root_claude_bootstrap_loaded"] is False
    assert "CLAUDE.md" not in first.manifest["core_policies"]
    assert "docs/PRODUCT_ROADMAP.md" not in first.manifest["task_contracts"]


def test_orchestrated_claude_is_explicit_and_does_not_load_root_bootstrap() -> None:
    bundle = compose_policy_bundle(
        root=ROOT,
        registry=load_registry(),
        worker_name="claude-code",
        route_role="primary-implementation",
    )
    assert bundle.manifest["provider_policy"] == ".agents/providers/CLAUDE.md"
    assert bundle.manifest["root_claude_bootstrap_loaded"] is False
    assert "--- POLICY: CLAUDE.md ---" not in bundle.prompt


def test_dry_run_manifest_attributes_actual_worker_provider_and_model() -> None:
    from scripts.agents.orchestrate import run_delegation

    result = run_delegation(
        registry=load_registry(),
        root=ROOT,
        task="ENG-AGENT-04-MANIFEST",
        worker_name="opencode2-gemini-flash-lite",
        role="focused-tests",
        model=None,
        intensity=None,
        why="verify deterministic attribution",
        prompt_args=["Inspect policy tests."],
        scope_paths=["tests/test_agent_policy.py"],
        dry_run=True,
        allow_write=False,
        allow_overflow=False,
        timeout=None,
    )
    policy = result.manifest["policy_manifest"]
    assert policy["actual_worker"] == "opencode2-gemini-flash-lite"
    assert policy["actual_provider"] == "Google"
    assert policy["actual_model"] == "google/gemini-3.5-flash-lite"


def test_missing_provider_policy_and_incompatible_registry_fail_closed() -> None:
    registry = load_registry()
    broken_worker = dataclasses.replace(registry.get("claude-code"), provider_policy=".agents/providers/MISSING.md")
    broken = Registry({**registry.workers, "claude-code": broken_worker}, registry.routes, registry.intensities)
    with pytest.raises(PolicyError, match="does not exist"):
        compose_policy_bundle(root=ROOT, registry=broken, worker_name="claude-code", route_role="primary-implementation")

    bad_read_worker = dataclasses.replace(
        registry.get("codex-review"), allowed_policy_roles=("IMPLEMENTER",)
    )
    bad = Registry({**registry.workers, "codex-review": bad_read_worker}, registry.routes, registry.intensities)
    with pytest.raises(ValueError, match="read-only worker"):
        validate_registry_policies(ROOT, bad)


def test_claude_implementer_fallback_preserves_policy_and_permissions() -> None:
    registry = load_registry()
    states = _available_states(registry)
    states["claude-code"].state = "QUOTA_EXHAUSTED"
    original = compose_policy_bundle(
        root=ROOT, registry=registry, worker_name="claude-code", route_role="primary-implementation"
    ).manifest
    decision = fallback_for_failure(
        root=ROOT,
        registry=registry,
        provider_states=states,
        route_role="primary-implementation",
        failed_worker="claude-code",
        failure_message="quota exhausted",
        original_policy_manifest=original,
        classification=ROUTINE,
        codex_policy="unrestricted",
        codex_auto_eligible=True,
        codex_invocations=0,
        max_codex_invocations=1,
    )
    assert decision.status == "FALLBACK"
    assert decision.worker == "codex-build"
    assert decision.policy_manifest["role"] == "IMPLEMENTER"
    assert decision.policy_manifest["workflow"] == "IMPLEMENT"
    assert decision.policy_manifest["read_write_mode"] == "write"
    assert decision.policy_manifest["api_billing_enabled"] is False


def test_tester_and_reviewer_fallback_keep_contracts_and_read_only() -> None:
    registry = load_registry()
    states = _available_states(registry)
    tester = compose_policy_bundle(
        root=ROOT, registry=registry, worker_name="opencode2-gemini-flash-lite", route_role="focused-tests"
    ).manifest
    tester_fallback = fallback_for_failure(
        root=ROOT, registry=registry, provider_states=states, route_role="focused-tests",
        failed_worker="opencode2-gemini-flash-lite", failure_message="CLI unavailable",
        original_policy_manifest=tester, classification=MECHANICAL, codex_policy="conserve",
        codex_auto_eligible=False, codex_invocations=0, max_codex_invocations=1,
    )
    assert tester_fallback.worker == "antigravity-focused-tests"
    assert tester_fallback.policy_manifest["role"] == "TESTER"
    assert tester_fallback.policy_manifest["workflow"] == "TEST_AND_FIX"
    assert tester_fallback.policy_manifest["read_write_mode"] == "read-only"
    assert (
        tester_fallback.policy_manifest["repository_data_authorization"]
        == REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE
    )

    reviewer = compose_policy_bundle(
        root=ROOT, registry=registry, worker_name="antigravity-diff-review", route_role="diff-review"
    ).manifest
    reviewer_fallback = fallback_for_failure(
        root=ROOT, registry=registry, provider_states=states, route_role="diff-review",
        failed_worker="antigravity-diff-review", failure_message="provider outage",
        original_policy_manifest=reviewer, classification=ROUTINE, codex_policy="conserve",
        codex_auto_eligible=False, codex_invocations=0, max_codex_invocations=1,
    )
    assert reviewer_fallback.worker == "opencode2-gemini-flash-lite-review"
    assert reviewer_fallback.policy_manifest["role"] == "REVIEWER"
    assert reviewer_fallback.policy_manifest["workflow"] == "REVIEW"
    assert reviewer_fallback.policy_manifest["read_write_mode"] == "read-only"
    assert (
        reviewer_fallback.policy_manifest["repository_data_authorization"]
        == REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE
    )


def test_safety_blocks_and_context_minimizes_before_provider_hop() -> None:
    registry = load_registry()
    states = _available_states(registry)
    original = compose_policy_bundle(
        root=ROOT, registry=registry, worker_name="claude-code", route_role="primary-implementation"
    ).manifest
    common = dict(
        root=ROOT, registry=registry, provider_states=states, route_role="primary-implementation",
        failed_worker="claude-code", original_policy_manifest=original, classification=ROUTINE,
        codex_policy="unrestricted", codex_auto_eligible=True, codex_invocations=0,
        max_codex_invocations=2,
    )
    blocked = fallback_for_failure(failure_message="worktree lock safety block", **common)
    minimized = fallback_for_failure(failure_message="context window exceeded", **common)
    assert (blocked.status, blocked.worker) == ("BLOCKED", None)
    assert (minimized.status, minimized.worker) == ("MINIMIZE_CONTEXT", None)


def test_registry_policy_paths_and_billing_routes_are_valid() -> None:
    registry = load_registry()
    validate_registry_policies(ROOT, registry)
    assert all((ROOT / worker.provider_policy).is_file() for worker in registry.workers.values())
    assert all(worker.allow_api_billing is False for worker in registry.workers.values())
    assert registry.get("codex-build").auth_mode == "chatgpt-subscription-session"


def test_google_workers_have_persistent_provider_repository_authorization() -> None:
    registry = load_registry()
    google = [worker for worker in registry.workers.values() if worker.provider == "Google"]
    assert google
    assert all(
        registry.effective_repository_data_authorization(worker.name)
        == REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE
        for worker in google
    )
    assert all(registry.repository_data_reuse_allowed(worker.name) for worker in google)
    bundle = compose_policy_bundle(
        root=ROOT,
        registry=registry,
        worker_name="opencode2-gemini-flash-lite",
        route_role="focused-tests",
    )
    assert bundle.manifest["repository_data_authorization_source"] == "provider-level"


def test_grok_workers_have_persistent_provider_repository_authorization() -> None:
    registry = load_registry()
    grok = [worker for worker in registry.workers.values() if worker.provider == "xAI"]
    assert grok
    assert all(
        registry.effective_repository_data_authorization(worker.name)
        == REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE
        for worker in grok
    )
    assert all(registry.repository_data_reuse_allowed(worker.name) for worker in grok)


def test_provider_preauthorization_never_exposes_secret_scopes_or_values(tmp_path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("XAI_API_KEY=xai-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="protected file"):
        validate_scope_path(".env", tmp_path)
    redacted = redact_text(secret.read_text(encoding="utf-8"))
    assert "xai-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert PLACEHOLDER in redacted


def test_preauthorization_does_not_enable_billable_or_overflow_routes() -> None:
    registry = load_registry()
    for worker in registry.workers.values():
        if worker.provider in {"Google", "xAI"}:
            assert worker.allow_api_billing is False
            assert worker.cost_class != "optional-overflow"
    overflow = registry.get("deepseek-overflow")
    assert not registry.repository_data_reuse_allowed(overflow.name)
    assert overflow.enabled is False


def test_non_claude_bootstrap_and_release_policy_are_scoped() -> None:
    tester = (ROOT / ".opencode/agents/tester.md").read_text(encoding="utf-8")
    assert "do not depend on root `CLAUDE.md`" in tester
    assert "docs/PRODUCT_ROADMAP.md" not in tester
    assert requires_release_artifacts(["app/domains/jobs/service.py"]) is False
    assert requires_release_artifacts(["docs/engineering/ENG-AGENT-04.md"]) is False
    assert requires_release_artifacts(["VERSION"]) is True


def test_t0_t1_t2_t3_and_ui_scope_selection() -> None:
    assert classify([".agents/core/TESTING.md"]).test_tier == "T0"
    backend = classify(["app/domains/jobs/service.py"])
    assert (backend.test_tier, backend.ui_audit_scope) == ("T1", "none")
    product = classify(["frontend/v2/src/pages/ConnectionsPage.tsx"])
    assert (product.test_tier, product.ui_audit_scope) == ("T1", "product-focused")
    control = classify(["scripts/agents/control_plane/dashboard/app.js"])
    assert (control.test_tier, control.ui_audit_scope) == ("T1", "control-center-focused")
    shared = classify(["frontend/v2/src/components/Button.tsx"])
    assert (shared.test_tier, shared.ui_audit_scope) == ("T2", "product-full")
    assert classify(["scripts/ci/local_gate.py"]).test_tier == "T3"
