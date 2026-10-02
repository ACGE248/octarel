"""ENG-PC-08: immutable runtime configuration revisions and rollback."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.agents.control_plane.commands import CommandContext
from scripts.agents.control_plane.config_revisions import (
    CONFIGURATION_OWNERSHIP,
    REVISIONABLE_SETTING_KEYS,
    InvalidConfigurationError,
    StaleConfigurationError,
    validate_configuration,
)
from scripts.agents.control_plane.dashboard_api import create_app
from scripts.agents.control_plane.scheduler import Scheduler
from scripts.agents.control_plane.state import State
from scripts.agents.control_plane.supervisor import Supervisor
from scripts.agents.registry import load_registry


def _context(tmp_path: Path, state: State | None = None) -> CommandContext:
    registry = load_registry()
    actual_state = state or State(tmp_path / "orchestrator.db")
    return CommandContext(
        state=actual_state,
        registry=registry,
        scheduler=Scheduler(),
        supervisor=Supervisor(registry=registry, repo_root=tmp_path, state=actual_state),
        repo_root=tmp_path,
    )


def test_migrates_existing_runtime_settings_into_immutable_revision_zero(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE control_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO control_settings(key, value) VALUES(?, ?)",
        [
            ("max_write_workers", "3"),
            ("repository_health_cache", '{"derived": true}'),
            ("merge_prepare:operation-1", '{"path": "/private/operator/project"}'),
        ],
    )
    connection.commit()
    connection.close()

    state = State(db_path)
    revision = state.current_configuration_revision()
    assert revision == {
        "id": 0,
        "actor": "system:migration",
        "created_at": revision["created_at"],
        "reason": "initial revision migrated from existing runtime-editable settings",
        "changed_fields": {"max_write_workers": {"before": None, "after": "3"}},
        "predecessor_id": None,
        "settings": {"max_write_workers": "3"},
    }
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute("UPDATE configuration_revisions SET reason = 'rewrite' WHERE id = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        state._conn.execute("DELETE FROM configuration_revisions WHERE id = 0")


def test_healthy_configuration_migration_performs_no_ddl(tmp_path: Path):
    state = State(tmp_path / "state.db")
    statements: list[str] = []
    state._conn.set_trace_callback(statements.append)
    state._migrate_configuration_revisions()
    state._conn.set_trace_callback(None)
    ddl = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("ALTER ", "CREATE ", "DROP "))
    ]
    assert ddl == []


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("value", '["sk-abcdefghijklmnop"]'),
        ("value", '["/private/operator/project"]'),
        ("value", '["C:\\\\Users\\\\operator\\\\project"]'),
        ("reason", "token=abcdefghijklmnop"),
        ("reason", "restore /private/operator/project"),
    ],
)
def test_secret_and_absolute_paths_never_reach_revision_payload(
    tmp_path: Path, field: str, unsafe_value: str
):
    state = State(tmp_path / "state.db")
    with pytest.raises(InvalidConfigurationError):
        state.apply_configuration(
            {"held_task_ids": unsafe_value} if field == "value" else {"max_write_workers": "2"},
            actor="operator@example.test",
            reason=unsafe_value if field == "reason" else "unsafe proposal",
            predecessor_id=0,
        )

    rows = state._conn.execute(
        "SELECT actor, reason, changed_fields, settings FROM configuration_revisions"
    ).fetchall()
    assert [row[3] for row in rows] == ["{}"]
    stored = json.dumps([tuple(row) for row in rows])
    assert "sk-" not in stored
    assert "/private/operator" not in stored
    assert "token=abcdefghijklmnop" not in stored


def test_invalid_historical_configuration_is_refused_on_rollback(tmp_path: Path):
    state = State(tmp_path / "state.db")
    state._conn.execute(
        "INSERT INTO configuration_revisions "
        "(id, actor, created_at, reason, changed_fields, predecessor_id, settings) "
        "VALUES (1, 'legacy', '2026-01-01T00:00:00+00:00', 'legacy', '{}', 0, ?)",
        (json.dumps({"max_write_workers": "0"}),),
    )
    state._conn.execute(
        "INSERT INTO configuration_revisions "
        "(id, actor, created_at, reason, changed_fields, predecessor_id, settings) "
        "VALUES (2, 'migration', '2026-01-02T00:00:00+00:00', 'repaired', '{}', 1, '{}')"
    )
    state._conn.commit()

    with pytest.raises(InvalidConfigurationError, match="at least 1"):
        state.rollback_configuration(
            1,
            actor="operator@example.test",
            reason="restore legacy",
            predecessor_id=2,
        )
    assert state.current_configuration_revision()["id"] == 2


def test_stale_rollback_cannot_clobber_an_intervening_change(tmp_path: Path):
    state = State(tmp_path / "state.db")
    first = state.apply_configuration(
        {"max_write_workers": "2"}, actor="alice", reason="first", predecessor_id=0
    )
    second = state.apply_configuration(
        {"max_read_workers": "3"}, actor="bob", reason="intervening", predecessor_id=first["id"]
    )

    with pytest.raises(StaleConfigurationError, match=f"current revision is {second['id']}"):
        state.rollback_configuration(
            0,
            actor="alice",
            reason="stale rollback",
            predecessor_id=first["id"],
        )
    assert state.current_configuration_revision()["settings"] == {
        "max_read_workers": "3",
        "max_write_workers": "2",
    }


def test_two_writers_use_compare_and_swap_so_one_loses_explicitly(tmp_path: Path):
    db_path = tmp_path / "state.db"
    bootstrap = State(db_path)
    bootstrap.close()
    first = State(db_path)
    second = State(db_path)

    def update(state: State, key: str) -> str:
        try:
            state.apply_configuration({key: "2"}, actor=key, reason="race", predecessor_id=0)
        except StaleConfigurationError:
            return "stale"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda pair: update(*pair),
                [(first, "max_write_workers"), (second, "max_read_workers")],
            )
        )
    assert sorted(outcomes) == ["stale", "won"]


def test_apply_rollback_and_refused_rollback_keep_audit_continuity(tmp_path: Path, monkeypatch):
    from scripts.agents.control_plane import dashboard_api

    ctx = _context(tmp_path)
    remote_audits: list[tuple[str, str]] = []

    def capture_remote(_ctx, _request, *, verb, target, result):  # noqa: ANN001
        remote_audits.append((verb, result))

    monkeypatch.setattr(dashboard_api, "_record_remote_audit", capture_remote)
    client = TestClient(create_app(ctx, roadmap_path=tmp_path / "missing-roadmap.md"))
    applied = client.post(
        "/api/configuration/apply",
        json={"changes": {"max_write_workers": "2"}, "predecessor_id": 0, "reason": "scale up"},
    )
    assert applied.status_code == 200
    applied_id = applied.json()["revision"]["id"]
    rolled_back = client.post(
        "/api/configuration/rollback",
        json={"revision_id": 0, "predecessor_id": applied_id, "reason": "undo scale"},
    )
    assert rolled_back.status_code == 200
    refused = client.post(
        "/api/configuration/rollback",
        json={"revision_id": 0, "predecessor_id": applied_id, "reason": "stale retry"},
    )
    assert refused.status_code == 409

    assert [item[0] for item in remote_audits] == [
        "configuration_apply",
        "configuration_rollback",
        "configuration_rollback",
    ]
    assert remote_audits[-1][1].startswith("REFUSED:")
    audit_messages = [
        event.message for event in ctx.state.list_events(limit=20) if event.category == "configuration_audit"
    ]
    assert any("local-operator applied" in message for message in audit_messages)
    assert any("local-operator rolled" in message for message in audit_messages)
    assert any("local-operator refused" in message for message in audit_messages)
    assert all(revision["actor"] == "local-operator" for revision in ctx.state.list_configuration_revisions()[:2])


def test_repository_controlled_sources_are_facts_not_revision_targets(tmp_path: Path):
    workers = tmp_path / "scripts" / "agents" / "workers.json"
    policy = tmp_path / ".agents" / "core" / "SECURITY.md"
    workers.parent.mkdir(parents=True)
    policy.parent.mkdir(parents=True)
    workers.write_text('{"owned": "repository"}', encoding="utf-8")
    policy.write_text("repository policy", encoding="utf-8")
    state = State(tmp_path / "state.db")
    client = TestClient(create_app(_context(tmp_path, state), roadmap_path=tmp_path / "missing.md"))

    for key in ("workers.json", ".agents/core/SECURITY.md"):
        response = client.post(
            "/api/configuration/apply",
            json={"changes": {key: "replacement"}, "predecessor_id": 0, "reason": "out of scope"},
        )
        assert response.status_code == 400
        assert "not runtime-revisionable" in response.json()["detail"]
    assert workers.read_text(encoding="utf-8") == '{"owned": "repository"}'
    assert policy.read_text(encoding="utf-8") == "repository policy"
    assert client.get("/api/configuration").json()["ownership"] == CONFIGURATION_OWNERSHIP


def test_allowlist_is_exact_and_nothing_else_is_revisioned(tmp_path: Path):
    expected = {
        "held_task_ids",
        "max_global_workers",
        "max_heavy_workers",
        "max_provider_workers",
        "max_read_workers",
        "max_write_workers",
        "stop_after_current",
    }
    assert REVISIONABLE_SETTING_KEYS == expected
    samples = {key: "2" for key in expected if key.startswith("max_")}
    samples.update({"held_task_ids": "[]", "stop_after_current": "0"})
    assert set(validate_configuration(samples)) == expected

    state = State(tmp_path / "state.db")
    state.set_control_setting("repository_health_cache", '{"derived": true}')
    state.set_control_setting("schema_version", "999")
    state.set_control_setting("merge_prepare:123", '{"path": "/private/operator/project"}')
    assert state.current_configuration_revision()["id"] == 0
    assert state.current_configuration_revision()["settings"] == {}


def test_rollback_appends_instead_of_rewriting_history(tmp_path: Path):
    state = State(tmp_path / "state.db")
    changed = state.apply_configuration(
        {"max_write_workers": "4"}, actor="alice", reason="temporary", predecessor_id=0
    )
    rolled_back = state.rollback_configuration(
        0, actor="bob", reason="restore baseline", predecessor_id=changed["id"]
    )
    assert rolled_back["id"] > changed["id"]
    assert rolled_back["predecessor_id"] == changed["id"]
    assert rolled_back["settings"] == {}
    assert rolled_back["reason"].endswith("(rollback to revision 0)")
    assert [revision["id"] for revision in state.list_configuration_revisions()] == [2, 1, 0]
