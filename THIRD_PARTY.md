# Third-party licenses

Runtime and development dependencies used by Octarel. Vendor copies of
dashboard assets are also listed in
`scripts/agents/control_plane/dashboard/ASSET_LICENSES.md` and `NOTICE`.

This inventory is for redistribution review. It is not legal advice.

## Python runtime (`requirements.txt`)

| Package | Use | License (upstream) |
|---|---|---|
| fastapi | Control Center HTTP API | MIT |
| uvicorn | ASGI server | BSD-3-Clause |
| httpx | HTTP client | BSD-3-Clause |
| psutil | process/health inspection | BSD-3-Clause |
| PyJWT | Cloudflare Access JWT checks | MIT |

## Python development (`requirements-dev.txt`)

| Package | Use | License (upstream) |
|---|---|---|
| pytest | tests | MIT |
| ruff | lint | MIT |

## Node development (`package.json`)

These are **development-only**. Octarel is not published to npm
(`package.json` `"private": true`).

| Package | Use | License (upstream) |
|---|---|---|
| @playwright/test | Control Center browser tests | Apache-2.0 |
| @axe-core/playwright | accessibility audit helper | MPL-2.0 |
| @xterm/xterm | terminal widget (vendored copy served locally) | MIT |
| simple-icons | provider marks (vendored SVG, CC0) | CC0-1.0 |

MPL-2.0 (`@axe-core/playwright`) applies to the devDependency as used by
Playwright tests. It is not shipped as a runtime dashboard asset.

## Vendored dashboard files

- `scripts/agents/control_plane/dashboard/vendor/xterm/` — @xterm/xterm 6.0.0, MIT
- `scripts/agents/control_plane/dashboard/assets/icons/` — Simple Icons 16.5.0, CC0-1.0

Provider marks remain subject to the respective providers' trademark rules.
Nothing in this repository grants a trademark license.
