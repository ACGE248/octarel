# ENG-PC — Paperclip research notes

**Research date:** 2026-09-26  
**Upstream:** https://github.com/paperclipai/paperclip  
**Program:** [ROADMAP.md](../ROADMAP.md#eng-pc--paperclip-derived-orchestration-hardening) / GitHub issue #28

This is a research/reference document, not a second roadmap. Implementation status belongs in the roadmap/issues and current code. Paperclip is an external reference, never an Octarel runtime authority.

## Evaluation method

The review examined Paperclip's public repository/documentation and searched implementation areas for:

- heartbeat scheduling and wake requests;
- issue/task checkout and ownership;
- adapter runtime/session persistence;
- run/event/log recording;
- token/cost accounting and budgets;
- compact task context/ancestry;
- restart/orphan recovery;
- configuration revisions/rollback;
- runtime/dev-server ownership;
- approvals;
- plugin/capability boundaries and secrets.

Before implementing any ENG-PC issue, refresh the relevant upstream area and record the exact Paperclip commit SHA used. Do not copy code blindly; translate the invariant into Octarel's architecture and license/attribution requirements.

## High-value mechanisms selected

### Atomic task checkout

Paperclip's heartbeat protocol makes checkout a deliberate ownership transition rather than assuming that assignment means execution ownership. This maps well to Octarel's existing task claims + worktree locks + advancement leases, but Octarel should consolidate the operator-visible ownership model instead of layering an unrelated lock.

Starting reference:
https://github.com/paperclipai/paperclip/blob/master/docs/guides/agent-developer/heartbeat-protocol.md

### Task-scoped adapter sessions

Paperclip adapters separate runtime execution and session state. The useful invariant is that continuation state belongs to a task/agent runtime and is adapter-defined, rather than every orchestrator caller reverse-engineering CLI session behavior.

Starting references:
https://github.com/paperclipai/paperclip/tree/master/packages/adapters
https://github.com/paperclipai/paperclip/blob/master/docs/start/architecture.md

### Wake coalescing / heartbeat triggers

Paperclip treats wakeups as explicit durable triggers. Octarel can use the pattern to make schedule, task-unblock, provider-recovery, approval and stage-completion triggers observable and coalesced while retaining one scheduler authority.

Starting reference:
https://github.com/paperclipai/paperclip/blob/master/docs/guides/agent-developer/heartbeat-protocol.md

### Cost events and budgets

Paperclip's cost/budget model demonstrates the value of durable attribution and enforceable limits. Octarel must translate this carefully because much of its execution is subscription-backed: tokens/context may be measurable while per-run cash cost is not meaningful.

Starting reference:
https://github.com/paperclipai/paperclip/blob/master/docs/guides/board-operator/costs-and-budgets.md

### Compact incremental context

Paperclip's heartbeat guidance favors compact context rather than replaying all task state. Octarel can adapt this only below mandatory repository policy/task contracts; the managed project's current truth always wins.

### Configuration revisions

Paperclip's revision/rollback pattern is useful for runtime-editable orchestration settings. Octarel should not turn repository-controlled policy into mutable DB configuration.

### Runtime services

Paperclip's workspace/runtime model is useful for generalizing Octarel's existing owned app lifecycle. Ownership proof and loopback safety remain Octarel requirements.

### Typed approvals

Paperclip's approval objects are useful for genuine operator decisions. They must not replace deterministic software review/acceptance gates or become a way to waive hard safety rules.

## Ideas not adopted

### Company/CEO/org hierarchy

Paperclip intentionally models companies and reporting trees. Octarel's roles are engineering execution responsibilities and provider capabilities. Simulated corporate hierarchy adds little value and can obscure actual task/provider ownership.

### Paperclip-owned task truth

Octarel deliberately reads managed repositories' current policy/roadmap/task sources. Copying that truth into a Paperclip-like task database would recreate documentation drift.

### Paperclip as a parent orchestrator

Running Paperclip -> Octarel -> workers would duplicate scheduler, task, recovery, approval and history authority. The selected mechanisms are implemented natively instead.

### Plugin marketplace now

Paperclip's plugin/capability architecture is worth monitoring, but Octarel does not currently need a marketplace. ENG-PC-11 creates a typed adapter seam first. A plugin task should be opened only for a concrete extension need.

### Generic secret vault now

Octarel currently minimizes credential exposure through local subscription sessions, environment allowlists and redaction. A secret store adds attack surface. Paperclip's scoped-secret design is a reference if Octarel later gains a real storage requirement.

## Mapping to Octarel tasks

- #29 ENG-PC-01 — atomic task leases.
- #30 ENG-PC-02 — resumable agent sessions.
- #31 ENG-PC-03 — durable wake queue.
- #32 ENG-PC-04 — structured run events.
- #33 ENG-PC-05 — durable usage/budgets; extends #25.
- #34 ENG-PC-06 — incremental context/ancestry.
- #35 ENG-PC-07 — restart/orphan recovery.
- #36 ENG-PC-08 — configuration revisions.
- #37 ENG-PC-09 — runtime services.
- #38 ENG-PC-10 — approvals.
- #39 ENG-PC-11 — adapter capability/result contract.

## Attribution / copying rule

The program is based on architectural research. If implementation later copies or closely derives upstream code rather than independently implementing the idea, the implementer must inspect Paperclip's then-current license and update Octarel's NOTICE/THIRD_PARTY attribution as required before merge.
