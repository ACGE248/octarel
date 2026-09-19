# Octarel

Standalone orchestration control plane. Octarel owns **orchestration
mechanisms**. Managed repositories own their own product and repository truth.

Do not treat this repository as a second source of any managed project's
roadmap, ledger, or policy content. Publication history is a public-safe
snapshot; see `docs/PUBLICATION.md`.

## What it is

Octarel schedules, reviews, and records engineering work against one or more
**managed projects**. A project is a Git checkout plus optional GitHub remote.
Octarel reads that checkout's own policy, task sources, and validation command
on every request. It never copies those files into Octarel state.

The first managed project in the original extraction is typically OctaScene
(`ACGE248/octages`). A second repository does not need any OctaScene-specific
layout.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Install](docs/INSTALL.md)
- [Project registration](docs/PROJECTS.md)
- [Providers](docs/PROVIDERS.md)
- [Worktrees](docs/WORKTREES.md)
- [Testing and review](docs/TESTING.md)
- [Remote access](docs/REMOTE_ACCESS.md)
- [State, backup, restore](docs/STATE.md)
- [Releases, upgrades, migrations](docs/RELEASE.md)
- [Security and privacy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Publication](docs/PUBLICATION.md)
- [CPX-07](docs/engineering/ENG-CP-07.md)

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Octarel does **not** require an OctaScene application stack (`app/`, media
SDKs, product frontend).

Dashboard Playwright tests need Node only for `@playwright/test`:

```bash
npm install
```

## Entrypoints

```bash
python -m octarel version
python -m octarel health
python -m octarel providers
python -m octarel public-safety --tree-only
python -m octarel dashboard --port 8877
python -m octarel run --dry-run
python -m octarel project add --id example --path /path/to/checkout
python -m octarel project select example
python -m octarel migrate --from /path/to/octages/.orchestrator-state
python -m scripts.agents.orchestrator status
```

State lives in this checkout's `.orchestrator-state/` (or `OCTAREL_STATE_DIR`),
never in a managed project's tree unless you point it there on purpose.

## Register a project

```bash
python -m octarel project detect /path/to/checkout
python -m octarel project add --id myproject --path /path/to/checkout --name "My Project"
python -m octarel project select myproject
```

OctaScene, when used, is still registered by local checkout path — never by
copying that repository's files into Octarel:

```bash
export OCTAREL_OCTASCENE_ROOT=/path/to/octages-checkout
python -m octarel dashboard
```

Or use the Control Center **Add Project** flow.

## Tests

```bash
.venv/bin/python -m pytest -q
npx playwright test --config scripts/agents/control_plane/playwright.config.js --project=desktop-1280
python3 scripts/ci/local_gate.py --docs-reviewed
```

Exact-tree local-gate evidence is the engineering acceptance authority. GitHub
Actions is not.

## Remote access

Bind only `127.0.0.1`. Optional Cloudflare Tunnel + Access can expose a
hostname such as `dev.octascene.com` to that loopback port. See
[docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md).

## License

MIT. See `LICENSE`, `NOTICE`, and `THIRD_PARTY.md`.
