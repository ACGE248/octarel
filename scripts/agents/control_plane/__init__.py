"""ENG-AGENT-02 orchestrator control plane.

Durable daemon/scheduler/dashboard layer that extends ENG-AGENT-01's single-shot
delegation tooling (``scripts/agents/orchestrate.py``, ``registry.py``,
``runner.py``, ``manifest.py``, ``redaction.py``, ``validation.py``) with a
persistent local process: a SQLite-backed task/provider/worktree/event store, an
adaptive routing/scheduling layer, a supervisor that launches and tracks
delegated worker subprocesses, and a developer-only control-center dashboard.

This package is developer infrastructure only. It must never be imported from
``app/`` or ``frontend/`` and must never become a runtime dependency of the
shipped OctaScene product.
"""
