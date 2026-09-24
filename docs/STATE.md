# State, backup, and restore

## Location

Default: `<octarel-checkout>/.orchestrator-state/orchestrator.db`

Override with `OCTAREL_STATE_DIR` (a directory, not a managed-repo path).
Live service configuration must point at the canonical Octarel checkout, not
a leftover feature worktree.

The directory is gitignored. Never commit SQLite, WAL, SHM, or reports.

## What is stored

Orchestration records: tasks, runbooks, events, worktrees, provider state,
operations, project registry. Not product databases, not managed-repo files.

Rows that belong to a project carry `project_id`. Switching projects does not
rewrite another project's history.

Graphify cache (optional, derived): `<state dir>/graph-context/<project>/<worktree>/<tree>/`. It is
disposable; delete it freely. It is never a source of task or repository truth.

## Backup

Copy the state directory after a SQLite checkpoint, or use:

```bash
python -m octarel cutover snapshot \
  --octages-state /path/to/octages/.orchestrator-state \
  --octarel-state /path/to/octarel/.orchestrator-state \
  --out /path/to/checkpoints
```

`python -m octarel migrate --from <dir>` uses SQLite backup (WAL-safe) and
never deletes the source.

## Restore

Point `OCTAREL_STATE_DIR` at the restored directory, or copy
`orchestrator.db` plus WAL/SHM back into the canonical state dir while the
dashboard is stopped.

## Canonicalize after a worktree-pinned install

```bash
python -m octarel service canonicalize-state \
  --from /path/to/old-worktree/.orchestrator-state \
  --to /path/to/octarel/.orchestrator-state
```

Then set `OCTAREL_STATE_DIR` on the local launchd plist to the destination
and restart. Keep the source directory.

## Embedded Octages-era state

Import with `python -m octarel migrate` or `python -m octarel cutover adopt`.
A second import of the same snapshot is a no-op. Do not delete the Octages
`.orchestrator-state/` directory; it remains rollback evidence.
