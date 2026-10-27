from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.agents.runner import run_worker_process
from scripts.ci.development_storage import (
    DevelopmentStorageError,
    prepare_development_storage,
    resolve_storage_root,
)


def test_storage_environment_uses_one_external_root(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    storage = prepare_development_storage(
        tmp_path, environ={"OCTA_DEV_STORAGE_ROOT": str(root)}, minimum_free_bytes=0,
    )
    assert storage.environment["TMPDIR"] == str(root / "tmp")
    assert storage.environment["npm_config_cache"] == str(root / "cache" / "npm")
    assert storage.environment["PLAYWRIGHT_BROWSERS_PATH"] == str(root / "cache" / "playwright")


def test_storage_supports_sqlite_wal(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    storage = prepare_development_storage(
        tmp_path, environ={"OCTA_DEV_STORAGE_ROOT": str(root)}, minimum_free_bytes=0,
    )
    connection = sqlite3.connect(Path(storage.environment["TMPDIR"]) / "worker.db")
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def test_relative_storage_override_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DevelopmentStorageError, match="must be an absolute path"):
        resolve_storage_root(tmp_path, {"OCTA_DEV_STORAGE_ROOT": "relative"})


def test_managed_worker_process_receives_storage_environment(tmp_path: Path) -> None:
    storage_root = tmp_path / "managed-storage"
    code, output = run_worker_process(
        ["/usr/bin/env"],
        tmp_path,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "OCTA_DEV_STORAGE_ROOT": str(storage_root)},
    )

    assert code == 0
    environment = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    assert environment["TMPDIR"] == str(storage_root / "tmp")
    assert environment["npm_config_cache"] == str(storage_root / "cache" / "npm")
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(storage_root / "cache" / "playwright")
