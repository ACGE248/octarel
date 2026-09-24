"""Octarel tests must not import the OctaScene application composition root."""

from __future__ import annotations

import pytest

# Intentionally empty of application bootstrap. Isolation of Octarel from
# ``app.bootstrap.services`` is a CPX-05 acceptance requirement.


@pytest.fixture(autouse=True)
def _isolated_opencode_model_catalog(tmp_path, monkeypatch):
    """ENG-AO-03: no test may read or write the real OpenCode catalog cache or spawn the real ``opencode``."""

    from scripts.agents import model_catalog

    monkeypatch.setattr(model_catalog, "opencode_state_dir", lambda: tmp_path / "opencode-models-state")
    monkeypatch.setattr(model_catalog, "OPENCODE_BIN", "opencode-not-installed-under-test")
