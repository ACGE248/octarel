# Project registration

A managed project is a Git checkout Octarel can read. Octarel stores only
*where and how* to obtain truth: path, remote, default branch, relative policy
and task-source paths, and an argv validation command.

## CLI

```bash
python -m octarel project detect /path/to/checkout
python -m octarel project add --id tools --path /path/to/checkout --name "Tools" \
  --policy POLICY.md --tasks TASKS.md --validate sh validate.sh \
  --python /path/to/checkout/.venv/bin/python   # optional; see "Python environment"
python -m octarel project select tools
python -m octarel project list
python -m octarel project remove tools --confirm
```

Remove unregisters the Control Plane row. The Git repository is never deleted.
Historical Octarel events/tasks for that project id are retained.

## Control Center

The sidebar project selector and Add Project flow call the same registry as
the CLI. Detection is generic: `AGENTS.md` is a hint, not a requirement. A
repository with only `TASKS.md` is valid.

## OctaScene

OctaScene is one adapter (`octascene_project.py`). It is not required for a
generic repository. Auto-registration happens only when
`OCTAREL_OCTASCENE_ROOT` (or an OctaScene-shaped checkout) is present. The
Octarel code root is never auto-registered as OctaScene.

## Isolation

- Policy, tasks, validation, worktrees, runbooks, and events are project-scoped.
- Switching projects must not display another project's tasks as current.
- Validation runs inside the selected project's checkout, not Octarel cwd.

## Python environment

A managed project's commands and gates run under **that project's** Python, never the interpreter Octarel
happens to be running from (ENG-AO-09). Octarel resolves it, in order:

1. `capabilities.python_interpreter` (`octarel project add --python PATH`; absolute, or relative to the
   checkout). If declared it is the only candidate: missing or unusable fails closed, it never falls through.
2. `<worktree>/.venv/bin/python` when the worktree has its own environment.
3. `<project checkout>/.venv/bin/python`, the project's canonical environment. A newly created sibling
   worktree therefore needs no manual `.venv` link.

The interpreter is probed before use: it must run, must import the modules the gate needs (`pytest`, `ruff` for
the OctaScene exact-tree gate), and must not be Octarel's own active virtual environment. Octarel's interpreter
is never an implicit fallback. On success the child environment gets the managed `bin` first on `PATH`,
`VIRTUAL_ENV` set to it, and Octarel's `VIRTUAL_ENV`/`PYTHONHOME`/`PYTHONPATH`/venv `PATH` entries removed. This
applies to the exact-tree gate, declared `validation_command`s, worker launches, the managed app launch
(`app_lifecycle_command`), the readiness check of a freshly provisioned worktree, and the dashboard terminal
(which only has Octarel's environment removed from `PATH`; the project's own `.venv` may lead it). Nothing is written into the managed worktree, so tree/source evidence is unchanged.

When the environment cannot be resolved the gate is **not started**: the runbook's test stage records `FAIL`
with the reason (searched paths, missing module, unusable interpreter) and a `managed_environment` fact block,
instead of a misleading failure from the wrong interpreter. A declared-command project with no Python
environment at all still runs (Octarel's environment is simply removed from `PATH`); one whose declared or
worktree environment is present but broken is not started. A worker launch never blocks on this - the gate is
the fail-closed authority - but the reason is recorded as a `supervisor` event.
