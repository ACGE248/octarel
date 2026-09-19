# Changelog

All notable changes to Octarel are documented in this file.

## [Unreleased]

### Added

- CPX-07 public-safety scanner, clean-room acceptance, and public-history snapshot helper
- `octarel project`, `octarel providers`, `octarel public-safety`, and `octarel service canonicalize-state`
- Maintained install, architecture, provider, release, state, and publication docs

### Changed

- Control Center Overview: grouped the sidebar's 12 flat nav items into Monitor/Work/Configure/System sections, added connector chevrons between Live Workflow pipeline stages on desktop/tablet, collapsed the Provider Usage row's not-exposed tiles into one summary chip, added a blocked-status border accent to the status cards, and added a leading icon to the steering command bar. No API, data, or route changes.

### Fixed

- Write the launchd dashboard pid file under `~/Library/Application Support/Octarel/` (boot volume). launchd cannot write onto an external-volume state directory; loopback health remains the listener authority.

### Security

- Public history is produced with `scripts/ci/prepare_public_history.py` as a fresh `git init` of `git archive HEAD`, so unpublished merge-commit emails and historical operator paths never enter the public object store.

## [0.1.0] — unreleased

First intended public tag. Create `v0.1.0` only after every gate in
`docs/PUBLICATION.md` is green on the public-safe snapshot.
