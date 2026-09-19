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
