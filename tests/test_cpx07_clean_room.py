"""ENG-CP-07 (issue #175): clean-room clone, bootstrap, CLI, dashboard, project add."""

from __future__ import annotations

import os

from scripts.ci.clean_room_acceptance import run_clean_room
from scripts.ci.prepare_public_history import prepare_public_history
from tests.octarel_paths import OCTAREL_ROOT


def test_clean_room_install_from_fresh_clone(tmp_path) -> None:
    report = run_clean_room(source=OCTAREL_ROOT, destination=tmp_path / "room", with_node=True)
    assert report.ok, report.as_text()
    assert "clone" in report.steps
    assert "pip" in report.steps
    assert "npm" in report.steps
    assert "project-add-select" in report.steps
    assert any(step.startswith("dashboard:") for step in report.steps)
    clean_venv = tmp_path / "room" / "octarel" / ".venv"
    assert clean_venv.is_dir()
    dev_venv = OCTAREL_ROOT / ".venv"
    if dev_venv.exists():
        assert clean_venv.resolve() != dev_venv.resolve()


def test_prepare_public_history_uses_public_identity(tmp_path) -> None:
    dest = tmp_path / "public-safe"
    prepare_public_history(OCTAREL_ROOT, dest)
    log = os.popen(f"git -C {dest} log -1 --format='%an <%ae>'").read().strip()
    assert log == "Octarel contributors <octarel@users.noreply.github.com>"
    count = os.popen(f"git -C {dest} rev-list --all --count").read().strip()
    assert count == "1"
    branch = os.popen(f"git -C {dest} branch --show-current").read().strip()
    assert branch == "main"
