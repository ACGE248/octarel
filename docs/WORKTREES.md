# Worktree model

Substantial work uses a dedicated branch and a dedicated Git worktree.
Exactly one write-capable owner may use a checkout at a time.

- Parallel writers need separate worktrees.
- Read-only reviewers may inspect a bounded checkout.
- Never force-push, reset, or clean another worker's tree.
- Octarel records worktrees per managed project. Discovery uses
  `git worktree list` inside that project's repository, never Octarel cwd.
- Starting a runbook adopts its dedicated checkout into the managed read model.
  Cancellation keeps a physically present Git worktree visible and releases a
  dead owned-process lock only when its PID and standard lock signature match;
  a live or unrelated holder is never removed.

Policy: `.agents/core/GIT_WORKTREES.md`.
