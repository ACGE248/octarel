"""ENG-AGENT-02-S6 (issue #95): Cloudflare Access verification, allowlist,

CSRF/origin protection, and rate limiting for the secure remote Control
Center. Every test here uses a locally generated throwaway RSA keypair and a
fake JWKS fetcher — no real Cloudflare account, network access, or
credentials are ever required (acceptance criteria 13/15).
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from scripts.agents.control_plane.remote_access import (
    ENV_ALLOWED_EMAILS,
    ENV_AUD,
    ENV_ENABLED,
    ENV_HOSTNAME,
    ENV_TEAM_DOMAIN,
    AccessIdentityError,
    AccessVerifier,
    RemoteAccessConfig,
    SlidingWindowLimiter,
    extract_access_token,
    origin_matches_hostname,
    request_carries_access_assertion,
)

TEAM_DOMAIN = "testteam.cloudflareaccess.com"
AUDIENCE = "test-application-aud-tag"
KID = "test-key-1"


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _jwks_dict(public_key, *, kid: str = KID) -> dict:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


def _sign(private_key, claims: dict, *, kid: str = KID) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "email": "maintainer@example.com",
        "aud": AUDIENCE,
        "iss": f"https://{TEAM_DOMAIN}",
        "iat": now,
        "exp": now + 300,
        "sub": "user-123",
    }
    claims.update(overrides)
    return claims


@pytest.fixture()
def config() -> RemoteAccessConfig:
    return RemoteAccessConfig(enabled=True, hostname="dev.octascene.com", team_domain=TEAM_DOMAIN, audience=AUDIENCE)


@pytest.fixture()
def keypair():
    return _generate_keypair()


def _verifier_for(config: RemoteAccessConfig, public_key) -> AccessVerifier:
    return AccessVerifier(config, jwks_fetcher=lambda url: _jwks_dict(public_key))


# --------------------------------------------------------------------------- config


def test_config_defaults_to_disabled_with_no_environment():
    config = RemoteAccessConfig.from_env({})
    assert config.enabled is False
    assert config.hostname == "dev.octascene.com"
    assert config.allowed_emails == frozenset()


def test_config_from_env_parses_every_field():
    env = {
        ENV_ENABLED: "true",
        ENV_HOSTNAME: "dev.octascene.com",
        ENV_TEAM_DOMAIN: TEAM_DOMAIN,
        ENV_AUD: AUDIENCE,
        ENV_ALLOWED_EMAILS: "Maintainer@Example.com, second@example.com ,",
    }
    config = RemoteAccessConfig.from_env(env)
    assert config.enabled is True
    assert config.team_domain == TEAM_DOMAIN
    assert config.audience == AUDIENCE
    # Emails are normalized to lowercase so a case-mismatched Access claim still matches.
    assert config.allowed_emails == frozenset({"maintainer@example.com", "second@example.com"})


@pytest.mark.parametrize("falsy", ["0", "false", "", "no", "off"])
def test_config_enabled_requires_an_explicit_truthy_value(falsy):
    assert RemoteAccessConfig.from_env({ENV_ENABLED: falsy}).enabled is False


def test_startup_problems_empty_when_disabled():
    assert RemoteAccessConfig(enabled=False).startup_problems() == []


def test_startup_problems_requires_team_domain_and_audience_when_enabled():
    problems = RemoteAccessConfig(enabled=True).startup_problems()
    assert any("TEAM_DOMAIN" in p for p in problems)
    assert any("AUD" in p for p in problems)


def test_startup_problems_warns_on_empty_allowlist_but_team_domain_and_aud_present():
    config = RemoteAccessConfig(enabled=True, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    problems = config.startup_problems()
    assert len(problems) == 1
    assert "ALLOWED_EMAILS" in problems[0]


# --------------------------------------------------------------------------- token extraction


def test_extract_access_token_prefers_header_over_cookie():
    headers = {"Cf-Access-Jwt-Assertion": "header-token"}
    cookies = {"CF_Authorization": "cookie-token"}
    assert extract_access_token(headers, cookies) == "header-token"


def test_extract_access_token_falls_back_to_cookie():
    assert extract_access_token({}, {"CF_Authorization": "cookie-token"}) == "cookie-token"


def test_extract_access_token_header_lookup_is_case_insensitive():
    headers = {"CF-ACCESS-JWT-ASSERTION": "shout-case-token"}
    assert extract_access_token(headers, {}) == "shout-case-token"


def test_request_carries_access_assertion_false_for_an_ordinary_local_request():
    assert request_carries_access_assertion({}, {}) is False


def test_request_carries_access_assertion_true_when_either_is_present():
    assert request_carries_access_assertion({"Cf-Access-Jwt-Assertion": "x"}, {}) is True
    assert request_carries_access_assertion({}, {"CF_Authorization": "x"}) is True


# --------------------------------------------------------------------------- origin check


@pytest.mark.parametrize(
    "origin,hostname,expected",
    [
        ("https://dev.octascene.com", "dev.octascene.com", True),
        ("https://DEV.OCTASCENE.COM", "dev.octascene.com", True),
        ("http://127.0.0.1:8899", "127.0.0.1:8899", True),
        ("https://evil.example.com", "dev.octascene.com", False),
        (None, "dev.octascene.com", False),
        ("not a url", "dev.octascene.com", False),
        ("https://dev.octascene.com.evil.com", "dev.octascene.com", False),
    ],
)
def test_origin_matches_hostname(origin, hostname, expected):
    assert origin_matches_hostname(origin, hostname) is expected


# --------------------------------------------------------------------------- AccessVerifier


def test_verify_accepts_a_correctly_signed_token(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    token = _sign(private_key, _base_claims())
    identity = verifier.verify(token)
    assert identity.email == "maintainer@example.com"
    assert identity.subject == "user-123"


def test_verify_rejects_a_token_signed_by_a_different_key(config, keypair):
    """The core anti-spoofing test (issue #94/#95 criterion 6): a token that

    is well-formed and even carries the right claims, but was never actually
    signed by Cloudflare's real private key, must be rejected outright.
    """

    _real_private, real_public = keypair
    attacker_private, _attacker_public = _generate_keypair()
    verifier = _verifier_for(config, real_public)
    forged = _sign(attacker_private, _base_claims(), kid=KID)  # same kid, wrong key
    with pytest.raises(AccessIdentityError, match="invalid Cloudflare Access token"):
        verifier.verify(forged)


def test_verify_rejects_wrong_audience(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    token = _sign(private_key, _base_claims(aud="some-other-applications-aud"))
    with pytest.raises(AccessIdentityError):
        verifier.verify(token)


def test_verify_rejects_wrong_issuer(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    token = _sign(private_key, _base_claims(iss="https://not-our-team.cloudflareaccess.com"))
    with pytest.raises(AccessIdentityError):
        verifier.verify(token)


def test_verify_rejects_an_expired_token(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    now = int(time.time())
    token = _sign(private_key, _base_claims(iat=now - 1000, exp=now - 500))
    with pytest.raises(AccessIdentityError):
        verifier.verify(token)


def test_verify_rejects_a_token_with_no_email_claim(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    claims = _base_claims()
    del claims["email"]
    token = _sign(private_key, claims)
    with pytest.raises(AccessIdentityError, match="email"):
        verifier.verify(token)


def test_verify_rejects_malformed_tokens(config, keypair):
    _private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    with pytest.raises(AccessIdentityError):
        verifier.verify("not-a-jwt-at-all")
    with pytest.raises(AccessIdentityError):
        verifier.verify("")


def test_verify_rejects_a_token_whose_kid_is_not_in_the_jwks(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    token = _sign(private_key, _base_claims(), kid="some-unknown-kid")
    with pytest.raises(AccessIdentityError, match="signing key"):
        verifier.verify(token)


def test_verify_refreshes_jwks_once_when_kid_is_rotated(config, keypair):
    """A key rotation on Cloudflare's side must not require a restart: the

    verifier refreshes its cache exactly once per unknown kid before giving up.
    """

    old_private, old_public = keypair
    new_private, new_public = _generate_keypair()
    calls = {"n": 0}

    def fetcher(url):
        calls["n"] += 1
        # Simulate rotation: the first fetch only knows the old key, every
        # subsequent fetch (i.e. the forced refresh) knows the new one too.
        if calls["n"] == 1:
            return _jwks_dict(old_public, kid="old-kid")
        return _jwks_dict(new_public, kid="new-kid")

    verifier = AccessVerifier(config, jwks_fetcher=fetcher)
    token = _sign(new_private, _base_claims(), kid="new-kid")
    identity = verifier.verify(token)
    assert identity.email == "maintainer@example.com"
    assert calls["n"] == 2


def test_verify_raises_when_no_team_domain_is_configured(keypair):
    _private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, team_domain=None, audience=AUDIENCE)
    verifier = _verifier_for(config, public_key)
    with pytest.raises(AccessIdentityError, match="team domain"):
        verifier.verify(_sign(_generate_keypair()[0], _base_claims()))


def test_verify_raises_when_no_audience_is_configured_rather_than_skipping_the_check(keypair):
    """Defense in depth (independent Grok Build review, issue #95): PyJWT's

    ``jwt.decode(audience=None, ...)`` silently *skips* the audience check
    rather than failing, so a config with no audience must never be allowed
    to reach that call at all -- otherwise any validly-signed token for any
    Access application under the same team domain would pass, not only the
    application this deployment is meant to trust.
    """

    private_key, public_key = keypair
    config = RemoteAccessConfig(enabled=True, team_domain=TEAM_DOMAIN, audience=None)
    verifier = _verifier_for(config, public_key)
    # Even a token that would otherwise be perfectly valid (correct key,
    # correct issuer) must still be rejected, precisely because it was never
    # actually checked against any specific audience.
    token = _sign(private_key, _base_claims())
    with pytest.raises(AccessIdentityError, match="audience"):
        verifier.verify(token)


def test_verify_raises_before_any_jwks_fetch_when_audience_is_missing(keypair):
    """The audience guard must fire before any network-shaped work happens."""

    _private_key, public_key = keypair
    calls = {"n": 0}

    def fetcher(url):
        calls["n"] += 1
        return _jwks_dict(public_key)

    config = RemoteAccessConfig(enabled=True, team_domain=TEAM_DOMAIN, audience=None)
    verifier = AccessVerifier(config, jwks_fetcher=fetcher)
    with pytest.raises(AccessIdentityError, match="audience"):
        verifier.verify(_sign(_generate_keypair()[0], _base_claims()))
    assert calls["n"] == 0


# --------------------------------------------------------------------------- allowlist


def test_is_allowed_accepts_anyone_when_allowlist_is_empty(config, keypair):
    private_key, public_key = keypair
    verifier = _verifier_for(config, public_key)
    identity = verifier.verify(_sign(private_key, _base_claims(email="anyone@example.com")))
    assert verifier.is_allowed(identity) is True


def test_is_allowed_rejects_identities_outside_a_configured_allowlist(keypair):
    private_key, public_key = keypair
    config = RemoteAccessConfig(
        enabled=True, team_domain=TEAM_DOMAIN, audience=AUDIENCE, allowed_emails=frozenset({"maintainer@example.com"})
    )
    verifier = _verifier_for(config, public_key)
    allowed = verifier.verify(_sign(private_key, _base_claims(email="maintainer@example.com")))
    denied = verifier.verify(_sign(private_key, _base_claims(email="stranger@example.com")))
    assert verifier.is_allowed(allowed) is True
    assert verifier.is_allowed(denied) is False


# --------------------------------------------------------------------------- rate limiter


def test_sliding_window_limiter_blocks_after_the_limit_then_recovers():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=10.0)
    now = 1000.0
    assert limiter.allow("k", now=now) is True
    assert limiter.allow("k", now=now) is True
    assert limiter.allow("k", now=now) is True
    assert limiter.allow("k", now=now) is False  # 4th within the window is blocked
    assert limiter.allow("k", now=now + 11.0) is True  # window has slid past the old hits


def test_sliding_window_limiter_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=10.0)
    assert limiter.allow("a", now=0.0) is True
    assert limiter.allow("b", now=0.0) is True
    assert limiter.allow("a", now=0.0) is False


# --------------------------------------------------------------------------- loopback bind guard


def test_dashboard_never_treats_a_public_host_as_loopback():
    from scripts.agents.orchestrator import _is_loopback_host

    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("LOCALHOST") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("192.168.1.5") is False
    assert _is_loopback_host("dev.octascene.com") is False
    assert _is_loopback_host("not-an-ip-or-known-host") is False
