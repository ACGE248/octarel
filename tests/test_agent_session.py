"""ENG-PC-02 task-scoped resumable session acceptance coverage."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents.adapter_contract import capabilities_for, run_result_from_record
from scripts.agents.control_plane import agent_session
from scripts.agents.control_plane.agent_session import SessionIdentity
from scripts.agents.control_plane.state import State
from scripts.agents.manifest import RESULT_PASS, RunRecord
from scripts.agents.registry import RegistryError, load_registry
from scripts.ci.local_gate import candidate

REGISTRY = load_registry()
RESUME_WORKER = dataclasses.replace(
    REGISTRY.get("opencode-free-review"),
    name="fixture-resume-review",
    resume_args=("--resume", "{session_id}"),
    resume_state_source="session_id",
)


def identity(**changes: str) -> SessionIdentity:
    values = {
        "project_id": "project-1",
        "task_id": "ENG-PC-02",
        "worker": RESUME_WORKER.name,
        "provider": "FixtureProvider",
        "effective_model": "fixture-model",
        "worktree_path": "worktrees/eng-pc-02",
        "tree_sha": "a" * 40,
        "permission_profile": "standard",
        "capability": "read-only",
        "policy_digest": "policy-v1",
    }
    values.update(changes)
    return SessionIdentity(**values)


def seed_resumable(state: State, current: SessionIdentity | None = None):
    current = current or identity()
    first = agent_session.resolve_session(
        state, current, worker=RESUME_WORKER, run_id="run-1", now="2026-01-01T00:00:00+00:00"
    )
    assert first.mode == agent_session.MODE_FRESH
    reason = agent_session.record_session_outcome(
        state,
        decision=first,
        worker=RESUME_WORKER,
        structured_result={"session_id": "native-session-123"},
        run_id="run-1",
        now="2026-01-01T00:00:01+00:00",
    )
    assert reason == agent_session.REASON_SESSION_STATE_RECORDED
    return first


def test_fixture_worker_declaring_resume_resumes_and_increments_continuations():
    state = State(":memory:")
    first = seed_resumable(state)

    resumed = agent_session.resolve_session(
        state, identity(), worker=RESUME_WORKER, run_id="run-2", now="2026-01-01T00:01:00+00:00"
    )

    assert RESUME_WORKER.is_read_only  # resume is transport continuity, not write authority
    assert capabilities_for(RESUME_WORKER).can_resume_session is True
    assert resumed.mode == agent_session.MODE_RESUMED
    assert resumed.session_id == first.session_id
    assert resumed.continuation_count == 1
    assert agent_session.resume_args_for_session(state, decision=resumed, worker=RESUME_WORKER) == (
        "--resume",
        "native-session-123",
    )


def test_agent_session_exposes_no_path_that_puts_resume_argv_in_a_run_record():
    """The latent integration boundary must keep opaque resume argv caller-only."""

    module_contract = " ".join((agent_session.__doc__ or "").split())
    builder_contract = " ".join((agent_session.resume_args_for_session.__doc__ or "").split()).replace("`", "")
    assert "one construct from this module that legitimately carries the opaque value" in module_contract
    assert "must never store it in RunRecord.requested_command" in builder_contract
    assert all(artifact in builder_contract for artifact in ("manifest", "summary", "event"))

    module_tree = ast.parse(inspect.getsource(agent_session))
    assert not any(isinstance(node, ast.Name) and node.id == "RunRecord" for node in ast.walk(module_tree))
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "requested_command" for node in ast.walk(module_tree)
    )


def test_unparseable_session_age_is_unknown_but_new_session_age_is_numeric():
    row = {
        "id": "session-id",
        "continuation_count": 0,
        "created_at": "not-a-timestamp",
        "last_activity_at": "2026-01-01T00:00:00+00:00",
        "identity_fingerprint": "fingerprint",
    }
    unknown = agent_session._decision(
        row,
        mode=agent_session.MODE_FRESH,
        reason=agent_session.REASON_NO_PRIOR_SESSION,
        now="2026-01-01T00:00:00+00:00",
    )
    assert unknown.session_age_seconds is None

    row["created_at"] = "2026-01-01T00:00:00+00:00"
    new = agent_session._decision(
        row,
        mode=agent_session.MODE_FRESH,
        reason=agent_session.REASON_NO_PRIOR_SESSION,
        now="2026-01-01T00:00:00.250000+00:00",
    )
    assert new.session_age_seconds is not None
    assert 0.0 <= new.session_age_seconds < 1.0


def test_all_real_workers_remain_unsupported_with_truthful_declared_reasons():
    for worker in REGISTRY.workers.values():
        caps = capabilities_for(worker)
        assert worker.resume_args == ()
        assert worker.resume_state_source == ""
        assert caps.can_resume_session is False
        if worker.session_persistence_optout_flag:
            assert worker.session_persistence_optout_flag in caps.resume_unavailable_reason
        else:
            assert "declares no cli.resume block" in caps.resume_unavailable_reason


def test_non_resume_adapter_always_starts_fresh_with_adapter_reason():
    state = State(":memory:")
    worker = REGISTRY.get("codex-build")
    current = identity(worker=worker.name, provider=worker.provider, capability=worker.capability)
    one = agent_session.resolve_session(state, current, worker=worker)
    two = agent_session.resolve_session(state, current, worker=worker)
    assert one.mode == two.mode == agent_session.MODE_FRESH
    assert two.reason == agent_session.REASON_ADAPTER_DOES_NOT_SUPPORT_RESUME


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("tree_sha", "b" * 40, agent_session.REASON_TREE_CHANGED),
        ("policy_digest", "policy-v2", agent_session.REASON_POLICY_DIGEST_CHANGED),
        ("permission_profile", "repo_configured_auto", agent_session.REASON_PERMISSION_PROFILE_CHANGED),
        ("provider", "FallbackProvider", agent_session.REASON_PROVIDER_CHANGED),
        ("effective_model", "fixture-model-2", agent_session.REASON_MODEL_CHANGED),
        ("worktree_path", "worktrees/recreated", agent_session.REASON_WORKTREE_CHANGED),
    ],
)
def test_each_identity_change_has_its_own_specific_reason(field, value, reason):
    state = State(":memory:")
    first = seed_resumable(state)
    changed = agent_session.resolve_session(
        state,
        identity(**{field: value}),
        worker=RESUME_WORKER,
        run_id="run-2",
        now="2026-01-01T00:02:00+00:00",
    )
    assert changed.mode == agent_session.MODE_FRESH
    assert changed.session_id != first.session_id
    assert changed.reason.startswith(reason)
    assert "stored=" in changed.reason and "current=" in changed.reason


def test_provider_fallback_starts_a_new_provider_session():
    state = State(":memory:")
    prior = seed_resumable(state)
    fallback = agent_session.resolve_session(
        state, identity(provider="FallbackProvider"), worker=RESUME_WORKER
    )
    assert fallback.mode == agent_session.MODE_FRESH
    assert fallback.session_id != prior.session_id
    assert fallback.reason.startswith(agent_session.REASON_PROVIDER_CHANGED)


def test_session_survives_state_store_restart(tmp_path: Path):
    path = tmp_path / "state.db"
    with State(path) as state:
        first = seed_resumable(state)
    with State(path) as reopened:
        resumed = agent_session.resolve_session(reopened, identity(), worker=RESUME_WORKER)
    assert resumed.mode == agent_session.MODE_RESUMED
    assert resumed.session_id == first.session_id


def test_operator_forced_fresh_affects_exactly_the_next_attempt():
    state = State(":memory:")
    seed_resumable(state)
    assert agent_session.request_forced_fresh(
        state, project_id="project-1", task_id="ENG-PC-02", worker=RESUME_WORKER.name
    )
    forced = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)
    assert forced.mode == agent_session.MODE_FRESH
    assert forced.reason == agent_session.REASON_OPERATOR_FORCED_FRESH

    agent_session.record_session_outcome(
        state,
        decision=forced,
        worker=RESUME_WORKER,
        structured_result={"session_id": "native-session-after-force"},
    )
    resumed = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)
    assert resumed.mode == agent_session.MODE_RESUMED


def test_secret_bearing_opaque_payload_is_rejected_not_redacted():
    state = State(":memory:")
    first = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)
    secret = "sk-ABCDEFGHIJKLMNOP1234567890"
    reason = agent_session.record_session_outcome(
        state,
        decision=first,
        worker=RESUME_WORKER,
        structured_result={"session_id": secret},
    )
    assert reason == agent_session.REASON_STORED_STATE_REJECTED_SECRET
    row = state.get_active_agent_session(project_id="project-1", task_id="ENG-PC-02", worker=RESUME_WORKER.name)
    assert row["has_stored_state"] is False
    assert row["reason"] == agent_session.REASON_NO_PRIOR_SESSION
    assert row["state_reason"] == agent_session.REASON_STORED_STATE_REJECTED_SECRET
    assert secret not in json.dumps(row)
    next_attempt = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)
    assert next_attempt.mode == agent_session.MODE_FRESH
    assert next_attempt.reason == agent_session.REASON_STORED_STATE_REJECTED_SECRET


def test_opaque_payload_never_appears_in_lists_manifest_or_events():
    state = State(":memory:")
    first = seed_resumable(state)
    opaque = "native-session-123"
    assert opaque not in json.dumps(state.list_agent_sessions())
    assert opaque not in json.dumps([dataclasses.asdict(event) for event in state.list_events()])

    record = RunRecord(
        task="ENG-PC-02",
        role="primary-implementation",
        worker=RESUME_WORKER.name,
        planned_execution_system=RESUME_WORKER.execution_system,
        planned_provider=RESUME_WORKER.provider,
        planned_model=RESUME_WORKER.default_model,
        planned_intensity="low",
        requested_command=["fixture"],
        result=RESULT_PASS,
        session_id=first.session_id,
        session_updated=True,
    )
    manifest = record.to_manifest(paths={})
    result = run_result_from_record(record, capabilities=capabilities_for(RESUME_WORKER), evidence_paths={})
    assert result.session_update == first.session_id
    assert opaque not in json.dumps(manifest)
    assert opaque not in json.dumps(result.as_dict())


@pytest.mark.parametrize(
    "resume,match",
    [
        ({"args": ["--resume"], "state_source": "session_id"}, "placeholder"),
        ({"args": ["--resume", "{session_id}"], "state_source": ""}, "non-empty"),
    ],
)
def test_registry_rejects_invalid_resume_declarations(tmp_path: Path, resume, match):
    raw = json.loads(Path("scripts/agents/workers.json").read_text(encoding="utf-8"))
    raw["workers"]["claude-code"]["cli"]["resume"] = resume
    path = tmp_path / "workers.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RegistryError, match=match):
        load_registry(path)


def test_partial_unique_index_conflict_is_contained_not_raised():
    state = State(":memory:")
    first = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)
    competing = {
        "id": "competing",
        **dataclasses.asdict(identity()),
        "identity_fingerprint": identity().fingerprint,
        "continuation_state": None,
        "continuation_count": 0,
        "force_fresh_next": 0,
        "active": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_activity_at": "2026-01-01T00:00:00+00:00",
        "last_run_id": None,
        "last_mode": agent_session.MODE_FRESH,
        "reason": agent_session.REASON_NO_PRIOR_SESSION,
        "state_reason": None,
    }
    winner, won = state.replace_active_agent_session(row=competing, expected_session_id=None)
    assert won is False
    assert winner["id"] == first.session_id


def test_sustained_session_replacement_contention_degrades_to_fresh(monkeypatch):
    state = State(":memory:")
    attempts = 0

    def always_conflicts(*, row, expected_session_id):
        nonlocal attempts
        attempts += 1
        return {}, False

    monkeypatch.setattr(state, "replace_active_agent_session", always_conflicts)

    decision = agent_session.resolve_session(state, identity(), worker=RESUME_WORKER)

    assert attempts == agent_session._MAX_SESSION_REPLACE_ATTEMPTS
    assert decision.mode == agent_session.MODE_FRESH
    assert decision.reason == agent_session.REASON_SESSION_CONTENTION


def test_partial_unique_index_contains_a_real_two_connection_race(tmp_path: Path):
    path = tmp_path / "race.db"
    State(path).close()
    current = identity()

    def create(candidate_id: str):
        with State(path) as state:
            row = {
                "id": candidate_id,
                **dataclasses.asdict(current),
                "identity_fingerprint": current.fingerprint,
                "continuation_state": None,
                "continuation_count": 0,
                "force_fresh_next": 0,
                "active": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_activity_at": "2026-01-01T00:00:00+00:00",
                "last_run_id": None,
                "last_mode": agent_session.MODE_FRESH,
                "reason": agent_session.REASON_NO_PRIOR_SESSION,
                "state_reason": None,
            }
            return state.replace_active_agent_session(row=row, expected_session_id=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ("candidate-a", "candidate-b")))
    assert sorted(won for _row, won in outcomes) == [False, True]
    with State(path) as state:
        assert len([row for row in state.list_agent_sessions() if row["active"]]) == 1


def test_exact_tree_candidate_is_independent_of_session_state(tmp_path: Path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)
    before = candidate(repo, "main")

    state = State(repo / ".orchestrator-state" / "orchestrator.db")
    seed_resumable(state)
    state.close()

    after = candidate(repo, "main")
    assert after == before
