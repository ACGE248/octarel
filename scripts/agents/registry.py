"""Load and query the provider-neutral worker registry (``workers.json``).

The registry is the single description of which local execution systems this repo
knows how to delegate to, what each one costs, whether it can write, and the
CLI template used to build its invocation. Routing preferences (cheapest capable
worker first) also live here so there is no second policy source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .probe import run_probe

REGISTRY_PATH = Path(__file__).with_name("workers.json")

WRITE_CAPABILITIES = frozenset({"write", "focused-edit"})
READ_ONLY_CAPABILITY = "read-only"
VALID_INTENSITIES = ("low", "medium", "high")

REPOSITORY_AUTH_EXPLICIT_WORKER_SELECTION = "explicit-worker-selection"
REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE = "preauthorized-scoped-reuse"
REPOSITORY_AUTH_AUTOMATIC_REUSE = frozenset(
    {REPOSITORY_AUTH_EXPLICIT_WORKER_SELECTION, REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE}
)

# A worker's normal, interactive-shaped permission mode (e.g. claude-code's
# ``--permission-mode manual``). Every worker supports this implicitly via its
# base ``cli_template``; it is never overridden. This is the single source of
# truth for the permission-profile name — ``control_plane.models`` re-exports
# it rather than defining a second copy, since Task/Runbook and Worker must
# agree on the exact same value.
PERMISSION_STANDARD = "standard"

# An explicit, opt-in unattended profile for a bounded Runbook session
# (ENG-AGENT-02-S5, issue #93/#94): only a worker that declares a
# ``permission_profiles`` entry for this name in ``workers.json`` supports it,
# and only a write-capable worker may ever be launched with it —
# ``supports_permission_profile``/``build_command`` reject a read-only worker
# directly (a malformed registry entry cannot make this profile reachable),
# and ``control_plane.runbooks`` (``create_runbook``, ``update_runbook``, and
# ``start_runbook`` immediately before launch) plus ``orchestrate.py``'s
# ``run_session`` independently re-check the same constraint. The sibling
# ``orchestrate.py`` ``run`` verb (ENG-AGENT-01's general narrow-scope
# delegation path) never accepts any profile but the default at all — only
# ``session`` (the whole-worktree Runbook mechanism) can. This profile never
# changes a worker's default/base template, so an ordinary delegated task's
# invocation is byte-for-byte unaffected.
PERMISSION_REPO_CONFIGURED_AUTO = "repo_configured_auto"

KNOWN_PERMISSION_PROFILES = frozenset({PERMISSION_STANDARD, PERMISSION_REPO_CONFIGURED_AUTO})

# ENG-AGENT-02-S7 (issue #97): a truthful reason a worker is not currently
# usable, replacing the previous binary "CLI present or NOT_CONFIGURED" model.
# A worker's own `check_auth()` (below) is the only thing that can report
# NOT_AUTHENTICATED; everything else is derivable from static registry/CLI
# facts alone. This is deliberately *not* the same vocabulary as
# `control_plane.provider_state`'s runtime states (QUOTA_EXHAUSTED,
# RATE_LIMITED, ...), which describe what happened during an actual run —
# this one describes why a provider was never routable in the first place.
REASON_AVAILABLE = "AVAILABLE"
REASON_DISABLED = "DISABLED"
REASON_CLI_MISSING = "CLI_MISSING"
REASON_NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
REASON_API_ONLY_NOT_AUTHORIZED = "API_ONLY_NOT_AUTHORIZED"
REASON_UNSUPPORTED = "UNSUPPORTED"
REASON_CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
REASON_CATALOG_ONLY = "CATALOG_ONLY"
# ENG-AGENT-11 (issue #131): the local auth-status probe ran (or tried to)
# but never produced a truthful answer either way -- a sandboxed/restricted
# launcher that cannot reach the CLI's session/keychain state, a timeout, or
# a status invocation that returned no parseable output. This is distinct
# from REASON_NOT_AUTHENTICATED, which is reserved for a probe that actually
# received and understood a negative answer (e.g. "Not logged in", or a
# rejected auth method). Conflating the two mislabels a launch-environment
# defect as "the operator needs to log in again" and can trigger routine
# same-role fallback for a worker that is, in fact, still usable.
REASON_LAUNCH_ENVIRONMENT_ERROR = "LAUNCH_ENVIRONMENT_ERROR"

KNOWN_AVAILABILITY_REASONS = frozenset(
    {
        REASON_AVAILABLE,
        REASON_DISABLED,
        REASON_CLI_MISSING,
        REASON_NOT_AUTHENTICATED,
        REASON_API_ONLY_NOT_AUTHORIZED,
        REASON_UNSUPPORTED,
        REASON_CONFIGURATION_ERROR,
        REASON_CATALOG_ONLY,
        REASON_LAUNCH_ENVIRONMENT_ERROR,
    }
)

# Checked first in Worker.check_auth() so a negated CLI status line (e.g. "Not
# logged in") can never be mistaken for a positive match just because it
# happens to contain a configured success_pattern as a literal substring.
_AUTH_NEGATION_MARKERS = (
    "not logged in",
    "not authenticated",
    "logged out",
    "please log in",
    "please run",
    "no credentials",
    '"loggedin": false',
)

# Phrases in a failed auth-check's own output that indicate the *launcher* or
# *sandbox* could not reach the CLI's session state at all -- never a
# provider-reported "you are logged out" answer. Checked only after the
# explicit negation markers above have already been ruled out.
_LAUNCH_ERROR_HINTS = (
    "permission denied",
    "operation not permitted",
    "sandbox",
    "no such file or directory",
    "unable to access",
    "econnrefused",
    "keychain",
    "enoent",
    "eperm",
)


class AuthCheckResult:
    """Outcomes of one local auth/session classification.

    Kept as string constants (not a real ``Enum``) so log/event payloads and
    ``failure_attribution`` can serialize a result directly without an extra
    conversion step.
    """

    AUTHENTICATED = "authenticated"
    EXPLICIT_NOT_AUTHENTICATED = "explicit_not_authenticated"
    LAUNCH_ERROR = "launch_error"


class RegistryError(ValueError):
    """Raised for a malformed registry or an unknown worker/role."""


@dataclass(frozen=True)
class Worker:
    name: str
    execution_system: str
    provider: str
    default_model: str
    default_intensity: str
    capability: str
    cost_class: str
    roles: tuple[str, ...]
    allowed_policy_roles: tuple[str, ...]
    provider_policy: str
    auth_mode: str
    repository_data_authorization: str
    allow_api_billing: bool
    requires_isolated_worktree: bool
    cli_bin: str
    cli_version_args: tuple[str, ...]
    cli_template: tuple[str, ...]
    enabled: bool
    preserve: bool
    notes: str
    permission_profile_templates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    auth_check_args: tuple[str, ...] = ()
    auth_check_success_pattern: str | None = None
    # ENG-AGENT-11 (issue #131): an optional, real, harmless CLI invocation
    # (e.g. ``claude -p "Reply with exactly: CLAUDE_OK"``) used only to
    # settle an ambiguous metadata auth-check result -- never run routinely.
    # See ``availability_reason`` for when this actually fires.
    launch_probe_args: tuple[str, ...] = ()
    launch_probe_success_pattern: str | None = None
    # ENG-AO-02: explicit, AO-orchestrated read-only bot fan-out policy for a write-capable primary
    # (empty for every ordinary worker, including ``grok-build``).  See ``scripts/agents/subagents.py``.
    subagents: dict[str, Any] = field(default_factory=dict)
    # ENG-AO-03: name of a runtime model pool (``scripts/agents/model_catalog.py``) that supplies this read-only worker's
    # model.  Empty for every configured worker, whose ``default_model`` stays authoritative.
    model_pool: str = ""
    # ENG-AO-04: native CLI family (``grok`` / ``codex``) whose verified newer model may replace ``default_model``
    # (``scripts/agents/native_models.py``).  ``default_model`` stays the last verified baseline; empty = never advances.
    native_model_family: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_write_capable(self) -> bool:
        return self.capability in WRITE_CAPABILITIES

    @property
    def is_read_only(self) -> bool:
        return self.capability == READ_ONLY_CAPABILITY

    @property
    def effective_model(self) -> str:
        """The model a run uses when none is requested: the verified native model, else ``default_model``.

        Cache-only (no subprocess); worker identity, role, permissions, and routing are unaffected by it.
        """

        if not self.native_model_family:
            return self.default_model
        from . import native_models

        return native_models.effective_model(
            self.native_model_family, self.default_model, worker=self.name,
            shape=native_models.shape_hash(native_models.WorkerShape.of(self)),
        )

    def cli_available(self) -> bool:
        return shutil.which(self.cli_bin) is not None

    def _classify_auth_check(self, *, timeout: float = 5.0) -> str | None:
        """Classify one local auth/session probe into an :class:`AuthCheckResult`.

        Returns ``None`` when this worker declares no ``cli.auth_check`` in
        ``workers.json``. Otherwise distinguishes an explicit, trustworthy
        negative answer (the CLI ran and clearly said "not logged in", or
        rejected an unsupported auth method) from a launch-environment
        failure that never produced a truthful answer either way: the CLI
        missing, the process failing to spawn, timing out, or returning no
        parseable output at all (empty stdout with no explicit negation is
        exactly what a restricted/sandboxed launcher that cannot see the
        real session looks like -- ENG-AGENT-11, issue #131). This never
        sends credentials anywhere; it only inspects the local CLI's own
        already-authenticated session/config on disk.
        """

        if not self.auth_check_args:
            return None
        if not self.cli_available():
            return AuthCheckResult.LAUNCH_ERROR
        try:
            result = run_probe([self.cli_bin, *self.auth_check_args], timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return AuthCheckResult.LAUNCH_ERROR
        combined = f"{result.stdout}\n{result.stderr}".lower()
        # Checked before the success pattern: a negated status line (e.g. "Not
        # logged in") can literally contain a positive pattern like "logged in"
        # as a substring, which would otherwise read as a false "authenticated."
        if any(marker in combined for marker in _AUTH_NEGATION_MARKERS):
            return AuthCheckResult.EXPLICIT_NOT_AUTHENTICATED
        if any(hint in combined for hint in _LAUNCH_ERROR_HINTS):
            return AuthCheckResult.LAUNCH_ERROR
        if not result.stdout.strip():
            # The process ran but said nothing usable -- an environment/
            # visibility failure, not a truthful logout signal.
            return AuthCheckResult.LAUNCH_ERROR
        if self.auth_check_success_pattern:
            if self.auth_check_success_pattern.lower() in combined:
                return AuthCheckResult.AUTHENTICATED
            return AuthCheckResult.EXPLICIT_NOT_AUTHENTICATED
        return AuthCheckResult.AUTHENTICATED if result.returncode == 0 else AuthCheckResult.EXPLICIT_NOT_AUTHENTICATED

    def check_auth(self, *, timeout: float = 5.0) -> bool | None:
        """Best-effort local authentication check for this worker's CLI.

        Returns ``None`` when this worker declares no ``cli.auth_check`` in
        ``workers.json`` — the caller should then fall back to "CLI present
        implies usable," exactly as every worker behaved before this
        capability existed (ENG-AGENT-02-S7, issue #97). Returns ``True``/
        ``False`` when a check is declared and actually ran. Never raises:
        any failure to even invoke the check (missing CLI, timeout, OS
        error) is reported as ``False`` — fail closed into "not usable yet"
        rather than fail open into a false "Available." The richer
        distinction between an explicit logout and an unreadable launch
        environment lives in ``availability_reason``/``_classify_auth_check``;
        this boolean shape is preserved unchanged for existing callers.
        """

        classification = self._classify_auth_check(timeout=timeout)
        if classification is None:
            return None
        return classification == AuthCheckResult.AUTHENTICATED

    def run_launch_probe(self, *, timeout: float = 30.0) -> bool | None:
        """Run this worker's optional authoritative harmless-prompt probe.

        Only meaningful when ``launch_probe_args`` is declared in
        ``workers.json`` (today, only ``claude-code``). Returns ``None`` when
        undeclared, ``True`` when the CLI actually produced the expected
        real reply, ``False`` otherwise (including any spawn/timeout
        failure). This is a real CLI invocation -- kept out of routine
        probing and fired only from ``availability_reason`` when the cheap
        metadata auth-check result was ambiguous, per issue #131's
        requirement that a successful authoritative prompt overrides a
        disagreeing heuristic.
        """

        if not self.launch_probe_args:
            return None
        if not self.cli_available():
            return False
        try:
            result = run_probe([self.cli_bin, *self.launch_probe_args], timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or not self.launch_probe_success_pattern:
            return False
        combined = f"{result.stdout}\n{result.stderr}"
        return self.launch_probe_success_pattern in combined

    def availability_reason(self, *, probe: bool = True) -> str:
        """One truthful reason this worker is or is not usable right now.

        Static/cheap checks only (enabled flag, CLI presence); this never
        makes a network call and never reports transient runtime states like
        quota exhaustion (see ``control_plane.provider_state`` for those,
        which only apply after an actual run has already happened).

        ``probe`` additionally runs the local auth-session check (a real,
        local subprocess spawn — e.g. ``codex login status``) when this
        worker declares one. It defaults to ``True`` for a caller that
        explicitly wants the fullest available truth (e.g. an operator- or
        dashboard-triggered probe), but passive/frequent callers — building
        an orchestrator command context, seeding a fresh database, a
        ``--dry-run`` invocation — must pass ``probe=False`` so that routine
        CLI use never silently spawns a subprocess per call.

        When the local auth-session check is ambiguous (``AuthCheckResult
        .LAUNCH_ERROR`` — the probe never produced a truthful answer either
        way) and this worker declares a ``launch_probe``, one real, harmless
        CLI invocation is given the chance to prove the worker is actually
        usable before anything is reported; a successful authoritative reply
        makes the route ``AVAILABLE`` even though the cheap metadata check
        disagreed (issue #131). Only when that authoritative probe is either
        undeclared or also fails is ``REASON_LAUNCH_ENVIRONMENT_ERROR``
        reported — distinct from ``REASON_NOT_AUTHENTICATED``, which is
        reserved for a probe that actually received and understood a
        negative answer, so a sandbox/launcher visibility defect is never
        mislabeled as the operator needing to log in again.
        """

        if not self.enabled:
            return REASON_DISABLED
        if not self.cli_available():
            return REASON_CLI_MISSING
        if not probe:
            return REASON_AVAILABLE
        classification = self._classify_auth_check()
        if classification in (None, AuthCheckResult.AUTHENTICATED):
            return REASON_AVAILABLE
        if classification == AuthCheckResult.EXPLICIT_NOT_AUTHENTICATED:
            return REASON_NOT_AUTHENTICATED
        if self.run_launch_probe() is True:
            return REASON_AVAILABLE
        return REASON_LAUNCH_ENVIRONMENT_ERROR

    def supports_permission_profile(self, permission_profile: str) -> bool:
        """Whether this worker has an explicit invocation for ``permission_profile``.

        Every worker trivially supports :data:`PERMISSION_STANDARD` (its
        unmodified base ``cli_template``); any other profile requires an
        explicit ``permission_profiles`` entry in ``workers.json`` *and* a
        write-capable worker. The read-only check is enforced here — not only
        by callers such as ``orchestrate.py``/``runbooks.py`` — so this
        registry method is itself a real gate: a malformed ``workers.json``
        that mistakenly declared a ``permission_profiles`` entry for a
        read-only worker still could not make ``build_command`` honor it.
        """

        if permission_profile == PERMISSION_STANDARD:
            return True
        return permission_profile in self.permission_profile_templates and not self.is_read_only

    def build_command(
        self, *, model: str | None, intensity: str, prompt: str, permission_profile: str = PERMISSION_STANDARD
    ) -> list[str]:
        """Return the full argv for this worker.

        Placeholders ``{model}`` and ``{intensity}`` in the template are
        substituted; the complete bounded prompt is appended as one argument.
        ``permission_profile`` selects which template to use: the default
        :data:`PERMISSION_STANDARD` always uses ``cli_template`` unchanged
        (so ordinary delegated/manual tasks are byte-for-byte unaffected by
        this parameter's existence); any other value requires the worker to
        declare a matching ``permission_profiles`` entry, or this raises
        :class:`RegistryError` rather than silently falling back to the
        interactive-shaped default.
        """

        resolved_model = model or self.effective_model
        if self.model_pool and not model:
            raise RegistryError(f"worker {self.name!r} draws its model from pool {self.model_pool!r}; resolve one first")
        if permission_profile == PERMISSION_STANDARD:
            template = self.cli_template
        else:
            if not self.supports_permission_profile(permission_profile):
                raise RegistryError(
                    f"worker {self.name!r} does not support permission_profile {permission_profile!r}"
                )
            template = self.permission_profile_templates[permission_profile]
        substituted: list[str] = []
        prompt_inserted = False
        for token in template:
            if token == "{prompt}":
                substituted.append(prompt)
                prompt_inserted = True
                continue
            token = token.replace("{model}", resolved_model).replace("{intensity}", intensity)
            substituted.append(token)
        if not prompt_inserted:
            substituted.append(prompt)
        return [self.cli_bin, *substituted]


def _coerce_worker(name: str, data: dict[str, Any]) -> Worker:
    try:
        cli = data["cli"]
        permission_profiles_raw = cli.get("permission_profiles", {})
        auth_check_raw = cli.get("auth_check") or {}
        launch_probe_raw = cli.get("launch_probe") or {}
        return Worker(
            name=name,
            execution_system=data["execution_system"],
            provider=data["provider"],
            default_model=data.get("default_model", ""),
            default_intensity=data.get("default_intensity", "low"),
            capability=data["capability"],
            cost_class=data["cost_class"],
            roles=tuple(data.get("roles", ())),
            allowed_policy_roles=tuple(data.get("allowed_policy_roles", ())),
            provider_policy=data.get("provider_policy", ""),
            auth_mode=data.get("auth_mode", "unspecified"),
            repository_data_authorization=data.get("repository_data_authorization", "unspecified"),
            allow_api_billing=bool(data.get("allow_api_billing", False)),
            requires_isolated_worktree=bool(data.get("requires_isolated_worktree", False)),
            cli_bin=cli["bin"],
            cli_version_args=tuple(cli.get("version_args", ("--version",))),
            cli_template=tuple(cli.get("template", ())),
            enabled=bool(data.get("enabled", True)),
            preserve=bool(data.get("preserve", False)),
            notes=data.get("notes", ""),
            permission_profile_templates={k: tuple(v) for k, v in permission_profiles_raw.items()},
            auth_check_args=tuple(auth_check_raw.get("args", ())),
            auth_check_success_pattern=auth_check_raw.get("success_pattern"),
            launch_probe_args=tuple(launch_probe_raw.get("args", ())),
            launch_probe_success_pattern=launch_probe_raw.get("success_pattern"),
            subagents=dict(data.get("subagents") or {}),
            model_pool=str(data.get("model_pool") or ""),
            native_model_family=str(data.get("native_model_family") or ""),
            raw=data,
        )
    except KeyError as exc:  # pragma: no cover - guarded by test_registry_is_well_formed
        raise RegistryError(f"worker {name!r} is missing required key {exc}") from None


@dataclass(frozen=True)
class Registry:
    workers: dict[str, Worker]
    routes: dict[str, tuple[str, ...]]
    intensities: tuple[str, ...]
    provider_repository_data_authorizations: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> Worker:
        try:
            return self.workers[name]
        except KeyError:
            known = ", ".join(sorted(self.workers))
            raise RegistryError(f"unknown worker {name!r}; known workers: {known}") from None

    def route(self, role: str) -> tuple[str, ...]:
        if role not in self.routes:
            known = ", ".join(sorted(self.routes))
            raise RegistryError(f"unknown role {role!r}; known roles: {known}")
        return self.routes[role]

    def effective_repository_data_authorization(self, worker_name: str) -> str:
        """Resolve repository-data authorization at provider then worker level."""

        worker = self.get(worker_name)
        return self.provider_repository_data_authorizations.get(
            worker.provider, worker.repository_data_authorization
        )

    def repository_data_reuse_allowed(self, worker_name: str) -> bool:
        """Whether minimized repository context may be reused without another prompt."""

        return self.effective_repository_data_authorization(worker_name) in REPOSITORY_AUTH_AUTOMATIC_REUSE

    def first_available(self, role: str) -> Worker | None:
        """Cheapest capable worker for ``role`` whose CLI is installed and enabled.

        Never falls through to a paid/overflow worker implicitly: the ordering in
        ``workers.json`` already lists free/verified workers first, and a
        disabled worker (e.g. overflow) is skipped rather than silently used.
        """

        for name in self.route(role):
            worker = self.workers.get(name)
            if worker and worker.enabled and worker.cli_available():
                return worker
        return None


def load_registry(path: Path | None = None) -> Registry:
    path = path or REGISTRY_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    workers = {name: _coerce_worker(name, data) for name, data in raw.get("workers", {}).items()}
    routes = {role: tuple(names) for role, names in raw.get("routes", {}).items()}
    provider_authorizations = {
        str(provider): str(mode)
        for provider, mode in raw.get("provider_repository_data_authorizations", {}).items()
    }

    for role, names in routes.items():
        for name in names:
            if name not in workers:
                raise RegistryError(f"route {role!r} references unknown worker {name!r}")
    for worker in workers.values():
        if not worker.provider_policy:
            raise RegistryError(f"worker {worker.name!r} is missing provider_policy")
        if not worker.allowed_policy_roles:
            raise RegistryError(f"worker {worker.name!r} is missing allowed_policy_roles")
        if worker.is_read_only and {"IMPLEMENTER", "ORCHESTRATOR"} & set(worker.allowed_policy_roles):
            raise RegistryError(f"read-only worker {worker.name!r} claims a write-capable canonical role")
        if worker.is_write_capable and not worker.requires_isolated_worktree:
            raise RegistryError(f"write-capable worker {worker.name!r} must require worktree isolation")
        if worker.allow_api_billing:
            raise RegistryError(f"worker {worker.name!r} may not enable API billing")
        if worker.native_model_family:
            from . import native_models

            if not native_models.known_family(worker.native_model_family) or worker.model_pool:
                raise RegistryError(f"worker {worker.name!r} has an invalid native_model_family")
        if worker.model_pool and not worker.is_read_only:
            raise RegistryError(f"dynamic-model worker {worker.name!r} must be read-only")
    from .subagents import validate_subagent_configs

    validate_subagent_configs(workers)
    known_providers = {worker.provider for worker in workers.values()}
    for provider, mode in provider_authorizations.items():
        if provider not in known_providers:
            raise RegistryError(f"repository-data authorization references unknown provider {provider!r}")
        if mode != REPOSITORY_AUTH_PREAUTHORIZED_SCOPED_REUSE:
            raise RegistryError(f"provider {provider!r} has unsupported repository-data authorization {mode!r}")
    return Registry(
        workers=workers,
        routes=routes,
        intensities=tuple(raw.get("intensities", VALID_INTENSITIES)),
        provider_repository_data_authorizations=provider_authorizations,
    )
