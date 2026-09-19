# Claude Code Bootstrap

This file exists only as the repository bootstrap for direct/local Claude Code sessions. It is not an
authoritative OctaScene engineering-policy, architecture, testing, workflow, product, or provider-rules source.

Before repository work:

1. Read `AGENTS.md` for universal rules.
2. Read `.agents/providers/CLAUDE.md` for Claude-specific execution rules.
3. Determine the assigned role and read `.agents/roles/<ROLE>.md`.
4. If the task uses a defined workflow, read `.agents/workflows/<WORKFLOW>.md`.
5. Read only the `.agents/core/` policies relevant to the task.
6. Read only product/program/ADR/task documents relevant to the affected subsystem.
7. Repository code and maintained canonical documentation override this bootstrap file.

Do not duplicate universal security, Git, worktree, testing, documentation, workflow, product, architecture,
or provider policy here. Orchestrated Claude work receives the canonical policy bundle explicitly and does
not depend on this file's native auto-discovery.
