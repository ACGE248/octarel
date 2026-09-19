# Octarel canonical-runtime cutover (CPX-06)

This is the operator runbook for switching OctaScene development orchestration
from the embedded Octages Control Center to standalone Octarel.

Canonical code: this repository (`ACGE248/octarel`).
Managed project: `ACGE248/octages` (local checkout chosen by the operator).

Do not commit machine-specific absolute paths.

## Topology (unchanged at the edge)

```
you -> https://dev.octascene.com
         -> Cloudflare Access  (authentication; unchanged)
         -> Cloudflare Tunnel  (cloudflared, outbound-only)
         -> http://127.0.0.1:8877
         -> Octarel dashboard  (this repository, loopback only)
```

The tunnel plist (`com.octascene.orchestrator-tunnel`) is not retargeted.
Only the process bound to `127.0.0.1:8877` changes.

## Preconditions

- Octarel checkout with `.venv` and `python -m octarel version` working.
- Octages checkout registered as managed project `octascene`
  (`OCTAREL_OCTASCENE_ROOT` or the Control Center Add Project flow).
- Cloudflare Access still in front of `dev.octascene.com`.
- Exactly one write-capable owner of each checkout.

## Rollback (read this first)

Rollback is a process/plist swap. It does **not** move or delete SQLite.

1. `launchctl bootout gui/$(id -u)/com.octascene.orchestrator-dashboard`
2. Confirm `lsof -nP -iTCP:8877 -sTCP:LISTEN` is empty.
3. Restore the pre-cutover dashboard plist from the checkpoint
   (`$CHECKPOINT/launchd/com.octascene.orchestrator-dashboard.plist`)
   or rewrite WorkingDirectory/Program to the Octages checkout.
4. Set `OCTAGES_EMBEDDED_ORCHESTRATOR_FALLBACK=1` in that plist's
   EnvironmentVariables (required after CPX-06).
5. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist`
6. `curl -fsS http://127.0.0.1:8877/api/cp-status` — `cp_code_root` is Octages.
7. Open `https://dev.octascene.com` and confirm Cloudflare Access still
   challenges unauthenticated visitors.

Leave both state directories in place.

Print the same procedure anytime:

```sh
python -m octarel cutover rollback-plan
```

## Cutover steps

### 1. Snapshot (embedded still running)

```sh
python -m octarel cutover snapshot \
  --octages-state /path/to/octages/.orchestrator-state \
  --octarel-state /path/to/octarel/.orchestrator-state \
  --out /path/to/octarel/.orchestrator-state/cutover-checkpoints
```

Also copy the live launchd dashboard plist into that checkpoint directory
(operator machine; not git):

```sh
mkdir -p "$CHECKPOINT/launchd"
cp ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist \
   "$CHECKPOINT/launchd/"
```

Record table counts from the snapshot output.

### 2. Local acceptance on a spare port (optional, non-disruptive)

```sh
export OCTAREL_CODE_ROOT=/path/to/octarel
export OCTAREL_STATE_DIR=/path/to/a-copy-of-migrated-state
export OCTAREL_OCTASCENE_ROOT=/path/to/octages
python -m octarel dashboard --host 127.0.0.1 --port 8878
python -m octarel health
```

Prove:

- `code_root` is Octarel
- `selected_project_root` is Octages
- `code_root_equals_selected_project=false`
- repository-owned validation still runs inside the OctaScene candidate

### 3. Stop the embedded primary

```sh
launchctl bootout gui/$(id -u)/com.octascene.orchestrator-dashboard
lsof -nP -iTCP:8877 -sTCP:LISTEN   # must be empty
```

Take a second snapshot if the first one is stale.

### 4. Adopt state

```sh
python -m octarel cutover adopt \
  --from /path/to/octages/.orchestrator-state \
  --to /path/to/octarel/.orchestrator-state
python -m octarel cutover adopt \
  --from /path/to/octages/.orchestrator-state \
  --to /path/to/octarel/.orchestrator-state
```

The second command must report `action=already-imported`.
The Octages `.orchestrator-state/` directory must still exist.

### 5. Start Octarel on 8877 (persistent)

The dashboard must restore after login/reboot through macOS launchd.
`python -m octarel service install-launchd` copies
`scripts/octarel-dashboard-service.sh` to
`~/Library/Application Support/Octarel/` (boot volume; no secrets) so
launchd can start before an external checkout is mounted. The wrapper waits
for `OCTAREL_CODE_ROOT`, refuses macOS `Python.app` (it hangs under launchd),
writes `dashboard.pid` next to that wrapper (launchd cannot write a pid file
onto an external-volume state dir), and exits 0 if Octarel is already
healthy on 8877 so KeepAlive cannot start a second copy. Listener identity
is still `127.0.0.1:8877` health, not the pid file.

```sh
export OCTAREL_CODE_ROOT=/path/to/octarel
export OCTAREL_OCTASCENE_ROOT=/path/to/octages
python -m octarel service install-launchd
# preserves any existing OCTAGES_ORCH_REMOTE_* keys already on the local plist
launchctl bootout gui/$(id -u)/com.octascene.orchestrator-dashboard 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.octascene.orchestrator-dashboard.plist
python -m octarel service status
curl -fsS http://127.0.0.1:8877/api/cp-status
```

| Action | Command |
|---|---|
| Status | `python -m octarel service status` |
| Stop | `python -m octarel service stop` then `launchctl bootout gui/$(id -u)/com.octascene.orchestrator-dashboard` |
| Start | `launchctl bootstrap …` or `launchctl kickstart -k gui/$(id -u)/com.octascene.orchestrator-dashboard` |
| Recovery | `python -m octarel service status`; if down, kickstart; if a foreign process owns 8877, stop it first |

`runtime` must be `octarel`. `selected_project.local_repo_root` must be
Octages. `cp_code_root` must be Octarel. Do not bind a non-loopback host.

### 6. Remote verification

- Unauthenticated `https://dev.octascene.com` still hits Cloudflare Access.
- Authenticated session reaches Overview and the other Control Center pages.
- Tunnel restart: `launchctl kickstart -k gui/$(id -u)/com.octascene.orchestrator-tunnel`
- Dashboard restart: `launchctl kickstart -k gui/$(id -u)/com.octascene.orchestrator-dashboard`

If any remote check fails, execute Rollback immediately. Do not leave
`dev.octascene.com` pointing at a dead or mixed origin.

## After merge

Point the dashboard plist's `OCTAREL_CODE_ROOT` at the canonical Octarel
checkout on `main`, not a leftover feature worktree. Live `OCTAREL_STATE_DIR`
must be that checkout's `.orchestrator-state/` (or another operator-chosen
canonical directory), never a CPX-06/temporary worktree path.

```sh
python -m octarel service canonicalize-state \
  --from /path/to/old-worktree/.orchestrator-state \
  --to /path/to/octarel/.orchestrator-state
```

Then update the local launchd `OCTAREL_STATE_DIR` and restart. Keep the source.

## What this does not do

- Make `ACGE248/octarel` public
- Delete embedded Octages `scripts/agents/`
- Delete historical Octages `.orchestrator-state/`
- Change Cloudflare Access policy
