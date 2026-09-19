# Install

## Requirements

- Python 3.11+
- Node.js only if you run Control Center Playwright tests
- Git

No OctaScene application dependencies are required.

## Bootstrap

```bash
git clone https://github.com/ACGE248/octarel.git
cd octarel
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m octarel version
.venv/bin/python -m octarel health
```

Optional dashboard tests:

```bash
npm install
npx playwright test --config scripts/agents/control_plane/playwright.config.js --project=desktop-1280
```

## First start

```bash
.venv/bin/python -m octarel dashboard --host 127.0.0.1 --port 8877
```

The process binds loopback only. Do not pass a non-loopback host.

State is created at `.orchestrator-state/orchestrator.db` unless
`OCTAREL_STATE_DIR` is set.

## macOS launchd (operator machine)

```bash
export OCTAREL_CODE_ROOT=/path/to/octarel
python -m octarel service install-launchd
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist
python -m octarel service status
```

The plist is local and must never be committed with secrets. Cloudflare Access
identifiers stay in that local plist.

## Clean-room check

```bash
python3 scripts/ci/clean_room_acceptance.py --source .
```

This clones into a new directory, creates a new venv and `node_modules`, and
proves CLI + dashboard + project add without the development caches.
