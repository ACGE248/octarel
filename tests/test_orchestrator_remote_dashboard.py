"""ENG-AGENT-02-S6 (issue #95): dashboard-level integration tests for secure

remote access — header spoofing, allowlist enforcement, CSRF/origin
protection, security headers, audit trail, and rate limiting, all exercised
through the real FastAPI app via ``TestClient``. Every Cloudflare Access
token here is signed with a locally generated throwaway RSA keypair verified
against a fake (non-network) JWKS fetcher — no live Cloudflare account,
network call, or credential is ever required (criteria 13/15).
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from scripts.agents.control_plane import agent_session, approvals
from scripts.agents.control_plane.agent_session import SessionIdentity
from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import (
    _terminal_websocket_identity,
    create_app,
)
from scripts.agents.control_plane.models import Task
from scripts.agents.control_plane.project import ProjectContract
from scripts.agents.control_plane.project_registry import (
    contract_to_row,
    select_project,
)
from scripts.agents.control_plane.provider_state import seed_provider_states
from scripts.agents.control_plane.remote_access import (
    ACCESS_JWT_HEADER,
    AccessVerifier,
    RemoteAccessConfig,
    RemoteAccessState,
    SlidingWindowLimiter,
)
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry

TEAM_DOMAIN = "testteam.cloudflareaccess.com"
AUDIENCE = "test-application-aud-tag"
HOSTNAME = "dev.octascene.com"
KID = "test-key-1"
MAINTAINER_EMAIL = "maintainer@example.com"


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _jwks_dict(public_key) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = KID
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


def _token(private_key, **claim_overrides) -> str:
    now = int(time.time())
    claims = {
        "email": MAINTAINER_EMAIL,
        "aud": AUDIENCE,
        "iss": f"https://{TEAM_DOMAIN}",
        "iat": now,
        "exp": now + 300,
        "sub": "user-123",
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture()
def keypair():
    return _generate_keypair()


@pytest.fixture()
def ctx(tmp_path: Path) -> CommandContext:
    registry = load_registry()
    state = State(":memory:")
    for provider in seed_provider_states(registry):
        state.upsert_provider_state(provider)
    state.upsert_task(Task(id="t1", task_ref="ENG-AGENT-02", role="focused-tests", worker="opencode2-gemini-flash-lite"))
    return CommandContext(
        state=state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=state),
        repo_root=tmp_path,
    )


def _remote_state(public_key, *, allowed_emails: frozenset[str] = frozenset()) -> RemoteAccessState:
    config = RemoteAccessConfig(
        enabled=True, hostname=HOSTNAME, team_domain=TEAM_DOMAIN, audience=AUDIENCE, allowed_emails=allowed_emails
    )
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    return RemoteAccessState(config=config, verifier=verifier)


def _disabled_remote_state() -> RemoteAccessState:
    return RemoteAccessState.from_config(RemoteAccessConfig(enabled=False))


def _select_fixture_project(ctx: CommandContext, project_id: str = "project-a") -> Task:
    ctx.state.upsert_project(
        contract_to_row(
            ProjectContract(
                project_id=project_id,
                display_name=project_id,
                local_repo_root=ctx.repo_root,
            )
        )
    )
    select_project(ctx.state, project_id)
    task = ctx.state.get_task("t1")
    assert task is not None
    task.project_id = project_id
    ctx.state.upsert_task(task)
    return task


def _resume_worker(ctx: CommandContext):
    worker = dataclasses.replace(
        ctx.registry.get("opencode-free-review"),
        name="fixture-resume-review",
        resume_args=("--resume", "{session_id}"),
        resume_state_source="session_id",
    )
    ctx.registry.workers[worker.name] = worker
    return worker


def _session_identity(
    ctx: CommandContext,
    *,
    project_id: str,
    task_ref: str,
    worker,
) -> SessionIdentity:
    return SessionIdentity(
        project_id=project_id,
        task_id=task_ref,
        worker=worker.name,
        provider=worker.provider,
        effective_model=worker.default_model,
        worktree_path=str(ctx.repo_root / "private-session-worktree"),
        tree_sha="a" * 40,
        permission_profile="standard",
        capability=worker.capability,
        policy_digest="fixture-policy-digest",
    )


def _seed_resumed_session(ctx: CommandContext, *, project_id: str, task_ref: str, worker) -> str:
    identity = _session_identity(ctx, project_id=project_id, task_ref=task_ref, worker=worker)
    first = agent_session.resolve_session(
        ctx.state,
        identity,
        worker=worker,
        run_id="fixture-run-1",
        now="2026-09-30T12:00:00+00:00",
    )
    opaque = "opaque-native-continuation-123"
    assert agent_session.record_session_outcome(
        ctx.state,
        decision=first,
        worker=worker,
        structured_result={worker.resume_state_source: opaque},
        run_id="fixture-run-1",
        now="2026-09-30T12:00:01+00:00",
    ) == agent_session.REASON_SESSION_STATE_RECORDED
    resumed = agent_session.resolve_session(
        ctx.state,
        identity,
        worker=worker,
        run_id="fixture-run-2",
        now="2026-09-30T12:01:00+00:00",
    )
    assert resumed.mode == agent_session.MODE_RESUMED
    return opaque


class _FakeWebSocket:
    def __init__(self, *, origin: str, token: str | None = None, client_host: str = "127.0.0.1") -> None:
        self.headers = {"origin": origin}
        if token:
            self.headers[ACCESS_JWT_HEADER] = token
        self.cookies = {}
        self.client = type("Client", (), {"host": client_host})()


def test_terminal_websocket_allows_only_direct_loopback_or_verified_access_identity(keypair):
    private_key, public_key = keypair
    remote = _remote_state(public_key, allowed_emails=frozenset({MAINTAINER_EMAIL}))
    assert _terminal_websocket_identity(_FakeWebSocket(origin="http://127.0.0.1:8877"), remote) == "local"
    assert _terminal_websocket_identity(_FakeWebSocket(origin=f"https://{HOSTNAME}"), remote) is None
    verified = _FakeWebSocket(
        origin=f"https://{HOSTNAME}", token=_token(private_key), client_host="127.0.0.1"
    )
    assert _terminal_websocket_identity(verified, remote) == MAINTAINER_EMAIL


def test_terminal_websocket_rejects_spoofed_remote_origin_even_from_loopback_proxy(keypair):
    _private_key, public_key = keypair
    remote = _remote_state(public_key)
    spoofed = _FakeWebSocket(origin="https://attacker.example", client_host="127.0.0.1")
    assert _terminal_websocket_identity(spoofed, remote) is None


# --------------------------------------------------------------------------- disabled by default


def test_remote_disabled_ignores_a_spoofed_access_header_entirely(ctx):
    """Criterion 12: with remote mode disabled, even a header that looks exactly

    like a real Cloudflare Access assertion changes nothing — the request is
    treated as ordinary trusted local access, byte-for-byte as before this slice.
    """

    client = TestClient(create_app(ctx, remote=_disabled_remote_state()))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: "totally-fake-not-even-a-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_mode"] == "local"
    assert body["remote_email"] is None


def test_default_create_app_remote_is_disabled(ctx):
    """``create_app`` with no ``remote=`` argument at all must behave identically

    to explicit disablement (every pre-S6 caller/test never passes it).
    """

    client = TestClient(create_app(ctx))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: "fake"})
    assert resp.json()["access_mode"] == "local"


# --------------------------------------------------------------------------- enabled: local path preserved


def test_remote_enabled_but_no_token_present_is_still_treated_as_local(ctx, keypair):
    _private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    resp = client.get("/api/identity")
    assert resp.status_code == 200
    assert resp.json()["access_mode"] == "local"


# --------------------------------------------------------------------------- criterion 4/6: reject unauthenticated/spoofed


def test_remote_enabled_unverifiable_token_is_rejected(ctx, keypair):
    _private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: "not-a-real-jwt"})
    assert resp.status_code == 401


def test_remote_enabled_token_signed_by_a_different_key_is_rejected(ctx, keypair):
    """The direct spoofing test: a well-formed token with the right claims but

    signed by an attacker's own key (not Cloudflare's) must never grant access.
    """

    _real_private, real_public = keypair
    attacker_private, _attacker_public = _generate_keypair()
    client = TestClient(create_app(ctx, remote=_remote_state(real_public)))
    forged = _token(attacker_private)
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: forged})
    assert resp.status_code == 401
    events = [e for e in ctx.state.list_events(limit=50) if e.category == "remote_access"]
    assert any("rejected" in e.message.lower() for e in events)


def test_rejected_auth_attempt_is_audited_without_leaking_the_token(ctx, keypair):
    _private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    secret_looking_token = "not-a-real-jwt-but-pretend-super-secret"
    client.get("/api/identity", headers={ACCESS_JWT_HEADER: secret_looking_token})
    events = ctx.state.list_events(limit=50)
    assert any(e.category == "remote_access" for e in events)
    for e in events:
        assert secret_looking_token not in e.message


# --------------------------------------------------------------------------- criterion 5: allowlist


def test_remote_enabled_valid_but_non_allowlisted_identity_is_rejected(ctx, keypair):
    private_key, public_key = keypair
    remote = _remote_state(public_key, allowed_emails=frozenset({"someone-else@example.com"}))
    client = TestClient(create_app(ctx, remote=remote))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: _token(private_key)})
    assert resp.status_code == 403


def test_remote_enabled_valid_and_allowlisted_identity_is_accepted(ctx, keypair):
    private_key, public_key = keypair
    remote = _remote_state(public_key, allowed_emails=frozenset({MAINTAINER_EMAIL}))
    client = TestClient(create_app(ctx, remote=remote))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: _token(private_key)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_mode"] == "remote"
    assert body["remote_email"] == MAINTAINER_EMAIL


# --------------------------------------------------------------------------- criterion 7: full bounded operations remotely


def test_authenticated_remote_maintainer_can_use_existing_bounded_operations(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    assert client.get("/api/tasks", headers=headers).status_code == 200
    assert client.get("/api/runbooks/presets", headers=headers).status_code == 200
    resp = client.post("/api/commands/pause", headers=headers, json={"task_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --------------------------------------------------------------------------- ENG-PC-02 session inspector


def test_session_read_returns_typed_facts_without_opaque_state_or_absolute_paths(ctx):
    task = _select_fixture_project(ctx)
    worker = _resume_worker(ctx)
    opaque = _seed_resumed_session(ctx, project_id="project-a", task_ref=task.task_ref, worker=worker)
    client = TestClient(create_app(ctx))

    response = client.get(f"/api/agent-sessions/{task.task_ref}/{worker.name}")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == {"value": "RESUMED", "class": "MEASURED", "reason": None, "unit": None}
    assert body["continuation_count"]["value"] == 1
    assert body["continuation_count"]["class"] == "MEASURED"
    assert body["session_age_seconds"]["value"] is not None
    assert body["last_activity"]["value"] == "2026-09-30T12:01:00+00:00"
    assert body["resume_capability"]["value"] is True
    assert body["resume_capability"]["class"] == "DERIVED"
    assert body["invalidation_reason"]["class"] == "NOT_REPORTED"
    assert body["start_fresh_next_attempt"] == {"enabled": True, "reason": None}

    serialized = response.text
    assert opaque not in serialized
    assert str(ctx.repo_root) not in serialized
    assert "private-session-worktree" not in serialized
    assert "continuation_state" not in serialized
    assert "worktree_path" not in serialized


def test_worktree_invalidation_reason_names_the_component_without_the_path(ctx):
    """A worktree invalidation must name the component without carrying the path.

    agent_session._invalidation_reason deliberately embeds ``stored=``/``current=``
    so an operator sees which component invalidated resume rather than only that
    something changed. For ``worktree_path`` those values are operator filesystem
    paths, so the specificity that makes the reason useful is exactly what would
    make it unsafe to surface -- and the reason is persisted on the session row,
    then read back by this endpoint.

    The RESUMED case cannot catch this: its invalidation_reason is NOT_REPORTED,
    so the string is never rendered at all.

    Scrubbing downstream was tried and rejected. redact_text redacts secrets, not
    paths; the event timeline is clean only because run_events scrubs host paths in
    its own envelope, and that regex does not match every shape an operator machine
    produces (a pytest tmp_path under /private/var/folders is not matched). So the
    values are withheld at the source instead, which is safe by construction for
    every consumer rather than for the ones that remember to scrub.
    """

    task = _select_fixture_project(ctx)
    worker = _resume_worker(ctx)
    identity = _session_identity(ctx, project_id="project-a", task_ref=task.task_ref, worker=worker)
    first = agent_session.resolve_session(ctx.state, identity, worker=worker, run_id="fixture-run-1")
    assert agent_session.record_session_outcome(
        ctx.state,
        decision=first,
        worker=worker,
        structured_result={worker.resume_state_source: "opaque-native-continuation-123"},
        run_id="fixture-run-1",
    ) == agent_session.REASON_SESSION_STATE_RECORDED

    moved = dataclasses.replace(identity, worktree_path=str(ctx.repo_root / "relocated-session-worktree"))
    invalidated = agent_session.resolve_session(ctx.state, moved, worker=worker, run_id="fixture-run-3")
    assert invalidated.mode == agent_session.MODE_FRESH
    # Specific about WHICH component, and carrying neither path.
    assert invalidated.reason.startswith(agent_session.REASON_WORKTREE_CHANGED)
    assert "private-session-worktree" not in invalidated.reason
    assert "relocated-session-worktree" not in invalidated.reason

    response = TestClient(create_app(ctx)).get(f"/api/agent-sessions/{task.task_ref}/{worker.name}")

    assert response.status_code == 200
    body = response.json()
    reason = body["invalidation_reason"]
    assert reason["class"] == "MEASURED"
    # Still names the component, so the operator-facing value is not lost.
    assert reason["value"].startswith(agent_session.REASON_WORKTREE_CHANGED)
    assert "private-session-worktree" not in response.text
    assert "relocated-session-worktree" not in response.text
    assert str(ctx.repo_root) not in response.text


def test_unknown_session_task_is_rejected_before_session_state_is_touched(ctx, monkeypatch):
    def unexpected_session_read(**_kwargs):
        raise AssertionError("unknown task must be rejected before session persistence is read")

    monkeypatch.setattr(ctx.state, "get_active_agent_session", unexpected_session_read)
    client = TestClient(create_app(ctx))

    response = client.get("/api/agent-sessions/not-this-project/opencode-free-review")

    assert response.status_code == 404


def test_forced_fresh_post_is_identity_audited_on_success_and_failure(ctx, keypair):
    task = _select_fixture_project(ctx)
    worker = _resume_worker(ctx)
    _seed_resumed_session(ctx, project_id="project-a", task_ref=task.task_ref, worker=worker)
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    endpoint = f"/api/agent-sessions/{task.task_ref}/{worker.name}/start-fresh"

    success = client.post(endpoint, headers=headers, json={})
    failure = client.post(endpoint, headers=headers, json={})

    assert success.status_code == 200
    assert success.json()["status"] == "REQUESTED"
    assert failure.status_code == 409
    messages = [e.message for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"]
    assert any(
        MAINTAINER_EMAIL in message and "agent_session_start_fresh" in message and "OK" in message
        for message in messages
    )
    assert any(
        MAINTAINER_EMAIL in message and "agent_session_start_fresh" in message and "FAIL" in message
        for message in messages
    )


def test_forced_fresh_post_cannot_target_a_task_outside_the_selected_project(ctx, keypair):
    _select_fixture_project(ctx)
    worker = _resume_worker(ctx)
    foreign = Task(
        id="foreign-task-id",
        task_ref="FOREIGN-SESSION",
        role="diff-review",
        worker=worker.name,
        project_id="project-b",
    )
    ctx.state.upsert_task(foreign)
    identity = _session_identity(
        ctx,
        project_id="project-b",
        task_ref=foreign.task_ref,
        worker=worker,
    )
    agent_session.resolve_session(ctx.state, identity, worker=worker)
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}

    response = client.post(
        f"/api/agent-sessions/{foreign.task_ref}/{worker.name}/start-fresh",
        headers=headers,
        json={},
    )

    assert response.status_code == 404
    row = ctx.state.get_active_agent_session(
        project_id="project-b",
        task_id=foreign.task_ref,
        worker=worker.name,
    )
    assert row is not None
    assert row["force_fresh_next"] is False


def test_worker_without_native_resume_reports_reason_and_disables_control(ctx):
    task = _select_fixture_project(ctx)
    worker = ctx.registry.get("opencode2-gemini-flash-lite")
    identity = _session_identity(ctx, project_id="project-a", task_ref=task.task_ref, worker=worker)
    decision = agent_session.resolve_session(ctx.state, identity, worker=worker)
    assert decision.mode == agent_session.MODE_FRESH
    client = TestClient(create_app(ctx))

    response = client.get(f"/api/agent-sessions/{task.task_ref}/{worker.name}")

    assert response.status_code == 200
    body = response.json()
    assert body["mode"]["value"] == "FRESH"
    assert body["invalidation_reason"]["value"] == agent_session.REASON_ADAPTER_DOES_NOT_SUPPORT_RESUME
    assert body["resume_capability"]["value"] is False
    assert worker.name in body["resume_capability"]["reason"]
    assert body["start_fresh_next_attempt"]["enabled"] is False
    assert body["start_fresh_next_attempt"]["reason"] == body["resume_capability"]["reason"]


# --------------------------------------------------------------------------- criterion 8: CSRF/origin + audit


def test_state_changing_remote_request_without_a_matching_origin_is_rejected(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key)}  # no Origin/Referer at all
    resp = client.post("/api/commands/pause", headers=headers, json={"task_id": "t1"})
    assert resp.status_code == 403


def test_state_changing_remote_request_with_a_foreign_origin_is_rejected(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": "https://evil.example.com"}
    resp = client.post("/api/commands/pause", headers=headers, json={"task_id": "t1"})
    assert resp.status_code == 403


def test_read_only_remote_requests_are_not_subject_to_the_origin_check(ctx, keypair):
    """Origin/CSRF protection is scoped to state-changing requests (criterion 8);

    a GET must never be blocked just for lacking an Origin header.
    """

    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    resp = client.get("/api/tasks", headers={ACCESS_JWT_HEADER: _token(private_key)})
    assert resp.status_code == 200


def test_successful_remote_state_change_is_audited_with_identity_verb_target_result(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    resp = client.post("/api/commands/pause", headers=headers, json={"task_id": "t1"})
    assert resp.status_code == 200
    audit_events = [e for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"]
    assert len(audit_events) == 1
    message = audit_events[0].message
    assert MAINTAINER_EMAIL in message
    assert "pause" in message
    assert "t1" in message
    assert "OK" in message


def test_remote_approval_uses_verified_identity_for_request_resolution_and_audit(ctx, keypair):
    _select_fixture_project(ctx)
    private_key, public_key = keypair
    client = TestClient(
        create_app(
            ctx,
            remote=_remote_state(public_key, allowed_emails=frozenset({MAINTAINER_EMAIL})),
        )
    )
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    created = client.post(
        "/api/approvals",
        headers=headers,
        json={
            "action_type": approvals.ACTION_AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE,
            "payload": {"question_ref": "ADR-REMOTE", "decision": "Keep the existing scheduler"},
            "reason": "Remote maintainer decision",
        },
    )
    assert created.status_code == 200
    assert created.json()["requested_by"] == f"remote:{MAINTAINER_EMAIL}"

    resolved = client.post(
        f"/api/approvals/{created.json()['id']}/resolve",
        headers=headers,
        json={"decision": "REJECT", "resolution_note": "Needs a local design review"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved_by"] == f"remote:{MAINTAINER_EMAIL}"
    messages = [
        event.message for event in ctx.state.list_events(limit=50) if event.category == "remote_audit"
    ]
    assert any(MAINTAINER_EMAIL in message and "approval_request" in message and "OK" in message for message in messages)
    assert any(MAINTAINER_EMAIL in message and "approval_resolve" in message and "REJECTED" in message for message in messages)


def test_remote_crafted_cleanup_steering_hands_off_with_verified_identity_and_audit(
    ctx, keypair, monkeypatch
):
    from scripts.agents.control_plane import dashboard_api, operations

    _select_fixture_project(ctx)
    private_key, public_key = keypair
    monkeypatch.setattr(
        operations,
        "refresh_worktree_statuses",
        lambda _ctx: [
            {
                "path": str(ctx.repo_root / "finished-clean"),
                "branch": "eng/finished-clean",
                "classification": "FINISHED_CLEAN",
                "dirty": False,
                "cleanup_eligible": True,
                "canonical_checkout": False,
            }
        ],
    )
    direct_calls = []

    def direct_execute(_ctx, verb, **kwargs):
        direct_calls.append((verb, kwargs))
        raise AssertionError("steering must not call the cleanup command before approval")

    monkeypatch.setattr(dashboard_api, "apply_command", direct_execute)
    client = TestClient(
        create_app(
            ctx,
            remote=_remote_state(public_key, allowed_emails=frozenset({MAINTAINER_EMAIL})),
        )
    )
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}

    initiated = client.post(
        "/api/steering/execute",
        headers=headers,
        json={"verb": "worktree_cleanup", "args": {}, "confirm": True},
    )

    assert initiated.status_code == 200
    approval = initiated.json()["data"]["approval_request"]
    assert approval["action_type"] == approvals.ACTION_DESTRUCTIVE_CLEANUP
    assert approval["state"] == "PENDING"
    assert approval["requested_by"] == f"remote:{MAINTAINER_EMAIL}"
    assert direct_calls == []
    messages = [
        event.message for event in ctx.state.list_events(limit=50) if event.category == "remote_audit"
    ]
    assert any(
        MAINTAINER_EMAIL in message
        and "worktree_cleanup" in message
        and "APPROVAL_REQUESTED" in message
        for message in messages
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("action_name", ["start", "stop", "restart"])
def test_authenticated_remote_runtime_actions_handoff_without_legacy_bypass(
    ctx, keypair, monkeypatch, legacy, action_name
):
    _select_fixture_project(ctx)
    private_key, public_key = keypair
    app = create_app(
        ctx,
        remote=_remote_state(public_key, allowed_emails=frozenset({MAINTAINER_EMAIL})),
    )
    manager = app.state.app_lifecycle
    running = action_name in {"stop", "restart"}
    service = {
        "id": "runtime-project-a-fixture",
        "project_id": "project-a",
        "name": "Fixture runtime",
        "worktree_path": None,
        "runbook_id": None,
        "cwd": str(ctx.repo_root),
        "argv": ["python3", "fixture.py"],
        "health": "HEALTHY" if running else "STOPPED",
        "ownership": "OWNED_VERIFIED" if running else "OCTAREL_DECLARED",
        "pid": 4242 if running else None,
        "process_create_time": 100.5 if running else None,
        "process_session_id": 4242 if running else None,
        "actions": {
            "start": not running,
            "stop": running,
            "restart": running,
        },
    }
    calls = []
    monkeypatch.setattr(manager, "list_services", lambda **_kwargs: [dict(service)])
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "service_id": service["id"],
            "status": "RUNNING" if running else "STOPPED",
            "actions": dict(service["actions"]),
        },
    )

    def action(action_name, actor, **scope):
        calls.append((action_name, actor, scope))
        return dict(service, health="STARTING")

    monkeypatch.setattr(manager, "action", action)
    client = TestClient(app)
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}

    endpoint = (
        f"/api/app-lifecycle/{action_name}"
        if legacy
        else f"/api/runtime-services/{service['id']}/{action_name}"
    )
    request_payload = {"confirm": True} if running else {}
    initiated = client.post(endpoint, headers=headers, json=request_payload)

    assert initiated.status_code == 200
    assert initiated.json()["status"] == "APPROVAL_REQUESTED"
    approval = initiated.json()["approval_request"]
    assert approval["action_type"] == approvals.ACTION_REMOTE_SENSITIVE
    assert approval["requested_by"] == f"remote:{MAINTAINER_EMAIL}"
    assert calls == []

    resolved = client.post(
        f"/api/approvals/{approval['id']}/resolve",
        headers=headers,
        json={"decision": "APPROVE", "resolution_note": "Remote action remains eligible"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["state"] == "APPROVED"
    assert len(calls) == 1
    assert calls[0][0] == action_name
    assert calls[0][1] == f"remote:{MAINTAINER_EMAIL}"
    messages = [
        event.message for event in ctx.state.list_events(limit=50) if event.category == "remote_audit"
    ]
    audit_verb = f"app_{action_name}" if legacy else f"runtime_service_{action_name}"
    assert any(
        MAINTAINER_EMAIL in message and audit_verb in message and "APPROVAL_REQUESTED" in message
        for message in messages
    )


def test_remote_graphify_check_and_refresh_are_identity_audited(ctx, keypair, monkeypatch):
    from scripts.agents.control_plane import dashboard_api

    monkeypatch.setattr(
        dashboard_api,
        "graphify_capability_status",
        lambda *, probe: {"status": "READY", "path": "/private/bin/graphify", "reason": "ready"},
    )
    queued = []
    monkeypatch.setattr(
        dashboard_api,
        "graphify_request_refresh",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}

    check = client.post("/api/graphify/check", headers=headers, json={})
    refresh = client.post("/api/graphify/refresh", headers=headers, json={})

    assert check.status_code == 200 and "path" not in check.json()
    assert refresh.status_code == 200 and queued
    messages = [e.message for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"]
    assert any(MAINTAINER_EMAIL in message and "graphify_check" in message and "OK" in message for message in messages)
    assert any(MAINTAINER_EMAIL in message and "graphify_refresh" in message and "OK" in message for message in messages)


def test_remote_manual_wake_is_identity_audited(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}

    resp = client.post(
        "/api/wake-queue/manual", headers=headers, json={"task_id": "t1", "stage": "build"}
    )

    assert resp.status_code == 200 and resp.json()["status"]
    messages = [e.message for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"]
    assert any(
        MAINTAINER_EMAIL in message and "wake_queue_manual" in message and "OK" in message
        for message in messages
    )


def test_local_state_changes_are_not_marked_as_remote_audit_events(ctx, keypair):
    """A purely local POST (no Access token at all) must not be misrecorded as

    a remote-identity action even while remote mode is enabled globally.
    """

    _private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    resp = client.post("/api/commands/pause", json={"task_id": "t1"})
    assert resp.status_code == 200
    assert [e for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"] == []


def test_blocked_unconfirmed_destructive_steering_is_still_audited_for_remote(ctx, keypair):
    private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    resp = client.post(
        "/api/steering/execute",
        headers=headers,
        json={"verb": "stop", "args": {"task_id": "t1"}, "raw_text": "/stop t1"},
    )
    assert resp.status_code == 409
    audit_events = [e for e in ctx.state.list_events(limit=50) if e.category == "remote_audit"]
    assert len(audit_events) == 1
    assert "BLOCKED" in audit_events[0].message


# --------------------------------------------------------------------------- security headers


def test_security_headers_present_on_every_response_local_or_remote(ctx, keypair):
    _private_key, public_key = keypair
    client = TestClient(create_app(ctx, remote=_remote_state(public_key)))
    resp = client.get("/api/tasks")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    # ENG-AGENT-02-S7: strengthened beyond a bare "no-store" so a Cloudflare edge
    # (fronting this dashboard since S6) never caches a stale response either.
    assert "no-store" in resp.headers["cache-control"]


def test_security_headers_present_when_remote_disabled_too(ctx):
    client = TestClient(create_app(ctx, remote=_disabled_remote_state()))
    resp = client.get("/api/tasks")
    assert resp.headers["x-frame-options"] == "DENY"


def test_static_dashboard_assets_and_index_are_never_cached(ctx):
    """ENG-AGENT-02-S7 (issue #97): since S6, this dashboard is also reachable

    through a real Cloudflare edge (Tunnel + Access), which applies its own
    default caching rules to static-shaped paths unless told not to. A stale
    cached ``app.js``/``styles.css``/``/`` shell can make a just-shipped UI fix
    invisible to a remote browser indefinitely — exactly the kind of defect
    real dogfooding surfaced (a preset dropdown that stayed empty). Every one
    of these responses must be explicitly uncacheable at every layer.
    """

    client = TestClient(create_app(ctx))
    for path in ("/", "/static/app.js", "/static/styles.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "no-store" in resp.headers["cache-control"], path


# --------------------------------------------------------------------------- rate limiting


def test_repeated_failed_auth_attempts_are_rate_limited(ctx, keypair):
    _private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, hostname=HOSTNAME, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    remote = RemoteAccessState(
        config=config, verifier=verifier, auth_failure_limiter=SlidingWindowLimiter(limit=2, window_seconds=60.0)
    )
    client = TestClient(create_app(ctx, remote=remote))
    headers = {ACCESS_JWT_HEADER: "not-a-real-jwt"}
    assert client.get("/api/identity", headers=headers).status_code == 401
    assert client.get("/api/identity", headers=headers).status_code == 401
    assert client.get("/api/identity", headers=headers).status_code == 429


def test_state_changing_requests_are_rate_limited_per_identity(ctx, keypair):
    private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, hostname=HOSTNAME, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    remote = RemoteAccessState(
        config=config, verifier=verifier, state_change_limiter=SlidingWindowLimiter(limit=1, window_seconds=60.0)
    )
    client = TestClient(create_app(ctx, remote=remote))
    headers = {ACCESS_JWT_HEADER: _token(private_key), "Origin": f"https://{HOSTNAME}"}
    first = client.post("/api/commands/pause", headers=headers, json={"task_id": "t1"})
    second = client.post("/api/commands/resume", headers=headers, json={"task_id": "t1"})
    assert first.status_code == 200
    assert second.status_code == 429


# --------------------------------------------------------------------------- review-driven hardening (issue #95)


def test_repeated_allowlist_rejections_are_rate_limited_too(ctx, keypair):
    """Independent Grok Build review: an authenticated-but-denied identity must

    consume the same failed-auth budget as an invalid signature, so an
    attacker cannot enumerate which emails an allowlist accepts by presenting
    many otherwise-valid tokens for different addresses.
    """

    private_key, public_key = keypair
    config = RemoteAccessConfig(
        enabled=True,
        hostname=HOSTNAME,
        team_domain=TEAM_DOMAIN,
        audience=AUDIENCE,
        allowed_emails=frozenset({"only-this-one@example.com"}),
    )
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    remote = RemoteAccessState(
        config=config, verifier=verifier, auth_failure_limiter=SlidingWindowLimiter(limit=2, window_seconds=60.0)
    )
    client = TestClient(create_app(ctx, remote=remote))
    headers = {ACCESS_JWT_HEADER: _token(private_key, email="denied@example.com")}
    assert client.get("/api/identity", headers=headers).status_code == 403
    assert client.get("/api/identity", headers=headers).status_code == 403
    assert client.get("/api/identity", headers=headers).status_code == 429


def test_rate_limited_failed_auth_is_itself_audited(ctx, keypair):
    _private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, hostname=HOSTNAME, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    remote = RemoteAccessState(
        config=config, verifier=verifier, auth_failure_limiter=SlidingWindowLimiter(limit=1, window_seconds=60.0)
    )
    client = TestClient(create_app(ctx, remote=remote))
    headers = {ACCESS_JWT_HEADER: "not-a-real-jwt"}
    client.get("/api/identity", headers=headers)
    resp = client.get("/api/identity", headers=headers)
    assert resp.status_code == 429
    events = [e for e in ctx.state.list_events(limit=50) if e.category == "remote_access"]
    assert any("rate-limited" in e.message.lower() for e in events)


def test_verifier_refuses_a_config_missing_audience_even_with_a_correctly_signed_token(ctx, keypair):
    """Dashboard-level proof of the AccessVerifier.verify() defense-in-depth fix:

    a RemoteAccessState built with no audience configured must reject every
    token outright, never silently accept on signature alone.
    """

    private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, hostname=HOSTNAME, team_domain=TEAM_DOMAIN, audience=None)
    verifier = AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))
    remote = RemoteAccessState(config=config, verifier=verifier)
    client = TestClient(create_app(ctx, remote=remote))
    resp = client.get("/api/identity", headers={ACCESS_JWT_HEADER: _token(private_key)})
    assert resp.status_code == 401
