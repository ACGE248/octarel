"""ENG-AGENT-02-S6 (issue #95): dashboard-level integration tests for secure

remote access — header spoofing, allowlist enforcement, CSRF/origin
protection, security headers, audit trail, and rate limiting, all exercised
through the real FastAPI app via ``TestClient``. Every Cloudflare Access
token here is signed with a locally generated throwaway RSA keypair verified
against a fake (non-network) JWKS fetcher — no live Cloudflare account,
network call, or credential is ever required (criteria 13/15).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.dashboard_api import (
    _terminal_websocket_identity,
    create_app,
)
from scripts.agents.control_plane.models import Task
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
