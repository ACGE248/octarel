# Claude provider adapter

Claude Code uses its local CLI/session and repository `.claude/settings.json` permissions. Root
`CLAUDE.md` is a minimal native bootstrap for direct/local Claude sessions only; orchestrated Claude
receives this provider policy and the canonical composed bundle explicitly.

Sonnet at moderate reasoning is the normal implementation choice when capable. Reserve higher reasoning
or Opus for ambiguous architecture, cross-cutting refactors, or difficult debugging after bounded evidence
shows the normal tier is insufficient. Claude may serve ORCHESTRATOR or IMPLEMENTER roles. CLI permission
modes never override the composed core/role/workflow policy.

Control Plane availability uses the CLI's local `claude auth status` command and requires the existing
`claude.ai` subscription session. Merely finding the executable is not sufficient, and API-key authentication
is not an eligible fallback. The probe is local and non-billable.

`claude auth status` failing to produce a parseable answer (missing CLI, spawn error, timeout, or empty
output with no explicit negative status line) is a launch-environment/sandbox visibility defect, reported as
`LAUNCH_ENVIRONMENT_ERROR` — never as `NOT_AUTHENTICATED`, which is reserved for a probe that actually
received and understood a negative answer (issue #131). When the metadata probe is ambiguous, one real,
harmless authoritative prompt (`claude -p "Reply with exactly: CLAUDE_OK"`) is allowed to settle it: a
successful reply makes the route `AVAILABLE` even though the cheap heuristic disagreed. Only when that
authoritative prompt also fails to produce the expected reply does the route report
`LAUNCH_ENVIRONMENT_ERROR` and fall back to another same-role worker — never as a request for the operator
to re-authenticate a session that is, in fact, still valid.
