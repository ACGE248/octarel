# Changelog

All notable changes to Octarel are documented in this file.

## [Unreleased]

### Added

- CPX-07 public-safety scanner, clean-room acceptance, and public-history snapshot helper
- `octarel project`, `octarel providers`, `octarel public-safety`, and `octarel service canonicalize-state`
- Maintained install, architecture, provider, release, state, and publication docs
- OCTAREL-OPS-02: after an accepted run, Octarel re-reads the managed project's current repository truth and records the next eligible task (or the exact reason advancement stopped: blocked, no eligible task, owner decision required). Shown on the Runs view; `auto_advance=true` project capability opts in to starting it automatically; `POST /api/runbooks/{id}/advance` re-evaluates.
- OCTAREL-TEST-01: `npm run test:dashboard:matrix` (`scripts/ci/playwright_matrix.py`) runs the seven Control Center viewport projects as isolated bounded-parallel lanes (default concurrency 3, `OCTAREL_PLAYWRIGHT_MATRIX_CONCURRENCY`); the local gate's Control Center phase uses it. Fixed the Agent Activity viewer for symlinked repo roots, made its specs order-independent, and raised modal sheets above the sidebar so they are clickable at 768px.
- OCTAREL-UI-02: Control Center polish pass. Overview now shows the controlled project, an Active Work panel (task, stage, agents, elapsed, branch, next stage, blocking reason) and a Needs-attention list using one glyph+label language (owner decision, auth, provider, blocked, failed test/review, quota, stale repository); Live Workflow gains a Plan→Implement→Test→Review→Integrate phase track and non-colour-only stage states; Providers are compact rows (auth, availability with reason, model, role, usage, fallback); the Agent Activity viewer is a log console (line numbers, stderr/warn markers, follow-tail, search count) with structured Details/Evidence; sidebar selection, contrast, focus and reduced-motion refined. No backend or API changes.

### Changed

- Control Center Overview: grouped the sidebar's 12 flat nav items into Monitor/Work/Configure/System sections, added connector chevrons between Live Workflow pipeline stages on desktop/tablet, collapsed the Provider Usage row's not-exposed tiles into one summary chip, added a blocked-status border accent to the status cards, and added a leading icon to the steering command bar. No API, data, or route changes.

### Fixed

- Write the launchd dashboard pid file under `~/Library/Application Support/Octarel/` (boot volume). launchd cannot write onto an external-volume state directory; loopback health remains the listener authority.

### Security

- Public history is produced with `scripts/ci/prepare_public_history.py` as a fresh `git init` of `git archive HEAD`, so unpublished merge-commit emails and historical operator paths never enter the public object store.

## [0.1.0] — unreleased

First intended public tag. Create `v0.1.0` only after every gate in
`docs/PUBLICATION.md` is green on the public-safe snapshot.
