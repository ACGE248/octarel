"""Native subscription-backed model freshness for the Grok and Codex CLIs (ENG-AO-04).

The configured workers in ``workers.json`` keep stable identities (``grok-build``, ``codex-review`` ...) and a
``default_model`` that is the *last verified* native model.  This module lets the installed native CLIs advance that
default without a repository edit, and only when the advance is proven:

    native CLI  ->  local model enumeration  ->  candidate  ->  compatibility verification
        ->  verified-model record (cache)  ->  effective model of the stable worker  ->  run evidence

Safety properties (all fail closed to the configured default):

* discovery uses only local, non-billable CLI commands (``--version``, ``--help``, ``grok models``,
  ``codex login status``, ``codex debug models``).  It never generates, never sends a prompt, and strips API-key
  variables from the child environment so the answer describes the existing subscription/session and nothing else;
* an OpenCode display label is only ever a *hint* (recorded, never applied).  The native identifier is whatever the
  native CLI itself enumerates;
* a candidate is adopted only when it is a strictly newer version of the *same* capability tier as the configured
  model (``grok-4.6`` -> ``grok-4.7``; ``gpt-5.6-sol`` -> a newer ``-sol``).  A different tier (for example
  ``gpt-6-astra``) is reported but never adopted: tier choice is routing policy, not freshness;
* it requires an authenticated subscription session (never an API key), a still-supported invocation flag set for every
  worker of the family, and the unchanged read-only / worktree-restricted permission shape of those workers;
* no verification means the configured model stays in force and the reason is recorded; nothing here raises into AO
  startup, changes a worker's role/permissions/routing, or enables any API-key or paid route.

Enumeration is entitlement evidence, not a generation: no billable request is made to prove a model works.  The
verification record says so (``probe: "none"``).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import model_catalog
from .graph_context import state_root
from .model_catalog import Runner, subprocess_runner

SCHEMA = 1
STATE_DIRNAME = "native-models"
DISCOVERY_TIMEOUT_SECONDS = 30.0
REFRESH_MAX_AGE_SECONDS = 300  # admission-time reuse window; every live run re-verifies (max age 0), never trusting a stale auth state
READ_MAX_AGE_SECONDS = 7 * 24 * 3600  # a cache older than this is ignored by cache-only readers
PROBE = "none"  # no billable generation is ever used to prove a model

STATUS_VERIFIED = "verified"  # a newer model was verified and is the effective default
STATUS_CURRENT = "current"  # the configured model is the newest compatible one the CLI offers
STATUS_UNVERIFIED = "unverified"  # a newer candidate exists but could not be verified; configured model retained
STATUS_UNAVAILABLE = "unavailable"  # the CLI/session/enumeration could not be used; configured model retained

_FLAG = re.compile(r"^--[a-z][a-z0-9-]*$")
# Environment that could turn a native CLI onto a billable API route; never given to a discovery command.
_API_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "XAI_API_KEY", "GROK_API_KEY", "GROK_CODE_XAI_API_KEY")


_which: Callable[[str], str | None] = shutil.which  # resolved at call time so tests can hide the real CLIs


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(moment: _dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _discovery_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _API_ENV}
    env["NO_COLOR"] = "1"
    return env


def _version_key(major: str, minor: str | None) -> tuple[int, int]:
    return int(major), int(minor or 0)


# --------------------------------------------------------------------------- provider adapters


@dataclass(frozen=True)
class Listing:
    """What one native CLI says about itself; ``error`` means it could not be understood."""

    models: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    authenticated: bool = False
    auth_reason: str = ""
    error: str | None = None


@dataclass(frozen=True)
class Adapter:
    family: str
    provider: str
    binary: str
    help_argv: tuple[str, ...]  # command whose output must still document every templated flag
    model_pattern: re.Pattern[str]  # identifies a same-tier versioned model; group ``tier`` is the tier
    enumerate: Callable[[Runner, Mapping[str, str]], Listing]


_GROK_MODEL = re.compile(r"^grok-(?P<major>\d+)(?:\.(?P<minor>\d+))?$")
_CODEX_MODEL = re.compile(r"^gpt-(?P<major>\d+)(?:\.(?P<minor>\d+))?-(?P<tier>[a-z]+)$")
_GROK_LINE = re.compile(r"^\s*[-*]\s+(?P<id>[A-Za-z0-9][\w.\-]*)(?:\s+\(default\))?\s*$")


def _negated(text: str) -> bool:
    """Registry's own negation-first markers, so ``Not logged in using ChatGPT`` never reads as a success phrase."""

    from .registry import _AUTH_NEGATION_MARKERS

    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_NEGATION_MARKERS)


def _enumerate_grok(runner: Runner, env: Mapping[str, str]) -> Listing:
    result = runner(["grok", "models"], None, DISCOVERY_TIMEOUT_SECONDS, env)
    if result.error or result.returncode != 0:
        return Listing(error=result.error or f"grok models exited {result.returncode}")
    text = f"{result.stdout}\n{result.stderr}"
    models = tuple(match["id"] for line in text.splitlines() if (match := _GROK_LINE.match(line)))
    if not models:
        return Listing(error="grok models produced no parseable model list")
    if _negated(text) or re.search(r"please (log|sign) ?in", text, re.IGNORECASE):
        return Listing(models=models, authenticated=False, auth_reason="grok reports the session is not authenticated")
    if re.search(r"api[ -]?key", text, re.IGNORECASE):
        return Listing(models=models, authenticated=False,
                       auth_reason="grok is using an API key; only the configured CLI session is eligible")
    if not re.search(r"logged in", text, re.IGNORECASE):
        return Listing(models=models, authenticated=False, auth_reason="grok did not confirm an authenticated session")
    return Listing(models=models, authenticated=True, auth_reason="grok models reports a logged-in session")


def _enumerate_codex(runner: Runner, env: Mapping[str, str]) -> Listing:
    status = runner(["codex", "login", "status"], None, DISCOVERY_TIMEOUT_SECONDS, env)
    if status.error:
        return Listing(error=f"codex login status failed: {status.error}")
    said = f"{status.stdout}\n{status.stderr}"
    if status.returncode != 0:
        return Listing(authenticated=False, auth_reason=f"codex login status exited {status.returncode}: no eligible ChatGPT subscription session")
    if _negated(said):
        return Listing(authenticated=False, auth_reason="codex login status reports no logged-in ChatGPT subscription session")
    if re.search(r"api[ -]?key", said, re.IGNORECASE):
        return Listing(authenticated=False, auth_reason="codex is using an API key; only the ChatGPT subscription session is eligible")
    if "logged in using chatgpt" not in said.lower():
        return Listing(authenticated=False, auth_reason="codex has no logged-in ChatGPT subscription session")
    catalog = runner(["codex", "debug", "models"], None, DISCOVERY_TIMEOUT_SECONDS, env)
    if catalog.error or catalog.returncode != 0:
        return Listing(authenticated=True, error=catalog.error or f"codex debug models exited {catalog.returncode}")
    try:
        entries = json.loads(catalog.stdout)["models"]
        listed, hidden = [], []
        for entry in entries:
            (listed if entry.get("visibility") == "list" else hidden).append(str(entry["slug"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return Listing(authenticated=True, error="codex debug models output was malformed or incomplete")
    if not listed:
        return Listing(authenticated=True, error="codex debug models listed no selectable model")
    return Listing(models=tuple(listed), hidden=tuple(hidden), authenticated=True,
                   auth_reason="codex login status reports the ChatGPT subscription session")


ADAPTERS: dict[str, Adapter] = {
    "grok": Adapter("grok", "xAI", "grok", ("grok", "--help"), _GROK_MODEL, _enumerate_grok),
    "codex": Adapter("codex", "OpenAI", "codex", ("codex", "exec", "--help"), _CODEX_MODEL, _enumerate_codex),
}


def known_family(family: str) -> bool:
    return family in ADAPTERS


# --------------------------------------------------------------------------- candidate selection


def _parse(adapter: Adapter, model_id: str) -> tuple[tuple[int, int], str] | None:
    match = adapter.model_pattern.match(model_id)
    if not match:
        return None
    groups = match.groupdict()
    return _version_key(groups["major"], groups.get("minor")), groups.get("tier") or ""


def pick_candidate(adapter: Adapter, configured: str, models: Iterable[str]) -> tuple[str | None, list[str]]:
    """Newest strictly-newer same-tier model, plus newer models of other tiers (reported, never adopted)."""

    current = _parse(adapter, configured)
    if current is None:
        return None, []
    best: tuple[tuple[int, int], str] | None = None
    other_tier: list[str] = []
    for model_id in models:
        parsed = _parse(adapter, model_id)
        if parsed is None or parsed[0] <= current[0]:
            continue
        if parsed[1] != current[1]:
            other_tier.append(model_id)
        elif best is None or parsed[0] > best[0]:
            best = (parsed[0], model_id)
    return (best[1] if best else None), sorted(other_tier)


def opencode_hints(adapter: Adapter, configured: str) -> list[str]:
    """Newer same-vendor labels in the cached OpenCode catalog.  Evidence that a family exists, never a native ID."""

    catalog = model_catalog.load_catalog()
    if catalog is None:
        return []
    current = _parse(adapter, configured)
    aliases = model_catalog._VENDOR_ALIASES.get("xai" if adapter.family == "grok" else "openai", ())
    hints: set[str] = set()
    for model in catalog.models:
        if not any(alias in f"{model.provider_id} {model.model_id}".lower() for alias in aliases):
            continue
        found = re.search(r"(\d+)(?:\.(\d+))?", model.display_name or model.model_id)
        if current and found and _version_key(found.group(1), found.group(2)) > current[0]:
            hints.add(model.display_name or model.model_id)
    return sorted(hints)[:8]


# --------------------------------------------------------------------------- compatibility verification


@dataclass(frozen=True)
class WorkerShape:
    """The slice of a registry worker that a model advance must leave untouched."""

    name: str
    read_only: bool
    write_capable: bool
    requires_isolated_worktree: bool
    templates: tuple[tuple[str, ...], ...]  # standard + every permission-profile template

    @classmethod
    def of(cls, worker: Any) -> "WorkerShape":
        templates = (tuple(worker.cli_template), *(tuple(t) for t in worker.permission_profile_templates.values()))
        return cls(worker.name, worker.is_read_only, worker.is_write_capable, worker.requires_isolated_worktree, templates)


def _flags(shapes: Sequence[WorkerShape]) -> list[str]:
    return sorted({token for shape in shapes for template in shape.templates for token in template if _FLAG.match(token)})


_FORBIDDEN_TOKENS = ("--dangerously-skip-permissions", "--always-approve", "--yolo", "bypassPermissions",
                     "danger-full-access", "--dangerously-bypass-approvals-and-sandbox")
# Adjacent flag/value pairs every template of a class must still carry (positive, so a removed guard fails closed).
_REQUIRED_PAIRS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "grok": {"read-only": (("--sandbox", "read-only"), ("--permission-mode", "plan")),
             "write": (("--sandbox", "work-tree"), ("--permission-mode", "default"))},
    "codex": {"read-only": (("--sandbox", "read-only"),), "write": (("--sandbox", "workspace-write"),)},
}
_REQUIRED_TOKENS = {"grok": ("--no-subagents", "--disable-web-search"), "codex": ()}


def _has_pair(template: Sequence[str], flag: str, value: str) -> bool:
    return any(a == flag and b == value for a, b in zip(template, template[1:]))


def _values(template: Sequence[str], flag: str) -> list[str]:
    return [b for a, b in zip(template, template[1:]) if a == flag]


def _shape_problems(adapter: Adapter, shapes: Sequence[WorkerShape]) -> list[str]:
    problems: list[str] = []
    for shape in shapes:
        kind = "read-only" if shape.read_only else "write" if shape.write_capable else None
        if kind is None:
            problems.append(f"{shape.name}: neither a read-only nor a write-capable route")
            continue
        if shape.write_capable and not shape.requires_isolated_worktree:
            problems.append(f"{shape.name}: write-capable route is not restricted to an isolated worktree")
        for template in shape.templates:
            for token in _FORBIDDEN_TOKENS:
                if token in template:
                    problems.append(f"{shape.name}: {kind} route carries forbidden {token}")
            for flag, value in _REQUIRED_PAIRS[adapter.family][kind]:
                if not _has_pair(template, flag, value):
                    problems.append(f"{shape.name}: {kind} route lost required {flag} {value}")
                elif _values(template, flag) != [value]:  # a later conflicting value would win in typical CLI parsing
                    problems.append(f"{shape.name}: {kind} route has conflicting {flag} values {_values(template, flag)}")
            for token in _REQUIRED_TOKENS[adapter.family]:
                if token not in template:
                    problems.append(f"{shape.name}: route lost required {token}")
    return sorted(set(problems))


def shape_hash(shape: WorkerShape) -> str:
    return fingerprint([shape.name, shape.read_only, shape.write_capable, shape.requires_isolated_worktree,
                        shape.templates])


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def shapes_fingerprint(shapes: Sequence[WorkerShape]) -> str:
    return fingerprint([(s.name, s.read_only, s.write_capable, s.requires_isolated_worktree, s.templates) for s in shapes])


# --------------------------------------------------------------------------- state


def state_dir() -> Path:
    return state_root() / STATE_DIRNAME


def _path(family: str, directory: Path | None) -> Path:
    return (directory or state_dir()) / f"{family}.json"


_UNWRITABLE: dict[str, dict[str, Any]] = {}  # newest record when the state file can be neither written nor removed


def load_record(family: str, directory: Path | None = None) -> dict[str, Any] | None:
    held = _UNWRITABLE.get(str(_path(family, directory)))
    if held is not None:
        return held
    try:
        body = json.loads(_path(family, directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and body.get("schema") == SCHEMA else None


def _write_record(record: Mapping[str, Any], directory: Path | None) -> None:
    path = _path(str(record["family"]), directory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        _UNWRITABLE.pop(str(path), None)
    except OSError:
        # A failed write must never leave an older *verified* record in force after a fail-closed re-verification:
        # remove it, and if even that fails keep the newest result in memory for this process.
        try:
            path.unlink(missing_ok=True)
            _UNWRITABLE.pop(str(path), None)
        except OSError:
            _UNWRITABLE[str(path)] = dict(record)


def _age(record: Mapping[str, Any], now: _dt.datetime) -> float | None:
    try:
        stamp = _dt.datetime.fromisoformat(str(record["checked_at"]))
    except (KeyError, ValueError):
        return None
    return max(0.0, (now - (stamp if stamp.tzinfo else stamp.replace(tzinfo=_dt.timezone.utc))).total_seconds())


# --------------------------------------------------------------------------- refresh


def _blank(adapter: Adapter, configured: str, shapes: Sequence[WorkerShape], now: _dt.datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "family": adapter.family, "provider": adapter.provider, "cli": adapter.binary,
        "cli_version": None, "checked_at": _iso(now), "probe": PROBE,
        "configured_default": configured, "last_verified_model": configured, "effective_model": configured,
        "candidate": None, "newer_other_tier": [], "opencode_hints": [], "supported_models": [],
        "auth": {"mode": "chatgpt-subscription-session" if adapter.family == "codex" else "configured-cli-session",
                 "authenticated": False, "reason": ""},
        "status": STATUS_UNAVAILABLE, "reason": "", "workers": [s.name for s in shapes],
        "worker_shapes": shapes_fingerprint(shapes), "shape_hashes": {s.name: shape_hash(s) for s in shapes},
        "checks": {},
    }


def refresh(
    family: str,
    configured: str,
    shapes: Sequence[WorkerShape],
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    now: _dt.datetime | None = None,
    directory: Path | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Enumerate the installed native CLI and record whether a newer model is verified.  Never raises."""

    adapter = ADAPTERS[family]
    runner = runner or subprocess_runner
    moment = now or _now()
    record = _blank(adapter, configured, shapes, moment)

    def finish(status: str, reason: str) -> dict[str, Any]:
        record["status"], record["reason"] = status, reason
        record["fingerprint"] = fingerprint(
            record["cli_version"], record["auth"]["authenticated"], record["supported_models"], configured,
            record["worker_shapes"],
        )
        if persist:
            _write_record(record, directory)
        return record

    try:
        if (which or _which)(adapter.binary) is None:
            return finish(STATUS_UNAVAILABLE, f"{adapter.binary!r} is not installed or not on PATH")
        env = _discovery_env()
        version = runner([adapter.binary, "--version"], None, DISCOVERY_TIMEOUT_SECONDS, env)
        if version.returncode == 0 and version.stdout.strip():
            record["cli_version"] = version.stdout.strip().splitlines()[0].strip()
        listing = adapter.enumerate(runner, env)
        record["supported_models"] = sorted(listing.models)
        record["auth"].update(authenticated=listing.authenticated, reason=listing.auth_reason)
        if listing.error:
            return finish(STATUS_UNAVAILABLE, f"native model enumeration failed: {listing.error}")
        if not listing.models:
            return finish(STATUS_UNAVAILABLE, listing.auth_reason or "the native CLI enumerated no models")
        candidate, other_tier = pick_candidate(adapter, configured, listing.models)
        record["newer_other_tier"] = other_tier
        record["opencode_hints"] = opencode_hints(adapter, configured)
        if candidate is None:
            if _parse(adapter, configured) is None:
                return finish(STATUS_UNVERIFIED, f"configured model {configured!r} is not a versioned {family} model id")
            if configured not in listing.models:
                return finish(STATUS_UNVERIFIED,
                              f"configured model {configured!r} is not enumerated by the installed CLI (stale default)")
            reason = f"{configured} is the newest {family} model of its tier the installed CLI offers"
            if other_tier:
                reason += f"; newer models of a different tier ({', '.join(other_tier)}) are not adopted by freshness"
            elif record["opencode_hints"]:
                reason += (f"; OpenCode labels {', '.join(record['opencode_hints'])} have no matching native model "
                           "identifier and were not applied")
            return finish(STATUS_CURRENT, reason)
        record["candidate"] = {"native_id": candidate, "source": f"{adapter.binary} enumeration"}
        if not listing.authenticated:
            return finish(STATUS_UNVERIFIED, f"{candidate} is listed but cannot be verified: {listing.auth_reason}")
        problems = _shape_problems(adapter, shapes)
        checks: dict[str, Any] = {"permission_shape": "ok" if not problems else problems}
        record["checks"] = checks
        if problems:
            return finish(STATUS_UNVERIFIED, f"{candidate} not promoted: {'; '.join(problems)}")
        help_result = runner(list(adapter.help_argv), None, DISCOVERY_TIMEOUT_SECONDS, env)
        if help_result.error or help_result.returncode != 0:
            checks["invocation_flags"] = "help unavailable"
            return finish(STATUS_UNVERIFIED, f"{candidate} not promoted: could not read {adapter.binary} --help to verify flags")
        documented = f"{help_result.stdout}\n{help_result.stderr}"
        missing = [flag for flag in _flags(shapes) if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", documented)]
        checks["invocation_flags"] = "ok" if not missing else {"missing": missing}
        if missing:
            return finish(STATUS_UNVERIFIED,
                          f"{candidate} not promoted: installed CLI no longer documents {', '.join(missing)}")
        record["last_verified_model"] = record["effective_model"] = candidate
        record["previous_verified_model"] = configured
        return finish(STATUS_VERIFIED, f"{candidate} is enumerated by the authenticated native session and compatible "
                                       f"with every {family} worker's flags and permission shape; {configured} retained as fallback")
    except Exception as exc:  # AO startup must never fail because a provider CLI misbehaved
        return finish(STATUS_UNAVAILABLE, f"native model verification error: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- consumers


def _group(registry: Any) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = {}
    for worker in registry.workers.values():
        if worker.native_model_family:
            groups.setdefault(worker.native_model_family, []).append(worker)
    return groups


def _configured(workers: Sequence[Any]) -> str:
    # Workers of one family share one configured default; the lowest is authoritative if they ever disagree.
    return sorted({w.default_model for w in workers})[0]


def ensure_fresh(
    registry: Any, *, max_age_seconds: float = REFRESH_MAX_AGE_SECONDS, force: bool = False,
    directory: Path | None = None, now: _dt.datetime | None = None, **discovery: Any,
) -> dict[str, dict[str, Any]]:
    """Refresh each family whose record is missing, old, or invalidated (CLI version / configuration change)."""

    moment = now or _now()
    out: dict[str, dict[str, Any]] = {}
    runner: Runner = discovery.get("runner") or subprocess_runner
    for family, workers in _group(registry).items():
        shapes = [WorkerShape.of(w) for w in workers]
        configured = _configured(workers)
        cached = load_record(family, directory)
        fresh = (
            cached is not None and not force
            and cached.get("configured_default") == configured
            and cached.get("worker_shapes") == shapes_fingerprint(shapes)
            and (age := _age(cached, moment)) is not None and age < max_age_seconds
        )
        if fresh:
            live = runner([workers[0].cli_bin, "--version"], None, DISCOVERY_TIMEOUT_SECONDS, _discovery_env()) \
                if (discovery.get("which") or _which)(workers[0].cli_bin) else None
            same = live is not None and live.returncode == 0 and (live.stdout.strip().splitlines() or [""])[0].strip() == cached.get("cli_version")
            fresh = bool(same)
        out[family] = cached if fresh else refresh(family, configured, shapes, directory=directory, now=moment, **discovery)
    return out


def effective_model(family: str, configured: str, *, worker: str | None = None, shape: str | None = None,
                    directory: Path | None = None, now: _dt.datetime | None = None) -> str:
    """The verified native model for ``family``; the configured default whenever no valid verification exists.

    ``worker``/``shape`` bind the record to that worker's current flags and permissions: a template edited since the
    verification invalidates the advance for that worker.  Cache-only: never spawns anything, never raises.
    """

    record = load_record(family, directory)
    if (
        record is None or record.get("status") != STATUS_VERIFIED
        or record.get("configured_default") != configured
        or (shape is not None and (record.get("shape_hashes") or {}).get(worker) != shape)
    ):
        return configured
    age = _age(record, now or _now())
    candidate = (record.get("candidate") or {}).get("native_id")
    if age is None or age > READ_MAX_AGE_SECONDS or not candidate or record.get("effective_model") != candidate:
        return configured
    return candidate


def freshness_evidence(worker: Any, used_model: str, *, directory: Path | None = None) -> dict[str, Any] | None:
    """Compact per-run evidence for the manifest; ``None`` for a worker outside a native family."""

    family = worker.native_model_family
    if not family:
        return None
    record = load_record(family, directory) or {}
    return {
        "family": family, "provider": ADAPTERS[family].provider, "configured_default": worker.default_model,
        "actual_model": used_model, "effective_model": worker.effective_model,
        "status": record.get("status", "not-refreshed"), "reason": record.get("reason", "no verification record"),
        "candidate": (record.get("candidate") or {}).get("native_id"),
        "last_verified_model": record.get("last_verified_model", worker.default_model),
        "checked_at": record.get("checked_at"), "fingerprint": record.get("fingerprint"),
        "cli_version": record.get("cli_version"), "probe": record.get("probe", PROBE),
    }


def inventory(registry: Any, *, directory: Path | None = None) -> dict[str, Any]:
    """Read-only dashboard view: never runs a CLI."""

    rows = []
    for family, workers in sorted(_group(registry).items()):
        record = load_record(family, directory)
        configured = _configured(workers)
        status = (record or {}).get("status", "not-refreshed")
        reason = (record or {}).get("reason", "no verification record; run `python -m scripts.agents.native_models refresh`")
        age = _age(record, _now()) if record else None
        if record and status == STATUS_VERIFIED and (age is None or age > READ_MAX_AGE_SECONDS):
            status, reason = "stale", "verification is older than seven days; the configured model is in force until a refresh"
        rows.append({
            "family": family, "provider": ADAPTERS[family].provider, "configured_default": configured,
            "effective_model": workers[0].effective_model,
            "workers": sorted(w.name for w in workers),
            "status": status, "reason": reason,
            "candidate": (record or {}).get("candidate"),
            "newer_other_tier": (record or {}).get("newer_other_tier", []),
            "opencode_hints": (record or {}).get("opencode_hints", []),
            "last_verified_model": (record or {}).get("last_verified_model", configured),
            "cli_version": (record or {}).get("cli_version"), "auth": (record or {}).get("auth"),
            "checked_at": (record or {}).get("checked_at"), "fingerprint": (record or {}).get("fingerprint"),
            "probe": PROBE,
        })
    return {"families": rows}


# --------------------------------------------------------------------------- CLI


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from .registry import load_registry

    parser = argparse.ArgumentParser(prog="scripts.agents.native_models", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="re-enumerate the installed native CLIs (local, non-billable)")
    sub.add_parser("list", help="show cached freshness state (no subprocess)")
    args = parser.parse_args(argv)
    registry = load_registry()
    if args.command == "list":
        print(json.dumps(inventory(registry), indent=2))
        return 0
    ensure_fresh(registry, force=True)
    print(json.dumps(inventory(registry), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
