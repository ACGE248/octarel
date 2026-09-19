# Project registration

A managed project is a Git checkout Octarel can read. Octarel stores only
*where and how* to obtain truth: path, remote, default branch, relative policy
and task-source paths, and an argv validation command.

## CLI

```bash
python -m octarel project detect /path/to/checkout
python -m octarel project add --id tools --path /path/to/checkout --name "Tools" \
  --policy POLICY.md --tasks TASKS.md --validate sh validate.sh
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
