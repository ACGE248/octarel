# ENG-PC-09 — managed runtime service ownership and previews

**Issue:** [#37](https://github.com/ACGE248/octarel/issues/37)
**Implemented:** 2026-10-02

## Research provenance

The design review used Paperclip commit
`cf8ad63c806685bfd7c48e3ed4a919d61a7c55f1` (2026-10-02), especially
`server/src/services/workspace-runtime.ts` and its runtime-service tests.
Octarel independently implements only the applicable concepts: durable process
identity, project-declared command/cwd/port, readiness and health, verified
process-group termination, port-collision refusal, append-only logs, and
non-authoritative preview metadata. No Paperclip code, package, runtime,
database, scheduler, or task model is copied or depended on.

## Contract and ownership

`control_plane.runtime_services.RuntimeServiceManager` is the only managed-app
supervisor. `AppLifecycleManager` is now a compatibility adapter over it, so the
legacy app endpoints and the generic runtime endpoints cannot disagree about
ownership or lifecycle state. A single re-entrant supervisor boundary serializes
threadpool reads/reconciliation with start, stop, restart, and shutdown, so an
observation captured before launch cannot later overwrite the launched identity.

The durable `runtime_services` row is scoped by `project_id` plus optional
`worktree_path` and `runbook_id`. It records only:

- the project-declared fixed argv, stable OS-observed process argv, validated
  cwd, loopback port/preview URL;
- PID, process create time, process-session id, health and ownership;
- owner, relative append-only log pointer, timestamps, reason, and exit code.

The selected project must declare `runtime_service_argv` as a JSON string list
and `runtime_service_port` as an unprivileged TCP port, with
`runtime_service_host` fixed to `127.0.0.1`. Existing
`app_lifecycle_command`/`app_lifecycle_port` declarations remain a bounded
compatibility input while registered projects migrate. In both cases the
process uses `shell=False`; shell/env/privilege wrappers, shell metacharacters,
public-bind arguments, and secret-bearing arguments are refused, and the
minimal child environment fixes `HOST=127.0.0.1`. The cwd is never supplied directly:
it is the selected project's registered root or a worktree/runbook relationship
validated against project-scoped durable state.

## Safety invariants

- Start binds operational intent to the selected project and exact scope. It
  refuses an occupied declared port and never kills the listener.
- Launch waits for process identity to remain stable across a bounded
  post-spawn window. It preserves declared argv as intent and permits observed
  argv to differ only by an absolute argv[0] replacement (the macOS framework
  Python normalization); every remaining argument must match exactly. If
  capture fails, the manager verifies and terminates only the new child's
  dedicated process group, then waits/reaps within the shared eight-second
  bound. The handle is removed only after confirmed exit; a timeout remains
  tracked and fails closed without force-kill. Before a later launch may reuse
  that service ID, the retained exact handle is checked: a live child blocks
  the launch without another signal, while an exited child is waited/reaped and
  removed before spawning its replacement.
- Stop/restart/shutdown require a live, exact match for PID, create time, cwd,
  the captured process argv, and process-session id, with the PID also the
  session leader. After verified `SIGTERM`, `STOPPED` is persisted only after
  the exact child handle is waited/reaped or durable observation confirms the
  process is no longer live, within the shared eight-second bound. When process
  facts are unavailable, the PID must disappear. Uninspectable live PIDs and
  timeouts fail closed; there is no force kill. Legacy rows without captured
  process argv fail closed.
- PID reuse, incomplete identity, changed argv/cwd/session, or an external
  listener is non-stoppable. A terminal row whose historical PID has been
  reused remains terminal evidence and cannot authorize a signal; when its
  port is free it does not block a fresh safe launch. There is no force-steal
  or executable-name path.
- Crash reconciliation preserves identity, exit state and the same append-only
  log pointer, then permits a fresh launch only when the port is free.
- Services and reads are filtered by selected project. Runbook scope must belong
  to that project and its worktree must match.
- The child environment reuses the existing managed-environment isolation. No
  provider/API call, credential injection, paid route, or public bind is added.
- `preview_url` is always loopback operational metadata and carries
  `preview_is_validation_evidence: false`.

## API and Control Center

- `GET /api/runtime-services` reconciles and returns selected-project local
  state only. Optional declared worktree/runbook scopes are safe projections,
  not launches.
- `POST /api/runtime-services/{service_id}/{start|restart|stop}` re-derives the
  scope and service id on the server. Restart and stop retain the established
  confirmation, remote-identity, CSRF/origin, and audit boundaries.
- `/api/app-status` and `/api/app-lifecycle/{action}` address the selected
  project's canonical runtime scope through the same manager.
- Generic and legacy stop APIs return identity, PID-reuse, signal, and timeout
  refusals unchanged. The legacy “no managed process” wording is used only when
  the requested scope truly has no durable service record.
- Run Detail and Worktree inspectors show health, ownership, PID/session,
  loopback preview metadata, log pointer, and the server-derived action set.
  External/unowned rows are informational; start/restart/stop/open-preview are
  disabled. There is no new top-level page.
- Polling reads local SQLite/process/loopback facts and never invokes a provider.

## Validation scope

Deterministic coverage owns schema migration, fixed-command allowlisting,
worktree/runbook validation, PID reuse, incomplete or changed observed argv,
post-spawn argv normalization, external-port protection, crash/restart evidence
preservation, multi-project isolation, selected-project API refusal, UI
ownership/action states, log/preview metadata, and the existing zero-provider
refresh loop. A focused real-process regression allocates a dynamic loopback
port, writes and launches a temporary Python loopback listener, waits for
`HEALTHY`, stops through the manager, and guarantees exact-child cleanup on
assertion failure. The browser fixture contains an explicit
`EXTERNAL_UNOWNED` row so unsafe controls are tested without binding a real
listener or signalling a process.

Focused Python tests, Python compilation, JavaScript syntax, JSON manifest
parsing, fixture construction, Playwright discovery, and the responsive browser
test are the intended validation set. Any environment that forbids loopback
binds or cannot resolve an absent local Playwright package must report those
checks blocked rather than passing them.
