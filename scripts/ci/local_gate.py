#!/usr/bin/env python3
"""Authoritative, deterministic local gate for an exact Git tree.

Run this against a clean HEAD or a fully staged candidate.  Every selected
command, exit code, result and log path is recorded under ignored
``.local-gate/``.  Readiness is tied to the Git tree object, so later edits or
commits cannot inherit stale green evidence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from scripts.ci.change_risk import ChangeRisk, classify
    from scripts.ci.environment import (
        bootstrap_dependencies,
        prepare_worktree,
        resolve_python,
    )
    from scripts.ci.ports import PortLeases
    from scripts.ci.review_contract import (
        ReviewVerdict,
        indicates_agent_fallback,
        parse_review_response,
    )
    from scripts.ci.runtime_paths import (
        ensure_git_excludes,
        filter_runtime_paths,
        has_non_runtime_changes,
        runtime_dir_pathspecs,
    )
    from scripts.ci.test_impact import TestImpact, select_tests
except ModuleNotFoundError:  # direct ``python scripts/ci/local_gate.py`` entry point
    from change_risk import ChangeRisk, classify
    from environment import bootstrap_dependencies, prepare_worktree, resolve_python
    from ports import PortLeases
    from review_contract import (
        ReviewVerdict,
        indicates_agent_fallback,
        parse_review_response,
    )
    from runtime_paths import (
        ensure_git_excludes,
        filter_runtime_paths,
        has_non_runtime_changes,
        runtime_dir_pathspecs,
    )
    from test_impact import TestImpact, select_tests

_COUNT_RE = re.compile(r"(?P<count>\d+)\s+(?P<label>passed|failed|skipped|xfailed|xpassed|errors?)", re.I)
# Different CLI wrappers key the model's actual reply differently:
# OpenCode2/Antigravity write {"response": "..."}; Grok Build's CLI writes
# {"text": "...", "stopReason": ..., "usage": {...}}. Compiled once here
# (matching this module's own _COUNT_RE convention) since _review_log_response
# runs once per candidate manifest in acceptance._find_review_manifest's loop.
_RESPONSE_FIELD_RE = re.compile(r'"response"\s*:\s*("(?:\\.|[^"\\])*")')
_TEXT_FIELD_RE = re.compile(r'"text"\s*:\s*("(?:\\.|[^"\\])*")')


@dataclass(frozen=True)
class GatePhase:
    name: str
    argv: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    resource: str = "light"

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "command": shlex.join(self.argv),
            "dependencies": list(self.dependencies),
            "resource": self.resource,
        }


def result_counts(output: str, *, returncode: int) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "errors": 0}
    for match in _COUNT_RE.finditer(output):
        label = match.group("label").lower()
        if label == "error":
            label = "errors"
        # Test runners may print both file and test summaries (Vitest) or
        # intermediate retry summaries. The terminal value for each category
        # is authoritative; summing those lines fabricates a larger test count.
        counts[label] = int(match.group("count"))
    if not any(counts.values()):
        counts["passed" if returncode == 0 else "failed"] = 1
    return counts


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def candidate(root: Path, base: str) -> tuple[str, str | None, list[str]]:
    # ENG-AGENT-16 (issue #146): this is the canonical candidate-tree
    # boundary. Control Plane runtime/evidence directories (.agent-output/,
    # .orchestrator-state/, .local-gate/) must never influence the returned
    # tree SHA or changed-path list, regardless of what a given (possibly
    # historical) branch's own tracked .gitignore does or does not know about
    # them -- so this never relies on `--exclude-standard` alone.
    ensure_git_excludes(root)
    # Self-heal an index that already picked up CP runtime writes (e.g. a
    # caller's own `git add -A` on a worktree whose tracked .gitignore
    # predates one or more of these directories) before staged content is
    # ever inspected: the boundary must not depend on every caller excluding
    # them correctly.
    _git(root, "reset", "--quiet", "--", *runtime_dir_pathspecs())
    unstaged = filter_runtime_paths(_git(root, "diff", "--name-only").splitlines())
    untracked = filter_runtime_paths(_git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    staged = _git(root, "diff", "--cached", "--name-only").splitlines()
    if unstaged or untracked:
        raise RuntimeError("local gate requires no unstaged/untracked candidate files; stage the complete candidate first")
    if staged:
        tree = _git(root, "write-tree")
        merge_base = _git(root, "merge-base", base, "HEAD")
        paths = filter_runtime_paths(_git(root, "diff", "--name-only", merge_base, tree).splitlines())
        return tree, None, paths
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    paths = filter_runtime_paths(_git(root, "diff", "--name-only", f"{base}...HEAD").splitlines())
    return tree, head, paths


def _review_log_response(review_path: Path, manifest: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(raw_log_text, extracted_response)``.

    ``raw_log_text`` is the complete, unmodified log content (used to detect
    an agent-preset fallback anywhere in the log, including CLI diagnostics
    printed outside any JSON payload); ``extracted_response`` is the model's
    actual reply text used for content parsing. Either may be ``None`` when
    the log cannot be located/read/parsed at all.
    """

    log_value = str((manifest.get("paths") or {}).get("log") or "")
    log_path = (review_path.parents[4] / log_value).resolve()
    run_root = review_path.parent.resolve()
    if not log_path.is_file() or not log_path.is_relative_to(run_root):
        return None, None
    try:
        log_text = log_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    # Redaction deliberately replaces token counts with bare placeholders,
    # which can make an otherwise structured provider log invalid JSON. Parse
    # only the JSON-escaped response string we need for review acceptance.
    plain_prefix = log_text.split("\x1b", 1)[0].strip()
    match = None
    if log_text.lstrip().startswith("{"):
        # Different CLI wrappers key the model's actual reply differently;
        # without trying both keys, every Grok Build review response was
        # silently unparseable here and could never satisfy this check
        # regardless of its actual content.
        match = _RESPONSE_FIELD_RE.search(log_text) or _TEXT_FIELD_RE.search(log_text)
    if match:
        try:
            return log_text, str(json.loads(match.group(1))).strip()
        except json.JSONDecodeError:
            return log_text, None
    # OpenCode writes the model response as plain text, followed by ANSI
    # transport diagnostics that may themselves contain reviewed source
    # shaped like JSON. Only examine the response prefix for content.
    return log_text, plain_prefix


def review_response_verdict(review_path: Path, manifest: dict[str, Any]) -> ReviewVerdict:
    """The full, reasoned verdict for one review manifest's logged response.

    Never raises; a manifest/log this cannot even read resolves to a
    ``ReviewVerdict(ready=False, ...)`` with a truthful reason, exactly like
    every other unready case.
    """

    raw_log_text, response = _review_log_response(review_path, manifest)
    if raw_log_text is None:
        return ReviewVerdict(False, "review log is missing, unreadable, or unparseable")
    # issue #148: a preset/agent-name fallback anywhere in the raw log (not
    # only within an extracted JSON "response" field -- CLI wrappers often
    # print this diagnostic outside any structured payload) must never be
    # accepted as a real review, however READY-shaped the extracted text
    # happens to look; check the complete raw log before content parsing.
    if indicates_agent_fallback(raw_log_text):
        return ReviewVerdict(False, "reviewer CLI fell back to a default agent (preset not found)")
    if response is None:
        return ReviewVerdict(False, "review response could not be extracted from the log")
    return parse_review_response(response)


def _review_response_is_ready(review_path: Path, manifest: dict[str, Any]) -> bool:
    """Require the review's written conclusion, not merely a successful CLI exit."""

    return review_response_verdict(review_path, manifest).ready


def _python_source_roots(root: Path | None) -> tuple[str, ...]:
    """Compile/lint only directories that exist in this checkout.

    Standalone Octarel has no OctaScene ``app/`` or ``workers/`` tree. Missing
    roots must not be passed to compileall/ruff or the local gate fails closed
    on a layout it does not own.
    """

    candidates = ("app", "tests", "workers", "scripts", "octarel")
    if root is None:
        return candidates
    present = tuple(name for name in candidates if (root / name).is_dir())
    return present or ("tests", "scripts")


def phase_plan(
    risk: ChangeRisk, impact: TestImpact, python: str, *, ports: dict[str, int] | None = None,
    root: Path | None = None,
) -> list[GatePhase]:
    """Build the one deterministic dependency DAG used by plan and execution."""

    ports = ports or {}
    py_roots = _python_source_roots(root)
    phases: list[GatePhase] = [GatePhase("tree-whitespace", ("git", "diff", "--check", "--cached"))]
    if risk.run_python:
        phases.extend(
            [
                GatePhase("python-compile", (python, "-m", "compileall", "-q", *py_roots), ("tree-whitespace",)),
                GatePhase("python-ruff", (python, "-m", "ruff", "check", *py_roots, "--select", "I,F401,E703"), ("tree-whitespace",)),
            ]
        )
    if impact.product_ui:
        phases.append(GatePhase("frontend-build", ("npm", "run", "build"), ("tree-whitespace",), "heavy"))
    if risk.run_python:
        selectors = () if impact.python_full else impact.python_selectors
        dependencies = ["python-compile", "python-ruff"]
        if impact.product_ui:
            dependencies.append("frontend-build")
        phases.append(GatePhase("python-tests", (python, "-m", "pytest", "-q", *selectors), tuple(dependencies), "heavy"))
    if impact.product_ui:
        argv = ["npm", "run", "test:v2"]
        if not impact.frontend_full and impact.frontend_selectors:
            selectors = [p.removeprefix("frontend/v2/") for p in impact.frontend_selectors]
            argv.extend(["--", *selectors])
        phases.append(GatePhase("frontend-tests", tuple(argv), ("frontend-build",), "heavy"))
    if risk.run_ui_audit and impact.product_ui:
        audit_command = "ui:audit:smoke" if risk.ui_audit_scope.endswith("focused") else "ui:audit"
        phases.append(
            GatePhase("ui-audit", ("env", f"OCTAGES_AUDIT_PORT={ports.get('product_ui', 0)}",
                      f"OCTAGES_AUDIT_PYTHON={python}", "npm", "run", audit_command),
                      ("frontend-build", "frontend-tests"), "heavy")
        )
    if impact.control_center_ui:
        phases.append(
            GatePhase(
                "control-center-ui-audit",
                # OCTAREL-TEST-01: the full responsive matrix runs through the isolated
                # bounded-parallel runner (own ports/fixtures/results per viewport lane).
                (python, "scripts/ci/playwright_matrix.py", "--python", python),
                ("tree-whitespace",), "heavy",
            )
        )
    return phases


def commands_for(risk: ChangeRisk, python: str, paths: list[str] | None = None,
                 *, root: Path | None = None, ports: dict[str, int] | None = None) -> list[tuple[str, list[str]]]:
    """Compatibility projection of the authoritative phase plan."""

    root = root or Path.cwd()
    impact = select_tests(root, risk, paths or []) if paths else TestImpact(
        risk.test_tier, python_full=risk.run_python, frontend_full=risk.run_frontend,
        product_ui=risk.run_frontend, control_center_ui=False,
    )
    return [(phase.name, list(phase.argv)) for phase in phase_plan(risk, impact, python, ports=ports, root=root)]


def product_ui_audit_required(paths: list[str]) -> bool:
    """Select the shipped-product matrix only for shipped-product UI changes."""

    return any(path.startswith(("frontend/v2/", "ui-audit/")) for path in paths)


def reviewer_health(root: Path, provider: str | None) -> dict[str, Any]:
    """Validate a configured read-only review route without sending source."""

    if not provider:
        return {"required": False, "status": "NOT_REQUIRED", "failures": []}
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.agents.registry import REASON_AVAILABLE, load_registry

        registry = load_registry(root / "scripts/agents/workers.json")
    except (OSError, ValueError, ImportError):
        return {"required": True, "status": "BLOCKED", "failures": ["worker registry is unreadable"]}
    matches = []
    for name, worker in registry.workers.items():
        if worker.provider.casefold() != provider.casefold() or "diff-review" not in worker.roles:
            continue
        reasons: list[str] = []
        if not worker.is_read_only:
            reasons.append("review route is not read-only")
        if worker.repository_data_authorization != "explicit-worker-selection":
            reasons.append("repository-data authorization is not explicit")
        if worker.allow_api_billing:
            reasons.append("review route permits API billing")
        availability = worker.availability_reason(probe=True)
        if availability != REASON_AVAILABLE:
            reasons.append(f"review route is unavailable: {availability}")
        if worker.execution_system == "OpenCode2" and "--agent" in worker.cli_template:
            agent_index = worker.cli_template.index("--agent") + 1
            agent = worker.cli_template[agent_index] if agent_index < len(worker.cli_template) else ""
            config = subprocess.run(
                [worker.cli_bin, "debug", "agent", agent], cwd=root, capture_output=True, text=True, check=False,
            )
            if not agent or config.returncode != 0:
                reasons.append("review adapter configuration is invalid")
            else:
                try:
                    configured_agent = json.loads(config.stdout)
                except json.JSONDecodeError:
                    reasons.append("review adapter configuration is not parseable")
                else:
                    if configured_agent.get("mode") not in {"primary", "all"}:
                        reasons.append("review adapter cannot be selected directly")
                    effective = {
                        item.get("permission"): item.get("action")
                        for item in configured_agent.get("permission", [])
                        if item.get("pattern") == "*"
                    }
                    if any(effective.get(permission) != "deny" for permission in ("edit", "bash", "task")):
                        reasons.append("review adapter does not deny edit, unmatched bash, and task delegation")
        # ENG-AGENT-13/14 finding: sorting by the path string (task-ref/worker/
        # run-id) assumes task-ref names happen to sort chronologically, which
        # is not guaranteed -- a differently-named but older task folder can
        # sort after a newer one alphabetically, picking a stale manifest as
        # "most recent" and reporting a healthy route unhealthy (or vice
        # versa) based on naming rather than actual recency.
        recent = sorted(
            (root / ".agent-output").glob(f"*/{name}/*/manifest.json"),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        if recent:
            try:
                recent_manifest = json.loads(recent[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reasons.append("latest review health evidence is unreadable")
            else:
                result_healthy = recent_manifest.get("result") == "PASS"
                if result_healthy and recent_manifest.get("role") == "diff-review":
                    result_healthy = _review_response_is_ready(recent[0], recent_manifest)
                if not result_healthy:
                    finished_raw = str(recent_manifest.get("finished_at") or "")
                    try:
                        finished = dt.datetime.fromisoformat(finished_raw).timestamp()
                    except ValueError:
                        finished = float("inf")
                    adapter_paths = [root / worker.provider_policy]
                    if worker.execution_system == "OpenCode2" and agent:
                        adapter_paths.append(root / ".opencode" / "agents" / f"{agent}.md")
                    adapter_repaired = any(path.is_file() and path.stat().st_mtime > finished for path in adapter_paths)
                    if not adapter_repaired:
                        reasons.append("latest review transport/schema result failed")
        matches.append({"worker": name, "failures": reasons})
    healthy = next((item for item in matches if not item["failures"]), None)
    failures = [] if healthy else (["no configured usable read-only review route for named provider"] if not matches else matches[0]["failures"])
    return {
        "required": True,
        "status": "READY" if healthy else "BLOCKED",
        "worker": healthy["worker"] if healthy else None,
        "failures": failures,
        "probe_mode": "local-registry-and-cli-only",
        "repository_source_transmitted": False,
        "result_contract": "READY-only-no-findings",
    }


def preflight(root: Path, risk: ChangeRisk, paths: list[str], python: str,
              *, impact: TestImpact | None = None, phases: list[GatePhase] | None = None,
              ports: dict[str, int] | None = None, review_provider: str | None = None,
              registry_root: Path | None = None) -> dict[str, Any]:
    """Cheaply prove the selected exact-tree gate can launch before running it.

    ``registry_root`` is the canonical Control Plane checkout used to resolve
    the worker registry (``scripts/agents/workers.json``); it defaults to
    ``root`` for direct/CLI callers where the candidate *is* the canonical
    checkout. A candidate worktree acceptance-tested from an older branch --
    e.g. a product branch that predates the ``scripts/agents/control_plane``
    package entirely -- never carries that orchestration infrastructure in
    its own tree, so the registry must always resolve against the canonical
    checkout rather than the candidate (ENG-AGENT-18, issue #150).
    """

    failures: list[str] = []
    impact = impact or select_tests(root, risk, paths)
    require_ffmpeg = any(p.startswith(("app/domains/rendering/", "app/domains/exports/", "app/domains/video_editor/")) for p in paths)
    require_frontend = impact.product_ui or impact.control_center_ui
    require_browser = risk.run_ui_audit or impact.control_center_ui
    bootstrap_result = None
    if (require_frontend or require_browser) and (root / "package-lock.json").is_file():
        # ENG-AGENT-18 (issue #150): self-heal the candidate's own Node/
        # Playwright dependencies via the existing fingerprint-aware
        # reuse-or-install path (ENG-AGENT-14, issue #140) before checking
        # readiness -- a worktree whose node_modules is stale or was never
        # installed for its own current package-lock.json (common for a
        # long-lived branch that predates a dependency bump) is repaired
        # here, reusing the npm cache and the shared Playwright browser
        # cache and installing only what is genuinely missing, instead of
        # being reported permanently BLOCKED. Guarded on an actual
        # package-lock.json existing so a non-Node candidate tree (or a
        # unit test's bare tmp_path) never attempts an install/download.
        bootstrap_result = bootstrap_dependencies(root, require_frontend=require_frontend, require_browser=require_browser)
    readiness = prepare_worktree(
        root, require_python=risk.run_python, require_frontend=require_frontend,
        require_browser=require_browser, require_ffmpeg=require_ffmpeg,
    )
    if bootstrap_result is not None:
        readiness["bootstrap"] = bootstrap_result
    failures.extend(readiness["failures"])
    if risk.run_ui_audit and risk.ui_audit_scope == "none":
        failures.append("UI audit was selected without a path-relevant audit scope")
    allocated = ports or {}
    if len(set(allocated.values())) != len(allocated):
        failures.append("selected isolated ports are not distinct")
    registry_root = registry_root or root
    review = reviewer_health(registry_root, review_provider) if risk.review_level in {"high", "critical"} else reviewer_health(registry_root, None)
    failures.extend(review["failures"])
    phases = phases or phase_plan(risk, impact, python, ports=allocated, root=root)
    names = {phase.name for phase in phases}
    for phase in phases:
        missing = set(phase.dependencies) - names
        if missing:
            failures.append(f"phase {phase.name} has missing dependencies: {', '.join(sorted(missing))}")
        command_index = 0
        if phase.argv and phase.argv[0] == "env":
            command_index = next((i for i, value in enumerate(phase.argv[1:], start=1) if "=" not in value), len(phase.argv))
        if len(phase.argv) <= command_index or not shutil.which(phase.argv[command_index]) and not Path(phase.argv[command_index]).is_file():
            failures.append(f"phase {phase.name} command is unavailable: {phase.argv[command_index] if phase.argv else 'empty'}")
    return {
        "result": "pass" if not failures else "fail",
        "failures": failures,
        "python": python,
        "environment": readiness,
        "ui_audit_scope": risk.ui_audit_scope,
        "port_allocations": allocated,
        "reviewer_health": review,
        "test_impact": impact.as_dict(),
        "phase_plan": [phase.as_dict() for phase in phases],
        "build_precedes_consumers": all("frontend-build" in phase.dependencies for phase in phases if phase.name in {"frontend-tests", "ui-audit"}),
        "changed_paths": paths,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _phase_inputs(root: Path, phase: GatePhase, changed_paths: list[str]) -> list[str]:
    tracked = _git(root, "ls-files").splitlines()
    if phase.name == "tree-whitespace":
        return sorted(changed_paths)
    if phase.name in {"python-compile", "python-ruff"}:
        return [p for p in tracked if p.startswith(("app/", "tests/", "workers/", "scripts/")) or p in {"pyproject.toml", "requirements.txt", "requirements-dev.txt"}]
    if phase.name == "python-tests":
        selectors = {value for value in phase.argv if value.startswith("tests/")}
        return [p for p in tracked if p.startswith(("app/", "workers/")) or p in selectors or (not selectors and p.startswith(("tests/", "scripts/"))) or p in {"pyproject.toml", "requirements.txt", "requirements-dev.txt"}]
    if phase.name in {"frontend-build", "frontend-tests"}:
        return [p for p in tracked if p.startswith("frontend/v2/") or p in {"package.json", "package-lock.json"}]
    if phase.name == "ui-audit":
        return [p for p in tracked if p.startswith(("frontend/v2/", "ui-audit/")) or p in {"package.json", "package-lock.json"}]
    if phase.name == "control-center-ui-audit":
        return [p for p in tracked if p.startswith("scripts/agents/control_plane/")
                or p in {"package.json", "package-lock.json", "scripts/ci/playwright_matrix.py", "scripts/ci/ports.py"}]
    return tracked


def _phase_fingerprint(root: Path, phase: GatePhase, changed_paths: list[str], environment: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(shlex.join(phase.argv).encode())
    digest.update(str(environment.get("fingerprint", "UNKNOWN")).encode())
    for rel in sorted(_phase_inputs(root, phase, changed_paths)):
        path = root / rel
        if path.is_file():
            digest.update(rel.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _prior_checks(root: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path in sorted((root / ".local-gate").glob("**/*.json"), reverse=True):
        if path.name == "latest.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for check in record.get("checks", []):
            if check.get("result") == "pass":
                checks.append({**check, "source_tree_sha": record.get("tree_sha"), "source_evidence": str(path.relative_to(root))})
    return checks


def _estimate_phases(root: Path, phases: list[GatePhase]) -> dict[str, float | None]:
    samples: dict[str, list[float]] = {phase.name: [] for phase in phases}
    for check in _prior_checks(root):
        if check.get("name") in samples and isinstance(check.get("duration_seconds"), (int, float)):
            samples[check["name"]].append(float(check["duration_seconds"]))
    return {name: round(sum(values) / len(values), 3) if values else None for name, values in samples.items()}


# Set by the launching Control Plane process for its own canonical-truth
# resolution (ENG-AGENT-12, ``scripts/agents/runner.ENV_CANONICAL_REPO_ROOT``)
# -- not imported by name here to keep this module runnable as a standalone
# script. Every phase subprocess is already scoped to the exact candidate via
# ``cwd=root``; leaking the launching process's own canonical-root override
# into it breaks that binding without changing the candidate at all (issue
# #161: it forced ``_resolved_canonical_repo_root``'s ``configured`` branch
# during an unrelated ``pytest -q`` run, and 8 orchestrator CLI tests whose
# ``repo_root`` stubs don't accept that call's positional argument failed --
# not because the candidate tree had a real defect, but because the gate
# wasn't hermetic against the operator's own daemon environment).
_ORCHESTRATION_ONLY_ENV_VARS = ("OCTAGES_ORCH_CANONICAL_REPO_ROOT",)


def _run_phase(root: Path, logs: Path, index: int, phase: GatePhase, fingerprint: str) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    env = {key: value for key, value in os.environ.items() if key not in _ORCHESTRATION_ONLY_ENV_VARS}
    result = subprocess.run(list(phase.argv), cwd=root, capture_output=True, text=True, check=False, env=env)
    log_path = logs / f"{index:02d}-{phase.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8", errors="replace")
    return {
        "name": phase.name, "argv": list(phase.argv), "command": shlex.join(phase.argv),
        "dependencies": list(phase.dependencies), "resource": phase.resource,
        "exit_code": result.returncode, "result": "pass" if result.returncode == 0 else "fail",
        "counts": result_counts(result.stdout + result.stderr, returncode=result.returncode),
        "duration_seconds": round((dt.datetime.now(dt.timezone.utc) - started).total_seconds(), 3),
        "log": str(log_path.relative_to(root)), "input_fingerprint": fingerprint,
        "evidence_action": "executed",
    }


def execute_plan(root: Path, logs: Path, phases: list[GatePhase], changed_paths: list[str],
                 environment: dict[str, Any], *, reuse_evidence: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute dependency waves; only independent light phases overlap."""

    prior = _prior_checks(root) if reuse_evidence else []
    checks: dict[str, dict[str, Any]] = {}
    invalidated: list[dict[str, Any]] = []
    pending = {phase.name: phase for phase in phases}
    order = {phase.name: index for index, phase in enumerate(phases, start=1)}
    while pending:
        ready = [phase for phase in pending.values() if all(dep in checks and checks[dep]["result"] == "pass" for dep in phase.dependencies)]
        if not ready:
            break
        reusable: list[tuple[GatePhase, dict[str, Any]]] = []
        runnable: list[tuple[GatePhase, str]] = []
        for phase in ready:
            fingerprint = _phase_fingerprint(root, phase, changed_paths, environment)
            match = next((item for item in prior if item.get("name") == phase.name and item.get("command") == shlex.join(phase.argv)
                          and item.get("input_fingerprint") == fingerprint), None)
            if match:
                source_log = root / str(match.get("log", ""))
                if not source_log.is_file():
                    match = None
                else:
                    output = source_log.read_text(encoding="utf-8", errors="replace")
                    match = {
                        **match,
                        "counts": result_counts(output, returncode=int(match.get("exit_code", 1))),
                    }
            if match:
                reusable.append((phase, match))
            else:
                if reuse_evidence:
                    invalidated.append({"phase": phase.name, "reason": "command, environment, or relevant inputs changed"})
                runnable.append((phase, fingerprint))
        for phase, match in reusable:
            checks[phase.name] = {**match, "evidence_action": "reused", "reuse_reason": "identical command, environment, and relevant-input fingerprint"}
            pending.pop(phase.name)
        light = [item for item in runnable if item[0].resource == "light"]
        heavy = [item for item in runnable if item[0].resource != "light"]
        batch = light if light else heavy[:1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(batch)) or 1) as pool:
            futures = {pool.submit(_run_phase, root, logs, order[phase.name], phase, fp): phase for phase, fp in batch}
            for future, phase in [(f, futures[f]) for f in futures]:
                checks[phase.name] = future.result()
                pending.pop(phase.name)
        if any(checks[phase.name]["result"] != "pass" for phase, _ in batch):
            break
    return [checks[p.name] for p in phases if p.name in checks], invalidated


def run_gate(*, root: Path, base: str, docs_reviewed: bool, review_provider: str | None,
             review_evidence: str | None = None,
             force_full: bool = False, dry_run: bool = False,
             reuse_evidence: bool = True, rerun_reason: str | None = None,
             registry_root: Path | None = None) -> dict[str, Any]:
    tree, head, paths = candidate(root, base)
    risk = classify(paths, force_full=force_full)
    impact = select_tests(root, risk, paths)
    if not (root / "frontend" / "v2").is_dir():
        impact = TestImpact(
            tier=impact.tier,
            python_selectors=impact.python_selectors,
            frontend_selectors=(),
            python_full=impact.python_full,
            frontend_full=False,
            product_ui=False,
            control_center_ui=impact.control_center_ui
            or any(path.startswith("scripts/agents/control_plane/dashboard/") for path in paths),
            groups=impact.groups,
            reason=impact.reason,
        )
    out = root / ".local-gate"
    logs = out / "logs" / tree
    logs.mkdir(parents=True, exist_ok=True)
    prerequisites: list[str] = []
    if not docs_reviewed:
        prerequisites.append("documentation reconciliation was not attested")
    if risk.review_level in {"high", "critical"} and not review_provider:
        prerequisites.append("high-risk candidate lacks independent provider review")
    if risk.review_level in {"high", "critical"} and not review_evidence and not dry_run:
        prerequisites.append("high-risk candidate lacks independent review evidence path")
    if review_evidence:
        review_path = (root / review_evidence).resolve()
        evidence_root = (root / ".agent-output").resolve()
        if not review_path.is_file() or not review_path.is_relative_to(evidence_root):
            prerequisites.append("independent review evidence must be an existing manifest under .agent-output/")
        elif review_path.name != "manifest.json":
            # ENG-AGENT-14 (issue #140): a second, additive evidence shape for a
            # review performed against an already-committed candidate (no
            # uncommitted diff for --include-diff to bind) -- see
            # scripts/ci/review_attestation.py. The existing manifest.json /
            # candidate_tree_sha path below is completely unchanged for the
            # original uncommitted-candidate flow.
            try:
                from scripts.ci.review_attestation import validate_attestation
            except ModuleNotFoundError:  # pragma: no cover - direct script entry point
                from review_attestation import validate_attestation  # type: ignore

            ready, reason = validate_attestation(
                root, review_path, base=base, review_provider=review_provider, expected_repository=None,
            )
            if not ready:
                prerequisites.append(reason)
        else:
            try:
                review_manifest = json.loads(review_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                review_manifest = {}
            actual_provider = str((review_manifest.get("actual") or {}).get("provider") or "")
            if (
                review_manifest.get("role") != "diff-review"
                or review_manifest.get("result") != "PASS"
                or actual_provider.casefold() != str(review_provider or "").casefold()
                or review_manifest.get("files_changed")
                or review_manifest.get("candidate_tree_sha") != tree
                or not _review_response_is_ready(review_path, review_manifest)
            ):
                prerequisites.append(
                    "review manifest must prove an exact-tree passing read-only diff-review with a READY conclusion "
                    "by the named provider"
                )
    if review_provider and review_provider.lower() in {"openai", "codex", "anthropic", "claude"}:
        # ENG-AGENT-14 (issue #140): an implementer can never be its own
        # independent reviewer -- Claude/Anthropic is rejected here exactly
        # like Codex already was, for both evidence shapes above.
        prerequisites.append("independent review provider must be non-Codex and non-Claude/Anthropic")

    merge_base = _git(root, "merge-base", base, "HEAD")
    python = resolve_python(root)
    started_gate = dt.datetime.now(dt.timezone.utc)
    with PortLeases(root) as leases:
        allocations: dict[str, int] = {}
        if impact.product_ui and risk.run_ui_audit:
            allocations["product_ui"] = leases.allocate("product-ui-audit")
        phases = phase_plan(risk, impact, python, ports=allocations, root=root)
        phases[0] = GatePhase("tree-whitespace", ("git", "diff", "--check", merge_base, tree))
        preflight_result = preflight(
            root, risk, paths, python, impact=impact, phases=phases, ports=allocations,
            review_provider=review_provider if risk.review_level in {"high", "critical"} else None,
            registry_root=registry_root,
        )
        prerequisites.extend(preflight_result["failures"])
        estimates = _estimate_phases(root, phases)
        if dry_run or prerequisites:
            checks: list[dict[str, Any]] = []
            invalidated: list[dict[str, Any]] = []
        else:
            checks, invalidated = execute_plan(
                root, logs, phases, paths, preflight_result["environment"], reuse_evidence=reuse_evidence,
            )
    try:
        final_tree, _, _ = candidate(root, base)
        if final_tree != tree:
            prerequisites.append("candidate tree changed while the local gate was running")
    except RuntimeError as exc:
        prerequisites.append(f"candidate changed while the local gate was running: {exc}")
    all_phases_proven = len(checks) == len(phases) and all(item["result"] == "pass" for item in checks)
    passed = not dry_run and not prerequisites and all_phases_proven
    prior_records = len(list((out / "runs").glob("*.json")))
    evidence = {
        "schema_version": 2,
        "authority": "local-deterministic-gate",
        "result": "pass" if passed else ("planned" if dry_run and not prerequisites else "fail"),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base": base,
        "head_sha": head,
        "tree_sha": tree,
        "changed_paths": paths,
        "classification": risk.classification,
        "selection": {
            **risk.outputs(),
            "run_product_ui_audit": str(product_ui_audit_required(paths)).lower(),
            "run_control_center_ui_audit": str(impact.control_center_ui).lower(),
            "test_impact": impact.as_dict(),
        },
        "phase_dag": [phase.as_dict() for phase in phases],
        "estimated_phase_seconds": estimates,
        "estimate_label": "ESTIMATED" if any(value is not None for value in estimates.values()) else "UNKNOWN",
        "documentation_reconciled": docs_reviewed,
        "independent_review_provider": review_provider,
        "independent_review_evidence": review_evidence,
        "prerequisite_failures": prerequisites,
        "preflight": preflight_result,
        "checks": checks,
        "evidence_reuse": {
            "enabled": reuse_evidence,
            "reused_phases": [item["name"] for item in checks if item.get("evidence_action") == "reused"],
            "executed_phases": [item["name"] for item in checks if item.get("evidence_action") == "executed"],
            "invalidated": invalidated,
        },
        "throughput": {
            "gate_duration_seconds": round((dt.datetime.now(dt.timezone.utc) - started_gate).total_seconds(), 3),
            "rerun_count": prior_records,
            "rerun_reason": rerun_reason or ("initial gate" if prior_records == 0 else "not supplied"),
            "waiting_seconds": "UNKNOWN",
            "premium_model_active_seconds": "UNKNOWN",
            "premium_model_active_ratio": "UNKNOWN",
        },
    }
    if dry_run:
        return {**evidence, "evidence_path": None}
    evidence_path = out / f"{tree}.json"
    _write_json(evidence_path, evidence)
    run_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    _write_json(out / "runs" / f"{run_stamp}-{tree}.json", evidence)
    _write_json(out / "latest.json", {**evidence, "evidence_path": str(evidence_path.relative_to(root))})
    return {**evidence, "evidence_path": str(evidence_path.relative_to(root))}


def read_latest_gate(root: Path, *, expected_tree: str | None = None) -> dict[str, Any]:
    path = root / ".local-gate" / "latest.json"
    if not path.is_file():
        return {"ready": False, "reason": "no local-gate evidence", "evidence": None}
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ready": False, "reason": "local-gate evidence is unreadable", "evidence": None}
    if expected_tree is None:
        if has_non_runtime_changes(root):
            return {"ready": False, "reason": "working tree has uncommitted changes", "evidence": evidence}
    tree = expected_tree or _git(root, "rev-parse", "HEAD^{tree}")
    if evidence.get("result") != "pass":
        return {"ready": False, "reason": "latest local gate did not pass", "evidence": evidence}
    if evidence.get("tree_sha") != tree:
        return {"ready": False, "reason": "local-gate evidence is stale for the current tree", "evidence": evidence}
    evidence = dict(evidence)
    evidence["current_head_sha"] = _git(root, "rev-parse", "HEAD")
    return {"ready": True, "reason": "exact-tree local gate passed", "evidence": evidence}


def read_gate_history(root: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    """Read immutable local performance evidence; never launches a provider."""

    records: list[dict[str, Any]] = []
    run_paths = list((root / ".local-gate" / "runs").glob("*.json"))
    for path in run_paths:
        if path.name == "latest.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records.append({
            "tree_sha": record.get("tree_sha"), "recorded_at": record.get("recorded_at"),
            "result": record.get("result"), "classification": record.get("classification"),
            "test_tier": (record.get("selection") or {}).get("test_tier"),
            "duration_seconds": (record.get("throughput") or {}).get("gate_duration_seconds", "UNKNOWN"),
            "rerun_reason": (record.get("throughput") or {}).get("rerun_reason", "UNKNOWN"),
            "reused_phases": (record.get("evidence_reuse") or {}).get("reused_phases", []),
        })
    records.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return records[: max(1, limit)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--docs-reviewed", action="store_true")
    parser.add_argument("--independent-review-provider")
    parser.add_argument("--independent-review-evidence")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="preflight and print the resolved phase plan without running it")
    parser.add_argument("--no-reuse-evidence", action="store_true", help="rerun every selected phase")
    parser.add_argument("--rerun-reason", help="truthful operator reason recorded for a repeated gate")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    if args.status:
        result = read_latest_gate(root)
    else:
        try:
            result = run_gate(
                root=root, base=args.base, docs_reviewed=args.docs_reviewed,
                review_provider=args.independent_review_provider,
                review_evidence=args.independent_review_evidence,
                force_full=args.force_full,
                dry_run=args.dry_run,
                reuse_evidence=not args.no_reuse_evidence,
                rerun_reason=args.rerun_reason,
            )
        except (OSError, RuntimeError) as exc:
            result = {"result": "fail", "error": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ready", result.get("result") in {"pass", "planned"}) else 1


if __name__ == "__main__":
    raise SystemExit(main())
