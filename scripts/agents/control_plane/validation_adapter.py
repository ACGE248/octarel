"""ENG-CP-04 (issue #169): repository-owned validation dispatch.

Generic Control Plane code never hard-codes OctaScene's
``scripts/ci/local_gate.py`` argv. It executes the selected project's
declared ``validation_command`` from the exact candidate/worktree, reusing
CPX-02's ``run_project_validation`` / ``run_worker_process`` isolation.

OctaScene's exact-tree local gate remains behind the OctaScene compatibility
adapter (``capabilities["validation_adapter"]`` /
``validation_authority``), which preserves fail-closed exact-tree evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .project import ProjectContract, ProjectValidationError, run_project_validation

ADAPTER_DECLARED_COMMAND = "declared_command"
ADAPTER_EXACT_TREE_LOCAL_GATE = "exact_tree_local_gate"
OCTASCENE_VALIDATION_AUTHORITY = "repository-owned-exact-tree-local-gate"


class ValidationDispatchError(RuntimeError):
    """Selected-project validation could not be dispatched at all."""


def validation_adapter_name(project: ProjectContract) -> str:
    named = (project.capabilities.get("validation_adapter") or "").strip()
    if named:
        return named
    if project.capabilities.get("validation_authority") == OCTASCENE_VALIDATION_AUTHORITY:
        return ADAPTER_EXACT_TREE_LOCAL_GATE
    return ADAPTER_DECLARED_COMMAND


def _target_root(project: ProjectContract, worktree: Path | str | None) -> Path:
    target = Path(worktree) if worktree is not None else project.local_repo_root
    if not target.exists() or not target.is_dir():
        raise ValidationDispatchError(
            f"project {project.project_id!r} validation target does not exist or is not a directory: {target!r}"
        )
    return target


def run_selected_project_validation(
    project: ProjectContract,
    *,
    worktree: Path | str | None = None,
    timeout: float | None = 1800.0,
    gate_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the selected project's declared validation from ``worktree``.

    Never falls back to Control Plane ``cwd``. The returned dict is a common
    shape both adapters can feed into acceptance/merge:

    - ``result``: ``"pass"`` or ``"fail"``
    - ``exit_code``: subprocess exit when the declared-command adapter ran
    - ``output``: captured subprocess output (declared-command)
    - ``evidence_path`` / ``prerequisite_failures``: exact-tree gate fields
      when the OctaScene compatibility adapter ran
    """

    target = _target_root(project, worktree)
    name = validation_adapter_name(project)
    if name == ADAPTER_EXACT_TREE_LOCAL_GATE:
        from .octascene_project import run_octascene_exact_tree_gate

        raw = run_octascene_exact_tree_gate(target, **(gate_kwargs or {}))
        result = dict(raw)
        result.setdefault("result", "pass" if result.get("result") == "pass" or result.get("ready") else "fail")
        return result
    if name != ADAPTER_DECLARED_COMMAND:
        raise ValidationDispatchError(
            f"project {project.project_id!r} declares unknown validation_adapter {name!r}"
        )
    try:
        exit_code, output = run_project_validation(project, worktree=target, timeout=timeout)
    except ProjectValidationError as exc:
        raise ValidationDispatchError(str(exc)) from exc
    passed = exit_code == 0
    return {
        "result": "pass" if passed else "fail",
        "exit_code": exit_code,
        "output": output,
        "evidence_path": None,
        "prerequisite_failures": [] if passed else [output.strip() or f"validation_command exited {exit_code}"],
        "adapter": ADAPTER_DECLARED_COMMAND,
        "worktree": str(target),
        "project_id": project.project_id,
    }
