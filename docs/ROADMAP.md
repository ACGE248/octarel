# Octarel roadmap

This file is the canonical product/engineering roadmap for **Octarel itself**. It does not copy or replace the roadmap, ledger, ADRs, or task truth of any managed project. Current repository code and `AGENTS.md` override historical planning notes.

**Reconciled against `main` at `ed4ca59` (2026-09-29)**, after the Glass Orchestration Studio UI program (#27) and the telemetry follow-ups (#43, #47) landed. Where this file describes delivered behaviour it is a pointer to the code, not a second specification; where a delivered surface is named as the home for future work, extend that surface rather than rebuilding it.

## Current direction

Octarel is a standalone, multi-repository development orchestration control plane. Its differentiators remain:

- managed-repository truth is read from the selected checkout rather than copied into Octarel;
- local/subscription-backed workers are preferred and silent paid/API fallback is prohibited;
- one-writer/worktree safety, provider-diverse review, exact-tree acceptance and auditable evidence remain first-class;
- the Control Center is a truthful operator surface: unavailable facts stay `UNKNOWN`, `NOT_EXPOSED`, `NOT_REPORTED`, or explicitly `DERIVED`;
- orchestration mechanisms may evolve, but Octarel must not become a second source of managed-project product truth.

## Immediate infrastructure prerequisite

### ENG-AO-10 — First-class Graphify lifecycle, warm index and post-change refresh

- **Issue:** [#41](https://github.com/ACGE248/octarel/issues/41)

Complete ENG-AO-10 before beginning the ENG-PC implementation waves below.

Octarel already has safe tree-matched Graphify context from ENG-AO-01 (`scripts/agents/graph_context.py`), and #26 has since delivered its **per-run** Control Center evidence: `agent_activity._graph_context_row` exposes the recorded status, reason, injection flag and precedence for an attempt, reading only the run manifest and never invoking Graphify.

ENG-AO-10 makes the capability operationally first-class on top of that: supported installation/health probing, warm canonical indexes, candidate refresh after safe implementation checkpoints, canonical refresh after merge/reconciliation, refresh coalescing, and tree/worktree-aware evidence.

Its UI work is an **extension of the delivered #26 surface**, not a replacement for it. #26 answers "did this run get a graph?"; ENG-AO-10 adds the selected-project/global health that does not exist today — installed version, indexed tree identity, graph age, last refresh, and `READY`/`MISSING`/`OUTDATED`/`STALE`/`REFRESHING`/`FAILED_SAFE` — in System / Operational Overlays. Do not add a second Graphify page.

This is an **implementation sequencing prerequisite**, not a runtime hard dependency. If Graphify is missing or fails safely, normal Octarel execution must continue without graph context and record the reason.

Expected lifecycle:

```text
managed repository changes
        |
        +-- implementation safe checkpoint
        |       -> refresh candidate-worktree graph
        |       -> tester/reviewer receives exact candidate graph
        |
        +-- accepted merge/reconciliation
                -> refresh canonical graph
                -> re-read repository truth
                -> next task starts with warm current graph
```

Octarel owns refresh lifecycle; do not depend on Graphify Git hooks inside managed repositories. Graphify remains local/code-only, secret-filtered, provider-neutral, advisory, and below repository truth.

## UI program state

The Glass Orchestration Studio program has landed. It is the live Control Center, not a planned one, and every ENG-PC surface below composes into it.

| Task | Status | What exists in `main` |
|---|---|---|
| **#23 OCTAREL-UI-04** — Glass Orchestration Studio | Merged in #27 (`a12daf9`, tree `78918c02`); issue open for deferred scope | Design tokens and light/dark parity, app shell and navigation IA, Priority & Fallback Matrix over `/api/priority-matrix`, cross-entity ⌘K command palette |
| **#24 OCTAREL-UI-05** — Manager Chat | Delivered in #27, issue closed | `control_plane/manager_chat.py`: orchestration-backed chat that proposes deterministic commands for review; not a model client. Cross-entity navigation is the ⌘K palette, not this module |
| **#25 OCTAREL-UI-06** — usage/context/cost telemetry | Delivered in #27, issue closed | `control_plane/usage_telemetry.py` behind `/api/usage-telemetry`: per-run rows with `MEASURED`/`DERIVED`/`UNKNOWN`/`NOT_EXPOSED` classes, plus windowed aggregates |
| **#26 OCTAREL-UI-07** — Graphify status | Delivered in #27, issue closed | Per-run recorded Graphify status, reason, injection and precedence in agent activity; no page view can build a graph |
| **#42** — subscription-aware value and actual cost | Merged in #43 (`3bafe97`) | `estimated_api_equivalent_usd` and `actual_cost_usd` as separate fields that are never added together; billing class derived from `cost_class` via `telemetry.execution_route_for_cost_class`; prices from the OpenCode catalog through `control_plane/pricing.py` |
| **#44** — worker-reported per-run cost | Merged in #47 (`ed4ca59`) | On an **API-billed** route, a CLI that reports its own run cost makes `actual_cost_usd` `MEASURED` rather than derived from tokens × a price snapshot. Billing class is decided first and that ordering is load-bearing: a subscription CLI printing a `total_cost_usd` means "what this would have cost on the API", so reading it before classifying would turn a subscription session into reported spend |
| **#45 OCTAREL-TEST-02** — deterministic fixture task recency | Merged in #46 (`7116c5d`) | The seven-viewport Control Center matrix seeds relative task recency explicitly |

The `OCTAREL-UI-07` label is used by two different things and always has been: issue #26 (Graphify status) and the commit/engineering-doc name for issue #42 (subscription-aware cost). Cite the issue number, not the label.

**#23 remains open for three unbuilt surfaces,** named in its merge comment on #27: the Flow canvas (minimap, fit-to-screen, zoom %, fullscreen), the Runs → Run Detail split, and shared entity inspectors. All three are genuinely absent from the dashboard today — `view-runs` is still one page and only one-off sheets exist, no shared inspector component.

Their provenance differs, and the difference matters to whoever picks them up. Only the Flow canvas is recorded as **Deferred** in `docs/engineering/OCTAREL-UI-04.md`; the Run Detail split and shared entity inspectors are not in that file's decision table at all. Treat them as this roadmap's planning rather than as scope #23 formally logged. Several ENG-PC tasks below name a Run Detail or entity inspector as their UI home; whichever task reaches that surface first builds it once, and the rest extend it.

Two telemetry gaps are shipped as visible, explained `NOT_EXPOSED` cells rather than estimated, and no ENG-PC task may quietly fill them with an approximation: **cache categories** (nothing in the stack records fresh input, cache reads or cache writes, so no cache hit rate can be derived) and **effective context limit** (no runtime reports one, and a context window is never inferred from a model name).

---

# ENG-PC — Paperclip-derived orchestration hardening

- **Parent:** [#28 ENG-PC-00](https://github.com/ACGE248/octarel/issues/28)
- **Research date:** 2026-09-26
- **External reference:** [paperclipai/paperclip](https://github.com/paperclipai/paperclip)

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

0. **Repository intelligence prerequisite:** ENG-AO-10 (#41), extending the delivered #26 Graphify status surface with selected-project health rather than adding a second one.
1. **Foundation:** ENG-PC-11, ENG-PC-01.
2. **Continuity:** ENG-PC-02, ENG-PC-03, ENG-PC-04.
3. **Efficiency/governance:** ENG-PC-05, ENG-PC-06.
4. **Resilience/operator control:** ENG-PC-07, ENG-PC-08, ENG-PC-10.
5. **Workspace operations:** ENG-PC-09.

Do not start all tasks in parallel. Schema-owning tasks must establish contracts before dependent writers modify the same state/API/UI surfaces.

---

## ENG-PC-01 — Atomic task leases and execution ownership

- **Issue:** [#29](https://github.com/ACGE248/octarel/issues/29)
- **Depends on:** existing task claims, worktree locks, ENG-AO single-writer advancement lease.
- **Paperclip idea:** atomic issue checkout/execution ownership with conflict refusal and safe reclaim.

### Required implementation

Introduce one transactional execution-ownership abstraction binding project, stable task, run/runbook, worker, worktree and lease generation. Acquisition must use compare-and-swap/expected-state semantics. A conflict must fail explicitly rather than starting another writer. Stale recovery is allowed only when existing Octarel process/run evidence proves the owner dead.

This abstraction must compose with worktree locks and runbook advancement leases; it must not become a third unrelated lock hierarchy. Document the invariant that each mechanism protects.

### UI

Runs/Tasks entity inspector shows current owner, worktree, acquired/heartbeat/released timestamps and conflict/recovery reason. Attention shows genuinely stale/conflicting leases. Any recovery action must be server-proven safe; no generic force-steal button.

### Acceptance

Concurrent acquisition, separate processes, crash/restart, stale PID reuse, lease generation, worktree conflict and no-double-writer tests. Existing ENG-AO advancement tests remain green.

---

## ENG-PC-11 — Structured adapter capability and result contract

- **Issue:** [#39](https://github.com/ACGE248/octarel/issues/39)
- **Paperclip idea:** adapters explicitly own runtime/session/result translation instead of callers guessing capabilities.

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

- **Issue:** [#30](https://github.com/ACGE248/octarel/issues/30)
- **Depends on:** ENG-PC-01 and preferably ENG-PC-11.
- **Paperclip idea:** persist adapter runtime/session state by task so subsequent heartbeats can continue safely.

### Required implementation

Persist an `AgentSession` identity keyed by project/task/worker/provider/effective model/worktree/tree and policy identity. Store only safe opaque adapter state required for native resume; never credentials or secret-bearing prompts.

Resume is allowed only when the adapter declares support and compatibility still holds. Tree, policy, permission, provider/model or worktree incompatibility must start fresh and record why. Provider fallback normally starts a new provider session.

### UI

Run/Agent inspector shows Fresh/Resumed, session age, continuation count, last activity, resume capability and invalidation reason. Add a bounded operator option to force the next attempt fresh. Never expose opaque native session payloads.

### Acceptance

Restart persistence, supported/unsupported adapters, tree/policy invalidation, provider fallback, explicit fresh restart, secret/redaction tests.

---

## ENG-PC-03 — Durable wake queue and trigger coalescing

- **Issue:** [#31](https://github.com/ACGE248/octarel/issues/31)
- **Depends on:** ENG-PC-01.
- **Paperclip idea:** persist wake requests and coalesce duplicate pending triggers instead of treating every signal as a new execution.

### Required implementation

Add a durable queue to existing Octarel state with typed reasons such as `TASK_ELIGIBLE`, `TASK_UNBLOCKED`, `IMPLEMENTATION_FINISHED`, `TEST_FINISHED`, `REVIEW_FINISHED`, `APPROVAL_RESOLVED`, `PROVIDER_RECOVERED`, `QUOTA_RESET`, `SCHEDULE`, `OVERNIGHT_TICK`, and `MANUAL`.

Duplicate pending wakes for the same project/task/stage should coalesce while retaining count, reasons and provenance. Use bounded retry/backoff and an explicit poisoned/stuck state. The daemon remains scheduling authority; dashboard reads must not schedule work or call AI providers.

### UI

Execution Events/Run Detail shows wake reason, coalesced count and lifecycle. Attention surfaces stuck wakes. System shows queue depth and oldest age using local state only.

### Acceptance

Concurrent enqueue/coalescing, restart durability, no duplicate advancement, poisoned wake behavior and overnight compatibility.

---

## ENG-PC-04 — Structured run-event timeline and live execution evidence

- **Issue:** [#32](https://github.com/ACGE248/octarel/issues/32)
- **Depends on:** ENG-PC-01.
- **Paperclip idea:** first-class run events/log timeline.

### Required implementation

Extend Octarel's existing events and `.agent-output` evidence into a typed append-only `RunEvent` envelope. Event classes include lifecycle, wake, route, lease, process, adapter/tool status, checkpoint, usage, review, gate, PR/merge, wait/attention and approval.

Large logs remain redacted evidence files. SQLite stores safe structured summaries and pointers, not a duplicate raw log store. Events require stable ordering and source/provenance. Do not convert planned phases into progress.

### UI

Execution Events becomes a chronological, filterable timeline in the shipped Glass Orchestration Studio shell. Visually distinguish lifecycle, model/tool, safety/approval and validation events. Evidence pointers can expand/open safely. Preserve `UNKNOWN`/`NOT_REPORTED`.

The Runs → Run Detail split is still deferred #23 scope. ENG-PC-04 is the first task whose evidence genuinely needs it, so if it is still unbuilt when this task starts, ENG-PC-04 builds it once as shared structure and later tasks extend it — it does not get rebuilt per task, and it does not become a reason to defer the timeline.

### Acceptance

Ordering, restart persistence, redaction, evidence pointer validation, migration and proof that no second history system was created.

---

## ENG-PC-05 — Durable usage ledger and hierarchical budgets

- **Issue:** [#33](https://github.com/ACGE248/octarel/issues/33)
- **Extends:** the delivered telemetry stack — #25 (`usage_telemetry.py`, `/api/usage-telemetry`), #42 (`pricing.py`, two money fields, billing class) and #44 (worker-reported cost).
- **Recommended after:** ENG-PC-11 and ENG-PC-04.
- **Paperclip idea:** durable cost events plus hierarchical budgets.

### Already delivered — do not rebuild

The cost/telemetry *read model* exists and is truthful. ENG-PC-05 must extend it in place; re-specifying any of the following is scope this task has already lost:

- per-run rows carrying model, tokens, duration and provenance class, read from durable usage-governance records written by the supervisor;
- the two-field money contract, which is more particular than a summary makes it look and must be read in `usage_telemetry.py` before being extended:
  - `estimated_api_equivalent_usd` is route-blind and never spend. When it carries a figure that figure is always `DERIVED`; with no catalog price or no token counts it is `UNKNOWN`, never guessed.
  - `actual_cost_usd` consults **billing class before any monetary evidence**. Subscription-included and free routes are `DERIVED` `$0.00` from the classification itself; an unclassified route is `UNKNOWN`, never `$0.00`. Only an API-billed route reaches the evidence ladder, and there it is `MEASURED` from a worker-reported cost, else `DERIVED` from exact provider tokens × a price, else `UNKNOWN` — an approximated token count never produces a charge.
- billing class (`SUBSCRIPTION_INCLUDED` / `API_BILLED` / `FREE_TIER` / `UNKNOWN`) derived from the worker's `cost_class` through the single existing classifier, `telemetry.execution_route_for_cost_class`;
- windowed aggregates that keep subscription-equivalent value, free-tier value and API spend in separate totals, counting a figure only when its own class established it;
- `NOT_EXPOSED` cache categories and context limit, with their reasons.

A subscription route **does** carry a visible API-equivalent estimate; that was settled by #42. The prohibition is narrower than the original wording here suggested: an estimate may never be presented as, or accumulated into, actual spend.

### Required implementation

The remaining gap is attribution depth, durability as a first-class ledger, and enforcement.

Promote usage from a read model over runbook-scoped governance records into a durable, append-only usage ledger attributable to project, task, runbook, run, session, worker, provider, effective model and time — reusing the existing state store and the ENG-PC-04 event envelope rather than adding a second telemetry store. Extend aggregation to filter by project, task, provider and model, not only by time window and billing class. Preserve every existing provenance class and formula string; a ledger row may not be more confident than the record it came from.

Add budget/usage policy scopes: global -> provider -> program/task -> run/session. Supported constraints may include metered cash, tokens, wall-clock, attempts/fallbacks and provider quota reserve when a real source exists. Enforcement happens before launch/fallback and cannot authorize a paid route.

### UI

Extend the shipped usage surface; do not add a parallel one.

- the per-run card and the subscription/metered separation already exist — reuse them;
- add project/task/provider/model filtering to the aggregate Usage & Costs view;
- add budget progress with source/confidence;
- surface warnings and hard blocks in Attention and Run Events.

### Acceptance

Durable aggregation, no double counting, restart/migration, budget enforcement, truthful unknowns, authoritative cache-hit calculation only, subscription semantics tests. Existing `test_usage_telemetry*`, `test_pricing` and `test_reported_cost` coverage stays green unchanged; a change to one of those expectations is a contract change and needs its own justification.

---

## ENG-PC-06 — Compact incremental task context and ancestry

- **Issue:** [#34](https://github.com/ACGE248/octarel/issues/34)
- **Recommended after:** ENG-PC-04.
- **Paperclip idea:** compact heartbeat context plus goal/task ancestry.

### Required implementation

Introduce a `ContextCursor` that records delivered authoritative identities/events, never copied roadmap content. Mandatory AGENTS/core/role/workflow/provider/task contracts still compose normally. Incremental context adds only relevant changes since the cursor: dependency/task status, comments/events, run outcomes, review/gate evidence.

Provide bounded ancestry explaining why the task exists: parent program/task/dependency/source references. A changed tree, policy or task contract invalidates/refreshes the cursor. Graphify remains advisory below source/policy/contracts.

### UI

Context inspector shows authoritative bundle identity/size, incremental additions, ancestry/source links and Graphify supplied/status. Context savings may be shown only as explicitly `DERIVED` with a documented formula.

### Acceptance

Stale cursor invalidation, mandatory-policy preservation, deterministic bundle identity, bounded size and multi-project isolation.

---

## ENG-PC-07 — Restart/orphan recovery state machine

- **Issue:** [#35](https://github.com/ACGE248/octarel/issues/35)
- **Depends on:** ENG-PC-01, ENG-PC-03, ENG-PC-04.
- **Paperclip idea:** explicit runtime/wait/recovery state rather than ambiguous failed/running records.

### Required implementation

Normalize recovery states such as `RUNNING`, `WAITING_EXTERNAL`, `WAITING_APPROVAL`, `WAITING_PROVIDER`, `OWNER_ACTION_REQUIRED`, `RECOVERABLE_ORPHAN`, and terminal states. Reuse existing PID/create-time/cwd/argv/session evidence to prove ownership; never kill/reclaim by executable name.

Preserve valid completed implementation/review/gate evidence and resume only the minimum remaining stage. Recovery attempts are bounded; repeated uncertainty becomes owner action rather than a loop.

### UI

Attention explains exact wait/recovery reason and next safe action. Run Detail shows recovery chain and preserved evidence. One-click recovery appears only when the server proves eligibility.

### Acceptance

Daemon/dashboard crash, worker orphan, reboot, provider outage, stale PID reuse and preserved acceptance-stage tests.

---

## ENG-PC-08 — Revisioned orchestration configuration and rollback

- **Issue:** [#36](https://github.com/ACGE248/octarel/issues/36)
- **Paperclip idea:** configuration revision history and rollback.

### Required implementation

Revision only Octarel-owned runtime-editable orchestration settings. Repository-controlled policy files and `workers.json` remain repository-controlled unless separately redesigned.

Each immutable revision records actor, timestamp, reason, changed fields and predecessor. Secrets are excluded. Rollback validates current contracts and creates a new revision rather than rewriting history. Preserve remote identity/audit/CSRF protections and concurrent-update checks.

### UI

Settings shows change history, field-level diff/source/actor and preview-before-rollback. Clearly distinguish repository-controlled values from runtime-editable ones.

### Acceptance

Migration, redaction, invalid/stale rollback rejection, concurrent update and audit-continuity tests.

---

## ENG-PC-09 — Managed runtime service ownership and previews

- **Issue:** [#37](https://github.com/ACGE248/octarel/issues/37)
- **Paperclip idea:** runtime/dev-server services attached to a workspace.

### Required implementation

Refactor the existing owned application lifecycle behind a generic `RuntimeService` record scoped to managed project/worktree/run. Record only declared/fixed command, cwd, PID/session/create-time, port/URL, health and owner.

A managed project must explicitly declare an allowed runtime command. Octarel may stop only a process it proves it owns. Preserve loopback/remote-access policy. Preview URL is operational metadata, not validation evidence.

### UI

Run/Worktree inspector shows service health, preview/open action, logs pointer and eligible start/restart/stop controls. External/unowned services are informational and non-stoppable.

### Acceptance

PID reuse, port conflict, crash/restart, multi-project isolation and external-process protection.

---

## ENG-PC-10 — Typed approvals and decision handoffs

- **Issue:** [#38](https://github.com/ACGE248/octarel/issues/38)
- **Recommended after:** ENG-PC-04.
- **Paperclip idea:** typed approval objects and explicit resolution.

### Required implementation

Use approvals only where an operator decision is genuinely required: destructive cleanup, explicitly authorized metered/overflow route, material scope change, ambiguous product/architecture choice, or sensitive remote action. Do not replace deterministic review/gate policy with human approval.

An `ApprovalRequest` records project/task/run, typed action, safe payload summary, risk/reason, requester, expiry, state, resolver and resolution note. Resolution re-validates current state before execution. No approval can waive non-waivable secret/security/spend protections.

### UI

Attention/Approvals shows impact preview and approve/reject actions. Run Detail embeds request/resolution. Risk/destructive classification is server-derived, never a client `safe=true` flag.

### Acceptance

Expiry/staleness, changed-state revalidation, remote identity/audit, rejection, and proof that approval cannot bypass hard safety controls.

---

## Program-level UI contract

The **Glass Orchestration Studio** shell is live in `main` (#27). All ENG-PC UI work integrates into it rather than creating a parallel dashboard, and reuses its design tokens, light/dark parity and inspector conventions.

Two homes named in the table below — Run Detail and shared entity inspectors — are deferred #23 scope that no task has built yet. The first ENG-PC task to need one builds it as shared structure; the rest extend it.

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
