# State, backup, and restore

## Location

Default: `<octarel-checkout>/.orchestrator-state/orchestrator.db`

Override with `OCTAREL_STATE_DIR` (a directory, not a managed-repo path).
Live service configuration must point at the canonical Octarel checkout, not
a leftover feature worktree.

The directory is gitignored. Never commit SQLite, WAL, SHM, or reports.

## What is stored

Orchestration records: tasks, runbooks, events, worktrees, provider state,
operations, project registry, advancement decisions, runtime services, and
overnight sessions (bounds, counters, current runbook pointer, stop reason;
never a task list). Not product databases, not managed-repo files.

Rows that belong to a project carry `project_id`. Switching projects does not
rewrite another project's history.

ENG-PC-09 runtime-service rows retain only the selected project/scope, declared
fixed argv, separately observed stable process argv, cwd,
PID/create-time/process-session identity, loopback port and preview URL,
health/ownership, append-only log pointer, timestamps, and exit state. An older
row with no observed process argv is not backfilled from its declaration and
cannot authorize a signal. Logs live under `<state dir>/runtime-services/`;
the database stores the relative pointer, not a duplicate raw log. A dead or
ambiguous process record is retained as evidence and cannot authorize a signal.
Runtime preview URLs are operational metadata, never test or acceptance
evidence.

Graphify cache (optional, derived): `<state dir>/graph-context/<project>/<worktree>/<tree>/`. It is
disposable; delete it freely. Each worktree directory also has an atomic `status.json` containing only its
latest lifecycle health/evidence. At most two completed tree directories are retained per worktree; hidden
staging builds are never pruned as completed caches. Tree identity, not age or `status.json`, decides whether a
graph is current. This derived cache is never a source of task or repository truth.

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
