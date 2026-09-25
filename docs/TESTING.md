# Testing and review

Risk selects evidence. Opening a PR does not automatically require the full
suite. Policy: `.agents/core/TESTING.md`.

| Tier | When |
|---|---|
| T0 | prose / tiny config |
| T1 | ordinary bounded implementation |
| T2 | shared subsystem |
| T3 | releases, migrations, gates, broad refactors, program slices |

The authoritative command is:

```bash
python3 scripts/ci/local_gate.py --docs-reviewed
```

High/critical risk also requires independent read-only review of the **exact
candidate tree**, from a provider that is not the implementer. GitHub Actions
is not acceptance evidence.

Control Center browser tests:

```bash
npx playwright test --config scripts/agents/control_plane/playwright.config.js
```

Use `--project=desktop-1280` for a focused run. The full viewport matrix is
the release/UI-audit evidence.

Never delete, skip, `xfail`, or weaken a legitimate test to pass a gate.

## Dependency reuse is fail-closed

The local gate and review provisioning reuse an existing `node_modules` only when its lockfile fingerprint
matches **and** `scripts/ci/environment.py::dependency_health` passes. The health contract is derived from the
managed project's own `package.json` (every declared dependency installed, every declared executable linked in
`node_modules/.bin`, npm's `.package-lock.json` completed-install record present); Octarel names no
project-specific package. A fingerprint hit on an incomplete tree is reported with `install_state:
"stale_incomplete"` and the exact problems, then repaired with the canonical `npm ci --ignore-scripts` under a
cross-process install lock kept in the worktree's git dir (ENG-AO-07's runbook lease still serialises
advancement; the lock also covers non-runbook callers). Tracked source is hashed before/after the install and a
change fails the repair; a failed or still-incomplete repair fails gate preflight. Per-root results
(`install_state`, `reason`, `health`, `source_tree_unchanged`) are recorded in the existing gate evidence
(`environment.bootstrap`).
