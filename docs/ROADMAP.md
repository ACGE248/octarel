# Octarel roadmap

This file is the canonical product/engineering roadmap for **Octarel itself**. It does not copy or replace the roadmap, ledger, ADRs, or task truth of any managed project. Current repository code and `AGENTS.md` override historical planning notes.

## Current direction

Octarel is a standalone, multi-repository development orchestration control plane. Its differentiators remain:

- managed-repository truth is read from the selected checkout rather than copied into Octarel;
- local/subscription-backed workers are preferred and silent paid/API fallback is prohibited;
- one-writer/worktree safety, provider-diverse review, exact-tree acceptance and auditable evidence remain first-class;
- the Control Center is a truthful operator surface: unavailable facts stay `UNKNOWN`, `NOT_EXPOSED`, `NOT_REPORTED`, or explicitly `DERIVED`;
- orchestration mechanisms may evolve, but Octarel must not become a second source of managed-project product truth.

## Active UI program

- **#23 OCTAREL-UI-04** — Stitch Glass Orchestration Studio integration.
- **#24 OCTAREL-UI-05** — real orchestration-backed Manager Chat.
- **#25 OCTAREL-UI-06** — truthful model/session usage, context and cost telemetry.
- **#26 OCTAREL-UI-07** — Graphify repository-intelligence status.

The Paperclip-derived program below must compose with these tasks. In particular, ENG-PC-05 extends #25; it must not create a competing telemetry surface.

---

# ENG-PC — Paperclip-derived orchestration hardening

**Parent:** [#28 ENG-PC-00](https://github.com/ACGE248/octarel/issues/28)  
**Research date:** 2026-09-26  
**External reference:** [paperclipai/paperclip](https://github.com/paperclipai/paperclip)

## Why this program exists

Paperclip is a general agent-company control plane, while Octarel is a repository-aware software-development orchestrator. Paperclip contains several durable execution mechanisms worth adapting, but adopting Paperclip itself would introduce overlapping schedulers, task databases, histories, approvals and sources of truth.

The program therefore borrows **implemented architectural patterns**, not Paperclip's product metaphor or runtime. Paperclip is a research reference only; it is not a dependency or authority. Every implementation task must re-check current upstream code and pin the Paperclip commit SHA actually reviewed in its implementation notes/PR because upstream paths and contracts can change.

## Explicit non-goals

- No Paperclip runtime/package/database dependency.
- No CEO/company/org-chart abstraction.
- No Paperclip issue database as managed-project task truth.
- No weakening of Octarel worktree locks, runbook advancement leases, provider-diverse review or exact-tree gates.
- No silent API-key, metered, premium or overflow fallback.
- No invented dollar value for subscription-backed CLI usage.
- No second run history, scheduler, telemetry store, or settings authority when an Octarel owner already exists.

## Research map

Future implementers should begin from the Paperclip repository and the listed conceptual/code areas, then record exact current paths and upstream commit SHA before coding.

| Paperclip mechanism | Upstream starting point | Octarel adaptation |
|---|---|---|
| Agent heartbeat protocol, atomic task checkout, compact context | [heartbeat protocol](https://github.com/paperclipai/paperclip/blob/master/docs/guides/agent-developer/heartbeat-protocol.md) and current server issue/heartbeat services | ENG-PC-01, 03, 06, 07 |
| Agent adapters and task-scoped runtime/session state | [packages/adapters](https://github.com/paperclipai/paperclip/tree/master/packages/adapters), [architecture](https://github.com/paperclipai/paperclip/blob/master/docs/start/architecture.md) | ENG-PC-02, 11 |
| Run/event/log model | current Paperclip server run/event services and run-detail UI | ENG-PC-04 |
| Cost events and budgets | [costs and budgets](https://github.com/paperclipai/paperclip/blob/master/docs/guides/board-operator/costs-and-budgets.md), current server cost/budget services | ENG-PC-05 |
| Configuration revisions/rollback | current Paperclip agent/config revision services and UI | ENG-PC-08 |
| Runtime/dev-server ownership | current Paperclip runtime/workspace services | ENG-PC-09 |
| Typed approvals | current Paperclip approval schema/service/UI | ENG-PC-10 |
| Plugins/capability gating | current Paperclip plugin/host-service architecture | research input to ENG-PC-11; no plugin marketplace task until a concrete Octarel need exists |
| Secrets | current Paperclip scoped-secret architecture | research reference only; Octarel continues minimal-env/redaction and should create a separate security task only if secret storage becomes necessary |

### Paperclip ideas deliberately not scheduled

Paperclip's organization hierarchy, CEO/manager reporting tree, business-goal/company abstraction, and Paperclip-owned task truth do not fit Octarel's current purpose. Likewise, a plugin marketplace and generic secret vault are not being added speculatively. A later issue should exist only when a real Octarel use case requires them.

## Dependency/order plan

```text
ENG-PC-11 adapter capability/result contract
       |\
       | +------> ENG-PC-02 resumable sessions
       | +------> ENG-PC-05 usage ledger/budgets
       |
ENG-PC-01 atomic task leases
       |\
       | +------> ENG-PC-02
       | +------> ENG-PC-03 wake queue
       | +------> ENG-PC-04 run events
       |
       +--> ENG-PC-07 recovery <--- ENG-PC-03 + ENG-PC-04

ENG-PC-04 ---> ENG-PC-05
       |
       +------> ENG-PC-06 incremental context
       +------> ENG-PC-10 approvals

ENG-PC-08 configuration revisions   (parallel after schema review)
ENG-PC-09 runtime services          (parallel; refactor existing operations lifecycle)
```

Recommended implementation waves:

1. **Foundation:** ENG-PC-11, ENG-PC-01.
2. **Continuity:** ENG-PC-02, ENG-PC-03, ENG-PC-04.
3. **Efficiency/governance:** ENG-PC-05, ENG-PC-06.
4. **Resilience/operator control:** ENG-PC-07, ENG-PC-08, ENG-PC-10.
5. **Workspace operations:** ENG-PC-09.

Do not start all tasks in parallel. Schema-owning tasks must establish contracts before dependent writers modify the same state/API/UI surfaces.

---

## ENG-PC-01 — Atomic task leases and execution ownership

**Issue:** [#29](https://github.com/ACGE248/octarel/issues/29)  
**Depends on:** existing task claims, worktree locks, ENG-AO single-writer advancement lease.  
**Paperclip idea:** atomic issue checkout/execution ownership with conflict refusal and safe reclaim.

### Required implementation

Introduce one transactional execution-ownership abstraction binding project, stable task, run/runbook, worker, worktree and lease generation. Acquisition must use compare-and-swap/expected-state semantics. A conflict must fail explicitly rather than starting another writer. Stale recovery is allowed only when existing Octarel process/run evidence proves the owner dead.

This abstraction must compose with worktree locks and runbook advancement leases; it must not become a third unrelated lock hierarchy. Document the invariant that each mechanism protects.

### UI

Runs/Tasks entity inspector shows current owner, worktree, acquired/heartbeat/released timestamps and conflict/recovery reason. Attention shows genuinely stale/conflicting leases. Any recovery action must be server-proven safe; no generic force-steal button.

### Acceptance

Concurrent acquisition, separate processes, crash/restart, stale PID reuse, lease generation, worktree conflict and no-double-writer tests. Existing ENG-AO advancement tests remain green.

---

## ENG-PC-11 — Structured adapter capability and result contract

**Issue:** [#39](https://github.com/ACGE248/octarel/issues/39)  
**Paperclip idea:** adapters explicitly own runtime/session/result translation instead of callers guessing capabilities.

### Required implementation

Normalize worker transports behind a typed compatibility layer while preserving `workers.json` as routing authority. Capabilities must explicitly declare read/write, resume/session, structured usage categories, effective-model discovery, context-limit source, streaming/event support, auth probe, permission profile, cancellation and native subagents.

A typed RunResult should carry actual provider/model, exit/failure category, authoritative usage facts with provenance, optional opaque session update and evidence pointers. Missing capability is explicit; never infer it from a marketing model name.

Migrate Claude, Codex, Grok and OpenCode incrementally. Adapters may not widen `worker_routes`, bypass cost class, or inject credentials outside existing minimal-environment rules.

### UI

Provider/Agent inspectors consume capability facts for chips/details instead of maintaining parallel UI assumptions.

### Acceptance

Representative adapter contract tests for Claude/Codex/Grok/OpenCode, compatibility tests against current workers, no routing-order drift, no API fallback, redaction tests.

---

## ENG-PC-02 — Task-scoped resumable agent sessions

**Issue:** [#30](https://github.com/ACGE248/octarel/issues/30)  
**Depends on:** ENG-PC-01 and preferably ENG-PC-11.  
**Paperclip idea:** persist adapter runtime/session state by task so subsequent heartbeats can continue safely.

### Required implementation

Persist an `AgentSession` identity keyed by project/task/worker/provider/effective model/worktree/tree and policy identity. Store only safe opaque adapter state required for native resume; never credentials or secret-bearing prompts.

Resume is allowed only when the adapter declares support and compatibility still holds. Tree, policy, permission, provider/model or worktree incompatibility must start fresh and record why. Provider fallback normally starts a new provider session.

### UI

Run/Agent inspector shows Fresh/Resumed, session age, continuation count, last activity, resume capability and invalidation reason. Add a bounded operator option to force the next attempt fresh. Never expose opaque native session payloads.

### Acceptance

Restart persistence, supported/unsupported adapters, tree/policy invalidation, provider fallback, explicit fresh restart, secret/redaction tests.

---

## ENG-PC-03 — Durable wake queue and trigger coalescing

**Issue:** [#31](https://github.com/ACGE248/octarel/issues/31)  
**Depends on:** ENG-PC-01.  
**Paperclip idea:** persist wake requests and coalesce duplicate pending triggers instead of treating every signal as a new execution.

### Required implementation

Add a durable queue to existing Octarel state with typed reasons such as `TASK_ELIGIBLE`, `TASK_UNBLOCKED`, `IMPLEMENTATION_FINISHED`, `TEST_FINISHED`, `REVIEW_FINISHED`, `APPROVAL_RESOLVED`, `PROVIDER_RECOVERED`, `QUOTA_RESET`, `SCHEDULE`, `OVERNIGHT_TICK`, and `MANUAL`.

Duplicate pending wakes for the same project/task/stage should coalesce while retaining count, reasons and provenance. Use bounded retry/backoff and an explicit poisoned/stuck state. The daemon remains scheduling authority; dashboard reads must not schedule work or call AI providers.

### UI

Execution Events/Run Detail shows wake reason, coalesced count and lifecycle. Attention surfaces stuck wakes. System shows queue depth and oldest age using local state only.

### Acceptance

Concurrent enqueue/coalescing, restart durability, no duplicate advancement, poisoned wake behavior and overnight compatibility.

---

## ENG-PC-04 — Structured run-event timeline and live execution evidence

**Issue:** [#32](https://github.com/ACGE248/octarel/issues/32)  
**Depends on:** ENG-PC-01.  
**Paperclip idea:** first-class run events/log timeline.

### Required implementation

Extend Octarel's existing events and `.agent-output` evidence into a typed append-only `RunEvent` envelope. Event classes include lifecycle, wake, route, lease, process, adapter/tool status, checkpoint, usage, review, gate, PR/merge, wait/attention and approval.

Large logs remain redacted evidence files. SQLite stores safe structured summaries and pointers, not a duplicate raw log store. Events require stable ordering and source/provenance. Do not convert planned phases into progress.

### UI

Stitch Run Detail and Execution Events become a chronological, filterable timeline. Visually distinguish lifecycle, model/tool, safety/approval and validation events. Evidence pointers can expand/open safely. Preserve `UNKNOWN`/`NOT_REPORTED`.

### Acceptance

Ordering, restart persistence, redaction, evidence pointer validation, migration and proof that no second history system was created.

---

## ENG-PC-05 — Durable usage ledger and hierarchical budgets

**Issue:** [#33](https://github.com/ACGE248/octarel/issues/33)  
**Extends:** [#25 OCTAREL-UI-06](https://github.com/ACGE248/octarel/issues/25).  
**Recommended after:** ENG-PC-11 and ENG-PC-04.  
**Paperclip idea:** durable cost events plus hierarchical budgets.

### Required implementation

Persist usage events attributable to project, task, runbook, run, session, worker, provider, effective model and time. Store token categories, context limit/used, duration and metered cost only when a source is authoritative. Every field retains provenance/confidence compatible with `MEASURED`, `DERIVED`, `UNKNOWN`, and `NOT_EXPOSED`.

Subscription/free execution must not be assigned an invented API-dollar equivalent. Cash cost is meaningful only for genuinely metered execution with a trustworthy price/accounting source.

Add budget/usage policy scopes: global -> provider -> program/task -> run/session. Supported constraints may include metered cash, tokens, wall-clock, attempts/fallbacks and provider quota reserve when a real source exists. Enforcement happens before launch/fallback and cannot authorize a paid route.

### UI

Fulfill #25 with:
- per-run/session model/context/token/cache/duration/cost card;
- aggregate Usage & Costs view filtered by project/task/provider/model/time;
- separate subscription/free usage from metered cash;
- budget progress with source/confidence;
- warnings/hard blocks in Attention and Run Events.

### Acceptance

Durable aggregation, no double counting, restart/migration, budget enforcement, truthful unknowns, authoritative cache-hit calculation only, subscription semantics tests.

---

## ENG-PC-06 — Compact incremental task context and ancestry

**Issue:** [#34](https://github.com/ACGE248/octarel/issues/34)  
**Recommended after:** ENG-PC-04.  
**Paperclip idea:** compact heartbeat context plus goal/task ancestry.

### Required implementation

Introduce a `ContextCursor` that records delivered authoritative identities/events, never copied roadmap content. Mandatory AGENTS/core/role/workflow/provider/task contracts still compose normally. Incremental context adds only relevant changes since the cursor: dependency/task status, comments/events, run outcomes, review/gate evidence.

Provide bounded ancestry explaining why the task exists: parent program/task/dependency/source references. A changed tree, policy or task contract invalidates/refreshes the cursor. Graphify remains advisory below source/policy/contracts.

### UI

Context inspector shows authoritative bundle identity/size, incremental additions, ancestry/source links and Graphify supplied/status. Context savings may be shown only as explicitly `DERIVED` with a documented formula.

### Acceptance

Stale cursor invalidation, mandatory-policy preservation, deterministic bundle identity, bounded size and multi-project isolation.

---

## ENG-PC-07 — Restart/orphan recovery state machine

**Issue:** [#35](https://github.com/ACGE248/octarel/issues/35)  
**Depends on:** ENG-PC-01, ENG-PC-03, ENG-PC-04.  
**Paperclip idea:** explicit runtime/wait/recovery state rather than ambiguous failed/running records.

### Required implementation

Normalize recovery states such as `RUNNING`, `WAITING_EXTERNAL`, `WAITING_APPROVAL`, `WAITING_PROVIDER`, `OWNER_ACTION_REQUIRED`, `RECOVERABLE_ORPHAN`, and terminal states. Reuse existing PID/create-time/cwd/argv/session evidence to prove ownership; never kill/reclaim by executable name.

Preserve valid completed implementation/review/gate evidence and resume only the minimum remaining stage. Recovery attempts are bounded; repeated uncertainty becomes owner action rather than a loop.

### UI

Attention explains exact wait/recovery reason and next safe action. Run Detail shows recovery chain and preserved evidence. One-click recovery appears only when the server proves eligibility.

### Acceptance

Daemon/dashboard crash, worker orphan, reboot, provider outage, stale PID reuse and preserved acceptance-stage tests.

---

## ENG-PC-08 — Revisioned orchestration configuration and rollback

**Issue:** [#36](https://github.com/ACGE248/octarel/issues/36)  
**Paperclip idea:** configuration revision history and rollback.

### Required implementation

Revision only Octarel-owned runtime-editable orchestration settings. Repository-controlled policy files and `workers.json` remain repository-controlled unless separately redesigned.

Each immutable revision records actor, timestamp, reason, changed fields and predecessor. Secrets are excluded. Rollback validates current contracts and creates a new revision rather than rewriting history. Preserve remote identity/audit/CSRF protections and concurrent-update checks.

### UI

Settings shows change history, field-level diff/source/actor and preview-before-rollback. Clearly distinguish repository-controlled values from runtime-editable ones.

### Acceptance

Migration, redaction, invalid/stale rollback rejection, concurrent update and audit-continuity tests.

---

## ENG-PC-09 — Managed runtime service ownership and previews

**Issue:** [#37](https://github.com/ACGE248/octarel/issues/37)  
**Paperclip idea:** runtime/dev-server services attached to a workspace.

### Required implementation

Refactor the existing owned application lifecycle behind a generic `RuntimeService` record scoped to managed project/worktree/run. Record only declared/fixed command, cwd, PID/session/create-time, port/URL, health and owner.

A managed project must explicitly declare an allowed runtime command. Octarel may stop only a process it proves it owns. Preserve loopback/remote-access policy. Preview URL is operational metadata, not validation evidence.

### UI

Run/Worktree inspector shows service health, preview/open action, logs pointer and eligible start/restart/stop controls. External/unowned services are informational and non-stoppable.

### Acceptance

PID reuse, port conflict, crash/restart, multi-project isolation and external-process protection.

---

## ENG-PC-10 — Typed approvals and decision handoffs

**Issue:** [#38](https://github.com/ACGE248/octarel/issues/38)  
**Recommended after:** ENG-PC-04.  
**Paperclip idea:** typed approval objects and explicit resolution.

### Required implementation

Use approvals only where an operator decision is genuinely required: destructive cleanup, explicitly authorized metered/overflow route, material scope change, ambiguous product/architecture choice, or sensitive remote action. Do not replace deterministic review/gate policy with human approval.

An `ApprovalRequest` records project/task/run, typed action, safe payload summary, risk/reason, requester, expiry, state, resolver and resolution note. Resolution re-validates current state before execution. No approval can waive non-waivable secret/security/spend protections.

### UI

Attention/Approvals shows impact preview and approve/reject actions. Run Detail embeds request/resolution. Risk/destructive classification is server-derived, never a client `safe=true` flag.

### Acceptance

Expiry/staleness, changed-state revalidation, remote identity/audit, rejection, and proof that approval cannot bypass hard safety controls.

---

## Program-level UI contract

All ENG-PC UI work must integrate into the current Stitch **Glass Orchestration Studio** program rather than create a parallel dashboard.

Preferred homes:

| Information/action | Primary UI home |
|---|---|
| lease/session/context | contextual Run/Task/Agent inspector |
| wake/run events/recovery | Run Detail + Execution Events |
| token/context/cost | Run inspector + Usage & Costs aggregate surface |
| budget/usage blocks | Usage & Costs + Attention |
| approvals | Attention/Approvals + Run Detail |
| config revisions | Settings |
| runtime service | Run/Worktree inspector |
| adapter capabilities | Providers/Agent Fleet |
| queue/service health | System/Operational Overlays |

Requirements across all surfaces:
- light/dark structural parity;
- responsive Control Center viewport matrix;
- no fabricated progress or usage;
- provenance labels preserved;
- no page view may trigger an AI/provider call;
- destructive actions remain server-derived and confirmed;
- remote identity/audit and CSP remain intact;
- entity inspectors are preferred over adding a top-level page for every new record type.

## Program completion criteria

ENG-PC is complete only when the selected tasks are implemented through normal Octarel workflow, affected documentation is reconciled, deterministic migrations/tests exist, browser/UI coverage covers new interactive controls, and the program has not introduced duplicate task truth, scheduler authority, history, telemetry, or unsafe provider paths.
