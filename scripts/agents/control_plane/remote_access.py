"""ENG-AGENT-02-S6 (issue #95): secure remote access for the Control Center.

Preferred topology: ``dev.octascene.com -> Cloudflare Access -> Cloudflare
Tunnel -> 127.0.0.1:8877 -> local Control Center``. This module implements the
*application-side* half of that defense-in-depth: it never talks to the
network on its own except to fetch Cloudflare's public JWKS (and even that is
injectable for tests), never stores a secret, and never changes behavior
unless remote access is explicitly enabled via configuration.

Design summary
---------------
- **Disabled by default.** :func:`RemoteAccessConfig.from_env` returns
  ``enabled=False`` unless ``OCTAGES_ORCH_REMOTE_ENABLED`` is explicitly set.
  Disabled means every function in this module that could reject/rate-limit/
  audit a request is never called at all — a request's *authentication,
  authorization, rate-limiting, and audit* behavior is unaffected in every
  way. (The dashboard's response also gains a small, harmless, always-on
  addition regardless of this flag — a few extra security headers and two
  new `/api/identity` fields — see :mod:`dashboard_api`'s middleware
  docstring; this module itself performs zero work when disabled.)
- **A request only enters this module's logic at all if it itself carries a
  Cloudflare Access identity assertion** (the presence check, not the
  verification) — never based on source IP or ``Host`` header, both of which
  are attacker-controllable on the same machine the tunnel runs on, since
  Cloudflare Tunnel always connects to the origin over local loopback (this
  matches Cloudflare's own documented pattern for self-hosted applications
  validating Access JWTs). A request that carries neither
  ``Cf-Access-Jwt-Assertion`` nor a ``CF_Authorization`` cookie is treated as
  an ordinary trusted local request, exactly as before this slice existed —
  **this is a deliberate, documented threat-model choice, not an oversight**:
  Cloudflare Access is what must prevent an unauthenticated stranger from
  ever reaching the tunnel's local origin in the first place. This module
  cannot and does not distinguish "the tunnel forwarded this after Access
  authenticated it" from "a local process on this same machine talked to the
  origin directly" other than by the assertion's presence, and a local
  process by definition already has the same trust an ordinary local
  operator has always had. Never combine
  ``OCTAGES_ORCH_ALLOW_NONLOOPBACK_BIND`` with remote mode: this module's
  protection only ever applies to requests carrying an assertion, so a
  non-loopback bind would expose the dashboard to any request that simply
  omits one.
- **The identity assertion is never trusted at face value.** It is a signed
  JWT; :class:`AccessVerifier` fetches (and caches) Cloudflare's published
  JWKS for the configured team domain and cryptographically verifies the
  signature, audience, issuer, and expiry before extracting an ``email``
  claim. A request that merely *sets* the header/cookie without a valid
  signature is rejected — this is precisely what stops a local script from
  spoofing remote privileges (issue #95 acceptance criterion 6).
- **JWKS fetching is fully injectable** (``jwks_fetcher``) so every test in
  this repository verifies real signature/audience/issuer/expiry checking
  against a locally generated throwaway RSA keypair — no live network call,
  no real Cloudflare account or credentials, ever required (criteria 13/15).
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Mapping
from urllib.parse import urlparse

import jwt
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm

ACCESS_JWT_HEADER = "cf-access-jwt-assertion"
ACCESS_COOKIE_NAME = "CF_Authorization"

ENV_ENABLED = "OCTAGES_ORCH_REMOTE_ENABLED"
ENV_HOSTNAME = "OCTAGES_ORCH_REMOTE_HOSTNAME"
ENV_TEAM_DOMAIN = "OCTAGES_ORCH_REMOTE_TEAM_DOMAIN"
ENV_AUD = "OCTAGES_ORCH_REMOTE_AUD"
ENV_ALLOWED_EMAILS = "OCTAGES_ORCH_REMOTE_ALLOWED_EMAILS"

DEFAULT_HOSTNAME = "dev.octascene.com"
JWKS_CACHE_TTL_SECONDS = 3600.0

# Rate limits are intentionally generous for legitimate dashboard polling
# (every read view refreshes every ~2s, per the existing client) but bound
# how fast an attacker can burn through failed-auth or state-changing
# attempts. Pure in-process sliding windows: no paid infra, no external
# dependency, matches this codebase's existing "no paid infrastructure"
# constraint for dev tooling.
AUTH_FAILURE_LIMIT = 20
AUTH_FAILURE_WINDOW_SECONDS = 300.0
STATE_CHANGE_LIMIT = 60
STATE_CHANGE_WINDOW_SECONDS = 60.0


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RemoteAccessConfig:
    """Explicit, disabled-by-default remote-access configuration.

    Built once at dashboard startup from process environment variables (see
    the module docstring for the exact names) — never from a client request,
    never from a database row a remote caller could influence.
    """

    enabled: bool = False
    hostname: str = DEFAULT_HOSTNAME
    team_domain: str | None = None
    audience: str | None = None
    allowed_emails: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RemoteAccessConfig":
        env = env if env is not None else os.environ
        enabled = _truthy(env.get(ENV_ENABLED, ""))
        hostname = env.get(ENV_HOSTNAME, "").strip() or DEFAULT_HOSTNAME
        team_domain = env.get(ENV_TEAM_DOMAIN, "").strip() or None
        audience = env.get(ENV_AUD, "").strip() or None
        raw_emails = env.get(ENV_ALLOWED_EMAILS, "")
        allowed = frozenset(e.strip().lower() for e in raw_emails.split(",") if e.strip())
        return cls(enabled=enabled, hostname=hostname, team_domain=team_domain, audience=audience, allowed_emails=allowed)

    def startup_problems(self) -> list[str]:
        """Configuration problems that make ``enabled=True`` unsafe to serve.

        Checked once at dashboard startup (never per-request) so a
        misconfigured deployment fails loudly and immediately rather than
        silently accepting unverifiable tokens.
        """

        if not self.enabled:
            return []
        problems = []
        if not self.team_domain:
            problems.append(f"{ENV_TEAM_DOMAIN} is required when remote access is enabled")
        if not self.audience:
            problems.append(f"{ENV_AUD} is required when remote access is enabled")
        if not self.allowed_emails:
            problems.append(
                f"{ENV_ALLOWED_EMAILS} is empty: any Cloudflare-Access-authenticated identity for this "
                "application would be accepted. Set it to your maintainer email(s) unless that is intended."
            )
        return problems

    @property
    def certs_url(self) -> str | None:
        if not self.team_domain:
            return None
        return f"https://{self.team_domain}/cdn-cgi/access/certs"

    @property
    def issuer(self) -> str | None:
        if not self.team_domain:
            return None
        return f"https://{self.team_domain}"


class AccessIdentityError(ValueError):
    """A Cloudflare Access identity assertion failed verification or policy."""


@dataclass(frozen=True)
class RemoteIdentity:
    email: str
    subject: str | None
    verified_at: float


def _default_jwks_fetcher(url: str) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or "keys" not in data:
        raise AccessIdentityError("malformed JWKS response from Cloudflare Access")
    return data


class AccessVerifier:
    """Verifies a Cloudflare Access JWT against the team's published JWKS.

    Never performs its own network I/O directly — ``jwks_fetcher`` is the
    only seam that can reach the network, defaulting to a short-timeout
    ``httpx`` GET. Every test in this repository supplies a fake fetcher
    backed by a locally generated RSA keypair, so signature/audience/issuer
    verification is exercised without any live Cloudflare account.
    """

    def __init__(
        self,
        config: RemoteAccessConfig,
        *,
        jwks_fetcher: Callable[[str], dict[str, Any]] | None = None,
        cache_ttl_seconds: float = JWKS_CACHE_TTL_SECONDS,
    ) -> None:
        self._config = config
        self._fetcher = jwks_fetcher or _default_jwks_fetcher
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0

    def _jwks(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if force_refresh or self._cache is None or (now - self._cache_at) > self._cache_ttl:
            url = self._config.certs_url
            if not url:
                raise AccessIdentityError("remote access is enabled but no team domain is configured")
            self._cache = self._fetcher(url)
            self._cache_at = now
        return self._cache

    def _signing_key_for(self, kid: str):
        for force_refresh in (False, True):  # a rotated key deserves exactly one forced refresh, not a retry loop
            jwks = self._jwks(force_refresh=force_refresh)
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    return RSAAlgorithm.from_jwk(json.dumps(key))
        raise AccessIdentityError(f"no Cloudflare Access signing key found for kid={kid!r}")

    def verify(self, token: str) -> RemoteIdentity:
        """Return the verified :class:`RemoteIdentity`, or raise :class:`AccessIdentityError`.

        Refuses outright, before any signature work, if either ``audience``
        or ``team_domain`` is unset. This is deliberate defense in depth
        beyond ``RemoteAccessConfig.startup_problems``/the orchestrator CLI's
        own hard-stop (independent review, issue #95): ``PyJWT`` silently
        *skips* the audience/issuer check entirely when the corresponding
        ``jwt.decode`` argument is ``None`` rather than failing, so a
        ``RemoteAccessState`` ever constructed with a missing audience (e.g.
        by a future caller that bypasses the CLI's own startup validation)
        must not be allowed to fall through to "verify signature only" — that
        would accept any validly-signed token for *any* Access application
        under the same Cloudflare team, not only this one.
        """

        if not self._config.audience or not self._config.team_domain:
            raise AccessIdentityError(
                "remote access is enabled but audience/team domain is not configured; refusing to verify any token"
            )
        if not token or not isinstance(token, str):
            raise AccessIdentityError("missing Cloudflare Access token")
        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:  # noqa: BLE001 - any malformed token is the same outcome: reject
            raise AccessIdentityError(f"malformed Cloudflare Access token: {exc}") from None
        kid = header.get("kid")
        if not kid:
            raise AccessIdentityError("Cloudflare Access token header is missing 'kid'")
        signing_key = self._signing_key_for(kid)
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={"require": ["exp", "iat", "aud"]},
            )
        except InvalidTokenError as exc:
            raise AccessIdentityError(f"invalid Cloudflare Access token: {exc}") from None
        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise AccessIdentityError("Cloudflare Access token has no 'email' claim")
        return RemoteIdentity(email=email, subject=claims.get("sub"), verified_at=time.time())

    def is_allowed(self, identity: RemoteIdentity) -> bool:
        """True if ``identity`` may use the Control Center remotely.

        An empty allowlist accepts any identity Cloudflare Access already
        cryptographically vouched for (still gated by team domain + audience
        above) — :meth:`RemoteAccessConfig.startup_problems` flags that
        configuration loudly rather than silently allowing it unnoticed.
        """

        if not self._config.allowed_emails:
            return True
        return identity.email in self._config.allowed_emails


def extract_access_token(headers: Mapping[str, str], cookies: Mapping[str, str]) -> str | None:
    """Pull a Cloudflare Access token from the header Cloudflare Tunnel injects,

    falling back to the browser-session cookie Cloudflare Access itself sets.
    Header wins when both are present (matches Cloudflare's own documented
    precedence for tunnel-forwarded requests).
    """

    for name, value in headers.items():
        if name.lower() == ACCESS_JWT_HEADER:
            return value
    return cookies.get(ACCESS_COOKIE_NAME)


def request_carries_access_assertion(headers: Mapping[str, str], cookies: Mapping[str, str]) -> bool:
    """Whether this request even claims to be Access-authenticated.

    Used to decide *whether the remote code path applies at all* — an
    ordinary local request (opening the dashboard directly at
    ``http://127.0.0.1:8877``) carries neither and is completely unaffected
    by remote mode being enabled, preserving criterion 12.
    """

    return extract_access_token(headers, cookies) is not None


def origin_matches_hostname(origin_or_referer: str | None, hostname: str) -> bool:
    """CSRF/origin check: does the request's Origin (or Referer) point at

    the exact configured remote hostname, regardless of scheme/port
    formatting quirks between environments (``https://dev.octascene.com`` in
    production, ``http://127.0.0.1:PORT`` pointed at the same value in a
    Playwright fixture)?
    """

    if not origin_or_referer:
        return False
    try:
        netloc = urlparse(origin_or_referer).netloc
    except ValueError:
        return False
    return netloc.lower() == hostname.lower()


class SlidingWindowLimiter:
    """A tiny in-process sliding-window rate limiter. No external dependency,

    no persistence (a daemon/dashboard restart simply resets it, which is
    acceptable for this dev-tooling abuse-slowing purpose, not a hard
    security boundary on its own — Cloudflare Access authentication is).
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, Deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return True


@dataclass
class RemoteAccessState:
    """Per-process runtime state the dashboard middleware needs: the verifier

    plus the two rate limiters. Kept separate from :class:`RemoteAccessConfig`
    (immutable, env-derived) so tests can construct one with a fake verifier
    and fresh limiters without touching process environment variables.
    """

    config: RemoteAccessConfig
    verifier: AccessVerifier
    auth_failure_limiter: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(limit=AUTH_FAILURE_LIMIT, window_seconds=AUTH_FAILURE_WINDOW_SECONDS)
    )
    state_change_limiter: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(limit=STATE_CHANGE_LIMIT, window_seconds=STATE_CHANGE_WINDOW_SECONDS)
    )

    @classmethod
    def from_config(cls, config: RemoteAccessConfig, **verifier_kwargs: Any) -> "RemoteAccessState":
        return cls(config=config, verifier=AccessVerifier(config, **verifier_kwargs))
