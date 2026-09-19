"""Durable boundary: ``scripts/agents/control_plane`` is dev tooling only.

Mirrors the spirit of ``tests/test_architecture.py``'s import-boundary checks
without modifying that file: nothing under ``app/`` or ``frontend/`` may import
the ENG-AGENT-02 orchestrator control plane, and the control plane itself must
never import ``app`` or ``frontend``.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _imported_roots(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - no such files expected
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_app_or_frontend_source_imports_the_orchestrator_control_plane():
    offenders = []
    for base in ("app", "frontend"):
        base_dir = REPO_ROOT / base
        if not base_dir.exists():
            continue
        for path in base_dir.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:  # pragma: no cover
                continue
            if "scripts.agents" in text or "scripts/agents" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"app/frontend source references scripts.agents: {offenders}"


def test_control_plane_source_never_imports_app_or_frontend():
    control_plane_dir = REPO_ROOT / "scripts" / "agents" / "control_plane"
    offenders = {}
    for path in control_plane_dir.rglob("*.py"):
        roots = _imported_roots(path)
        bad = roots & {"app", "frontend"}
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(bad)
    assert offenders == {}, f"control_plane source imports app/frontend: {offenders}"


def test_orchestrator_entrypoint_never_imports_app_or_frontend():
    path = REPO_ROOT / "scripts" / "agents" / "orchestrator.py"
    roots = _imported_roots(path)
    assert not (roots & {"app", "frontend"})


def test_dashboard_static_assets_do_not_reference_the_product_bundle():
    dashboard_dir = REPO_ROOT / "scripts" / "agents" / "control_plane" / "dashboard"
    for path in dashboard_dir.iterdir():
        if path.suffix in {".html", ".js", ".css"}:
            text = path.read_text(encoding="utf-8")
            assert "octages.bundle" not in text
