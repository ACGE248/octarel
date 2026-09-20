"""Deterministic, durable usage-aware routing policy (ENG-AGENT-03).

The policy is deliberately pure: repository facts decide the task class and
role before provider availability is considered.  Provider/model selection is
then filtered by the runbook's explicit Codex policy and persisted by callers.
No billing API or inferred quota number is treated as authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

from ..policy import PolicyError, compose_policy_bundle, validate_policy_preservation

CODEX_CONSERVE = "conserve"
CODEX_BALANCED = "balanced"
CODEX_UNRESTRICTED = "unrestricted"
CODEX_POLICIES = frozenset({CODEX_CONSERVE, CODEX_BALANCED, CODEX_UNRESTRICTED})

MECHANICAL = "mechanical"
ROUTINE = "routine"
SERIOUS_INTEGRATION = "serious-integration"
HARD_DEBUGGING = "hard-debugging"
ARCHITECTURE_HIGH_RISK = "architecture-high-risk"
TASK_CLASSES = (MECHANICAL, ROUTINE, SERIOUS_INTEGRATION, HARD_DEBUGGING, ARCHITECTURE_HIGH_RISK)

NON_ESCALATING_FAILURES = frozenset({"quota", "rate-limit", "context-limit", "auth-cli", "provider-outage"})
_SECRET_PATH = re.compile(r"(^|/)(data(?:/|$)|\.env(?:\.|$)|credentials?|secrets?|.*\.pem$|.*\.key$)", re.I)


@dataclass(frozen=True)
class RoutingDecision:
    role: str
    classification: str
    worker: str | None
    provider: str | None
    model: str | None
    intensity: str | None
    reason: str
    alternatives: tuple[str, ...]
    codex_used: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "classification": self.classification,
            "worker": self.worker,
            "provider": self.provider,
            "model": self.model,
            "intensity": self.intensity,
            "reason": self.reason,
            "alternatives": list(self.alternatives),
            "codex_used": self.codex_used,
        }


@dataclass(frozen=True)
class FallbackDecision:
    status: str
    failure_category: str
    reason: str
    worker: str | None
    alternatives_considered: tuple[str, ...]
    policy_manifest: dict[str, object] | None = None
    eligibility_reasons: tuple[str, ...] = ()


def classify_task(*, role: str, changed_paths: Iterable[str] = (), prior_failures: int = 0,
                  explicit: str | None = None) -> str:
    """Classify from stable facts; an explicit valid operator class wins."""

    if explicit:
        if explicit not in TASK_CLASSES:
            raise ValueError(f"unknown task classification {explicit!r}")
        return explicit
    paths = tuple(path.strip().replace("\\", "/") for path in changed_paths if path.strip())
    if role in {"mechanical-testing", "focused-tests", "impact-search", "doc-drift-review"}:
        return MECHANICAL
    if prior_failures >= 2:
        return HARD_DEBUGGING
    high = any(
        path.startswith(("app/core/", "app/api/", "scripts/agents/", "scripts/ci/", ".github/"))
        or PurePosixPath(path).name in {"AGENTS.md", "pyproject.toml", "requirements.txt"}
        for path in paths
    )
    if high or role in {"architecture-review", "security-review"}:
        return ARCHITECTURE_HIGH_RISK
    if len(paths) > 12 or (any(p.startswith("frontend/") for p in paths) and any(p.startswith("app/") for p in paths)):
        return SERIOUS_INTEGRATION
    return ROUTINE


def classify_failure(message: str | None) -> str:
    text = (message or "").lower()
    if any(term in text for term in ("secret exposure", "authorization denied", "not authorized for this repository", "worktree lock", "unsafe worktree", "policy violation", "safety block")):
        return "safety-policy"
    if any(term in text for term in ("quota", "usage limit", "weekly limit", "credit exhausted")):
        return "quota"
    if any(term in text for term in ("rate limit", "rate_limit", "too many requests", "429")):
        return "rate-limit"
    if any(term in text for term in ("context window", "context length", "too many tokens")):
        return "context-limit"
    if any(term in text for term in (
        "not authenticated", "not_authenticated", "login required", "cli unavailable", "cli_missing", "command not found"
    )):
        return "auth-cli"
    if any(term in text for term in ("provider outage", "service unavailable", "temporarily unavailable", "connection refused")):
        return "provider-outage"
    if any(term in text for term in ("reasoning failure", "implementation failure", "incorrect after retry")):
        return "reasoning"
    return "capability"


def codex_allowed(*, policy: str, classification: str, auto_eligible: bool,
                  invocations: int, max_invocations: int, operator_override: bool = False) -> tuple[bool, str]:
    if policy not in CODEX_POLICIES:
        raise ValueError(f"unknown Codex policy {policy!r}")
    if operator_override:
        return True, "operator premium override"
    if not auto_eligible:
        return False, "runbook is not auto-eligible for Codex"
    if invocations >= max_invocations:
        return False, f"Codex invocation limit reached ({invocations}/{max_invocations})"
    if policy == CODEX_CONSERVE and classification not in {HARD_DEBUGGING, ARCHITECTURE_HIGH_RISK}:
        return False, "conserve policy reserves Codex for hard debugging or high-risk architecture"
    if policy == CODEX_BALANCED and classification == MECHANICAL:
        return False, "balanced policy excludes mechanical work"
    return True, f"{policy} policy permits this {classification} task"


def route_for_role(*, registry, provider_states: Mapping[str, object], role: str, classification: str,
                   codex_policy: str, codex_auto_eligible: bool, codex_invocations: int,
                   max_codex_invocations: int, operator_override: bool = False) -> RoutingDecision:
    """Choose the first routable role candidate after applying Codex policy."""

    candidates: list[str] = []
    blocked: list[str] = []
    for name in registry.route(role):
        worker = registry.get(name)
        state = provider_states.get(name)
        routable = bool(state and getattr(state, "configured", False) and getattr(state, "state", "") in {"AVAILABLE", "BUSY"})
        if not routable:
            blocked.append(f"{name}: unavailable")
            continue
        if worker.provider == "OpenAI" or name.startswith("codex"):
            allowed, why = codex_allowed(
                policy=codex_policy, classification=classification, auto_eligible=codex_auto_eligible,
                invocations=codex_invocations, max_invocations=max_codex_invocations,
                operator_override=operator_override,
            )
            if not allowed:
                blocked.append(f"{name}: {why}")
                continue
        candidates.append(name)
    if not candidates:
        return RoutingDecision(role, classification, None, None, None, None,
                               "; ".join(blocked) or "no configured route", (), False)
    selected = registry.get(candidates[0])
    return RoutingDecision(
        role, classification, selected.name, selected.provider, selected.default_model,
        selected.default_intensity, f"first eligible worker for role {role}", tuple(candidates[1:]),
        selected.provider == "OpenAI" or selected.name.startswith("codex"),
    )


def should_escalate(*, failure: str, attempts_at_tier: int, stronger_tier_available: bool) -> tuple[bool, str]:
    """Require evidence before capability escalation; never escalate quota/context failures."""

    if failure in NON_ESCALATING_FAILURES:
        return False, f"{failure} requires wait/compact/reroute, not a stronger model"
    if not stronger_tier_available:
        return False, "no stronger eligible tier"
    if attempts_at_tier < 1:
        return False, "current tier has not produced evidence of insufficiency"
    return True, "capability failure persisted after a bounded attempt"


def provider_diverse_review(implementation_provider: str | None, decisions: Sequence[RoutingDecision]) -> RoutingDecision | None:
    return next((decision for decision in decisions if decision.worker and decision.provider != implementation_provider), None)


def fallback_for_failure(
    *,
    root: Path,
    registry,
    provider_states: Mapping[str, object],
    route_role: str,
    failed_worker: str,
    failure_message: str,
    original_policy_manifest: dict[str, object],
    classification: str,
    codex_policy: str,
    codex_auto_eligible: bool,
    codex_invocations: int,
    max_codex_invocations: int,
    excluded_workers: Iterable[str] = (),
    permission_profile: str = "standard",
    worker_availability: Mapping[str, str] | None = None,
    allowed_workers: Iterable[str] | None = None,
) -> FallbackDecision:
    """Select an equivalent provider without changing engineering behavior."""

    failure = classify_failure(failure_message)
    if failure == "safety-policy":
        return FallbackDecision("BLOCKED", failure, "safety/policy failures cannot be bypassed by provider hopping", None, ())
    if failure == "context-limit":
        return FallbackDecision("MINIMIZE_CONTEXT", failure, "minimize bounded context before any provider fallback", None, ())
    if failure == "reasoning":
        return FallbackDecision("ESCALATION_REQUIRED", failure, "use ENG-AGENT-03 evidence-based escalation", None, ())
    if failure not in {"quota", "rate-limit", "auth-cli", "provider-outage"}:
        return FallbackDecision(
            "BLOCKED",
            failure,
            f"{failure} is not an automatic provider-fallback category",
            None,
            (),
        )

    excluded = frozenset(excluded_workers) | {failed_worker}
    considered: list[str] = []
    blocked: list[str] = []
    required_write = original_policy_manifest.get("read_write_mode") == "write"
    route = tuple(allowed_workers) if allowed_workers is not None else registry.route(route_role)
    for name in route:
        if name in excluded:
            continue
        worker = registry.get(name)
        considered.append(name)
        if route_role not in worker.roles:
            blocked.append(f"{name}: role {route_role} is not declared")
            continue
        if required_write and not worker.is_write_capable:
            blocked.append(f"{name}: replacement is not write-capable")
            continue
        if permission_profile != "standard" and not worker.supports_permission_profile(permission_profile):
            blocked.append(f"{name}: permission profile {permission_profile} is unsupported")
            continue
        availability = (worker_availability or {}).get(name)
        if availability is not None and availability != "AVAILABLE":
            blocked.append(f"{name}: {availability}")
            continue
        state = provider_states.get(name)
        if not worker.enabled:
            blocked.append(f"{name}: disabled by registry policy")
            continue
        if not state:
            blocked.append(f"{name}: provider state is unavailable")
            continue
        if not getattr(state, "configured", False):
            blocked.append(f"{name}: provider is not configured")
            continue
        if getattr(state, "state", "") not in {"AVAILABLE", "BUSY"}:
            blocked.append(f"{name}: provider state is {getattr(state, 'state', 'UNKNOWN')}")
            continue
        if worker.allow_api_billing:
            blocked.append(f"{name}: API billing is forbidden for automatic fallback")
            continue
        if not registry.repository_data_reuse_allowed(name):
            blocked.append(f"{name}: repository-data authorization does not permit automatic reuse")
            continue
        if worker.provider == "OpenAI" or name.startswith("codex"):
            allowed, why = codex_allowed(
                policy=codex_policy,
                classification=classification,
                auto_eligible=codex_auto_eligible,
                invocations=codex_invocations,
                max_invocations=max_codex_invocations,
            )
            if not allowed:
                blocked.append(f"{name}: {why}")
                continue
        try:
            replacement = compose_policy_bundle(
                root=root,
                registry=registry,
                worker_name=name,
                route_role=route_role,
                contracts=original_policy_manifest.get("task_contracts", ()),
                workflow=original_policy_manifest.get("workflow"),
                fallback_reason=f"{failure}: {failure_message}",
            ).manifest
            validate_policy_preservation(original_policy_manifest, replacement)
        except PolicyError as exc:
            blocked.append(f"{name}: policy preservation failed ({exc})")
            continue
        return FallbackDecision(
            "FALLBACK",
            failure,
            f"equivalent-role provider selected after {failure}",
            name,
            tuple(considered),
            replacement,
            tuple(blocked),
        )
    reason = "no eligible equivalent-role provider"
    if blocked:
        reason += ": " + "; ".join(blocked)
    return FallbackDecision("BLOCKED", failure, reason, None, tuple(considered), eligibility_reasons=tuple(blocked))


def build_context_manifest(*, paths: Iterable[str], excerpts: Mapping[str, str] | None = None,
                           test_output: str = "", max_excerpt_chars: int = 4000,
                           max_test_chars: int = 8000) -> dict[str, object]:
    """Describe bounded context without retaining secret contents."""

    safe_paths = sorted({p.replace("\\", "/") for p in paths if p and not _SECRET_PATH.search(p.replace("\\", "/"))})
    excerpt_meta = []
    for path, value in sorted((excerpts or {}).items()):
        normalized = path.replace("\\", "/")
        if normalized not in safe_paths or _SECRET_PATH.search(normalized):
            continue
        excerpt_meta.append({"path": normalized, "characters": min(len(value), max_excerpt_chars), "truncated": len(value) > max_excerpt_chars})
    bounded_test_chars = min(len(test_output), max_test_chars)
    return {
        "paths": safe_paths,
        "categories": ["task", "acceptance-criteria", "changed-paths", "bounded-excerpts", "test-output", "docs"],
        "excerpts": excerpt_meta,
        "test_output_characters": bounded_test_chars,
        "test_output_truncated": len(test_output) > max_test_chars,
        "secret_content_included": False,
    }


def new_usage_record(*, runbook_id: str, task_id: str | None, classification: str,
                     codex_policy: str, codex_auto_eligible: bool, max_codex_invocations: int,
                     context_manifest: dict[str, object] | None = None) -> dict[str, object]:
    if codex_policy not in CODEX_POLICIES:
        raise ValueError(f"unknown Codex policy {codex_policy!r}")
    if max_codex_invocations < 0:
        raise ValueError("max_codex_invocations must be non-negative")
    return {
        "runbook_id": runbook_id, "task_id": task_id, "classification": classification,
        "codex_policy": codex_policy, "codex_auto_eligible": codex_auto_eligible,
        "max_codex_invocations": max_codex_invocations, "codex_invocations": 0,
        "telemetry_quality": "unknown", "input_tokens": None, "output_tokens": None,
        "escalation_state": "none", "escalation_reason": None, "route_history": [],
        "escalation_history": [], "context_manifest": context_manifest or {},
    }
