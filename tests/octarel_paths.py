from __future__ import annotations

import os
from pathlib import Path

OCTAREL_ROOT = Path(__file__).resolve().parents[1]


def octascene_checkout() -> Path | None:
    raw = os.environ.get("OCTAREL_OCTASCENE_ROOT")
    if not raw:
        return None
    path = Path(raw)
    if (path / "AGENTS.md").is_file() and (path / ".git").exists():
        return path
    return None
