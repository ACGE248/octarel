"""Deterministic executable-test selection for the authoritative local gate.

``change_risk`` remains the risk authority.  This module translates its
T0/T1/T2/T3 decision into concrete, reviewable selectors.  Unknown executable
paths fail closed to T3 rather than producing an empty test plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

try:
    from .change_risk import ChangeRisk
except ImportError:  # direct local_gate.py entry point
    from change_risk import ChangeRisk

PYTHON_ANCHORS = ("tests/test_smoke.py", "tests/test_architecture.py")
CONTROL_CENTER_PREFIXES = (
    "scripts/agents/control_plane/dashboard/",
    "scripts/agents/control_plane/dashboard-tests/",
    "scripts/agents/control_plane/playwright.config.js",
)
PRODUCT_FRONTEND_PREFIX = "frontend/v2/"

# Explicit aliases cover domains whose test names intentionally use product
# vocabulary instead of mirroring their package name.  Generic prefix matching
# below handles the majority of domains without a fragile file-to-file table.
DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "providers": ("provider", "catalog", "model"),
    "projects": ("project",),
    "generation": ("generation", "job_generation"),
    "spend_safety": ("spend_safety", "connections_spend_safety"),
    "video_editor": ("video_editor/", "api_video_editor"),
    "timeline": ("timeline",),
    "intelligence": ("intelligence",),
}

T3_PREFIXES = (
    "scripts/ci/",
    "tests/",
    "scripts/agents/",
    "octarel/",
)
T3_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "package-lock.json",
}


@dataclass(frozen=True)
class TestImpact:
    tier: str
    python_selectors: tuple[str, ...] = ()
    frontend_selectors: tuple[str, ...] = ()
    python_full: bool = False
    frontend_full: bool = False
    product_ui: bool = False
    control_center_ui: bool = False
    groups: tuple[str, ...] = ()
    reason: str = ""

    @property
    def concrete_test_count(self) -> int | None:
        if self.python_full or self.frontend_full:
            return None
        return len(self.python_selectors) + len(self.frontend_selectors)

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "python_selectors": list(self.python_selectors),
            "frontend_selectors": list(self.frontend_selectors),
            "python_full": self.python_full,
            "frontend_full": self.frontend_full,
            "product_ui": self.product_ui,
            "control_center_ui": self.control_center_ui,
            "groups": list(self.groups),
            "concrete_test_count": self.concrete_test_count,
            "reason": self.reason,
        }


def _normalise(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(p.strip().replace("\\", "/").removeprefix("./") for p in paths if p.strip())


def _existing(root: Path, candidates: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({path for path in candidates if (root / path).is_file()}))


def _python_domain(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[:2] == ("app", "domains"):
        return parts[2]
    return None


def _domain_tests(root: Path, domain: str) -> tuple[str, ...]:
    aliases = DOMAIN_ALIASES.get(domain, (domain,))
    matches: list[str] = []
    for path in (root / "tests").rglob("test_*.py"):
        rel = path.relative_to(root).as_posix()
        searchable = rel.removeprefix("tests/test_")
        if any(alias in searchable for alias in aliases):
            matches.append(rel)
    if not matches:
        return ()
    return _existing(root, (*matches, *PYTHON_ANCHORS))


def _frontend_tests(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    candidates: set[str] = set()
    for raw in paths:
        path = root / raw
        if not raw.startswith(PRODUCT_FRONTEND_PREFIX) or path.suffix not in {".ts", ".tsx"}:
            continue
        if ".test." in path.name or ".spec." in path.name:
            candidates.add(raw)
            continue
        for suffix in (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"):
            candidate = path.with_name(path.stem + suffix)
            if candidate.is_file():
                candidates.add(candidate.relative_to(root).as_posix())
        parent = path.parent
        if not candidates and parent.is_dir():
            candidates.update(p.relative_to(root).as_posix() for p in parent.glob("*.test.*") if p.is_file())
    anchor = root / "frontend/v2/src/app.test.tsx"
    if candidates and anchor.is_file():
        candidates.add(anchor.relative_to(root).as_posix())
    return tuple(sorted(candidates))


def select_tests(root: Path, risk: ChangeRisk, paths: Iterable[str]) -> TestImpact:
    """Translate a risk tier into concrete applicable test selectors."""

    changed = _normalise(paths)
    shared_node = any(p in {"package.json", "package-lock.json", "frontend/v2/package-lock.json"} for p in changed)
    product_ui = risk.run_frontend and (risk.test_tier == "T3" or shared_node or any(p.startswith((PRODUCT_FRONTEND_PREFIX, "ui-audit/")) for p in changed))
    control_ui = shared_node or any(p.startswith(CONTROL_CENTER_PREFIXES) for p in changed)

    if risk.test_tier == "T0":
        return TestImpact("T0", product_ui=product_ui, control_center_ui=control_ui, reason="static-only change")

    if risk.test_tier == "T3" or any(p.startswith(T3_PREFIXES) or p in T3_FILES for p in changed):
        return TestImpact(
            "T3", python_full=risk.run_python, frontend_full=risk.run_frontend and product_ui,
            product_ui=product_ui, control_center_ui=control_ui, groups=("complete-applicable-suites",),
            reason="full validation required by risk authority or cross-cutting path",
        )

    domains = {domain for p in changed if (domain := _python_domain(p))}
    python_selectors: tuple[str, ...] = ()
    groups: list[str] = []
    if risk.run_python:
        if not domains:
            # Known API/adapter changes are subsystem work; other executable
            # layouts are not assumed safe and therefore fail closed.
            if all(p.startswith(("app/api/", "app/adapters/")) for p in changed if p.endswith(".py")):
                risk_tier = "T2"
                stems = {PurePosixPath(p).stem.removesuffix("_routes") for p in changed if p.endswith(".py")}
                selectors = [
                    p.relative_to(root).as_posix() for p in (root / "tests").glob("test_*.py")
                    if any(stem in p.stem for stem in stems) or "api" in p.stem
                ]
                if not selectors:
                    return TestImpact(
                        "T3", python_full=True, frontend_full=risk.run_frontend and product_ui,
                        product_ui=product_ui, control_center_ui=control_ui, groups=("unmapped-subsystem",),
                        reason="subsystem had no deterministic tests",
                    )
                python_selectors = _existing(root, (*selectors, *PYTHON_ANCHORS))
                groups.append("backend-contract-subsystem")
            else:
                return TestImpact(
                    "T3", python_full=True, frontend_full=risk.run_frontend and product_ui,
                    product_ui=product_ui, control_center_ui=control_ui, groups=("unknown-executable",),
                    reason="unknown executable path failed closed",
                )
        else:
            risk_tier = "T2" if risk.test_tier == "T2" or len(domains) > 1 else "T1"
            selected: set[str] = set()
            for domain in sorted(domains):
                selected.update(_domain_tests(root, domain))
                groups.append(f"domain:{domain}")
            python_selectors = tuple(sorted(selected))
            if not python_selectors:
                return TestImpact("T3", python_full=True, product_ui=product_ui, control_center_ui=control_ui,
                                  groups=("unmapped-domain",), reason="domain had no deterministic tests")
    else:
        risk_tier = risk.test_tier

    frontend_selectors = _frontend_tests(root, changed) if product_ui and risk_tier == "T1" else ()
    frontend_full = bool(product_ui and risk.run_frontend and (risk_tier == "T2" or not frontend_selectors))
    if frontend_selectors:
        groups.append("product-ui-focused")
    if control_ui:
        groups.append("control-center-ui")
    return TestImpact(
        risk_tier, python_selectors=python_selectors, frontend_selectors=frontend_selectors,
        frontend_full=frontend_full, product_ui=product_ui, control_center_ui=control_ui,
        groups=tuple(groups), reason="deterministic path ownership and test-name mapping",
    )
