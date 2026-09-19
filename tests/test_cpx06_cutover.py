"""ENG-CP-06 (issue #173): snapshot, adopt, idempotency, and runtime identity."""

from __future__ import annotations

import json
from pathlib import Path

from octarel.cli import main as octarel_main
from scripts.agents.control_plane.cutover import (
    adopt_octages_state,
    meaningful_history_counts,
    read_runtime_identity,
    rollback_procedure_text,
    write_cutover_checkpoint,
    write_runtime_identity,
)
from scripts.agents.control_plane.project_registry import (
    bootstrap_registry,
    selected_project,
)
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.state_migration import migrate_orchestrator_state


def test_checkpoint_preserves_source_and_records_counts(tmp_path: Path) -> None:
    octages = tmp_path / "octages-state"
    octarel = tmp_path / "octarel-state"
    src = State(octages / "orchestrator.db")
    src.record_event(category="test", message="keep")
    src.set_control_setting("max_write_workers", "2")
    (octages / "reports").mkdir()
    (octages / "reports" / "note.txt").write_text("evidence\n", encoding="utf-8")
    src._conn.commit()
    dest = State(octarel / "orchestrator.db")
    dest.record_event(category="bootstrap", message="cpx-05 init")
    dest._conn.commit()
    source_bytes = (octages / "orchestrator.db").read_bytes()

    report = write_cutover_checkpoint(
        destination=tmp_path / "checkpoints",
        octages_state=octages,
        octarel_state=octarel,
    )
    assert report.ok
    assert report.octages_tables["events"] >= 1
    assert (Path(report.checkpoint_dir) / "octages-state" / "reports" / "note.txt").is_file()
    assert (Path(report.checkpoint_dir) / "ROLLBACK.md").is_file()
    assert (octages / "orchestrator.db").read_bytes() == source_bytes


def test_adopt_is_idempotent_and_restart_does_not_duplicate(tmp_path: Path) -> None:
    source_dir = tmp_path / "octages-state"
    dest_dir = tmp_path / "octarel-state"
    src = State(source_dir / "orchestrator.db")
    src.record_event(category="history", message="one")
    src.set_control_setting("max_write_workers", "4")
    src._conn.commit()

    first = adopt_octages_state(source=source_dir, destination=dest_dir)
    assert first.ok and first.action == "imported"
    before = meaningful_history_counts(first.destination_tables or first.tables)
    second = adopt_octages_state(source=source_dir, destination=dest_dir)
    assert second.ok and second.action == "already-imported"
    dest = State(dest_dir / "orchestrator.db")
    bootstrap_registry(dest, None)
    bootstrap_registry(dest, None)
    events = dest.list_events()
    history = [e for e in events if e.message == "one"]
    assert len(history) == 1
    after = meaningful_history_counts(
        {name: dest._conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
         for name, in dest._conn.execute(
             "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
         )}
    )
    assert after["events"] >= before["events"]
    assert (source_dir / "orchestrator.db").is_file()


def test_replace_unmigrated_bootstrap_destination(tmp_path: Path) -> None:
    source_dir = tmp_path / "octages-state"
    dest_dir = tmp_path / "octarel-state"
    src = State(source_dir / "orchestrator.db")
    src.record_event(category="history", message="imported-row")
    src._conn.commit()
    dest = State(dest_dir / "orchestrator.db")
    dest.record_event(category="bootstrap", message="throwaway")
    dest._conn.commit()

    report = migrate_orchestrator_state(
        source=source_dir,
        destination=dest_dir,
        replace_unmigrated_destination=True,
    )
    assert report.ok and report.action == "imported"
    adopted = State(dest_dir / "orchestrator.db")
    messages = [e.message for e in adopted.list_events()]
    assert "imported-row" in messages


def test_octarel_state_dir_is_not_nested_again(tmp_path: Path, monkeypatch) -> None:
    from scripts.agents.control_plane.state import resolve_standalone_db_path

    monkeypatch.setenv("OCTAREL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OCTAREL_CODE_ROOT", raising=False)
    assert resolve_standalone_db_path() == tmp_path / "orchestrator.db"


def test_health_distinguishes_code_root_from_cwd(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert octarel_main(["health"]) == 0
    out = capsys.readouterr().out
    assert "runtime=octarel" in out
    assert "code_root=" in out
    assert "selected_project_root=" in out
    assert "cwd_equals_selected_project=" in out


def test_runtime_identity_roundtrip(tmp_path: Path) -> None:
    path = write_runtime_identity(
        state_dir=tmp_path,
        code_root=tmp_path / "octarel",
        pid=123,
        port=8877,
        selected_project_id="octascene",
        selected_project_root=str(tmp_path / "octages"),
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["runtime"] == "octarel"
    assert body["selected_project_id"] == "octascene"
    assert read_runtime_identity(tmp_path)["pid"] == 123


def test_rollback_plan_mentions_access_and_fallback() -> None:
    text = rollback_procedure_text()
    assert "Cloudflare Access" in text
    assert "OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK" in text
    assert "127.0.0.1:8877" in text
    assert octarel_main(["cutover", "rollback-plan"]) == 0


def test_bootstrap_does_not_register_octarel_as_octascene(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OCTAREL_OCTASCENE_ROOT", raising=False)
    monkeypatch.delenv("OCTAGES_ORCH_CANONICAL_REPO_ROOT", raising=False)
    state = State(tmp_path / "state.db")
    # Octarel-shaped root: git repo without OctaScene ledger/roadmap.
    import subprocess

    repo = tmp_path / "octarel-shaped"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("# Octarel\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "AGENTS.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    bootstrap_registry(state, repo)
    assert selected_project(state) is None or selected_project(state).local_repo_root.resolve() != repo.resolve()
    assert state.get_project("octascene") is None
