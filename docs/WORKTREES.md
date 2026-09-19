# Worktree model

Substantial work uses a dedicated branch and a dedicated Git worktree.
Exactly one write-capable owner may use a checkout at a time.

- Parallel writers need separate worktrees.
- Read-only reviewers may inspect a bounded checkout.
- Never force-push, reset, or clean another worker's tree.
- Octarel records worktrees per managed project. Discovery uses
  `git worktree list` inside that project's repository, never Octarel cwd.

Policy: `.agents/core/GIT_WORKTREES.md`.
