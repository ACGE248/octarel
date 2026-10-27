"""Shared external-volume storage policy for Octarel development processes."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV = "OCTA_DEV_STORAGE_ROOT"
MIN_FREE_BYTES = 20 * 1024**3


class DevelopmentStorageError(RuntimeError):
    """The configured development volume is unavailable or incompatible."""


@dataclass(frozen=True)
class DevelopmentStorage:
    root: Path
    free_bytes: int
    environment: dict[str, str]


def _common_repository_root(checkout: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=checkout,
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip()).resolve()
        return common.parent if common.name == ".git" else checkout.resolve()
    return checkout.resolve()


def resolve_storage_root(checkout: Path, environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    configured = source.get(ROOT_ENV, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise DevelopmentStorageError(f"{ROOT_ENV} must be an absolute path")
        return path.resolve()
    return _common_repository_root(checkout).parent / ".octa-dev-storage"


def _probe_filesystem(tmp_root: Path) -> None:
    probe = Path(tempfile.mkdtemp(prefix="storage-probe-", dir=tmp_root))
    try:
        source, renamed = probe / "source", probe / "renamed"
        source.write_bytes(b"octa-storage-probe")
        os.replace(source, renamed)
        os.link(renamed, probe / "hardlink")
        connection = sqlite3.connect(probe / "sqlite.db")
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO probe(value) VALUES ('ok')")
            connection.commit()
            if not mode or str(mode[0]).casefold() != "wal":
                raise DevelopmentStorageError("development storage does not support SQLite WAL mode")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise DevelopmentStorageError("development storage failed SQLite integrity_check")
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise DevelopmentStorageError(f"development storage filesystem probe failed: {exc}") from exc
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def prepare_development_storage(
    checkout: Path, *, environ: Mapping[str, str] | None = None,
    minimum_free_bytes: int = MIN_FREE_BYTES, verify: bool = True,
) -> DevelopmentStorage:
    root = resolve_storage_root(checkout, environ)
    directories = {
        "tmp": root / "tmp", "xdg": root / "cache" / "xdg", "npm": root / "cache" / "npm",
        "pip": root / "cache" / "pip", "playwright": root / "cache" / "playwright",
        "python": root / "cache" / "python", "artifacts": root / "artifacts",
    }
    try:
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        for directory in directories.values():
            directory.chmod(0o700)
        free = shutil.disk_usage(root).free
    except OSError as exc:
        raise DevelopmentStorageError(f"development storage is unavailable at {root}: {exc}") from exc
    if free < minimum_free_bytes:
        raise DevelopmentStorageError(
            f"development storage at {root} has only {free} bytes free; at least {minimum_free_bytes} are required"
        )
    if verify:
        _probe_filesystem(directories["tmp"])
    values = {
        ROOT_ENV: str(root), "TMPDIR": str(directories["tmp"]), "TMP": str(directories["tmp"]),
        "TEMP": str(directories["tmp"]), "XDG_CACHE_HOME": str(directories["xdg"]),
        "npm_config_cache": str(directories["npm"]), "NPM_CONFIG_CACHE": str(directories["npm"]),
        "PIP_CACHE_DIR": str(directories["pip"]), "PLAYWRIGHT_BROWSERS_PATH": str(directories["playwright"]),
        "PYTHONPYCACHEPREFIX": str(directories["python"]),
        "OCTA_TEST_ARTIFACT_ROOT": str(directories["artifacts"]),
    }
    return DevelopmentStorage(root=root, free_bytes=free, environment=values)


def environment_for_development(
    checkout: Path, base: Mapping[str, str] | None = None, *, verify: bool = True,
) -> dict[str, str]:
    result = dict(os.environ if base is None else base)
    storage = prepare_development_storage(checkout, environ=result, verify=verify)
    result.update(storage.environment)
    return result


def apply_development_storage(checkout: Path, *, verify: bool = True) -> DevelopmentStorage:
    storage = prepare_development_storage(checkout, verify=verify)
    os.environ.update(storage.environment)
    tempfile.tempdir = storage.environment["TMPDIR"]
    return storage
