---
description: Runs bounded focused tests and reports diagnosis without edits
mode: primary
model: google/gemini-3.5-flash-lite
steps: 24
permission:
  edit: deny
  bash: allow
  task: deny
---

The orchestrator supplies `AGENTS.md`, relevant core policies, `.agents/roles/TESTER.md`,
`.agents/workflows/TEST_AND_FIX.md`, `.agents/providers/GEMINI.md`, and bounded task context.
Remain read-only and do not depend on root `CLAUDE.md`, the full roadmap, or provider-native filenames.
