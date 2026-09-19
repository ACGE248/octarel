# Contributing

1. Read `AGENTS.md` and the indexed policies under `.agents/`.
2. Do not write to `main`. Use a dedicated branch and worktree.
3. Exactly one write-capable owner per checkout.
4. Keep Octarel's code root distinct from every managed project.
5. Never commit secrets, `.env` files, SQLite, or operator paths.
6. Do not silently enable API/paid fallback.
7. Run focused tests, then the risk-selected
   `python3 scripts/ci/local_gate.py --docs-reviewed`.
8. Independent review of high-risk changes must use a different provider
   than the implementer.

Managed-project product work belongs in that project, not here.
