"""ENG-PC-11 (issue #39): structured adapter capability and result contract.

Paperclip's adapter separation makes runtime/session/result translation an
explicit responsibility of the adapter instead of something every caller
re-guesses. This module is the same idea sized for Octarel: a typed,
side-effect-free read over the *existing* ``workers.json``/``Worker`` facts
(:mod:`scripts.agents.registry`) plus a typed view over an already-finished
:class:`~scripts.agents.manifest.RunRecord`.

Two invariants hold everywhere in this module, because they are the entire
point of the task:

* **A missing capability is explicit, never inferred from a model or
  marketing name.** Where this stack genuinely does not implement something
  yet (session resume, streaming events, on-demand cancellation), every
  worker reports that capability as unsupported with a concrete, named reason
  -- not silently omitted, and never guessed from how a worker's model
  happens to be named.
* **This is a read-only seam, not a second policy source.** Nothing here
  changes ``workers.json`` routing order, cost class, auth probing, or the
  ENG-AO-02/03/04 mechanisms; :func:`capabilities_for` only reads facts a
  worker already declares. It never spawns a subprocess or makes a network
  call, so it is safe to call on every request (e.g. a dashboard listing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .control_plane.usage_telemetry import (
    CLASS_DERIVED,
    CLASS_MEASURED,
    CLASS_NOT_EXPOSED,
    CLASS_UNKNOWN,
    REASON_NO_CONTEXT_LIMIT,
    metric,
)
from .manifest import (
    FAILURE_NONE,
    FAILURE_NOT_RUN,
    FAILURE_PERMISSION_DENIED,
    FAILURE_READ_ONLY_VIOLATION,
    FAILURE_TIMEOUT,
    FAILURE_WORKER_ERROR,
    RESULT_FAIL,
    RESULT_PASS,
    RunRecord,
    classify_failure,
)
from .redaction import redact_text
from .registry import PERMISSION_STANDARD, Worker
from .runner import first_structured_result, structured_failure

# --------------------------------------------------------------- capabilities

MODEL_DISCOVERY_STATIC = "static_registry_default"
MODEL_DISCOVERY_NATIVE_VERIFIED = "native_cli_verified_discovery"
MODEL_DISCOVERY_RUNTIME_POOL = "runtime_model_pool"

# Stated once, reused by every worker, because it is a true fact about the
# whole stack today rather than something that varies per provider: ENG-PC-02
# (resumable sessions) has not landed yet, so no adapter can claim resume
# support without lying about a task that has not been built.
RESUME_NOT_IMPLEMENTED_REASON = (
    "ENG-PC-02 task-scoped resumable sessions is not implemented yet; no worker "
    "in this registry declares native session/resume support"
)
# Likewise for ENG-PC-04's structured run-event timeline: every worker today
# runs as one blocking subprocess capture (scripts.agents.runner.run_worker_process),
# so there is no incremental event stream to report as a per-worker capability.
STREAMING_NOT_IMPLEMENTED_REASON = (
    "ENG-PC-04 structured run-event timeline is not implemented yet; every worker "
    "runs as one blocking subprocess capture with no incremental event stream"
)
# The orchestrator can force-kill a run's process group once its timeout
# elapses, but that is a safety net triggered by the orchestrator's own
# deadline, not a channel a caller can use to cancel a run on demand at an
# arbitrary point -- the two are not the same capability.
CANCELLATION_NOT_IMPLEMENTED_REASON = (
    "no worker transport exposes an on-demand cancel channel; the orchestrator can "
    "only force-kill a run's process group after its timeout elapses "
    "(scripts.agents.runner.run_worker_process/run_worker_process_group), which is "
    "a timeout safety net, not a caller-invoked cancellation capability"
)
# No worker's model ever gets a native subagent/tool-fanout channel from this stack: even
# grok-build-bots, the one worker with an ENG-AO-02 ``subagents`` block in workers.json, passes
# --no-subagents in its own CLI template (scripts.agents.subagents.GrokCliTransport.validate
# enforces this) because that block means the *orchestrator* runs bounded read-only bots beside
# the primary (scripts.agents.subagents.run_fanout) -- not that the model has a native subagent
# channel. Inferring this capability from the presence of that field would be exactly the kind of
# guess-from-an-unrelated-fact this contract exists to prevent.
NATIVE_SUBAGENTS_NOT_IMPLEMENTED_REASON = (
    "no worker's model has a native subagent/tool-fanout channel in this stack; a workers.json "
    "'subagents' block (ENG-AO-02) is an orchestrator-run bounded read-only bot fan-out beside the "
    "primary (scripts.agents.subagents.run_fanout), not a model-side subagent capability, and every "
    "worker carrying that block still runs with --no-subagents in its own CLI invocation"
)


@dataclass(frozen=True)
class AdapterCapabilities:
    """Explicit, worker-scoped capability facts for the typed adapter contract.

    Every field is derived only from static ``workers.json``/``Worker`` facts
    that already exist. Building one never spawns a subprocess, never makes a
    network call, and never looks at a model's name to guess something the
    registry does not actually declare.
    """

    worker: str
    provider: str
    execution_system: str
    # "write" / "read-only" / "focused-edit" -- worker.capability's own
    # vocabulary, carried through unchanged rather than re-encoded.
    capability: str
    can_write: bool
    can_resume_session: bool
    resume_unavailable_reason: str | None
    reports_structured_usage: bool
    structured_usage_reason: str | None
    effective_model_discovery: str
    effective_model_discovery_reason: str
    context_limit_class: str
    context_limit_reason: str | None
    supports_streaming_events: bool
    streaming_unavailable_reason: str | None
    supports_auth_probe: bool
    auth_probe_reason: str | None
    permission_profiles: tuple[str, ...]
    supports_cancellation: bool
    cancellation_unavailable_reason: str | None
    supports_native_subagents: bool
    native_subagents_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker,
            "provider": self.provider,
            "execution_system": self.execution_system,
            "capability": self.capability,
            "can_write": self.can_write,
            "resume": {"supported": self.can_resume_session, "reason": self.resume_unavailable_reason},
            "structured_usage": {"supported": self.reports_structured_usage, "reason": self.structured_usage_reason},
            "effective_model_discovery": {
                "source": self.effective_model_discovery,
                "reason": self.effective_model_discovery_reason,
            },
            "context_limit": {"class": self.context_limit_class, "reason": self.context_limit_reason},
            "streaming_events": {"supported": self.supports_streaming_events, "reason": self.streaming_unavailable_reason},
            "auth_probe": {"supported": self.supports_auth_probe, "reason": self.auth_probe_reason},
            "permission_profiles": list(self.permission_profiles),
            "cancellation": {"supported": self.supports_cancellation, "reason": self.cancellation_unavailable_reason},
            "native_subagents": {"supported": self.supports_native_subagents, "reason": self.native_subagents_reason},
        }


def _structured_output_requested(worker: Worker) -> bool:
    """Whether ``worker``'s own declared CLI template requests structured JSON output.

    This is exactly the structural assumption ``runner.structured_failure``/
    ``runner.structured_actual_model`` already make when they try to parse a
    worker's captured output as JSON -- made explicit and queryable here
    instead of left implicit. It reads the worker's own declared invocation
    template, never a model or marketing name.
    """

    template = " ".join(worker.cli_template).lower()
    return "json" in template


def capabilities_for(worker: Worker) -> AdapterCapabilities:
    """Build the explicit capability contract for one registry worker.

    Pure and side-effect-free: reading these facts never changes what
    ``registry.route``/``registry.first_available`` return, never probes a
    CLI, and never makes this worker eligible for anything it was not already
    eligible for.
    """

    structured = _structured_output_requested(worker)
    if worker.native_model_family:
        discovery = MODEL_DISCOVERY_NATIVE_VERIFIED
        discovery_reason = (
            f"{worker.name} verifies its native {worker.native_model_family!r} CLI model list "
            "(scripts.agents.native_models) and only ever replaces the configured default with a "
            "verified newer release, never a model inferred by name"
        )
    elif worker.model_pool:
        discovery = MODEL_DISCOVERY_RUNTIME_POOL
        discovery_reason = (
            f"{worker.name} draws its model from runtime pool {worker.model_pool!r} "
            "(scripts.agents.model_catalog), resolved per run rather than fixed in the registry"
        )
    else:
        discovery = MODEL_DISCOVERY_STATIC
        discovery_reason = (
            f"{worker.name} always uses its configured default_model "
            f"({worker.default_model!r}); no runtime model discovery is performed"
        )

    # Mirrors Worker.supports_permission_profile's real rule exactly (a
    # read-only worker never supports a non-standard profile, regardless of
    # what permission_profile_templates happens to list) so this contract can
    # be the one place orchestration code checks -- not a second, looser copy
    # of the same rule.
    permission_profiles = (
        (PERMISSION_STANDARD,)
        if worker.is_read_only
        else tuple(sorted({PERMISSION_STANDARD, *worker.permission_profile_templates}))
    )

    return AdapterCapabilities(
        worker=worker.name,
        provider=worker.provider,
        execution_system=worker.execution_system,
        capability=worker.capability,
        can_write=worker.is_write_capable,
        can_resume_session=False,
        resume_unavailable_reason=RESUME_NOT_IMPLEMENTED_REASON,
        reports_structured_usage=structured,
        structured_usage_reason=(
            None if structured else f"{worker.name}'s CLI template does not request structured JSON output"
        ),
        effective_model_discovery=discovery,
        effective_model_discovery_reason=discovery_reason,
        context_limit_class=CLASS_NOT_EXPOSED,
        context_limit_reason=REASON_NO_CONTEXT_LIMIT,
        supports_streaming_events=False,
        streaming_unavailable_reason=STREAMING_NOT_IMPLEMENTED_REASON,
        supports_auth_probe=bool(worker.auth_check_args),
        auth_probe_reason=(
            None if worker.auth_check_args else f"{worker.name} declares no cli.auth_check in workers.json"
        ),
        permission_profiles=permission_profiles,
        supports_cancellation=False,
        cancellation_unavailable_reason=CANCELLATION_NOT_IMPLEMENTED_REASON,
        supports_native_subagents=False,
        native_subagents_reason=NATIVE_SUBAGENTS_NOT_IMPLEMENTED_REASON,
    )


# --------------------------------------------------------------- run result

# FAILURE_* lives in scripts.agents.manifest (not here) because
# RunRecord.to_manifest must always populate it and manifest.py cannot import
# this module without an import cycle. Re-exported here -- this is the only
# name this module's own callers and tests use for the taxonomy -- via
# manifest.classify_failure, which is the single place that derives it from a
# finished record's own result/exit_status/notes.
SESSION_UPDATE_NOT_IMPLEMENTED_REASON = (
    "ENG-PC-02 task-scoped resumable sessions is not implemented yet; no adapter "
    "produces an opaque session-continuation payload"
)


def classify_finished_run(*, exit_status: int, log_text: str) -> tuple[str, str | None]:
    """The single authority for PASS/FAIL classification of one finished primary run.

    Every ``orchestrate`` entry point that runs a worker's primary process
    (``run_delegation`` and ``run_session``) reduces to the exact same
    decision once a run has an exit status and captured output: did the
    worker's own structured output report a failure, and if not, did the
    process exit zero. Kept in one place instead of copied per verb.
    """

    failure_reason = structured_failure(log_text)
    if failure_reason:
        return RESULT_FAIL, failure_reason
    return (RESULT_PASS if exit_status == 0 else RESULT_FAIL), None


def structural_usage_keys(capabilities: AdapterCapabilities, log_text: str) -> dict[str, Any]:
    """The literal per-model usage category keys this run's CLI reported, if any.

    Deliberately does not sum, price, or interpret these keys -- that belongs
    to ENG-PC-05's usage ledger, which can define its own schema once it
    exists. This only proves, per run, whether the worker's own structured
    output carried a usage breakdown at all, and names what it called its
    categories -- reusing the ``MEASURED``/``DERIVED``/``UNKNOWN``/
    ``NOT_EXPOSED`` vocabulary from ``control_plane.usage_telemetry`` rather
    than inventing a parallel one.
    """

    if not capabilities.reports_structured_usage:
        return metric(klass=CLASS_NOT_EXPOSED, reason=capabilities.structured_usage_reason)
    payload = first_structured_result(log_text)
    if payload is None:
        return metric(
            klass=CLASS_UNKNOWN,
            reason="worker declares structured JSON output but this run's captured output did not parse as JSON",
        )
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return metric(klass=CLASS_UNKNOWN, reason="structured result contained no modelUsage breakdown for this run")
    first_entry = next(iter(usage.values()), None)
    if not isinstance(first_entry, dict) or not first_entry:
        return metric(klass=CLASS_UNKNOWN, reason="modelUsage was present but not shaped as a per-model breakdown")
    categories = tuple(sorted(redact_text(str(key)) for key in first_entry))
    return metric(categories, klass=CLASS_MEASURED, source="worker CLI structured JSON output for this run")


@dataclass(frozen=True)
class RunResult:
    """A typed view over one already-finished delegated run.

    Feeds the existing manifest/``RunRecord`` evidence -- it is not a second
    evidence store. ``evidence_paths`` is exactly the repository-relative
    ``manifest``/``summary``/``log`` mapping ``manifest.write_artifacts``
    already produces.
    """

    task: str
    role: str
    worker: str
    provider: str
    execution_system: str
    requested_model: str
    actual_model: str
    actual_model_class: str
    result: str
    failure_category: str
    exit_status: int | None
    duration_seconds: float | None
    usage_categories: dict[str, Any]
    session_update: str | None
    session_update_reason: str | None
    evidence_paths: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "role": self.role,
            "worker": self.worker,
            "provider": self.provider,
            "execution_system": self.execution_system,
            "requested_model": self.requested_model,
            "actual_model": {"value": self.actual_model, "class": self.actual_model_class},
            "result": self.result,
            "failure_category": self.failure_category,
            "exit_status": self.exit_status,
            "duration_seconds": self.duration_seconds,
            "usage_categories": self.usage_categories,
            "session_update": {"value": self.session_update, "reason": self.session_update_reason},
            "evidence_paths": dict(self.evidence_paths),
        }


def run_result_from_record(
    record: RunRecord,
    *,
    capabilities: AdapterCapabilities,
    evidence_paths: dict[str, str],
    log_text: str = "",
) -> RunResult:
    """Build a typed :class:`RunResult` from an already-finished ``RunRecord``.

    ``capabilities`` must describe the same worker the record ran on --
    mismatching the two would silently attribute one worker's capability
    facts to another worker's run, which this refuses outright.
    """

    if record.worker != capabilities.worker:
        raise ValueError(
            f"capabilities for {capabilities.worker!r} do not match run record worker {record.worker!r}"
        )
    actual_model = record.actual_model or record.planned_model
    if record.actual_model and record.actual_model_measured:
        actual_model_class = CLASS_MEASURED
    elif actual_model:
        actual_model_class = CLASS_DERIVED
    else:
        actual_model_class = CLASS_UNKNOWN
    return RunResult(
        task=record.task,
        role=record.role,
        worker=record.worker,
        provider=record.actual_provider or record.planned_provider,
        execution_system=record.actual_execution_system or record.planned_execution_system,
        requested_model=record.planned_model,
        actual_model=actual_model,
        actual_model_class=actual_model_class,
        result=record.result,
        failure_category=classify_failure(record),
        exit_status=record.exit_status,
        duration_seconds=record.duration_seconds,
        usage_categories=structural_usage_keys(capabilities, log_text),
        session_update=None,
        session_update_reason=SESSION_UPDATE_NOT_IMPLEMENTED_REASON,
        evidence_paths=dict(evidence_paths),
    )
