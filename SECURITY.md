# Security and privacy

## Secrets

Never commit API keys, tokens, cookies, private keys, `.env` files, or
Cloudflare Access identifiers. Scan before publication:

```bash
python -m octarel public-safety --tree-only
python -m octarel public-safety --publication-gate
```

Redaction of worker logs is implemented in `scripts/agents/redaction.py`.

## Network

The Control Center binds `127.0.0.1` only. Remote access must sit behind
Cloudflare Access (or equivalent). See `docs/REMOTE_ACCESS.md`.

## Data

Octarel state is local SQLite. It must not contain managed-project product
databases or secrets. Do not copy OctaScene private evidence into this
repository.

## Providers

Local CLI subscription sessions only. API-key billing and silent paid
fallback are prohibited. See `docs/PROVIDERS.md` and
`.agents/core/COST_AND_PROVIDER_SAFETY.md`.

## Reporting

Open a private report to the repository owner. Do not file public issues that
include secrets.
