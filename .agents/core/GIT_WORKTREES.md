# Git and worktrees

Octarel uses a lighter Git policy than managed product repositories such as OctaScene.

## Default Octarel flow

- Low-risk Octarel-only changes may be made in the canonical Octarel checkout.
- Use a short-lived branch by default; a separate sibling worktree is not required for ordinary UI, docs,
  configuration, and bounded internal-tooling changes.
- Dedicated worktrees remain the default for parallel writers and are recommended for substantial,
  stateful, migration, security, provider-routing, or cross-cutting changes.
- Exactly one write-capable owner may use a checkout at a time. Parallel writers require separate worktrees
  unless ownership is explicitly serialized.
- Preserve unrelated changes. Never reset, clean, force-push, rewrite, or delete another worker's work.
- Verify branch, checkout/worktree, merge base, locks, and candidate state before mutation or integration.

## Owner override

The repository owner may explicitly waive the Octarel branch/worktree/PR flow for a bounded change, including
authorizing direct work on canonical `main` for low-risk Octarel-only changes.

When such an override is used:
- scope it only to the instructed change;
- do not touch unrelated work;
- report `WAIVED_BY_OWNER` for the normal Git controls that were skipped;
- never claim a PR/review/merge gate happened if it did not.

An Octarel override never automatically changes the Git policy of a managed project.
