"""Octarel tests must not import the OctaScene application composition root."""

from __future__ import annotations

# Intentionally empty of application bootstrap. Isolation of Octarel from
# ``app.bootstrap.services`` is a CPX-05 acceptance requirement.
