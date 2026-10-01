"""Compact, policy-preserving incremental task context (ENG-PC-06).

The mandatory policy bundle is always composed by :mod:`scripts.agents.policy`
and is never subject to this cursor. The cursor stores identities and positions,
not copies of repository policy, roadmap, task, or event content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from ..policy import PolicyBundle, validate_policy_preservation
from .models import Event, Task
from .state import State

OUTCOME_DELIVERED = "DELIVERED"
OUTCOME_SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"

REASON_TREE_CHANGED = "tree changed"
REASON_POLICY_DIGEST_CHANGED = "policy digest changed"
REASON_TASK_CONTRACT_CHANGED = "task contract digest changed"
REASON_PRESERVED_POLICY_CHANGED = "preserved policy identity changed"

DEFAULT_MAX_INCREMENTAL_CHARACTERS = 24_000
MAX_INCREMENTAL_CHARACTERS = 32_000
DEFAULT_ANCESTRY_LIMIT = 12
MAX_ANCESTRY_LIMIT = 32
MAX_EVENT_READ = 1_000


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextIdentity:
    """Component-wise invalidation identity for one task context."""

    project_id: str
    task_id: str
    consumer_id: str
    tree_sha: str
    policy_digest: str
    task_contract_digest: str

    @property
    def fingerprint(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class AncestryRequest:
    """Authoritative references that explain why the selected task exists."""

    parent_program: str | None = None
    parent_task: str | None = None
    source_references: tuple[str, ...] = ()
    limit: int = DEFAULT_ANCESTRY_LIMIT


@dataclass(frozen=True)
class ContextCursor:
    """Durable record of delivered identities and the event timeline position."""

    identity: ContextIdentity
    bundle_identity: Mapping[str, Any]
    preserved_policy_identity: Mapping[str, Any]
    delivered_task_identities: Mapping[str, str]
    delivered_ancestry_identity: str
    last_event_id: int
    last_outcome: str
    last_attempted_characters: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ContextCursor:
        identity = ContextIdentity(
            project_id=str(row["project_id"]),
            task_id=str(row["task_id"]),
            consumer_id=str(row["consumer_id"]),
            tree_sha=str(row["tree_sha"]),
            policy_digest=str(row["policy_digest"]),
            task_contract_digest=str(row["task_contract_digest"]),
        )
        return cls(
            identity=identity,
            bundle_identity=dict(row["bundle_identity"]),
            preserved_policy_identity=dict(row["preserved_policy_identity"]),
            delivered_task_identities=dict(row["delivered_task_identities"]),
            delivered_ancestry_identity=str(row["delivered_ancestry_identity"]),
            last_event_id=int(row["last_event_id"]),
            last_outcome=str(row["last_outcome"]),
            last_attempted_characters=int(row["last_attempted_characters"]),
        )


@dataclass(frozen=True)
class ContextDelivery:
    prompt: str
    manifest: Mapping[str, Any]
    bundle_identity: str
    outcome: str
    invalidation_reason: str | None
    incremental_characters: int
    required_characters: int
    ancestry_truncated: bool
    graphify_supplied: bool
    cursor: ContextCursor


_COMPONENT_REASONS: tuple[tuple[str, str], ...] = (
    ("tree_sha", REASON_TREE_CHANGED),
    ("policy_digest", REASON_POLICY_DIGEST_CHANGED),
    ("task_contract_digest", REASON_TASK_CONTRACT_CHANGED),
)


def _invalidation_reason(
    cursor: ContextCursor | None,
    current: ContextIdentity,
    preserved_policy_identity: Mapping[str, Any],
) -> str | None:
    if cursor is None:
        return None
    for field, reason in _COMPONENT_REASONS:
        old = getattr(cursor.identity, field)
        new = getattr(current, field)
        if old != new:
            return f"{reason}: stored={old!r}, current={new!r}"
    if dict(cursor.preserved_policy_identity) != dict(preserved_policy_identity):
        return REASON_PRESERVED_POLICY_CHANGED
    return None


def _task_identity(task: Task) -> str:
    return _digest(
        {
            "id": task.id,
            "task_ref": task.task_ref,
            "state": task.state,
            "dependencies": list(task.dependencies),
            "updated_at": task.updated_at,
        }
    )


def _task_context(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "task_ref": task.task_ref,
        "state": task.state,
        "dependencies": list(task.dependencies),
        "updated_at": task.updated_at,
    }


def _bounded_ancestry(
    tasks: Mapping[str, Task], task_id: str, request: AncestryRequest
) -> tuple[list[dict[str, str]], bool, set[str]]:
    if not 1 <= request.limit <= MAX_ANCESTRY_LIMIT:
        raise ValueError(f"ancestry limit must be between 1 and {MAX_ANCESTRY_LIMIT}")

    candidates: list[dict[str, str]] = []
    if request.parent_program:
        candidates.append({"kind": "parent_program", "reference": request.parent_program})
    if request.parent_task:
        candidates.append({"kind": "parent_task", "reference": request.parent_task})
    candidates.extend({"kind": "source", "reference": ref} for ref in request.source_references)

    relevant = {task_id}
    queue = list(tasks.get(task_id).dependencies if task_id in tasks else ())
    visited: set[str] = set()
    while queue and len(candidates) <= request.limit:
        dependency_id = queue.pop(0)
        if dependency_id in visited:
            continue
        visited.add(dependency_id)
        dependency = tasks.get(dependency_id)
        if dependency is None:
            candidates.append({"kind": "dependency_missing", "reference": dependency_id})
            continue
        relevant.add(dependency.id)
        candidates.append({"kind": "dependency", "reference": dependency.task_ref})
        queue.extend(dependency.dependencies)

    truncated = len(candidates) > request.limit or bool(queue)
    return candidates[: request.limit], truncated, relevant


def _event_context(event: Event) -> dict[str, object]:
    return {
        "event_id": event.id,
        "run_id": event.run_id,
        "run_sequence": event.run_sequence,
        "class": event.event_class or event.category,
        "type": event.event_type,
        "source": event.source,
        "provenance": event.provenance or "UNKNOWN",
        "task_id": event.task_id,
        "level": event.level,
        "message": event.message,
        "data": event.data,
        "evidence": event.evidence,
    }


def _graph_payload(supplier: Callable[[], object] | None) -> tuple[dict[str, object], str]:
    if supplier is None:
        return {"status": "ABSENT", "supplied": False}, ""
    try:
        supplied = supplier()
    except Exception as exc:  # noqa: BLE001 - advisory context must fail safe
        return {
            "status": "FAILED_SAFE",
            "supplied": False,
            "reason": f"Graphify supplier failed safely: {type(exc).__name__}",
        }, ""
    evidence = getattr(supplied, "evidence", None)
    text = getattr(supplied, "text", "")
    if isinstance(supplied, Mapping):
        evidence = supplied.get("evidence", evidence)
        text = supplied.get("text", text)
    safe_evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    return {
        "status": str(safe_evidence.get("status", "SUPPLIED" if text else "ABSENT")),
        "supplied": bool(text),
        "evidence": safe_evidence,
        "authority": "ADVISORY_BELOW_SOURCE_POLICY_AND_CONTRACTS",
    }, str(text or "")


def _cursor_row(
    *,
    identity: ContextIdentity,
    bundle_identity: Mapping[str, Any],
    preserved_policy_identity: Mapping[str, Any],
    task_identities: Mapping[str, str],
    ancestry_identity: str,
    last_event_id: int,
    outcome: str,
    attempted_characters: int,
) -> dict[str, object]:
    return {
        **asdict(identity),
        "identity_fingerprint": identity.fingerprint,
        "bundle_identity": dict(bundle_identity),
        "preserved_policy_identity": dict(preserved_policy_identity),
        "delivered_task_identities": dict(task_identities),
        "delivered_ancestry_identity": ancestry_identity,
        "last_event_id": last_event_id,
        "last_outcome": outcome,
        "last_attempted_characters": attempted_characters,
    }


def build_incremental_context(
    state: State,
    *,
    identity: ContextIdentity,
    policy_bundle: PolicyBundle,
    ancestry: AncestryRequest = AncestryRequest(),
    max_incremental_characters: int = DEFAULT_MAX_INCREMENTAL_CHARACTERS,
    graph_context_supplier: Callable[[], object] | None = None,
) -> ContextDelivery:
    """Compose mandatory policy plus bounded changes since the durable cursor."""

    if not 256 <= max_incremental_characters <= MAX_INCREMENTAL_CHARACTERS:
        raise ValueError(
            f"incremental context limit must be between 256 and {MAX_INCREMENTAL_CHARACTERS}"
        )
    preserved = policy_bundle.manifest.get("preserved_policy_identity")
    if not isinstance(preserved, Mapping):
        raise ValueError("policy bundle has no preserved_policy_identity")

    stored_row = state.get_context_cursor(
        project_id=identity.project_id,
        task_id=identity.task_id,
        consumer_id=identity.consumer_id,
    )
    stored = ContextCursor.from_row(stored_row) if stored_row else None
    invalidation = _invalidation_reason(stored, identity, preserved)
    reset = invalidation is not None

    tasks = {task.id: task for task in state.list_tasks(project_id=identity.project_id)}
    if identity.task_id not in tasks:
        raise ValueError(
            f"task {identity.task_id!r} does not belong to project {identity.project_id!r}"
        )
    ancestry_nodes, ancestry_truncated, relevant_task_ids = _bounded_ancestry(
        tasks, identity.task_id, ancestry
    )
    ancestry_record = {
        "nodes": ancestry_nodes,
        "limit": ancestry.limit,
        "truncated": ancestry_truncated,
        "truncation_reason": "explicit ancestry node limit reached" if ancestry_truncated else None,
    }
    ancestry_identity = _digest(ancestry_record)

    previous_tasks = {} if reset or stored is None else stored.delivered_task_identities
    current_task_identities = {
        task_id: _task_identity(tasks[task_id])
        for task_id in sorted(relevant_task_ids)
        if task_id in tasks
    }
    changed_tasks = [
        _task_context(tasks[task_id])
        for task_id, digest in current_task_identities.items()
        if previous_tasks.get(task_id) != digest
    ]

    previous_event_id = 0 if reset or stored is None else stored.last_event_id
    events = [
        event
        for event in state.list_run_events(
            project_id=identity.project_id,
            after_id=previous_event_id,
            task_ids=sorted(relevant_task_ids),
            limit=MAX_EVENT_READ,
            ascending=True,
        )
        if event.id is not None
    ]
    last_event_id = max((int(event.id) for event in events if event.id is not None), default=previous_event_id)
    include_ancestry = reset or stored is None or stored.delivered_ancestry_identity != ancestry_identity

    authoritative = {
        "invalidation_reason": invalidation,
        "task_changes": changed_tasks,
        "events": [_event_context(event) for event in events],
        "ancestry": ancestry_record if include_ancestry else None,
    }
    graph_record, graph_text = _graph_payload(graph_context_supplier)
    payload: dict[str, object] = {
        "schema": 1,
        "authority": "INCREMENTAL_BESIDE_MANDATORY_POLICY",
        "authoritative": authoritative,
        "graphify": graph_record,
    }
    if graph_text:
        payload["graphify_text"] = graph_text

    rendered = "\n--- INCREMENTAL TASK CONTEXT ---\n" + _canonical(payload) + "\n"
    if len(rendered) > max_incremental_characters and graph_text:
        payload.pop("graphify_text")
        payload["graphify"] = {
            **graph_record,
            "supplied": False,
            "status": "NOT_SUPPLIED_SIZE_BOUND",
            "reason": "advisory Graphify context would exceed the incremental size bound",
        }
        rendered = "\n--- INCREMENTAL TASK CONTEXT ---\n" + _canonical(payload) + "\n"

    required = len(rendered)
    base_bundle_identity = {
        "context_identity": asdict(identity),
        "preserved_policy_identity_digest": _digest(dict(preserved)),
        "task_identities": current_task_identities,
        "ancestry_identity": ancestry_identity,
        "event_references": [event.id for event in events],
        "incremental_payload_digest": _digest(payload),
    }
    bundle_digest = _digest(base_bundle_identity)
    delivery_manifest = dict(policy_bundle.manifest)
    delivery_manifest["context_cursor"] = {
        "identity_fingerprint": identity.fingerprint,
        "bundle_identity": bundle_digest,
        "invalidation_reason": invalidation,
        "max_incremental_characters": max_incremental_characters,
    }
    # The cursor is prohibited from changing the mandatory bundle. Reuse the
    # exact fallback preservation rule instead of introducing a weaker copy.
    validate_policy_preservation(policy_bundle.manifest, delivery_manifest)

    if required > max_incremental_characters:
        outcome = OUTCOME_SIZE_LIMIT_EXCEEDED
        marker = {
            "outcome": outcome,
            "required_characters": required,
            "max_incremental_characters": max_incremental_characters,
            "authoritative_changes_delivered": False,
        }
        rendered = "\n--- INCREMENTAL TASK CONTEXT OUTCOME ---\n" + _canonical(marker) + "\n"
        retained_tasks = {} if reset or stored is None else stored.delivered_task_identities
        retained_ancestry = "" if reset or stored is None else stored.delivered_ancestry_identity
        retained_event = 0 if reset or stored is None else stored.last_event_id
        stored_bundle = base_bundle_identity
    else:
        outcome = OUTCOME_DELIVERED
        retained_tasks = current_task_identities
        retained_ancestry = ancestry_identity
        retained_event = last_event_id
        stored_bundle = base_bundle_identity

    row = state.upsert_context_cursor(
        _cursor_row(
            identity=identity,
            bundle_identity=stored_bundle,
            preserved_policy_identity=preserved,
            task_identities=retained_tasks,
            ancestry_identity=retained_ancestry,
            last_event_id=retained_event,
            outcome=outcome,
            attempted_characters=required,
        )
    )
    cursor = ContextCursor.from_row(row)
    graph_supplied = bool(payload["graphify"].get("supplied"))  # type: ignore[union-attr]
    return ContextDelivery(
        prompt=policy_bundle.prompt + rendered,
        manifest=delivery_manifest,
        bundle_identity=bundle_digest,
        outcome=outcome,
        invalidation_reason=invalidation,
        incremental_characters=len(rendered),
        required_characters=required,
        ancestry_truncated=ancestry_truncated,
        graphify_supplied=graph_supplied,
        cursor=cursor,
    )
