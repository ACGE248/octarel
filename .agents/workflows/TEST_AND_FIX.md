# Test and fix workflow

1. Infer and reproduce the smallest relevant deterministic check from the task/diff.
2. Diagnose the root cause and distinguish pre-existing failures before proposing a repair.
3. If write-authorized, make only the focused understood fix; otherwise report it.
4. Rerun the invalidated focused evidence and expand to subsystem/full scope only when the risk policy requires.
5. Report exact checks, failures, cause, repair, files changed, and remaining risk. Do not commit or push unless
   separately authorized.

Use `python3 scripts/ci/local_gate.py --dry-run ...` before an expensive gate. Record `--rerun-reason` for a
repeat; the gate may reuse only phases whose exact command, environment, and relevant-input fingerprints still
match. A missing dependency, reviewer route, invalid DAG, or browser runtime blocks before long tests begin.
