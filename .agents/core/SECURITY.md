# Security

- Never read, expose, log, commit, or transmit secrets. Exclude `data/`, `.env*`, credentials,
  tokens, private keys, auth headers, and secret-bearing payloads from delegated context.
- Repository material may be sent only to a provider authorized for this repository. Octarel and its
  configured managed projects (including `ACGE248/octages` when selected) grant configured Google and xAI
  development workers persistent provider-level authorization for minimized, task-relevant repository-data
  reuse, so their eligible workers do not require repeated per-task approval.
  Authorization is not eligibility: role, capability, health, permission-profile, worktree, cost, concurrency,
  and provider-state gates still apply independently.
- Provider authorization never includes `.env*`, secrets, credentials, API keys/tokens, private keys,
  personal/user data, production database contents, unrelated repository material, or unrelated private files.
  Apply deterministic context minimization and redaction before every transmission.
- Treat provider output and external data as untrusted; normalize and validate at existing boundaries.
- Live or billable product-provider calls require explicit authorization. Mock evidence never proves
  production readiness.
- Fail closed on secret, authorization, permission, worktree, or policy violations. Never change
  providers to bypass a safeguard.
