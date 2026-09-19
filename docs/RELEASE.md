# Releases, upgrades, and migrations

## Versioning

Octarel uses **SemVer** from `octarel/__init__.py` and `pyproject.toml`.

- `0.y.z` — pre-1.0 public hardening; breaking changes may occur in `0.y`.
- First intended public tag: `v0.1.0` **only after** every CPX-07
  publication gate in `docs/PUBLICATION.md` is green.

Do not create a Git tag until those gates pass.

## Changelog

User-visible changes go in `CHANGELOG.md` (Keep a Changelog). Version and
release-note churn is required for an explicit release, not for every
feature commit.

## First public release procedure

1. Merge the CPX-07 candidate through the normal review flow.
2. Record the exact pre-public SHA.
3. Re-run `python -m octarel public-safety --publication-gate`.
4. If history is still unsafe, build `scripts/ci/prepare_public_history.py`
   output and use **that** tree as the public `main` (never publish the
   private email/path history).
5. Confirm README, description, sample config, issues, and PRs are public-safe.
6. Confirm clean-room, two-repo, browser, and exact-tree evidence.
7. Confirm independent review READY on that exact SHA.
8. Only then change GitHub visibility and tag `v0.1.0`.

## Upgrade between Octarel versions

1. Backup `.orchestrator-state/`.
2. Update the checkout (`git pull` or a new clone).
3. `pip install -r requirements-dev.txt`.
4. `python -m octarel health`.
5. Schema migrations run when `State` opens (additive SQLite).

## Rollback

- **Code:** check out the previous tag/SHA; keep the same state dir.
- **State:** restore the backup directory while the dashboard is stopped.
- **OctaScene hostname cutover:** `python -m octarel cutover rollback-plan`
  (embedded Octages daemon only with
  `OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK=1`).

## Compatibility

- Octarel talks to managed repos through `ProjectContract`. A generic repo
  does not need OctaScene files.
- Managed-repo policy/validation stays in that repo. Octarel upgrades must
  not require copying product truth.

## Release verification checklist

- [ ] `python -m octarel version` matches the tag
- [ ] LICENSE / NOTICE / THIRD_PARTY.md
- [ ] public-safety tree + history
- [ ] clean-room script
- [ ] two-repository tests
- [ ] `python -m octarel providers` (local)
- [ ] Control Center Playwright (full relevant matrix for UI/release)
- [ ] exact-tree local gate with independent review
- [ ] no Git tag until the above pass
