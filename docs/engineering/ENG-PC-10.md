# ENG-PC-10 — typed approvals and decision handoffs

**Issue:** [#38](https://github.com/ACGE248/octarel/issues/38)
**Implemented:** 2026-10-02

## Research provenance and attribution

The architectural review was pinned to Paperclip commit
`806af230b689717bfc3f0858c0a70ed939c82db4`. The review used its typed approval
records and explicit resolution lifecycle only as research input. Octarel's
implementation is independent: no Paperclip code, schema, package, runtime,
database, scheduler, task truth, or UI source was copied or introduced. The
result remains an Octarel-owned contract over the existing state, event,
operation, remote-authentication, and Glass Studio surfaces.

## Durable contract

`control_plane.approvals` owns exactly five operator-decision classes:

| Server-derived action | Risk | Allowlisted effect |
|---|---|---|
| `DESTRUCTIVE_CLEANUP` | `HIGH` | Re-run the existing `worktree_cleanup` command with confirmation; only worktrees still proven `FINISHED_CLEAN` may be removed. |
| `METERED_OVERFLOW_ROUTE` | `CRITICAL` | Apply the existing one-additional-invocation `usage_override` contract. This does not enable an API key, paid overflow, or a disabled/cost-blocked provider. |
| `MATERIAL_SCOPE_CHANGE` | `HIGH` | Record the bounded scope decision as typed approval/run evidence; execute no command. |
| `AMBIGUOUS_PRODUCT_ARCHITECTURE_CHOICE` | `MEDIUM` | Record the product/architecture choice as typed approval/run evidence; execute no command. |
| `REMOTE_SENSITIVE_ACTION` | `HIGH` | Call the existing runtime-service supervisor for a server-validated `start`, `stop`, or `restart`; the supervisor re-proves project scope and process ownership. |

The SQLite `approval_requests` row persists project, optional task/run,
server-owned action/risk, safe impact summary, the class-specific validated
payload, reason, requester, expiry, lifecycle state, immutable state
fingerprint/revision, resolver/note, timestamps, and a bounded result summary.
The request contract has an update trigger that makes attribution,
classification, payload, reason, expiry, and fingerprint immutable. A second
trigger forbids deletion because resolved requests are durable audit evidence.
Only lifecycle and resolution/result fields may change.

The execution payload is never returned by the dashboard API. The UI consumes
only `safe_payload_summary`; arbitrary command text, client-supplied project or
identity, `safe`, `destructive`, and `risk` fields are rejected rather than
ignored. Text is bounded and refused when redaction would be required, so a
secret-, credential-, or absolute-host-path-shaped operator decision never
reaches either the durable payload or the safe summary. Runtime scope paths are
not operator prose: the server re-derives and stores the selected-project
worktree identity only in the non-public execution payload and fingerprint.

## Resolution and changed-state revalidation

Resolution follows one fail-closed path:

1. The remote middleware first enforces authenticated allowlisted identity,
   same-origin/CSRF, and rate limits for a remote state-changing request.
2. The server resolves the selected project and rereads the request. Another
   project's request is indistinguishable from a missing request.
3. For approval, the server reconstructs the typed plan from the immutable
   payload and current project/task/run/action-specific state. Classification,
   risk, impact, handler eligibility, and the SHA-256 state revision are all
   re-derived; the client supplies none of them.
4. `BEGIN IMMEDIATE` atomically changes exactly one `PENDING` request to
   `REJECTED`, `EXPIRED`, `STALE`, or `EXECUTING`. The `WHERE state =
   'PENDING'` compare-and-swap makes concurrent/double resolution explicit.
5. Only an `EXECUTING` row reaches the fixed handler map. Success finalizes it
   as `APPROVED`; a handler refusal becomes `FAILED_SAFE`. A crash cannot make
   the request pending again or authorize a second execution.

The handler boundary repeats the destructive/current-state proof after that
claim. Cleanup carries a non-public, server-derived project id, resolved
project root, and exact eligible path set into `cleanup_worktrees`; a project
switch, target-set change, dirty checkout, or failed removal becomes
`FAILED_SAFE` and the recorded result lists removals and failures. The premium
route compares its approved runbook/usage snapshot and writes both rows inside
one `BEGIN IMMEDIATE` transaction, so a concurrent mutation cannot land
between comparison and effect.

Expiry and stale-state failures are terminal. The operator creates a fresh
request from current server state instead of reviving old authority. Every
request and resolution writes the existing ENG-PC-04 `RunEvent` envelope with
event class `approval`; there is no second history store. Remote changes also
use the existing verified-identity audit ledger.

## Non-waivable safeguards

An approval is a typed decision, not a policy waiver. It cannot:

- store or expose credentials, inject environment variables, or execute a
  submitted command;
- enable a provider, API key, paid fallback, premium overflow, or bypass a
  provider cost block, usage budget, capability, permission, sandbox, or
  authorization check;
- weaken worktree/process ownership proof, cross project boundaries, or signal
  an external/unverified process;
- replace deterministic test, review, exact-tree gate, or merge policy.

The route decision deliberately delegates to the existing bounded usage
override and leaves provider state unchanged. The cleanup and runtime handlers
re-run their operation-layer proof at execution time even after the approval
fingerprint matched.

Schema version 7 creates the approval table and triggers through the ordinary
idempotent schema bootstrap. The older one-time project-adoption migration is
gated by its dedicated `legacy_project_migration` marker rather than the
general schema version, so upgrading a version-6 database cannot relabel a
later invalid `project_id IS NULL` row as OctaScene state.

## API and Control Center

- `GET /api/approvals` returns selected-project safe projections and may filter
  by run for the shared Run Detail inspector. Reads expire overdue pending rows
  locally and never schedule work or contact a provider.
- `POST /api/approvals` validates one fixed action payload, derives actor from
  the verified remote identity or local control boundary, and creates a pending
  request.
- `POST /api/approvals/{id}/resolve` accepts only `APPROVE`/`REJECT` plus a
  required resolution note. It revalidates and resolves through the atomic path
  above.
- A confirmed Control Center worktree cleanup keeps the existing server preview
  as its impact source, then creates `DESTRUCTIVE_CLEANUP`. Creation removes no
  checkout; only a later approved resolution can call the existing cleanup
  command, which re-runs its finished-clean/canonical/ownership protections.
  The unconfirmed command remains a read-only preview; a refused preview is
  rendered as a refusal rather than as an empty target set.
- The Control Center Premium override creates `METERED_OVERFLOW_ROUTE` and
  leaves runbook and usage-governance state untouched. A later approved
  resolution alone reaches the existing bounded `usage_override` handler.
- `/api/steering/execute` routes crafted `usage_override` and
  `worktree_cleanup` verbs through those same confirmation, field-validation,
  and typed-request handoffs; neither verb can fall through to direct command
  execution from steering. Other parsed steering verbs retain their existing
  behavior.
- Generic runtime-service and legacy app-lifecycle `start`/`stop`/`restart`
  endpoints detect a verified authenticated remote identity and create
  `REMOTE_SENSITIVE_ACTION`. They do not launch or signal a process at request
  creation. Resolution revalidates the selected project, service scope, action
  eligibility and process identity before using the existing supervisor.
- Attention shows each pending request with risk, reason, expiry, impact
  preview, note field, and approve/reject controls. Run Detail shows related
  pending and terminal request/resolution evidence, including failed cleanup
  outcome details. A bounded recent-results projection keeps failed unscoped
  cleanup targets and reasons visible on Overview without inflating the bell's
  active-attention count. Advancement-derived Overview rows cannot displace
  approval controls from the Attention popover. Run Detail preserves a pending
  resolution-note draft and focused approval control across its two-second
  polling rebuild. Both extend the existing responsive Glass Studio structure;
  no approval page or history store was added.

Ordinary trusted-local runtime actions intentionally retain their existing
deterministic confirmation and supervisor safeguards, so normal local work is
not burdened with approvals. The command module also remains a direct
trusted-local CLI boundary; it is not remotely exposed by this contract. The
Control Center API is the production initiation boundary that converts cleanup
and premium-route decisions into durable requests before those internal
handlers can run.

## Validation

Deterministic coverage includes schema/immutability, the exact five-class
allowlist, client-flag rejection, redaction and safe summaries, expiry,
changed-state invalidation, concurrent/double resolution, rejection,
post-claim project/state changes, schema-upgrade non-readoption, failed
destructive execution, cross-project isolation, verified remote identity/audit,
hard-safeguard non-bypass, API projections, RunEvent continuity, Attention
actions, Run Detail resolution history, responsive layout, and the existing
state/dashboard/runtime-service contracts. No validation path invokes an AI
provider or a billable route.

Initial candidate evidence on 2026-10-02:

- All 224 focused approval, remote-access, dashboard API, and runtime-service
  tests passed outside the implementation worker's restricted sandbox,
  including the three real-loopback socket cases.
- All 104 fixture-reset, agent-policy, Manager Chat, and deterministic steering
  tests passed.
- The final approval-only regression rerun passed all 23 tests.
- Python compilation, JavaScript syntax, coverage-manifest JSON parsing,
  focused Ruff checks, and whitespace/diff validation passed.
- The no-network environment preparation reused the repository's local npm and
  browser caches (`network_install_performed=false`). The focused Playwright
  approval spec passed both desktop-1280 tests: request resolution persisted in
  Run Detail, and Premium override created a pending request without claiming
  routing had already changed.

Post-review focused evidence on 2026-10-05:

- All 176 approval, usage-policy, project-registry, and remote-dashboard tests
  passed after the handler-bound revalidation fixes.
- Ruff, JavaScript syntax, and the six-test desktop-1280 approval browser
  slice passed, including preview refusal, terminal cleanup visibility, and
  advancement/Attention displacement and Run Detail polling regressions.
