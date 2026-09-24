"""Dynamic OpenCode model catalog, free-model classification, qualification, and pool selection (ENG-AO-03).

The hard-coded OpenCode workers in ``workers.json`` stay the stable, configured identities.  This module adds a
provider-neutral, *runtime* view of whatever the installed OpenCode currently offers so that AO can use an eligible
free read-only model as a fallback without a repository edit each time the catalog changes:

    ``opencode models --verbose``  ->  discovery  ->  cost/auth + capability classification
        ->  bounded qualification (cached)  ->  eligible free pool  ->  routing / evidence

Safety properties (all fail closed):

* discovery uses only OpenCode's local, machine-readable ``models --verbose`` / ``providers list`` /
  ``debug agent`` commands (never the interactive UI, never a generation, never ``auth.json``);
* a model is a *free fallback candidate* only when OpenCode's own metadata proves it: an OpenCode Zen transport with a
  declared zero cost on every price field.  A display name containing "Free" proves nothing.  Credential-backed
  providers are ``subscription`` (oauth) or ``metered`` (API key); anything unclear is ``unknown`` and never enters
  automatic free fallback;
* a discovered model is not an unattended reviewer until a one-off bounded probe qualified it (launch, agent preset,
  read-only permissions, unchanged tree, strict review contract, no API-key environment).  Only proven-free models are
  ever probed, and the result is cached against the OpenCode version / model identity / preset content;
* models are only ever selected for read-only roles; nothing here can promote a model to a write-capable worker;
* a same-vendor candidate is rejected when provider diversity is required, and a quota/rate/context failure moves to
  another free candidate (via a cooldown) instead of a stronger model.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from .graph_context import state_root
from .redaction import redact_text

SCHEMA = 1
STATE_DIRNAME = "opencode-models"
CATALOG_FILENAME = "catalog.json"
QUALIFICATION_FILENAME = "qualifications.json"

OPENCODE_BIN = "opencode"
POOL_OPENCODE_FREE = "opencode-free"
POOL_PROVIDER_LABEL = "OpenCode Zen"

# Catalog status.
CATALOG_OK = "ok"
CATALOG_EMPTY = "empty"
CATALOG_ABSENT = "absent"
CATALOG_UNAVAILABLE = "unavailable"
CATALOG_UNPARSEABLE = "unparseable"

# Cost / auth classification.
COST_FREE_OPENCODE = "free-opencode"  # proven free / no-charge OpenCode (Zen) transport
COST_SUBSCRIPTION = "subscription"  # credential is an OAuth/subscription session
COST_METERED = "metered"  # API key or paid Zen usage
COST_UNKNOWN = "unknown"  # not proven; never eligible for automatic free fallback
COST_CLASSES = (COST_FREE_OPENCODE, COST_SUBSCRIPTION, COST_METERED, COST_UNKNOWN)

# Qualification.
QUALIFIED = "qualified"
UNQUALIFIED = "unqualified"
UNTESTED = "untested"
COOLING_DOWN = "cooling-down"

# Dashboard/API states.
STATE_QUALIFIED_FREE = "qualified-free-fallback"
STATE_UNQUALIFIED = "unqualified"
STATE_UNTESTED_FREE = "free-untested"
STATE_COOLING_DOWN = "cooling-down"
STATE_SUBSCRIPTION = "subscription-backed"
STATE_METERED = "metered-ineligible"
STATE_UNKNOWN_COST = "unknown-cost-ineligible"
STATE_INCAPABLE = "incapable"

PROFILE_REVIEW = "review"  # diff review + documentation drift review
PROFILE_TESTS = "tests"  # focused tests, mechanical testing, impact search, bounded research
PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_REVIEW: {"agent": "reviewer", "require_deny": ("edit", "task", "bash")},
    PROFILE_TESTS: {"agent": "tester", "require_deny": ("edit", "task")},
}
ROLE_PROFILES = {
    "diff-review": PROFILE_REVIEW,
    "doc-drift-review": PROFILE_REVIEW,
    "focused-tests": PROFILE_TESTS,
    "mechanical-testing": PROFILE_TESTS,
    "impact-search": PROFILE_TESTS,
}

PROBE_VERSION = 1
MIN_CONTEXT_TOKENS = 128_000
CATALOG_MAX_AGE_SECONDS = 1800
QUALIFIED_TTL_SECONDS = 7 * 24 * 3600
UNQUALIFIED_TTL_SECONDS = 3600
COOLDOWN_SECONDS = 3600
COOLDOWN_CATEGORIES = frozenset({"quota", "rate-limit", "context-limit", "provider-outage"})
DISCOVERY_TIMEOUT_SECONDS = 30.0
PROBE_TIMEOUT_SECONDS = 180.0
MAX_PROBES_PER_SELECTION = 2

# A vendor's other names, so a same-vendor model hosted behind another transport is still recognised.
_VENDOR_ALIASES = {
    "xai": ("xai", "grok"),
    "google": ("google", "gemini", "gemma"),
    "openai": ("openai", "gpt", "codex", "chatgpt"),
    "anthropic": ("anthropic", "claude"),
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HEADER = re.compile(r"^([A-Za-z0-9_.\-]+/\S+)[ \t]*$", re.MULTILINE)
_CREDENTIAL_LINE = re.compile(r"^\W*\s*(?P<name>[A-Za-z0-9][\w .\-/]*?)\s+(?P<kind>oauth|api|wellknown)\s*$", re.IGNORECASE)


def _now_dt() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(moment: _dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_iso(value: Any) -> _dt.datetime | None:
    try:
        parsed = _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- command runner seam


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None  # spawn failure or "timeout"


Runner = Callable[[Sequence[str], "Path | None", float, "Mapping[str, str] | None"], CommandResult]


def subprocess_runner(
    argv: Sequence[str], cwd: Path | None, timeout: float, env: Mapping[str, str] | None
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=timeout, env=dict(env) if env else None,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(None, error="timeout")
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(None, error=f"{type(exc).__name__}: {exc}")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def _discovery_env() -> dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return env


# --------------------------------------------------------------------------- data model


@dataclass(frozen=True)
class CatalogModel:
    id: str  # "<providerID>/<modelID>", the value passed to ``opencode --model``
    provider_id: str
    model_id: str
    display_name: str
    family: str
    status: str
    api_url: str
    context_limit: int | None
    output_limit: int | None
    cost: dict[str, Any] | None  # declared price fields, ``None`` when OpenCode did not declare them
    credential: str  # oauth | api | wellknown | none | unknown
    cost_class: str
    cost_reason: str
    capable: bool
    capability_reasons: tuple[str, ...]

    def identity(self) -> str:
        """Stable digest of everything that decides eligibility, used to invalidate qualification."""

        payload = json.dumps(
            [self.id, self.api_url, self.cost, self.context_limit, self.status, self.cost_class], sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Catalog:
    status: str
    reason: str
    opencode_version: str | None
    refreshed_at: str
    credentials_status: str  # ok | unavailable
    models: tuple[CatalogModel, ...] = ()
    skipped_entries: int = 0
    source: str = "opencode models --verbose"

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for model in sorted(self.models, key=lambda item: item.id):
            digest.update(model.identity().encode())
        return digest.hexdigest()[:16]

    def get(self, model_id: str) -> CatalogModel | None:
        return next((model for model in self.models if model.id == model_id), None)

    def evidence(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "opencode_version": self.opencode_version,
            "refreshed_at": self.refreshed_at,
            "fingerprint": self.fingerprint,
            "model_count": len(self.models),
            "skipped_entries": self.skipped_entries,
            "credentials_status": self.credentials_status,
            "source": self.source,
        }

    def to_json(self) -> dict[str, Any]:
        body = asdict(self)
        body["schema"] = SCHEMA
        return body


def catalog_from_json(body: Mapping[str, Any]) -> Catalog | None:
    try:
        if body.get("schema") != SCHEMA:
            return None
        models = tuple(
            CatalogModel(
                **{**raw, "capability_reasons": tuple(raw.get("capability_reasons", ()))}
            )
            for raw in body.get("models", ())
        )
        return Catalog(
            status=str(body["status"]), reason=str(body["reason"]), opencode_version=body.get("opencode_version"),
            refreshed_at=str(body["refreshed_at"]), credentials_status=str(body.get("credentials_status", "unavailable")),
            models=models, skipped_entries=int(body.get("skipped_entries", 0)), source=str(body.get("source", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- state files


def opencode_state_dir() -> Path:
    return state_root() / STATE_DIRNAME


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write_json(path: Path, body: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass  # a cache write failure only costs a later re-discovery


def load_catalog(state_dir: Path | None = None) -> Catalog | None:
    body = _read_json((state_dir or opencode_state_dir()) / CATALOG_FILENAME)
    return catalog_from_json(body) if body else None


def catalog_age_seconds(catalog: Catalog, now: _dt.datetime | None = None) -> float | None:
    refreshed = _parse_iso(catalog.refreshed_at)
    return None if refreshed is None else max(0.0, ((now or _now_dt()) - refreshed).total_seconds())


# --------------------------------------------------------------------------- discovery


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _parse_credentials(text: str) -> dict[str, str]:
    """Map a normalised provider name to its credential kind from ``opencode providers list`` output."""

    credentials: dict[str, str] = {}
    for line in _strip_ansi(text).splitlines():
        match = _CREDENTIAL_LINE.match(line.strip("│●└┌ \t"))
        if match:
            credentials[_normalise(match.group("name"))] = match.group("kind").lower()
    return credentials


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _numbers(node: Any) -> list[Any] | None:
    """Every price leaf of a declared cost object, or ``None`` when any leaf is not a plain number.

    OpenCode also nests context-size tier descriptors under a ``tier`` key (``{"type": "context", "size": ...}``);
    those describe *when* a price applies and are not prices, so they are skipped.
    """

    if isinstance(node, bool):
        return None
    if isinstance(node, (int, float)):
        return [node]
    if isinstance(node, list):
        node = dict(enumerate(node))
    if isinstance(node, dict):
        leaves: list[Any] = []
        for key, value in node.items():
            if key == "tier":
                continue
            inner = _numbers(value)
            if inner is None:
                return None
            leaves.extend(inner)
        return leaves
    return None


def classify_cost(
    *, provider_id: str, api_url: str, cost: Any, credential: str, credentials_known: bool
) -> tuple[str, str]:
    """Authoritative cost/auth class and the reason, from OpenCode's own metadata only (never the name)."""

    if not isinstance(cost, dict) or not isinstance(cost.get("input"), (int, float)) or not isinstance(
        cost.get("output"), (int, float)
    ):
        return COST_UNKNOWN, "OpenCode declared no complete cost metadata"
    leaves = _numbers(cost)
    if leaves is None:
        return COST_UNKNOWN, "OpenCode declared non-numeric cost metadata"
    parsed = urlparse(api_url or "")
    zen = provider_id == "opencode" and parsed.hostname == "opencode.ai" and parsed.path.startswith("/zen")
    if zen:
        if all(value == 0 for value in leaves):
            return COST_FREE_OPENCODE, "OpenCode Zen transport with zero declared cost on every price field"
        return COST_METERED, "OpenCode Zen model with a non-zero declared price"
    if not credentials_known:
        return COST_UNKNOWN, "provider credentials could not be inspected"
    if credential == "oauth":
        return COST_SUBSCRIPTION, "provider authenticated through an OAuth/subscription session"
    if credential == "api":
        return COST_METERED, "provider authenticated with an API key (metered)"
    if credential == "none":
        return COST_UNKNOWN, "provider has no credential OpenCode reports; transport billing is unproven"
    return COST_UNKNOWN, f"credential kind {credential!r} does not prove a no-charge transport"


def assess_capability(entry: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Coarse read-only-role capability from OpenCode metadata; incomplete metadata is not capable."""

    reasons: list[str] = []
    if entry.get("status") != "active":
        reasons.append(f"status is {entry.get('status')!r}, not 'active'")
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, dict):
        reasons.append("capability metadata missing")
    else:
        if capabilities.get("toolcall") is not True:
            reasons.append("tool calling not supported")
        if (capabilities.get("input") or {}).get("text") is not True or (capabilities.get("output") or {}).get(
            "text"
        ) is not True:
            reasons.append("text input/output not supported")
    context = (entry.get("limit") or {}).get("context") if isinstance(entry.get("limit"), dict) else None
    if not isinstance(context, int) or isinstance(context, bool):
        reasons.append("context limit missing")
    elif context < MIN_CONTEXT_TOKENS:
        reasons.append(f"context limit {context} below {MIN_CONTEXT_TOKENS}")
    return (not reasons), tuple(reasons)


def _build_model(header_id: str, entry: Mapping[str, Any] | None, credentials: Mapping[str, str] | None) -> CatalogModel:
    entry = entry or {}
    provider_id, _, model_id = header_id.partition("/")
    api = entry.get("api") if isinstance(entry.get("api"), dict) else {}
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    credential = "unknown"
    if credentials is not None:
        credential = credentials.get(_normalise(provider_id), "none")
    cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else None
    cost_class, cost_reason = classify_cost(
        provider_id=provider_id, api_url=str(api.get("url") or ""), cost=cost, credential=credential,
        credentials_known=credentials is not None,
    )
    capable, reasons = assess_capability(entry)

    def _int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return CatalogModel(
        id=header_id, provider_id=provider_id, model_id=model_id, display_name=str(entry.get("name") or model_id),
        family=str(entry.get("family") or ""), status=str(entry.get("status") or "unknown"),
        api_url=str(api.get("url") or ""), context_limit=_int(limit.get("context")),
        output_limit=_int(limit.get("output")), cost=cost, credential=credential, cost_class=cost_class,
        cost_reason=cost_reason, capable=capable, capability_reasons=reasons,
    )


def parse_models_output(text: str, credentials: Mapping[str, str] | None) -> tuple[list[CatalogModel], int]:
    """Parse ``opencode models --verbose`` (``provider/model`` header + JSON block); plain lines are metadata-less."""

    clean = _strip_ansi(text)
    decoder = json.JSONDecoder()
    models: dict[str, CatalogModel] = {}
    skipped = 0
    cursor = 0
    for match in _HEADER.finditer(clean):
        if match.start() < cursor:
            continue  # a bare line that is really part of the previous JSON block
        cursor = match.end()
        entry: dict[str, Any] | None = None
        start = cursor
        while start < len(clean) and clean[start].isspace():
            start += 1
        if start < len(clean) and clean[start] == "{":
            try:
                parsed, cursor = decoder.raw_decode(clean, start)
            except ValueError:
                skipped += 1
            else:
                if isinstance(parsed, dict):
                    entry = parsed
                else:
                    skipped += 1
        models[match.group(1)] = _build_model(match.group(1), entry, credentials)
    return list(models.values()), skipped


def discover_catalog(
    *,
    runner: Runner = subprocess_runner,
    which: Callable[[str], str | None] = shutil.which,
    binary: str | None = None,
    remote_refresh: bool = False,
    now: _dt.datetime | None = None,
    state_dir: Path | None = None,
    persist: bool = True,
) -> Catalog:
    """Ask the installed OpenCode for its current inventory; never raises, never generates, never bills.

    ``remote_refresh`` additionally passes OpenCode's own ``--refresh`` (a models.dev metadata fetch); it is off by
    default so routine discovery stays purely local.
    """

    stamp = _iso(now or _now_dt())
    binary = binary or OPENCODE_BIN

    def _finish(catalog: Catalog) -> Catalog:
        if persist:
            _write_json((state_dir or opencode_state_dir()) / CATALOG_FILENAME, catalog.to_json())
        return catalog

    if which(binary) is None:
        return _finish(Catalog(CATALOG_ABSENT, f"{binary!r} is not installed or not on PATH", None, stamp, "unavailable"))
    env = _discovery_env()
    version_result = runner([binary, "--version"], None, DISCOVERY_TIMEOUT_SECONDS, env)
    version = version_result.stdout.strip().splitlines()[0].strip() if version_result.returncode == 0 and version_result.stdout.strip() else None
    argv = [binary, "models", "--verbose", *(["--refresh"] if remote_refresh else [])]
    result = runner(argv, None, DISCOVERY_TIMEOUT_SECONDS, env)
    if result.returncode != 0 or result.error:
        detail = result.error or f"exit {result.returncode}"
        return _finish(
            Catalog(CATALOG_UNAVAILABLE, f"model catalog command failed ({detail}); OpenCode may be unauthenticated",
                    version, stamp, "unavailable")
        )
    credentials_result = runner([binary, "providers", "list"], None, DISCOVERY_TIMEOUT_SECONDS, env)
    credentials: dict[str, str] | None = None
    if credentials_result.returncode == 0 and not credentials_result.error:
        credentials = _parse_credentials(credentials_result.stdout)
    models, skipped = parse_models_output(result.stdout, credentials)
    credentials_status = "ok" if credentials is not None else "unavailable"
    if not models:
        status = CATALOG_UNPARSEABLE if result.stdout.strip() else CATALOG_EMPTY
        return _finish(Catalog(status, "OpenCode reported no usable models", version, stamp, credentials_status, (), skipped))
    return _finish(Catalog(CATALOG_OK, f"{len(models)} models discovered", version, stamp, credentials_status,
                           tuple(models), skipped))


def get_catalog(
    *, max_age_seconds: float = CATALOG_MAX_AGE_SECONDS, refresh: bool = False, discover: bool = True,
    state_dir: Path | None = None, now: _dt.datetime | None = None, **discovery: Any,
) -> Catalog | None:
    """The cached snapshot when fresh, otherwise a re-discovery (``discover=False`` never spawns anything)."""

    cached = None if refresh else load_catalog(state_dir)
    if cached is not None:
        age = catalog_age_seconds(cached, now)
        if age is not None and age <= max_age_seconds:
            return cached
    if not discover:
        return cached
    return discover_catalog(state_dir=state_dir, now=now, **discovery)


# --------------------------------------------------------------------------- qualification cache


def _preset_path(agent: str, code_root: Path) -> Path:
    return code_root / ".opencode" / "agents" / f"{agent}.md"


def _preset_digest(agent: str, code_root: Path) -> str:
    try:
        return hashlib.sha256(_preset_path(agent, code_root).read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def _code_root() -> Path:
    return Path(__file__).resolve().parents[2]


def qualification_key(model: CatalogModel, profile: str, opencode_version: str | None, code_root: Path) -> str:
    agent = PROFILES[profile]["agent"]
    payload = json.dumps(
        [PROBE_VERSION, opencode_version, model.identity(), profile, _preset_digest(agent, code_root)]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Qualification:
    status: str
    reason: str
    checked_at: str
    expires_at: str
    key: str
    checks: dict[str, str] = field(default_factory=dict)


def _load_store(state_dir: Path | None) -> dict[str, Any]:
    body = _read_json((state_dir or opencode_state_dir()) / QUALIFICATION_FILENAME) or {}
    if body.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "qualifications": {}, "cooldowns": {}}
    body.setdefault("qualifications", {})
    body.setdefault("cooldowns", {})
    return body


def cached_qualification(
    model: CatalogModel, profile: str, *, opencode_version: str | None, state_dir: Path | None = None,
    code_root: Path | None = None, now: _dt.datetime | None = None,
) -> Qualification | None:
    """A still-valid cached result for exactly this version/model/preset, else ``None``."""

    moment = now or _now_dt()
    key = qualification_key(model, profile, opencode_version, code_root or _code_root())
    raw = _load_store(state_dir)["qualifications"].get(f"{model.id}|{profile}")
    if not isinstance(raw, dict) or raw.get("key") != key:
        return None
    expires = _parse_iso(raw.get("expires_at"))
    if expires is None or expires <= moment:
        return None
    return Qualification(
        status=str(raw.get("status")), reason=str(raw.get("reason")), checked_at=str(raw.get("checked_at")),
        expires_at=str(raw.get("expires_at")), key=key, checks=dict(raw.get("checks") or {}),
    )


def _store_qualification(model: CatalogModel, profile: str, record: Qualification, state_dir: Path | None) -> None:
    store = _load_store(state_dir)
    store["qualifications"][f"{model.id}|{profile}"] = asdict(record)
    _write_json((state_dir or opencode_state_dir()) / QUALIFICATION_FILENAME, store)


def active_cooldown(model_id: str, *, state_dir: Path | None = None, now: _dt.datetime | None = None) -> dict[str, Any] | None:
    raw = _load_store(state_dir)["cooldowns"].get(model_id)
    if not isinstance(raw, dict):
        return None
    until = _parse_iso(raw.get("until"))
    return raw if until is not None and until > (now or _now_dt()) else None


def record_model_failure(
    model_id: str, category: str, *, state_dir: Path | None = None, now: _dt.datetime | None = None,
    cooldown_seconds: int = COOLDOWN_SECONDS,
) -> bool:
    """Cool a model down after a quota/rate/context/outage failure so the next selection uses another free model.

    Deliberately never escalates: the only consequence is that this candidate is skipped for a while.
    """

    if category not in COOLDOWN_CATEGORIES:
        return False
    moment = now or _now_dt()
    store = _load_store(state_dir)
    store["cooldowns"][model_id] = {
        "category": category, "recorded_at": _iso(moment), "until": _iso(moment + _dt.timedelta(seconds=cooldown_seconds)),
    }
    _write_json((state_dir or opencode_state_dir()) / QUALIFICATION_FILENAME, store)
    return True


# --------------------------------------------------------------------------- qualification probe

_PROBE_DIFF = (
    "diff --git a/probe.py b/probe.py\n--- a/probe.py\n+++ b/probe.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
    "-    return a - b\n+    return a + b\n"
)
_PROBE_PROMPTS = {
    PROFILE_REVIEW: (
        "Qualification probe for a read-only reviewer. Review the tiny diff below; it is correct and has no "
        "actionable finding. Do not edit anything or run any command. Respond with the strict review contract "
        "from .agents/workflows/REVIEW.md: because there is no finding, your entire response must be the single "
        "word READY.\n```diff\n" + _PROBE_DIFF + "```"
    ),
    PROFILE_TESTS: (
        "Qualification probe for a read-only tester. Do not edit any file. Reply with exactly: TESTER_OK"
    ),
}
_HEADER_LINE = re.compile(r"^>\s*\S+\s+·\s+.*$")


def _response_text(stdout: str) -> str:
    lines = [line for line in _strip_ansi(stdout).splitlines() if not _HEADER_LINE.match(line.strip())]
    return "\n".join(lines).strip()


def _effective_permissions(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(item.get("permission")): str(item.get("action"))
        for item in payload.get("permission", [])
        if isinstance(item, dict) and item.get("pattern") == "*"
    }


def qualify_model(
    model: CatalogModel,
    profile: str,
    *,
    opencode_version: str | None,
    runner: Runner = subprocess_runner,
    binary: str | None = None,
    state_dir: Path | None = None,
    code_root: Path | None = None,
    now: _dt.datetime | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Qualification:
    """One bounded, cached probe.  Only a proven-free, capable model is ever launched (no billing path exists)."""

    from scripts.ci.review_contract import (
        indicates_agent_fallback,
        parse_review_response,
    )
    from scripts.ci.reviewer_presets import ensure_opencode_agent_preset

    from .runner import _worker_environment, worktree_snapshot

    moment = now or _now_dt()
    root = code_root or _code_root()
    binary = binary or OPENCODE_BIN
    key = qualification_key(model, profile, opencode_version, root)
    checks: dict[str, str] = {}

    def _done(status: str, reason: str) -> Qualification:
        ttl = QUALIFIED_TTL_SECONDS if status == QUALIFIED else UNQUALIFIED_TTL_SECONDS
        record = Qualification(status, reason, _iso(moment), _iso(moment + _dt.timedelta(seconds=ttl)), key, checks)
        _store_qualification(model, profile, record, state_dir)
        return record

    if profile not in PROFILES:
        raise ValueError(f"unknown qualification profile {profile!r}")
    if model.cost_class != COST_FREE_OPENCODE:
        return _done(UNQUALIFIED, f"not probed: cost class {model.cost_class!r} is not proven free")
    if not model.capable:
        return _done(UNQUALIFIED, "not probed: " + "; ".join(model.capability_reasons))

    agent = PROFILES[profile]["agent"]
    with tempfile.TemporaryDirectory(prefix="octarel-qualify-") as tmp:
        work = Path(tmp)
        try:
            (work / "probe.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            identity = ["-c", "user.name=octarel", "-c", "user.email=octarel@localhost"]
            for git_args in (["init", "-q"], ["add", "-A"], [*identity, "commit", "-q", "-m", "probe"]):
                subprocess.run(["git", *git_args], cwd=work, check=True, capture_output=True, timeout=30)
            if not ensure_opencode_agent_preset(work, agent):
                checks["agent_preset"] = "missing"
                return _done(UNQUALIFIED, f"agent preset {agent!r} is not available")
            before = worktree_snapshot(work)
        except (OSError, subprocess.SubprocessError) as exc:
            return _done(UNQUALIFIED, f"could not prepare the probe workspace ({type(exc).__name__})")
        env = _worker_environment()
        config = runner([binary, "debug", "agent", agent], work, 60.0, env)
        try:
            permissions = _effective_permissions(json.loads(config.stdout)) if config.returncode == 0 else {}
        except ValueError:
            permissions = {}
        missing = [name for name in PROFILES[profile]["require_deny"] if permissions.get(name) != "deny"]
        checks["agent_preset"] = "ok" if config.returncode == 0 else "unreadable"
        checks["read_only_permissions"] = "denied" if not missing else "not-denied: " + ", ".join(missing)
        if config.returncode != 0 or missing:
            return _done(UNQUALIFIED, f"agent {agent!r} does not enforce read-only permissions ({checks['read_only_permissions']})")

        result = runner([binary, "run", "--model", model.id, "--agent", agent, _PROBE_PROMPTS[profile]], work, timeout, env)
        checks["launch"] = "ok" if result.returncode == 0 and not result.error else (result.error or f"exit {result.returncode}")
        if result.returncode != 0 or result.error:
            return _done(UNQUALIFIED, f"model did not launch through OpenCode ({checks['launch']})")
        output = result.stdout + "\n" + result.stderr
        if indicates_agent_fallback(output):
            checks["agent_preset"] = "fell-back"
            return _done(UNQUALIFIED, "OpenCode substituted a default agent for the preset")
        lowered = output.lower()
        if "was denied" in lowered or "auto-denied" in lowered:
            checks["read_only_permissions"] = "tool-denied"
            return _done(UNQUALIFIED, "model attempted a tool action the read-only preset denied")
        if any(marker in lowered for marker in ("api key", "api_key", "billing", "insufficient credit", "payment")):
            checks["billing"] = "billing-or-api-key-signal"
            return _done(UNQUALIFIED, "probe output signalled an API-key/billing requirement")
        checks["billing"] = "none-observed"
        try:
            after = worktree_snapshot(work)
        except (OSError, subprocess.SubprocessError):
            after = {"<unreadable>": ""}
        checks["tree_unchanged"] = "ok" if before == after else "modified"
        if before != after:
            return _done(UNQUALIFIED, "read-only probe modified the workspace")
        response = _response_text(result.stdout)
        if profile == PROFILE_REVIEW:
            verdict = parse_review_response(response)
            checks["review_contract"] = "ok" if verdict.ready else f"failed: {verdict.reason}"
            if not verdict.ready:
                return _done(UNQUALIFIED, f"response did not satisfy the strict review contract ({verdict.reason})")
        else:
            checks["response_contract"] = "ok" if response.strip().rstrip(".!") == "TESTER_OK" else "failed"
            if checks["response_contract"] != "ok":
                return _done(UNQUALIFIED, "response did not follow the bounded tester instruction")
    return _done(QUALIFIED, "launch, read-only preset, unchanged tree, response contract, and no billing signal verified")


# --------------------------------------------------------------------------- selection


def vendor_conflict(model: CatalogModel, avoid_provider: str | None) -> bool:
    """Whether ``model`` belongs to the vendor provider-diversity must avoid.

    OpenCode Zen only reports the transport as the provider, so the vendor is inferred from the provider id and the
    model id/family/name.  Any recognisable match fails closed.
    """

    if not avoid_provider:
        return False
    avoided = _normalise(avoid_provider)
    tokens = set(_VENDOR_ALIASES.get(avoided, ())) | {avoided}
    haystack = _normalise(" ".join([model.provider_id, model.model_id, model.family, model.display_name]))
    return any(token and token in haystack for token in tokens)


def dashboard_state(model: CatalogModel, qualification: Qualification | None, cooldown: Mapping[str, Any] | None) -> str:
    if model.cost_class == COST_SUBSCRIPTION:
        return STATE_SUBSCRIPTION
    if model.cost_class == COST_METERED:
        return STATE_METERED
    if model.cost_class != COST_FREE_OPENCODE:
        return STATE_UNKNOWN_COST
    if not model.capable:
        return STATE_INCAPABLE
    if cooldown:
        return STATE_COOLING_DOWN
    if qualification is None:
        return STATE_UNTESTED_FREE
    return STATE_QUALIFIED_FREE if qualification.status == QUALIFIED else STATE_UNQUALIFIED


@dataclass(frozen=True)
class PoolSelection:
    model: CatalogModel | None
    profile: str
    reason: str
    evidence: dict[str, Any]


def _candidate_record(model: CatalogModel, decision: str, reason: str, qualification: Qualification | None) -> dict[str, Any]:
    return {
        "model": model.id, "display_name": model.display_name, "provider": model.provider_id,
        "cost_class": model.cost_class, "cost_reason": model.cost_reason, "decision": decision, "reason": reason,
        "qualification": qualification.status if qualification else UNTESTED,
        "qualification_reason": qualification.reason if qualification else "not yet qualified",
    }


def select_pool_model(
    catalog: Catalog | None,
    role: str,
    *,
    avoid_provider: str | None = None,
    only_model: str | None = None,
    exclude: Sequence[str] = (),
    allow_probe: bool = False,
    max_probes: int = MAX_PROBES_PER_SELECTION,
    state_dir: Path | None = None,
    code_root: Path | None = None,
    now: _dt.datetime | None = None,
    runner: Runner = subprocess_runner,
    binary: str | None = None,
) -> PoolSelection:
    """Pick one qualified free model for a read-only ``role``; record every rejected candidate and why.

    Without ``allow_probe`` this reads only cached data (no subprocess, no model call), so dispatch and the dashboard
    can call it freely.  With it, at most ``max_probes`` not-yet-qualified free candidates are probed, once each.
    """

    profile = ROLE_PROFILES.get(role)
    evidence: dict[str, Any] = {
        "pool": POOL_OPENCODE_FREE, "role": role, "profile": profile, "avoid_provider": avoid_provider,
        "catalog": catalog.evidence() if catalog else None, "candidates": [], "selected": None,
    }

    def _none(reason: str) -> PoolSelection:
        evidence["reason"] = reason
        return PoolSelection(None, profile or "", reason, evidence)

    if profile is None:
        return _none(f"role {role!r} is not a read-only role a free OpenCode model may serve")
    if catalog is None:
        return _none("OpenCode model catalog has not been discovered")
    if catalog.status != CATALOG_OK:
        return _none(f"OpenCode model catalog is {catalog.status}: {catalog.reason}")
    moment = now or _now_dt()
    root = code_root or _code_root()
    candidates: list[tuple[CatalogModel, Qualification | None]] = []
    for model in sorted(catalog.models, key=lambda item: (-(item.context_limit or 0), item.id)):
        if only_model and model.id != only_model:
            continue
        qualification = (
            cached_qualification(
                model, profile, opencode_version=catalog.opencode_version, state_dir=state_dir, code_root=root, now=moment
            )
            if model.cost_class == COST_FREE_OPENCODE and model.capable
            else None
        )
        rejection = None
        if model.cost_class != COST_FREE_OPENCODE:
            rejection = f"cost class {model.cost_class}: {model.cost_reason}"
        elif not model.capable:
            rejection = "not capable: " + "; ".join(model.capability_reasons)
        elif model.id in exclude:
            rejection = "already excluded for this attempt"
        elif vendor_conflict(model, avoid_provider):
            rejection = f"provider diversity: same vendor as {avoid_provider}"
        else:
            cooldown = active_cooldown(model.id, state_dir=state_dir, now=moment)
            if cooldown:
                rejection = f"cooling down after {cooldown.get('category')} until {cooldown.get('until')}"
            elif qualification is not None and qualification.status != QUALIFIED:
                rejection = f"unqualified: {qualification.reason}"
        if rejection:
            evidence["candidates"].append(_candidate_record(model, "rejected", rejection, qualification))
            continue
        candidates.append((model, qualification))

    if only_model and not any(model.id == only_model for model, _ in candidates) and not evidence["candidates"]:
        return _none(f"model {only_model!r} is not in the discovered catalog")

    probes = 0
    for model, qualification in sorted(candidates, key=lambda pair: pair[1] is None):
        if qualification is None:
            if not allow_probe or probes >= max_probes:
                evidence["candidates"].append(
                    _candidate_record(model, "rejected", "not yet qualified (no probe budget or probing not allowed)", None)
                )
                continue
            probes += 1
            qualification = qualify_model(
                model, profile, opencode_version=catalog.opencode_version, runner=runner, binary=binary,
                state_dir=state_dir, code_root=root, now=moment,
            )
            if qualification.status != QUALIFIED:
                evidence["candidates"].append(
                    _candidate_record(model, "rejected", f"unqualified: {qualification.reason}", qualification)
                )
                continue
        record = _candidate_record(model, "selected", "qualified free read-only candidate", qualification)
        evidence["candidates"].append(record)
        evidence["selected"] = model.id
        evidence["provider"] = POOL_PROVIDER_LABEL
        evidence["reason"] = f"selected {model.id}"
        return PoolSelection(model, profile, evidence["reason"], evidence)
    return _none("no qualified free OpenCode model satisfies the role and provider-diversity constraints")


def pool_block_reason(
    role: str, *, avoid_provider: str | None = None, state_dir: Path | None = None, code_root: Path | None = None,
    now: _dt.datetime | None = None, max_catalog_age_seconds: float = 24 * 3600,
) -> str | None:
    """Cached-only admission check for dispatch: ``None`` when a free candidate could serve, else why not."""

    catalog = load_catalog(state_dir)
    if catalog is not None:
        age = catalog_age_seconds(catalog, now)
        if age is None or age > max_catalog_age_seconds:
            return "OpenCode model catalog snapshot is stale; refresh it"
    selection = select_pool_model(
        catalog, role, avoid_provider=avoid_provider, allow_probe=False, state_dir=state_dir, code_root=code_root,
        now=now,
    )
    if selection.model is not None:
        return None
    # An untested-but-otherwise-eligible free model may still be qualified when the worker actually runs.
    if any(
        item["reason"].startswith("not yet qualified") for item in selection.evidence["candidates"]
    ):
        return None
    return selection.reason


def inventory(
    *, state_dir: Path | None = None, code_root: Path | None = None, now: _dt.datetime | None = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Dashboard/API view of the cached catalog with per-model qualification state (never spawns anything)."""

    catalog = catalog if catalog is not None else load_catalog(state_dir)
    if catalog is None:
        return {"catalog": None, "models": [], "note": "no OpenCode catalog snapshot; run the model-catalog refresh"}
    moment = now or _now_dt()
    root = code_root or _code_root()
    rows = []
    for model in sorted(catalog.models, key=lambda item: item.id):
        qualifications = {
            profile: cached_qualification(
                model, profile, opencode_version=catalog.opencode_version, state_dir=state_dir, code_root=root, now=moment
            )
            for profile in PROFILES
        }
        cooldown = active_cooldown(model.id, state_dir=state_dir, now=moment)
        states = {profile: dashboard_state(model, qual, cooldown) for profile, qual in qualifications.items()}
        rows.append(
            {
                "id": model.id, "display_name": model.display_name, "provider": model.provider_id, "family": model.family,
                "cost_class": model.cost_class, "cost_reason": model.cost_reason, "credential": model.credential,
                "context_limit": model.context_limit, "capable": model.capable,
                "capability_reasons": list(model.capability_reasons),
                "states": states,
                "qualification": {
                    profile: ({"status": qual.status, "reason": qual.reason, "checked_at": qual.checked_at}
                              if qual else None)
                    for profile, qual in qualifications.items()
                },
                "cooldown": dict(cooldown) if cooldown else None,
                "role_kind": "discovered-model",
            }
        )
    return {"catalog": {**catalog.evidence(), "age_seconds": catalog_age_seconds(catalog, moment)}, "models": rows}


def redacted_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Round-trip through the shared redactor so probe/catalog text can never carry a secret into a manifest."""

    return json.loads(redact_text(json.dumps(evidence)))


# --------------------------------------------------------------------------- CLI (refresh / list / qualify)


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="scripts.agents.model_catalog", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="re-discover the OpenCode catalog (local, non-billable)")
    refresh.add_argument("--remote", action="store_true", help="also pass OpenCode's --refresh (models.dev metadata)")
    sub.add_parser("list", help="show the cached catalog and qualification state (no subprocess)")
    qualify = sub.add_parser("qualify", help="probe not-yet-qualified free models once each (bounded)")
    qualify.add_argument("--profile", choices=sorted(PROFILES), default=PROFILE_REVIEW)
    qualify.add_argument("--model", help="qualify only this provider/model id")
    qualify.add_argument("--max", type=int, default=MAX_PROBES_PER_SELECTION)
    args = parser.parse_args(argv)

    if args.command == "list":
        print(json.dumps(inventory(), indent=2))
        return 0
    catalog = discover_catalog(remote_refresh=getattr(args, "remote", False)) if args.command == "refresh" else get_catalog()
    if args.command == "refresh":
        print(json.dumps(catalog.evidence(), indent=2))
        return 0 if catalog.status == CATALOG_OK else 1
    if catalog is None or catalog.status != CATALOG_OK:
        print(json.dumps({"error": catalog.reason if catalog else "no catalog"}))
        return 1
    done: list[dict[str, str]] = []
    for model in catalog.models:
        if len(done) >= args.max:
            break
        if (args.model and model.id != args.model) or model.cost_class != COST_FREE_OPENCODE or not model.capable:
            continue
        if cached_qualification(model, args.profile, opencode_version=catalog.opencode_version):
            continue
        qualification = qualify_model(model, args.profile, opencode_version=catalog.opencode_version)
        done.append({"model": model.id, "status": qualification.status, "reason": qualification.reason})
    print(json.dumps({"profile": args.profile, "probed": done}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
