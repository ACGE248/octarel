"""Playwright fixture reset: shared backend must restore the deterministic seed."""

from __future__ import annotations

import datetime as dt
import importlib.util
import itertools
from pathlib import Path
from typing import Any

import pytest

from scripts.agents.control_plane import state as state_mod
from scripts.agents.control_plane.state import default_db_path

ACTIVE_STATES = {"RUNNING", "QUEUED", "PENDING", "PAUSED", "BLOCKED"}


def _load_serve_fixture() -> Any:
    """Import serve_fixture via the path shim the harness uses.

    ``dashboard-tests`` is not a valid package identifier, so the module cannot
    be imported by name.
    """

    path = Path(__file__).resolve().parents[1] / "scripts" / "agents" / "control_plane" / "dashboard-tests" / "serve_fixture.py"
    spec = importlib.util.spec_from_file_location("serve_fixture_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _newest_active_task_ref(state: Any) -> str:
    """The task reference ``dashboard_api.workflow()`` would render.

    Mirrors its ``group_rank``: every candidate here is active, so the newest
    ``updated_at`` wins and ``max`` keeps the first entry on a tie.
    """

    newest: dict[str, str] = {}
    for task in state.list_tasks():
        if task.state in ACTIVE_STATES:
            newest[task.task_ref] = max(newest.get(task.task_ref, ""), task.updated_at)
    return max(newest.items(), key=lambda pair: pair[1])[0]


def test_reset_fixture_context_restores_draft_runbook(tmp_path: Path) -> None:
    mod = _load_serve_fixture()

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


def test_fixture_seed_keeps_one_current_task_ref_across_a_clock_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCTAREL-TEST-01: which task reference is "current" must not depend on seed duration.

    ``dashboard_api.workflow()`` renders the single most recently updated active
    task reference, and ``utc_now_iso()`` resolves to whole seconds. The fixture
    seeds ENG-AGENT-02's ``fx-*`` tasks before ENG-AGENT-07's fallback session,
    so a seed that crossed a wall-clock second between those writes used to hand
    the Live Workflow card to ENG-AGENT-07 -- and ``agent-activity.spec.js`` and
    ``ui-polish.spec.js``, which locate ``.workflow-stage-card[data-stage-id="fx-*"]``,
    then found no such card. The concurrent viewport matrix made that slow seed,
    and therefore the failure, load-dependent.

    Advancing the clock a full second on every write is strictly harsher than any
    real straddle: it puts each seeded row in its own second.
    """

    epoch = dt.datetime(2026, 9, 18, 10, 0, tzinfo=dt.timezone.utc)
    ticks = itertools.count()

    def advancing_clock() -> str:
        return (epoch + dt.timedelta(seconds=next(ticks))).isoformat(timespec="seconds")

    monkeypatch.setattr(state_mod, "utc_now_iso", advancing_clock)

    mod = _load_serve_fixture()
    ctx = mod.build_fixture_context(tmp_path / "fx")

    assert _newest_active_task_ref(ctx.state) == "ENG-AGENT-02"
    # The cards those specs open must all be present under that reference.
    for task_id in ("fx-running-1", "fx-running-2", "fx-blocked-1", "fx-done-1"):
        task = ctx.state.get_task(task_id)
        assert task is not None
        assert task.task_ref == "ENG-AGENT-02"
    # Every competing active reference is strictly older, not merely tied.
    current = ctx.state.get_task("fx-running-1").updated_at
    others = {
        task.task_ref: task.updated_at
        for task in ctx.state.list_tasks()
        if task.state in ACTIVE_STATES and task.task_ref != "ENG-AGENT-02"
    }
    assert others, "fixture no longer seeds a competing active task reference"
    assert all(stamp < current for stamp in others.values()), others
