"""Deterministic role/workflow/provider policy composition (ENG-AGENT-04).

This extends the ENG-AGENT-03 context-manifest seam.  It reads only explicit,
repository-relative policy/contracts and never invokes a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterable

from .registry import Registry, RegistryError

if TYPE_CHECKING:
    from .control_plane.project import ProjectContract

POLICY_MANIFEST_VERSION = 1

ROLE_ORCHESTRATOR = "ORCHESTRATOR"
ROLE_IMPLEMENTER = "IMPLEMENTER"
ROLE_TESTER = "TESTER"
ROLE_REVIEWER = "REVIEWER"
ROLE_RESEARCHER = "RESEARCHER"

WORKFLOW_IMPLEMENT = "IMPLEMENT"
WORKFLOW_TEST_AND_FIX = "TEST_AND_FIX"
WORKFLOW_REVIEW = "REVIEW"

_ROLE_POLICY = {
    ROLE_ORCHESTRATOR: ".agents/roles/ORCHESTRATOR.md",
    ROLE_IMPLEMENTER: ".agents/roles/IMPLEMENTER.md",
    ROLE_TESTER: ".agents/roles/TESTER.md",
    ROLE_REVIEWER: ".agents/roles/REVIEWER.md",
    ROLE_RESEARCHER: ".agents/roles/RESEARCHER.md",
}
_WORKFLOW_POLICY = {
    "IMPLEMENT": ".agents/workflows/IMPLEMENT.md",
    "TEST_AND_FIX": ".agents/workflows/TEST_AND_FIX.md",
    "REVIEW": ".agents/workflows/REVIEW.md",
    "PROVIDER_INTEGRATION": ".agents/workflows/PROVIDER_INTEGRATION.md",
    "UI_AUDIT": ".agents/workflows/UI_AUDIT.md",
}
_CORE_BY_ROLE = {
    ROLE_ORCHESTRATOR: (
        "SECURITY.md", "GIT_WORKTREES.md", "TESTING.md", "DOCUMENTATION.md", "COST_AND_PROVIDER_SAFETY.md"
    ),
    ROLE_IMPLEMENTER: (
        "SECURITY.md", "GIT_WORKTREES.md", "TESTING.md", "DOCUMENTATION.md", "COST_AND_PROVIDER_SAFETY.md"
    ),
    ROLE_TESTER: ("SECURITY.md", "TESTING.md", "COST_AND_PROVIDER_SAFETY.md"),
    ROLE_REVIEWER: ("SECURITY.md", "TESTING.md", "DOCUMENTATION.md", "COST_AND_PROVIDER_SAFETY.md"),
    ROLE_RESEARCHER: ("SECURITY.md", "COST_AND_PROVIDER_SAFETY.md"),
}
_ROUTE_CONTRACT = {
    "primary-implementation": (ROLE_IMPLEMENTER, WORKFLOW_IMPLEMENT),
    "secondary-implementation": (ROLE_IMPLEMENTER, WORKFLOW_IMPLEMENT),
    "overflow": (ROLE_IMPLEMENTER, WORKFLOW_IMPLEMENT),
    "mechanical-testing": (ROLE_TESTER, WORKFLOW_TEST_AND_FIX),
    "focused-tests": (ROLE_TESTER, WORKFLOW_TEST_AND_FIX),
    "diff-review": (ROLE_REVIEWER, WORKFLOW_REVIEW),
    "doc-drift-review": (ROLE_REVIEWER, WORKFLOW_REVIEW),
    "impact-search": (ROLE_RESEARCHER, None),
    # ENG-AO-02: explicit bot-enabled primary and its read-only, one-level investigation bots.
    "bot-implementation": (ROLE_IMPLEMENTER, WORKFLOW_IMPLEMENT),
    "bot-investigation": (ROLE_RESEARCHER, None),
}
_SENSITIVE_PARTS = frozenset({"data", ".env", "credentials", "secrets"})


class PolicyError(ValueError):
    """Policy composition failed closed."""


@dataclass(frozen=True)
class PolicyBundle:
    manifest: dict[str, object]
    prompt: str


def contract_for_route(route_role: str) -> tuple[str, str | None]:
    try:
        return _ROUTE_CONTRACT[route_role]
    except KeyError:
        raise PolicyError(f"route {route_role!r} has no canonical role/workflow mapping") from None


def _safe_repo_file(root: Path, raw: str) -> tuple[str, Path]:
    normalized = PurePosixPath(raw.replace("\\", "/")).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("../"):
        raise PolicyError(f"invalid policy/contract path {raw!r}")
    if any(part.lower() in _SENSITIVE_PARTS or part.lower().startswith(".env") for part in PurePosixPath(normalized).parts):
        raise PolicyError(f"sensitive policy/contract path is forbidden: {raw!r}")
    path = (root / normalized).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise PolicyError(f"required policy/contract does not exist: {normalized}")
    return normalized, path


def _compose_declared_project_policy(
    *,
    root: Path,
    project: "ProjectContract",
    worker: object,
    registry: Registry,
    worker_name: str,
    route_role: str,
    contracts: Iterable[str],
    workflow: str | None,
    fallback_reason: str | None,
    acceptance_criteria: str,
) -> PolicyBundle:
    """Compose a bundle for a project that does not use AGENTS.md.

    Selected-repository policy (declared entrypoints) takes precedence.
    Role/workflow/provider adapter files come from the Control Plane
    checkout because they are orchestration policy, not a substitute for
    the project's missing AGENTS.md.
    """

    from .control_plane.policy_loader import (
        load_project_policy,
        orchestration_policy_root,
    )

    loaded = load_project_policy(project, root=root)
    role, default_workflow = contract_for_route(route_role)
    resolved_workflow = workflow or default_workflow
    if role not in worker.allowed_policy_roles:  # type: ignore[attr-defined]
        raise PolicyError(f"worker {worker_name!r} does not permit canonical role {role!r}")
    if not worker.provider_policy:  # type: ignore[attr-defined]
        raise PolicyError(f"worker {worker_name!r} has no provider policy")

    controller = orchestration_policy_root()
    core = [f".agents/core/{name}" for name in _CORE_BY_ROLE[role]]
    core_files = [_safe_repo_file(controller, item) for item in core]
    role_item = _safe_repo_file(controller, _ROLE_POLICY[role])
    workflow_item = _safe_repo_file(controller, _WORKFLOW_POLICY[resolved_workflow]) if resolved_workflow else None
    provider_item = _safe_repo_file(controller, worker.provider_policy)  # type: ignore[attr-defined]
    contract_items = [_safe_repo_file(root, item) for item in contracts]

    rendered: list[str] = []
    approximate_characters = 0
    policy_files: list[str] = []
    for relative, text in loaded.texts.items():
        approximate_characters += len(text)
        policy_files.append(relative)
        rendered.append(f"\n--- POLICY: {relative} ---\n{text.rstrip()}\n")
    ordered = [*core_files, role_item]
    if workflow_item:
        ordered.append(workflow_item)
    ordered.append(provider_item)
    ordered.extend(contract_items)
    for relative, path in ordered:
        value = path.read_text(encoding="utf-8")
        approximate_characters += len(value)
        policy_files.append(relative)
        rendered.append(f"\n--- POLICY: {relative} ---\n{value.rstrip()}\n")
    if acceptance_criteria.strip():
        approximate_characters += len(acceptance_criteria)
        rendered.append(f"\n--- BOUNDED ACCEPTANCE CRITERIA ---\n{acceptance_criteria.strip()}\n")

    first_declared = next(iter(loaded.texts))
    manifest: dict[str, object] = {
        "manifest_version": POLICY_MANIFEST_VERSION,
        "universal_policy": {"path": first_declared, "version": POLICY_MANIFEST_VERSION},
        "core_policies": [item[0] for item in core_files],
        "role": role,
        "role_policy": role_item[0],
        "workflow": resolved_workflow,
        "workflow_policy": workflow_item[0] if workflow_item else None,
        "provider": worker.provider,  # type: ignore[attr-defined]
        "provider_policy": provider_item[0],
        "worker": worker.name,  # type: ignore[attr-defined]
        "actual_model": worker.default_model,  # type: ignore[attr-defined]
        "actual_intensity": worker.default_intensity,  # type: ignore[attr-defined]
        "task_contracts": [item[0] for item in contract_items],
        "route_role": route_role,
        "effective_capability": worker.capability,  # type: ignore[attr-defined]
        "read_write_mode": "read-only" if worker.is_read_only else "write",  # type: ignore[attr-defined]
        "approximate_context_characters": approximate_characters,
        "excluded_categories": ["secrets", "unrelated-repository-files", "provider-native-auto-discovery"],
        "fallback_reason": fallback_reason,
        "api_billing_enabled": worker.allow_api_billing,  # type: ignore[attr-defined]
        "repository_data_authorization": registry.effective_repository_data_authorization(worker_name),
        "repository_data_authorization_source": (
            "provider-level"
            if worker.provider in registry.provider_repository_data_authorizations  # type: ignore[attr-defined]
            else "worker-level"
        ),
        "root_claude_bootstrap_loaded": False,
        "policy_source": "selected-project-declared",
        "project_id": project.project_id,
        "project_policy_entrypoints": list(loaded.entrypoints),
    }
    identity = {
        key: manifest[key]
        for key in (
            "universal_policy",
            "core_policies",
            "role",
            "role_policy",
            "workflow",
            "workflow_policy",
            "task_contracts",
            "read_write_mode",
        )
    }
    manifest["preserved_policy_identity"] = identity
    header = (
        "EXPLICIT ORCHESTRATED POLICY BUNDLE\n"
        f"ROLE: {role}\nWORKFLOW: {resolved_workflow or 'none'}\nPROVIDER: {worker.provider}\n"  # type: ignore[attr-defined]
        f"POLICY FILES: {', '.join(policy_files)}\n"
        f"CAPABILITY: {worker.capability}\nREAD/WRITE MODE: {manifest['read_write_mode']}\n"  # type: ignore[attr-defined]
        f"FALLBACK/ESCALATION REASON: {fallback_reason or 'none'}\n"
    )
    return PolicyBundle(manifest=manifest, prompt=header + "".join(rendered))


def compose_policy_bundle(
    *,
    root: Path,
    registry: Registry,
    worker_name: str,
    route_role: str,
    contracts: Iterable[str] = (),
    workflow: str | None = None,
    fallback_reason: str | None = None,
    acceptance_criteria: str = "",
    project: "ProjectContract | None" = None,
) -> PolicyBundle:
    """Compose and describe one explicit worker instruction bundle.

    ENG-CP-04 (issue #169): when ``project`` is supplied, declared
    ``policy_entrypoints`` are loaded from ``root`` (the selected project's
    candidate/worktree) and missing/invalid files fail closed. Generic
    composition does not assume ``AGENTS.md``. The recovered-worktree
    fallback onto the Control Plane checkout is OctaScene compatibility
    only (``policy_missing_worktree_fallback=controller-checkout``) or the
    legacy no-project path.
    """

    worker = registry.get(worker_name)
    if project is not None:
        from .control_plane.policy_loader import PolicyLoadError, load_project_policy

        try:
            load_project_policy(project, root=root)
        except PolicyLoadError as exc:
            if project.capabilities.get("policy_missing_worktree_fallback") != "controller-checkout":
                raise PolicyError(str(exc)) from exc
            # OctaScene recovered/stacked worktree: declared AGENTS.md may be
            # absent on an older candidate. Generic projects never take this
            # path -- they fail closed above.

    policy_root = root
    allow_controller_fallback = project is None or (
        project.capabilities.get("policy_missing_worktree_fallback") == "controller-checkout"
    )
    if not (policy_root / "AGENTS.md").is_file() or not (policy_root / ".agents/README.md").is_file():
        if project is not None and "AGENTS.md" not in project.policy_entrypoints:
            return _compose_declared_project_policy(
                root=root,
                project=project,
                worker=worker,
                registry=registry,
                worker_name=worker_name,
                route_role=route_role,
                contracts=contracts,
                workflow=workflow,
                fallback_reason=fallback_reason,
                acceptance_criteria=acceptance_criteria,
            )
        if not allow_controller_fallback:
            raise PolicyError(
                f"project {project.project_id!r} declared AGENTS.md/.agents policy is missing at {root}"
            )
        # A recovered/stacked target worktree may predate ENG-AGENT-04. The
        # controller still injects its own current canonical policy explicitly,
        # just as Supervisor executes its current wrapper from the controller
        # checkout while targeting the older worktree via --repo-root.
        # ENG-CP-04: this fallback is not used for an arbitrary second
        # repository that simply has no AGENTS.md.
        policy_root = Path(__file__).resolve().parents[2]
    role, default_workflow = contract_for_route(route_role)
    resolved_workflow = workflow or default_workflow
    if role not in worker.allowed_policy_roles:
        raise PolicyError(f"worker {worker_name!r} does not permit canonical role {role!r}")
    if not worker.provider_policy:
        raise PolicyError(f"worker {worker_name!r} has no provider policy")

    universal, universal_path = _safe_repo_file(policy_root, "AGENTS.md")
    core = [f".agents/core/{name}" for name in _CORE_BY_ROLE[role]]
    core_files = [_safe_repo_file(policy_root, item) for item in core]
    role_item = _safe_repo_file(policy_root, _ROLE_POLICY[role])
    workflow_item = _safe_repo_file(policy_root, _WORKFLOW_POLICY[resolved_workflow]) if resolved_workflow else None
    provider_item = _safe_repo_file(policy_root, worker.provider_policy)
    contract_items = [_safe_repo_file(root, item) for item in contracts]

    ordered = [(universal, universal_path), *core_files, role_item]
    if workflow_item:
        ordered.append(workflow_item)
    ordered.append(provider_item)
    ordered.extend(contract_items)
    rendered = []
    approximate_characters = 0
    for relative, path in ordered:
        value = path.read_text(encoding="utf-8")
        approximate_characters += len(value)
        rendered.append(f"\n--- POLICY: {relative} ---\n{value.rstrip()}\n")
    if acceptance_criteria.strip():
        approximate_characters += len(acceptance_criteria)
        rendered.append(f"\n--- BOUNDED ACCEPTANCE CRITERIA ---\n{acceptance_criteria.strip()}\n")

    manifest: dict[str, object] = {
        "manifest_version": POLICY_MANIFEST_VERSION,
        "universal_policy": {"path": universal, "version": POLICY_MANIFEST_VERSION},
        "core_policies": [item[0] for item in core_files],
        "role": role,
        "role_policy": role_item[0],
        "workflow": resolved_workflow,
        "workflow_policy": workflow_item[0] if workflow_item else None,
        "provider": worker.provider,
        "provider_policy": provider_item[0],
        "worker": worker.name,
        "actual_model": worker.default_model,
        "actual_intensity": worker.default_intensity,
        "task_contracts": [item[0] for item in contract_items],
        "route_role": route_role,
        "effective_capability": worker.capability,
        "read_write_mode": "read-only" if worker.is_read_only else "write",
        "approximate_context_characters": approximate_characters,
        "excluded_categories": ["secrets", "unrelated-repository-files", "provider-native-auto-discovery"],
        "fallback_reason": fallback_reason,
        "api_billing_enabled": worker.allow_api_billing,
        "repository_data_authorization": registry.effective_repository_data_authorization(worker.name),
        "repository_data_authorization_source": (
            "provider-level"
            if worker.provider in registry.provider_repository_data_authorizations
            else "worker-level"
        ),
        "root_claude_bootstrap_loaded": False,
        "policy_source": "target-worktree" if policy_root.resolve() == root.resolve() else "controller-checkout",
    }
    identity = {
        key: manifest[key]
        for key in ("universal_policy", "core_policies", "role", "role_policy", "workflow", "workflow_policy", "task_contracts", "read_write_mode")
    }
    manifest["preserved_policy_identity"] = identity
    header = (
        "EXPLICIT ORCHESTRATED POLICY BUNDLE\n"
        f"ROLE: {role}\nWORKFLOW: {resolved_workflow or 'none'}\nPROVIDER: {worker.provider}\n"
        f"POLICY FILES: {', '.join(item[0] for item in ordered)}\n"
        f"CAPABILITY: {worker.capability}\nREAD/WRITE MODE: {manifest['read_write_mode']}\n"
        f"FALLBACK/ESCALATION REASON: {fallback_reason or 'none'}\n"
    )
    return PolicyBundle(manifest=manifest, prompt=header + "".join(rendered))


def validate_policy_preservation(original: dict[str, object], replacement: dict[str, object]) -> None:
    """Reject a replacement that changes role/workflow/core/contracts/access."""

    if original.get("preserved_policy_identity") != replacement.get("preserved_policy_identity"):
        raise PolicyError("provider fallback changed the preserved role/workflow/core/task capability bundle")
    if replacement.get("api_billing_enabled"):
        raise PolicyError("provider fallback would enable API billing")


def validate_registry_policies(root: Path, registry: Registry) -> None:
    for worker in registry.workers.values():
        _safe_repo_file(root, worker.provider_policy)
        if worker.is_read_only and ({ROLE_IMPLEMENTER, ROLE_ORCHESTRATOR} & set(worker.allowed_policy_roles)):
            raise RegistryError(f"read-only worker {worker.name!r} claims a write-capable canonical role")
        if worker.is_write_capable and not worker.requires_isolated_worktree:
            raise RegistryError(f"write-capable worker {worker.name!r} must require worktree isolation")
        if worker.allow_api_billing:
            raise RegistryError(f"worker {worker.name!r} may not silently enable API billing")
