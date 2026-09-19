#!/usr/bin/env python3
"""Classify a changed-file set for Octarel's deterministic local gate.

This deliberately routes conservatively: only known non-executable documents
avoid application checks.  Every Python, shared-contract, test, dependency,
build, or workflow change remains in the full deterministic Python path.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

DOCUMENT_ROOT_FILES = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "V1_CHECKLIST.md",
}
DOCUMENT_TEMPLATE_FILES = {".github/pull_request_template.md"}
DOCUMENT_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
POLICY_DOCUMENT_PREFIXES = (".agents/", ".claude/skills/", ".opencode/", ".gpt-work/")
UI_PREFIXES = ("frontend/v2/", "ui-audit/", "scripts/agents/control_plane/dashboard/")
UI_FILES = {"package.json", "package-lock.json"}
SHARED_UI_PREFIXES = (
    "frontend/v2/src/app/",
    "frontend/v2/src/components/",
    "frontend/v2/src/styles/",
    "ui-audit/",
)
T3_RISK_PREFIXES = (
    ".github/workflows/",
    "scripts/ci/",
    "tests/",
    "app/core/",
    "app/adapters/video_editor/interchange/",
    "app/domains/video_editor/commands/",
)
T3_RISK_FILES = {
    ".github/dependabot.yml",
    "Makefile",
    "app/schemas.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-packaging.txt",
}


@dataclass(frozen=True)
class ChangeRisk:
    classification: str
    docs_only: bool
    run_python: bool
    run_frontend: bool
    run_ui_audit: bool
    test_tier: str = "T3"
    review_level: str = "high"
    ui_audit_scope: str = "none"

    def outputs(self) -> dict[str, str]:
        return {
            "classification": self.classification,
            "docs_only": str(self.docs_only).lower(),
            "run_python": str(self.run_python).lower(),
            "run_frontend": str(self.run_frontend).lower(),
            "run_ui_audit": str(self.run_ui_audit).lower(),
            "test_tier": self.test_tier,
            "review_level": self.review_level,
            "ui_audit_scope": self.ui_audit_scope,
        }


def _normalise(paths: Iterable[str]) -> tuple[str, ...]:
    normalised = []
    for path in paths:
        path = path.strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path:
            normalised.append(path)
    return tuple(normalised)


def is_documentation(path: str) -> bool:
    """Return true only for maintained prose that cannot be test input/code."""
    if path in DOCUMENT_ROOT_FILES or path in DOCUMENT_TEMPLATE_FILES:
        return True
    if path.startswith(POLICY_DOCUMENT_PREFIXES) and PurePosixPath(path).suffix.lower() in DOCUMENT_SUFFIXES:
        return True
    return path.startswith("docs/") and PurePosixPath(path).suffix.lower() in DOCUMENT_SUFFIXES


def is_ui_change(path: str) -> bool:
    return path.startswith(UI_PREFIXES) or path in UI_FILES or path == ".github/workflows/ui-audit.yml"


def ui_scope(paths: Iterable[str], *, full: bool = False) -> str:
    changed = _normalise(paths)
    product = any(path.startswith(("frontend/v2/", "ui-audit/")) or path in UI_FILES for path in changed)
    control = any(path.startswith("scripts/agents/control_plane/dashboard/") for path in changed)
    if not product and not control:
        return "none"
    suffix = "full" if full else "focused"
    if product and control:
        return f"both-{suffix}"
    return f"{'product' if product else 'control-center'}-{suffix}"


def requires_release_artifacts(paths: Iterable[str], *, explicit_release: bool = False) -> bool:
    changed = _normalise(paths)
    return explicit_release or "VERSION" in changed


def is_high_risk(path: str) -> bool:
    return (
        path.startswith(T3_RISK_PREFIXES)
        or path in T3_RISK_FILES
        or path.startswith("requirements-")
        or path.endswith(("package.json", "package-lock.json"))
    )


def classify(paths: Iterable[str], *, force_full: bool = False) -> ChangeRisk:
    """Classify changed paths; unknown or empty input is intentionally conservative."""
    changed = _normalise(paths)
    if force_full or not changed:
        return ChangeRisk("high", False, True, True, False, "T3", "critical", "none")

    if all(is_documentation(path) for path in changed):
        return ChangeRisk("docs", True, False, False, False, "T0", "low", "none")

    ui_changed = any(is_ui_change(path) for path in changed)
    high_risk = any(
        (is_high_risk(path) and (not is_ui_change(path) or PurePosixPath(path).name in {"package.json", "package-lock.json"}))
        or (PurePosixPath(path).suffix.lower() in DOCUMENT_SUFFIXES and not is_documentation(path))
        for path in changed
    )
    non_ui_executable = any(not is_documentation(path) and not is_ui_change(path) for path in changed)

    shared_ui = any(path.startswith(SHARED_UI_PREFIXES) for path in changed)
    if high_risk:
        return ChangeRisk("high", False, True, True, ui_changed, "T3", "critical", ui_scope(changed, full=True))
    if ui_changed and non_ui_executable:
        return ChangeRisk("mixed", False, True, True, True, "T2", "high", ui_scope(changed, full=shared_ui))
    if ui_changed:
        tier = "T2" if shared_ui else "T1"
        return ChangeRisk("frontend", False, False, True, True, tier, "high" if shared_ui else "medium", ui_scope(changed, full=shared_ui))
    python_paths = [path for path in changed if path.endswith(".py")]
    domains = {
        PurePosixPath(path).parts[2]
        for path in python_paths
        if len(PurePosixPath(path).parts) >= 3 and PurePosixPath(path).parts[:2] == ("app", "domains")
    }
    known_subsystem = any(path.startswith(("app/api/", "app/adapters/", "app/domains/video_editor/")) for path in changed)
    if known_subsystem or len(domains) > 1:
        return ChangeRisk("backend", False, True, False, False, "T2", "high", "none")
    if python_paths and (domains or all(path.startswith("app/") for path in python_paths)):
        return ChangeRisk("backend", False, True, False, False, "T1", "medium", "none")
    # Executable layouts not explicitly owned above must never receive a
    # zero/under-scoped gate merely because they avoided a known prefix.
    return ChangeRisk("high", False, True, True, False, "T3", "critical", "none")


def required_jobs(risk: ChangeRisk) -> tuple[str, ...]:
    jobs = ["classify", "documentation"]
    if risk.run_python:
        jobs.append("python")
    if risk.run_frontend:
        jobs.append("frontend")
    if risk.run_ui_audit:
        jobs.append("ui_audit")
    return tuple(jobs)


def required_jobs_succeeded(risk: ChangeRisk, results: Mapping[str, str]) -> bool:
    return all(results.get(job) == "success" for job in required_jobs(risk))


def _read_paths(path_file: str | None, paths: list[str]) -> list[str]:
    if path_file:
        paths.extend(Path(path_file).read_text(encoding="utf-8").splitlines())
    return paths


def _write_outputs(outputs: Mapping[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as stream:
            for key, value in outputs.items():
                stream.write(f"{key}={value}\n")
    else:
        for key, value in outputs.items():
            print(f"{key}={value}")


def _risk_from_outputs() -> ChangeRisk:
    return ChangeRisk(
        classification=os.environ["CLASSIFICATION"],
        docs_only=os.environ["DOCS_ONLY"] == "true",
        run_python=os.environ["RUN_PYTHON"] == "true",
        run_frontend=os.environ["RUN_FRONTEND"] == "true",
        run_ui_audit=os.environ["RUN_UI_AUDIT"] == "true",
        test_tier=os.environ.get("TEST_TIER", "T3"),
        review_level=os.environ.get("REVIEW_LEVEL", "high"),
        ui_audit_scope=os.environ.get("UI_AUDIT_SCOPE", "none"),
    )


def _check_job_results() -> int:
    risk = _risk_from_outputs()
    results = {job: os.environ.get(f"RESULT_{job.upper()}", "") for job in required_jobs(risk)}
    missing = [job for job in required_jobs(risk) if results[job] != "success"]
    if missing:
        print(f"Required CI jobs did not succeed for {risk.classification}: {', '.join(missing)}")
        return 1
    print(f"Required CI jobs succeeded for {risk.classification}: {', '.join(required_jobs(risk))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths-file")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--check-job-results", action="store_true")
    args = parser.parse_args()

    if args.check_job_results:
        return _check_job_results()

    risk = classify(_read_paths(args.paths_file, args.path), force_full=args.force_full)
    _write_outputs(risk.outputs())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
