"""ENG-PC-06 compact incremental context and bounded ancestry acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.agents.control_plane import context_cursor
from scripts.agents.control_plane.context_cursor import (
    OUTCOME_DELIVERED,
    OUTCOME_SIZE_LIMIT_EXCEEDED,
    REASON_POLICY_DIGEST_CHANGED,
    REASON_PRESERVED_POLICY_CHANGED,
    REASON_TASK_CONTRACT_CHANGED,
    REASON_TREE_CHANGED,
    AncestryRequest,
    ContextIdentity,
    build_incremental_context,
)
from scripts.agents.control_plane.models import Task
from scripts.agents.control_plane.run_events import RunEvent
from scripts.agents.control_plane.state import State
from scripts.agents.policy import PolicyBundle

MANDATORY_PROMPT = "MANDATORY AGENTS/core/role/workflow/provider/task contracts\n"


def _policy(*, workflow: str = "IMPLEMENT") -> PolicyBundle:
    preserved = {
        "universal_policy": {"path": "AGENTS.md", "version": 1},
        "core_policies": [".agents/core/SECURITY.md"],
        "role": "IMPLEMENTER",
        "role_policy": ".agents/roles/IMPLEMENTER.md",
        "workflow": workflow,
        "workflow_policy": ".agents/workflows/IMPLEMENT.md",
        "task_contracts": ["docs/ROADMAP.md"],
        "read_write_mode": "write",
    }
    return PolicyBundle(
        manifest={"preserved_policy_identity": preserved, "api_billing_enabled": False},
        prompt=MANDATORY_PROMPT,
    )


def _identity(project_id: str = "project-a", **changes: str) -> ContextIdentity:
    values = {
        "project_id": project_id,
        "task_id": "task-a",
        "consumer_id": "worker-1",
        "tree_sha": "a" * 40,
        "policy_digest": "policy-v1",
        "task_contract_digest": "contract-v1",
    }
    values.update(changes)
    return ContextIdentity(**values)


def _seed(state: State, *, project_id: str = "project-a", prefix: str = "A") -> None:
    state.upsert_task(
        Task(
            id=f"task-{prefix.lower()}",
            task_ref=f"{prefix}-TASK",
            role="implementation",
            worker="fixture",
            project_id=project_id,
            dependencies=(f"dep-{prefix.lower()}",),
        ),
        updated_at="2026-01-01T00:00:02+00:00",
    )
    state.upsert_task(
        Task(
            id=f"dep-{prefix.lower()}",
            task_ref=f"{prefix}-DEPENDENCY",
            role="implementation",
            worker="fixture",
            project_id=project_id,
        ),
        updated_at="2026-01-01T00:00:01+00:00",
    )


def _record(state: State, *, project_id: str, task_id: str, message: str) -> None:
    state.record_run_event(
        RunEvent(
            run_id=f"run-{project_id}",
            event_class="review",
            event_type="review.result",
            source="test.context",
            provenance="MEASURED",
            message=message,
            project_id=project_id,
            task_id=task_id,
        )
    )


def _incremental(prompt: str) -> dict[str, object]:
    marker = "--- INCREMENTAL TASK CONTEXT ---\n"
    return json.loads(prompt.split(marker, 1)[1])


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("tree_sha", "b" * 40, REASON_TREE_CHANGED),
        ("policy_digest", "policy-v2", REASON_POLICY_DIGEST_CHANGED),
        ("task_contract_digest", "contract-v2", REASON_TASK_CONTRACT_CHANGED),
    ],
)
def test_stale_cursor_names_the_specific_changed_component(field: str, value: str, reason: str) -> None:
    state = State(":memory:")
    _seed(state)
    build_incremental_context(state, identity=_identity(), policy_bundle=_policy())

    changed = build_incremental_context(
        state,
        identity=_identity(**{field: value}),
        policy_bundle=_policy(),
    )

    assert changed.invalidation_reason is not None
    assert changed.invalidation_reason == reason


def test_changed_preserved_policy_identity_has_its_own_reason() -> None:
    state = State(":memory:")
    _seed(state)
    build_incremental_context(state, identity=_identity(), policy_bundle=_policy())

    changed = build_incremental_context(
        state, identity=_identity(), policy_bundle=_policy(workflow="TEST_AND_FIX")
    )

    assert changed.invalidation_reason == REASON_PRESERVED_POLICY_CHANGED


def test_cursor_never_omits_or_changes_mandatory_policy() -> None:
    state = State(":memory:")
    _seed(state)
    original = _policy()
    first = build_incremental_context(state, identity=_identity(), policy_bundle=original)
    second = build_incremental_context(state, identity=_identity(), policy_bundle=original)

    assert first.prompt.startswith(MANDATORY_PROMPT)
    assert second.prompt.startswith(MANDATORY_PROMPT)
    assert first.manifest["preserved_policy_identity"] == original.manifest["preserved_policy_identity"]
    assert second.manifest["preserved_policy_identity"] == original.manifest["preserved_policy_identity"]
    assert _incremental(second.prompt)["authoritative"] == {
        "ancestry": None,
        "event_window": {"limit": 1000, "truncated": False, "truncation_reason": None},
        "events": [],
        "evicted_event_positions": [],
        "invalidation_reason": None,
        "newly_relevant_tasks": [],
        "task_changes": [],
    }


def test_cursor_persists_identities_and_event_references_not_delivered_content() -> None:
    state = State(":memory:")
    _seed(state)
    _record(state, project_id="project-a", task_id="task-a", message="CONTENT_MUST_NOT_BE_CACHED")

    delivery = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    stored = state.get_context_cursor(
        project_id="project-a", task_id="task-a", consumer_id="worker-1"
    )
    rendered_cursor = json.dumps(stored, sort_keys=True)

    assert delivery.outcome == OUTCOME_DELIVERED
    assert "CONTENT_MUST_NOT_BE_CACHED" not in rendered_cursor
    assert "A-TASK" not in rendered_cursor
    assert "incremental_payload_digest" in rendered_cursor
    assert stored["bundle_identity"]["event_references"] == [1]


def test_bundle_identity_is_deterministic_across_state_instances(tmp_path: Path) -> None:
    identities: list[str] = []
    for name in ("one.db", "two.db"):
        with State(tmp_path / name) as state:
            _seed(state)
            _record(state, project_id="project-a", task_id="task-a", message="review passed")
            delivery = build_incremental_context(
                state,
                identity=_identity(),
                policy_bundle=_policy(),
                ancestry=AncestryRequest(
                    parent_program="ENG-PC", source_references=("docs/ROADMAP.md#ENG-PC-06",)
                ),
            )
            identities.append(delivery.bundle_identity)

    assert identities[0] == identities[1]


def test_size_bound_is_enforced_and_failure_is_durable_and_truthful() -> None:
    state = State(":memory:")
    _seed(state)
    _record(state, project_id="project-a", task_id="task-a", message="x" * 2_000)

    delivery = build_incremental_context(
        state,
        identity=_identity(),
        policy_bundle=_policy(),
        max_incremental_characters=256,
    )

    assert delivery.outcome == OUTCOME_SIZE_LIMIT_EXCEEDED
    assert delivery.required_characters > 256
    assert delivery.incremental_characters <= 256
    assert "authoritative_changes_delivered\":false" in delivery.prompt
    assert delivery.cursor.last_outcome == OUTCOME_SIZE_LIMIT_EXCEEDED
    assert delivery.cursor.last_event_id == 0
    assert delivery.cursor.delivered_event_positions == {}
    assert delivery.cursor.bundle_identity["event_references"] == []
    assert delivery.cursor.inspection_summary["task_additions"] == 0
    assert delivery.cursor.inspection_summary["event_additions"] == 0
    assert delivery.cursor.inspection_summary["ancestry_additions"] == 0

    retried = build_incremental_context(
        state,
        identity=_identity(),
        policy_bundle=_policy(),
    )
    assert _incremental(retried.prompt)["authoritative"]["events"][0]["message"] == "x" * 2_000
    assert retried.cursor.bundle_identity["event_references"] == [1]


def test_newly_relevant_task_receives_earlier_event_once_and_then_advances() -> None:
    state = State(":memory:")
    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a",
    ))
    state.upsert_task(Task(
        id="dep-a", task_ref="A-DEPENDENCY", role="implementation", worker="fixture",
        project_id="project-a",
    ))
    _record(state, project_id="project-a", task_id="dep-a", message="EARLIER DEPENDENCY HISTORY")
    _record(state, project_id="project-a", task_id="task-a", message="NEWER PRIMARY HISTORY")

    first = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    assert [event["message"] for event in _incremental(first.prompt)["authoritative"]["events"]] == [
        "NEWER PRIMARY HISTORY"
    ]

    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a", dependencies=("dep-a",),
    ))
    second = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    second_authoritative = _incremental(second.prompt)["authoritative"]
    assert [event["message"] for event in second_authoritative["events"]] == [
        "EARLIER DEPENDENCY HISTORY"
    ]
    assert second_authoritative["newly_relevant_tasks"] == [{
        "task_id": "dep-a", "reason": "entered bounded task ancestry"
    }]

    third = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    assert _incremental(third.prompt)["authoritative"]["events"] == []
    assert third.cursor.delivered_event_positions == {"dep-a": 1, "task-a": 2}


def test_relevant_task_leaves_and_reenters_without_repeating_delivered_history() -> None:
    state = State(":memory:")
    _seed(state)
    _record(state, project_id="project-a", task_id="dep-a", message="DEPENDENCY HISTORY ONCE")

    first = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    assert [event["message"] for event in _incremental(first.prompt)["authoritative"]["events"]] == [
        "DEPENDENCY HISTORY ONCE"
    ]

    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a",
    ))
    build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a", dependencies=("dep-a",),
    ))

    reentered = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    authoritative = _incremental(reentered.prompt)["authoritative"]
    assert authoritative["events"] == []
    assert authoritative["newly_relevant_tasks"] == [{
        "task_id": "dep-a", "reason": "entered bounded task ancestry"
    }]
    assert reentered.cursor.delivered_event_positions["dep-a"] == 1


def test_event_position_cap_reports_eviction_and_evicted_reentry_redelivers(monkeypatch) -> None:
    assert context_cursor.MAX_RETAINED_EVENT_POSITIONS == 64
    monkeypatch.setattr(context_cursor, "MAX_RETAINED_EVENT_POSITIONS", 3)
    state = State(":memory:")
    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a",
    ))
    for index in range(3):
        task_id = f"dep-{index}"
        state.upsert_task(Task(
            id=task_id, task_ref=f"DEPENDENCY-{index}", role="implementation", worker="fixture",
            project_id="project-a",
        ))
        _record(state, project_id="project-a", task_id=task_id, message=f"HISTORY {index}")

    build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    deliveries = []
    for index in range(3):
        state.upsert_task(Task(
            id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
            project_id="project-a", dependencies=(f"dep-{index}",),
        ))
        deliveries.append(build_incremental_context(
            state, identity=_identity(), policy_bundle=_policy()
        ))

    bounded = deliveries[-1]
    authoritative = _incremental(bounded.prompt)["authoritative"]
    expected_eviction = [{
        "task_id": "dep-0",
        "reason": "inactive cursor position evicted at cap; history will be re-delivered on re-entry",
    }]
    assert len(bounded.cursor.delivered_event_positions) == 3
    assert "dep-0" not in bounded.cursor.delivered_event_positions
    assert authoritative["evicted_event_positions"] == expected_eviction
    assert bounded.cursor.inspection_summary["evicted_event_positions"] == expected_eviction

    state.upsert_task(Task(
        id="task-a", task_ref="A-TASK", role="implementation", worker="fixture",
        project_id="project-a", dependencies=("dep-0",),
    ))
    reentered = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    reentered_authoritative = _incremental(reentered.prompt)["authoritative"]
    assert [event["message"] for event in reentered_authoritative["events"]] == ["HISTORY 0"]
    assert reentered_authoritative["newly_relevant_tasks"] == [{
        "task_id": "dep-0", "reason": "entered bounded task ancestry"
    }]


def test_event_position_map_ignores_tasks_that_were_never_relevant() -> None:
    state = State(":memory:")
    _seed(state)
    state.upsert_task(Task(
        id="unrelated", task_ref="UNRELATED", role="implementation", worker="fixture",
        project_id="project-a",
    ))
    _record(state, project_id="project-a", task_id="unrelated", message="UNRELATED HISTORY")

    delivered = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())

    assert "UNRELATED HISTORY" not in delivered.prompt
    assert "unrelated" not in delivered.cursor.delivered_event_positions


def test_invalidation_reason_never_echoes_an_absolute_path_into_provider_prompt() -> None:
    state = State(":memory:")
    _seed(state)
    build_incremental_context(state, identity=_identity(), policy_bundle=_policy())

    delivered = build_incremental_context(
        state,
        identity=_identity(task_contract_digest="/private/operator/secret-contract"),
        policy_bundle=_policy(),
    )

    assert delivered.invalidation_reason == REASON_TASK_CONTRACT_CHANGED
    assert "/private/operator/secret-contract" not in delivered.prompt


def test_event_window_reports_truncation_and_subsequent_complete_window(monkeypatch) -> None:
    monkeypatch.setattr(context_cursor, "MAX_EVENT_READ", 2)
    state = State(":memory:")
    _seed(state)
    for message in ("event one", "event two", "event three"):
        _record(state, project_id="project-a", task_id="task-a", message=message)

    first = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    first_authoritative = _incremental(first.prompt)["authoritative"]
    assert [event["message"] for event in first_authoritative["events"]] == [
        "event one", "event two"
    ]
    assert first_authoritative["event_window"] == {
        "limit": 2,
        "truncated": True,
        "truncation_reason": "event read limit reached",
    }
    assert first.cursor.inspection_summary["events_truncated"] is True
    assert first.cursor.bundle_identity["event_references"] == [1, 2]

    second = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    second_authoritative = _incremental(second.prompt)["authoritative"]
    assert [event["message"] for event in second_authoritative["events"]] == ["event three"]
    assert second_authoritative["event_window"] == {
        "limit": 2,
        "truncated": False,
        "truncation_reason": None,
    }
    assert second.cursor.inspection_summary["events_truncated"] is False
    assert second.cursor.bundle_identity["event_references"] == [3]


def test_multi_project_cursor_events_and_ancestry_are_isolated() -> None:
    state = State(":memory:")
    _seed(state, project_id="project-a", prefix="A")
    _seed(state, project_id="project-b", prefix="B")
    _record(state, project_id="project-a", task_id="task-a", message="PROJECT_A_PRIVATE_EVENT")
    _record(state, project_id="project-b", task_id="task-b", message="PROJECT_B_EVENT")

    first = build_incremental_context(state, identity=_identity(), policy_bundle=_policy())
    second = build_incremental_context(
        state,
        identity=_identity("project-b", task_id="task-b"),
        policy_bundle=_policy(),
    )

    assert "PROJECT_A_PRIVATE_EVENT" in first.prompt
    assert "A-DEPENDENCY" in first.prompt
    assert "PROJECT_A_PRIVATE_EVENT" not in second.prompt
    assert "A-DEPENDENCY" not in second.prompt
    assert "PROJECT_B_EVENT" in second.prompt
    assert "B-DEPENDENCY" in second.prompt
    assert first.cursor.identity.project_id == "project-a"
    assert second.cursor.identity.project_id == "project-b"


def test_ancestry_is_bounded_and_explicitly_reports_truncation() -> None:
    state = State(":memory:")
    _seed(state)

    delivery = build_incremental_context(
        state,
        identity=_identity(),
        policy_bundle=_policy(),
        ancestry=AncestryRequest(
            parent_program="ENG-PC",
            parent_task="ENG-PC-04",
            source_references=("source-one", "source-two"),
            limit=2,
        ),
    )
    ancestry = _incremental(delivery.prompt)["authoritative"]["ancestry"]

    assert delivery.ancestry_truncated is True
    assert ancestry["limit"] == 2
    assert len(ancestry["nodes"]) == 2
    assert ancestry["truncated"] is True
    assert ancestry["truncation_reason"] == "explicit ancestry node limit reached"


@pytest.mark.parametrize("supplier", [None, lambda: (_ for _ in ()).throw(RuntimeError("offline"))])
def test_graphify_absence_or_failure_never_reduces_authoritative_context(supplier) -> None:
    state = State(":memory:")
    _seed(state)
    _record(state, project_id="project-a", task_id="task-a", message="authoritative gate passed")

    delivery = build_incremental_context(
        state,
        identity=_identity(),
        policy_bundle=_policy(),
        graph_context_supplier=supplier,
    )
    incremental = _incremental(delivery.prompt)

    assert delivery.outcome == OUTCOME_DELIVERED
    assert delivery.graphify_supplied is False
    assert incremental["authoritative"]["task_changes"]
    assert incremental["authoritative"]["events"][0]["message"] == "authoritative gate passed"
    assert incremental["graphify"]["supplied"] is False


def test_a_tampered_cursor_cannot_reduce_delivered_mandatory_policy() -> None:
    """A corrupt or downgraded cursor must not shrink what the worker receives.

    ``test_cursor_never_omits_or_changes_mandatory_policy`` covers the honest
    path: a second delivery whose incremental section is legitimately empty.
    This covers the adversarial one, which is the property the whole feature
    rests on -- the cursor is persisted state, so a stale, hand-edited or
    partially-written row must not be able to talk the composer out of
    delivering a mandatory policy file.

    It cannot, structurally, because the authoritative bundle is an *input* to
    ``build_incremental_context`` rather than something the cursor reconstructs.
    Asserted rather than left to that argument, so a future refactor that makes
    the cursor a source of the bundle fails here.
    """

    state = State(":memory:")
    _seed(state)
    original = _policy()
    build_incremental_context(state, identity=_identity(), policy_bundle=original)

    row = state.get_context_cursor(
        project_id=_identity().project_id,
        task_id=_identity().task_id,
        consumer_id=_identity().consumer_id,
    )
    reduced = dict(row["preserved_policy_identity"])
    assert reduced.pop("core_policies", None) is not None, "fixture must carry core policies to drop"
    state.upsert_context_cursor({**dict(row), "preserved_policy_identity": reduced})

    delivered = build_incremental_context(state, identity=_identity(), policy_bundle=original)

    assert delivered.prompt.startswith(MANDATORY_PROMPT)
    assert delivered.manifest["preserved_policy_identity"] == original.manifest["preserved_policy_identity"]
    # And it is not silently tolerated: the mismatch names the policy component.
    assert delivered.invalidation_reason == REASON_PRESERVED_POLICY_CHANGED
