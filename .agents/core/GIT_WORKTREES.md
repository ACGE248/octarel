# Git and worktrees

- Normal development never writes directly to `main` or `master`.
- Substantial work uses its stable task reference, dedicated branch, and dedicated worktree.
- Exactly one write-capable owner may use a checkout at a time. Parallel writers require separate
  branches/worktrees; read-only workers may inspect a bounded checkout.
- Preserve unrelated changes. Never reset, clean, force-push, rewrite, or delete another worker's work.
- Verify branch, worktree, merge base, locks, and candidate state before mutation or integration.
- Rebase/merge only through the repository's normal review flow. Reconcile shared contracts and
  maintained documentation when integrating related branches.
