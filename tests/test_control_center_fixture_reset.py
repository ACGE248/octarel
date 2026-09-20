"""Playwright fixture reset: shared backend must restore the deterministic seed."""

from __future__ import annotations

from pathlib import Path

from scripts.agents.control_plane.state import default_db_path


def test_reset_fixture_context_restores_draft_runbook(tmp_path: Path) -> None:
    # Import from serve_fixture via path shim used by the harness.
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "agents" / "control_plane" / "dashboard-tests" / "serve_fixture.py"
    spec = importlib.util.spec_from_file_location("serve_fixture_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    root = tmp_path / "fx"
    ctx = mod.build_fixture_context(root)
    rb = ctx.state.get_runbook("fx-rb-draft")
    assert rb is not None
    rb.status = "BLOCKED"
    ctx.state.upsert_runbook(rb)
    assert ctx.state.get_runbook("fx-rb-draft").status == "BLOCKED"

    mod.reset_fixture_context(ctx, root)
    restored = ctx.state.get_runbook("fx-rb-draft")
    assert restored is not None
    assert restored.status == "DRAFT"
    assert default_db_path(root).is_file()
    review_rows = [w for w in ctx.state.list_worktrees() if w.review_pr]
    assert review_rows
    assert review_rows[0].review_repository == "ACGE248/octages"
    assert review_rows[0].review_pr == 999

    identity = mod._parent_identity()
    assert identity[0] > 1
    assert mod._parent_identity_alive(identity) is True
    assert mod._parent_identity_alive((identity[0] + 10_000_000, identity[1])) is False
