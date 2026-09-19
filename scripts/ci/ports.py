"""Loopback-only ephemeral port leases for local deterministic tools."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


@dataclass
class PortLeases:
    root: Path
    allocations: dict[str, int] = field(default_factory=dict)
    _files: list[Path] = field(default_factory=list)

    def __enter__(self) -> "PortLeases":
        lease_dir = self.root / ".local-gate" / "ports"
        lease_dir.mkdir(parents=True, exist_ok=True)
        for path in lease_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not _pid_alive(int(record.get("pid", 0))):
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
        return self

    def allocate(self, name: str) -> int:
        if name in self.allocations:
            return self.allocations[name]
        for _ in range(32):
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            finally:
                probe.close()
            path = self.root / ".local-gate" / "ports" / f"{port}.json"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            record = {"name": name, "port": port, "pid": os.getpid(), "loopback": "127.0.0.1",
                      "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
            self._files.append(path)
            self.allocations[name] = port
            return port
        raise RuntimeError(f"unable to reserve a distinct loopback port for {name}")

    def __exit__(self, *_: object) -> None:
        for path in self._files:
            path.unlink(missing_ok=True)
