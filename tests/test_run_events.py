"""ENG-PC-04 typed envelope over the existing append-only events table."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.agents.control_plane.run_events import EVENT_CLASSES, RunEvent
from scripts.agents.control_plane.state import State


def _event(index: int = 0, *, evidence: dict[str, str] | None = None) -> RunEvent:
    return RunEvent(
        run_id="run-1",
        event_class="process",
        event_type="process.status",
        source="test.real_writer",
        provenance="MEASURED",
        message=f"worker status {index}",
        task_id="task-1",
        data={"index": index},
        evidence=evidence or {},
    )


def test_event_class_contract_covers_required_timeline_domains() -> None:
    assert EVENT_CLASSES == {
        "lifecycle",
        "wake",
        "route",
        "lease",
        "process",
        "adapter_tool",
        "checkpoint",
        "usage",
        "review",
        "gate",
        "pr_merge",
        "wait_attention",
        "approval",
    }


def test_typed_event_migrates_the_existing_table_without_changing_legacy_writes(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    state = State(db)
    state.record_event(category="legacy", message="old caller still works")
    typed = state.record_run_event(_event())
    state.close()

    reopened = State(db)
    rows = reopened.list_events()
    assert {row.message for row in rows} == {"old caller still works", "worker status 0"}
    assert typed.run_sequence == 1
    assert typed.event_class == "process"
    assert rows[-1].run_id is None
    tables = {
        row[0]
        for row in reopened._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "run_events" not in tables, "ENG-PC-04 must extend events, not create a second history table"


def test_real_concurrent_connections_allocate_one_monotonic_sequence(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    State(db).close()

    def write(index: int) -> tuple[int, int]:
        with State(db) as writer:
            stored = writer.record_run_event(_event(index))
            return int(stored.run_sequence), int(stored.data["index"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        written = list(pool.map(write, range(40)))

    with State(db) as reader:
        persisted = reader.list_run_events(run_id="run-1", limit=100)
    assert sorted(sequence for sequence, _index in written) == list(range(1, 41))
    assert [row.run_sequence for row in persisted] == list(range(1, 41))
    assert {row.data["index"] for row in persisted} == set(range(40))


def test_restart_persists_typed_envelope_and_continues_sequence(tmp_path: Path) -> None:
    db = tmp_path / "restart.db"
    with State(db) as first:
        first.record_run_event(_event(1))
    with State(db) as second:
        stored = second.record_run_event(_event(2))
        rows = second.list_run_events(run_id="run-1")
    assert stored.run_sequence == 2
    assert [row.data["index"] for row in rows] == [1, 2]


def test_typed_event_redacts_secrets_and_omits_absolute_host_paths(tmp_path: Path) -> None:
    state = State(":memory:")
    event = RunEvent(
        run_id="safe-run",
        event_class="adapter_tool",
        event_type="adapter.failed",
        source="test.adapter",
        provenance="UNKNOWN",
        message="failed in /Users/private/repo with API_KEY=topsecretvalue",
        data={"cwd": "/Users/private/repo", "detail": "log at /tmp/private.log token=topsecretvalue"},
    )
    stored = state.record_run_event(event)
    rendered = f"{stored.message} {stored.data}"
    assert "/Users/private" not in rendered
    assert "/tmp/private.log" not in rendered
    assert "topsecretvalue" not in rendered
    assert "***REDACTED***" in rendered


def test_evidence_pointers_must_resolve_to_existing_agent_output_files(tmp_path: Path) -> None:
    evidence = tmp_path / ".agent-output" / "task" / "worker" / "run"
    evidence.mkdir(parents=True)
    manifest = evidence / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    state = State(":memory:")
    pointer = manifest.relative_to(tmp_path).as_posix()
    stored = state.record_run_event(_event(evidence={"manifest": pointer}), repo_root=tmp_path)
    assert stored.evidence == {"manifest": pointer}

    with pytest.raises(ValueError, match="existing file"):
        state.record_run_event(
            _event(evidence={"manifest": ".agent-output/task/worker/run/missing.json"}), repo_root=tmp_path
        )
    with pytest.raises(ValueError, match="stay under"):
        state.record_run_event(_event(evidence={"manifest": "../outside.json"}), repo_root=tmp_path)
    with pytest.raises(ValueError, match="stay under"):
        state.record_run_event(_event(evidence={"manifest": str(manifest)}), repo_root=tmp_path)


def test_pre_eng_pc_04_database_is_migrated_in_place(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
        "category TEXT NOT NULL, task_id TEXT, provider TEXT, level TEXT NOT NULL DEFAULT 'info', "
        "message TEXT NOT NULL, project_id TEXT)"
    )
    conn.execute(
        "INSERT INTO events (ts, category, level, message) VALUES ('2026-01-01T00:00:00+00:00', 'legacy', 'info', 'kept')"
    )
    conn.commit()
    conn.close()

    with State(db) as migrated:
        columns = {row["name"] for row in migrated._conn.execute("PRAGMA table_info(events)").fetchall()}
        assert {"run_id", "run_sequence", "event_class", "source", "provenance", "evidence"} <= columns
        assert migrated.list_events()[0].message == "kept"
        assert migrated.record_run_event(_event()).run_sequence == 1
