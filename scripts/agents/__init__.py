"""Provider-neutral local multi-provider delegation tooling for ``ENG-AGENT-01``.

This package is development tooling only. It never runs in GitHub Actions, never
makes product/provider API calls itself, and never commits or pushes. It builds a
redacted, auditable record of a single delegated worker invocation and stores the
detailed evidence under the git-ignored ``.agent-output/<TASK-ID>/`` tree so that
CLI truncation cannot lose it.

See ``scripts/agents/README.md`` and
``docs/engineering/GITHUB_DEVELOPMENT_WORKFLOW.md`` for the operating contract.
"""

from __future__ import annotations
