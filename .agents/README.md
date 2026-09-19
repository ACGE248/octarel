# Agent policy index

This directory is the canonical provider-neutral policy library for repository work.
`AGENTS.md` is the universal entrypoint; compose only the policies needed for the assigned task.

- `core/` owns cross-role security, Git/worktree, testing, documentation, and cost/provider safety.
- `roles/` defines provider-independent responsibilities and permissions.
- `workflows/` defines reusable procedures.
- `providers/` contains provider-specific execution differences only.

The deterministic loader in `scripts/agents/policy.py` composes policies in this order:

1. `AGENTS.md`
2. relevant core policies
3. one role policy
4. one workflow policy when applicable
5. one provider policy
6. bounded task/program/ADR contracts
7. task acceptance criteria

Provider-native files and directories are adapters only. They are never required for orchestrated
correctness. Historical implementation evidence remains historical and does not override this index.
