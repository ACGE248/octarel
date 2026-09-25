"""ENG-AO-09 (issue #21): managed-project Python environment isolation.

Octarel is normally launched from its own ``.venv``. A managed project (for example OctaScene) has its own
Python dependencies; running that project's commands or gates under Octarel's active interpreter produces
failures that say nothing about the project's tree (the VE-BRIDGE-02 acceptance run died on
``ModuleNotFoundError: No module named 'PIL'`` for an otherwise valid tree).

This module is the one place that decides *which* Python a managed project runs under and builds the child
environment for it:

1. ``capabilities["python_interpreter"]`` -- an interpreter the project declares (absolute, or relative to its
   checkout). A declared interpreter that is missing or unusable fails closed; it never falls through.
2. ``<worktree>/.venv/bin/python`` -- a worktree-local environment (the project's own gate honours it first).
3. ``<project checkout>/.venv/bin/python`` -- the project's canonical environment. A freshly created sibling
   worktree therefore needs no manual ``.venv`` link.

The resolved interpreter is probed (runs, imports the modules the caller requires, is not Octarel's own active
environment) and its facts become recorded evidence. Octarel's own interpreter is never an implicit fallback.
Source-tree evidence is untouched: nothing is written into the managed worktree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project import ProjectContract, cp_code_root

#: Optional project capability naming the managed project's Python interpreter.
PYTHON_INTERPRETER_CAPABILITY = "python_interpreter"

SOURCE_DECLARED = "declared"
SOURCE_WORKTREE = "worktree"
SOURCE_PROJECT = "project-canonical"

#: The environment could not be found at all (a soft condition for projects that do not need Python).
KIND_NOT_FOUND = "not_found"
#: An environment was declared or present but is unusable/inconsistent (always fail closed).
KIND_INCONSISTENT = "inconsistent"

#: Variables that make a child interpreter import from, or believe it lives in, some other environment.
ISOLATED_ENV_VARS: tuple[str, ...] = (
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "PYTHONHOME",
    "PYTHONPATH",
    "__PYVENV_LAUNCHER__",
)

_PROBE_SCRIPT = (
    "import importlib.util, json, sys\n"
    "print(json.dumps({'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
    "'version': sys.version.split()[0], "
    "'modules': {m: importlib.util.find_spec(m) is not None for m in sys.argv[1:]}}))\n"
)


class ManagedEnvironmentError(RuntimeError):
    """The managed project's Python environment is unavailable or inconsistent."""

    def __init__(self, reason: str, *, kind: str, evidence: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.evidence = evidence


@dataclass(frozen=True)
class ManagedEnvironment:
    project_id: str
    interpreter: Path
    source: str
    prefix: str
    base_prefix: str
    python_version: str
    modules: dict[str, bool] = field(default_factory=dict)

    @property
    def bin_dir(self) -> Path:
        return self.interpreter.parent

    @property
    def is_virtualenv(self) -> bool:
        return self.prefix != self.base_prefix

    def as_evidence(self) -> dict[str, Any]:
        return {
            "status": "READY",
            "project_id": self.project_id,
            "interpreter": str(self.interpreter),
            "source": self.source,
            "prefix": self.prefix,
            "python_version": self.python_version,
            "required_modules": dict(self.modules),
        }


def octarel_environment_bin_dirs() -> tuple[str, ...]:
    """Bin directories belonging to the Octarel process's own active virtual environment."""

    dirs: list[str] = []
    if sys.prefix != sys.base_prefix:
        dirs.append(os.path.join(sys.prefix, "bin"))
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        dirs.append(os.path.join(active, "bin"))
    return tuple(os.path.realpath(d) for d in dirs)


def isolated_environment(
    base: Mapping[str, str] | None = None, environment: ManagedEnvironment | None = None
) -> dict[str, str]:
    """A child environment with Octarel's Python environment removed and the managed one (if any) first.

    Drops the Python-environment variables in :data:`ISOLATED_ENV_VARS` and every ``PATH`` entry that is
    Octarel's own active virtual-environment ``bin``. When ``environment`` is given its ``bin`` leads ``PATH``
    (so a bare ``python3``/``pytest`` resolves inside the managed project) and ``VIRTUAL_ENV`` names its
    virtual environment.
    """

    env = dict(os.environ if base is None else base)
    for name in ISOLATED_ENV_VARS:
        env.pop(name, None)
    own = set(octarel_environment_bin_dirs())
    entries = [p for p in env.get("PATH", "").split(os.pathsep) if p and os.path.realpath(p) not in own]
    if environment is not None:
        entries.insert(0, str(environment.bin_dir))
        if environment.is_virtualenv:
            env["VIRTUAL_ENV"] = environment.prefix
    env["PATH"] = os.pathsep.join(entries)
    return env


def _candidates(project: ProjectContract, worktree: Path | None) -> list[tuple[str, Path]]:
    declared = (project.capabilities.get(PYTHON_INTERPRETER_CAPABILITY) or "").strip()
    if declared:
        path = Path(declared)
        return [(SOURCE_DECLARED, path if path.is_absolute() else project.local_repo_root / path)]
    found: list[tuple[str, Path]] = []
    if worktree is not None:
        found.append((SOURCE_WORKTREE, worktree / ".venv" / "bin" / "python"))
    found.append((SOURCE_PROJECT, project.local_repo_root / ".venv" / "bin" / "python"))
    return found


def _fail(reason: str, kind: str, evidence: dict[str, Any]) -> ManagedEnvironmentError:
    return ManagedEnvironmentError(reason, kind=kind, evidence={"status": "UNAVAILABLE", "reason": reason, **evidence})


def resolve_managed_environment(
    project: ProjectContract,
    *,
    worktree: Path | str | None = None,
    required_modules: Iterable[str] = (),
    timeout: float = 30.0,
) -> ManagedEnvironment:
    """Resolve and probe ``project``'s Python environment; raise :class:`ManagedEnvironmentError` otherwise."""

    target = Path(worktree) if worktree is not None else None
    modules = tuple(required_modules)
    candidates = _candidates(project, target)
    searched = [{"source": source, "path": str(path)} for source, path in candidates]
    base_evidence: dict[str, Any] = {"project_id": project.project_id, "searched": searched}

    chosen: tuple[str, Path] | None = None
    for source, interpreter in candidates:
        # Present means "the .venv directory/link (or declared file) exists", even if dangling: a present but
        # broken environment is an inconsistency to report, never something to silently skip past.
        anchor = interpreter if source == SOURCE_DECLARED else interpreter.parent.parent
        if not os.path.lexists(anchor):
            if source == SOURCE_DECLARED:
                raise _fail(
                    f"declared managed-project Python interpreter does not exist: {interpreter} "
                    f"(capabilities.{PYTHON_INTERPRETER_CAPABILITY}); it is never replaced by another environment",
                    KIND_INCONSISTENT,
                    {**base_evidence, "source": source, "interpreter": str(interpreter)},
                )
            continue
        if not (interpreter.is_file() and os.access(interpreter, os.X_OK)):
            raise _fail(
                f"managed-project Python interpreter ({source}) is present but not an executable file: {interpreter}",
                KIND_INCONSISTENT,
                {**base_evidence, "source": source, "interpreter": str(interpreter)},
            )
        chosen = (source, interpreter)
        break
    if chosen is None:
        raise _fail(
            f"no Python environment found for managed project {project.project_id!r}; searched "
            + ", ".join(str(path) for _source, path in candidates)
            + f". Create <project>/.venv or declare capabilities.{PYTHON_INTERPRETER_CAPABILITY}. "
            "Octarel's own interpreter is never used as a fallback.",
            KIND_NOT_FOUND,
            base_evidence,
        )

    source, interpreter = chosen
    evidence = {**base_evidence, "source": source, "interpreter": str(interpreter)}
    probe_env = isolated_environment(os.environ, None)
    probe_env["PATH"] = os.pathsep.join([str(interpreter.parent), probe_env.get("PATH", "")])
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", "-c", _PROBE_SCRIPT, *modules],
            cwd=str(target if target is not None and target.is_dir() else project.local_repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=probe_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _fail(
            f"managed-project Python interpreter could not be run: {interpreter}: {exc}",
            KIND_INCONSISTENT,
            evidence,
        ) from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise _fail(
            f"managed-project Python interpreter failed its health probe (exit {completed.returncode}): "
            f"{interpreter}: {detail[-1][:300] if detail else 'no output'}",
            KIND_INCONSISTENT,
            evidence,
        )
    try:
        facts = json.loads(completed.stdout.strip().splitlines()[-1])
        prefix, base_prefix, version = str(facts["prefix"]), str(facts["base_prefix"]), str(facts["version"])
        present = {str(k): bool(v) for k, v in dict(facts["modules"]).items()}
    except (ValueError, KeyError, IndexError, TypeError):
        raise _fail(
            f"managed-project Python interpreter returned an unreadable health probe: {interpreter}",
            KIND_INCONSISTENT,
            evidence,
        ) from None
    evidence.update(prefix=prefix, python_version=version, required_modules=present)

    own_is_virtualenv = sys.prefix != sys.base_prefix
    same_as_octarel = os.path.realpath(prefix) == os.path.realpath(sys.prefix)
    project_is_octarel = os.path.realpath(project.local_repo_root) == os.path.realpath(cp_code_root())
    if own_is_virtualenv and same_as_octarel and not project_is_octarel:
        raise _fail(
            f"managed-project Python interpreter {interpreter} resolves to Octarel's own active environment "
            f"({prefix}); a managed project must use its own environment",
            KIND_INCONSISTENT,
            evidence,
        )
    missing = sorted(name for name, ok in present.items() if not ok)
    if missing:
        raise _fail(
            f"managed-project Python environment {interpreter} is missing required module(s): {', '.join(missing)}",
            KIND_INCONSISTENT,
            evidence,
        )
    return ManagedEnvironment(
        project_id=project.project_id,
        interpreter=interpreter,
        source=source,
        prefix=prefix,
        base_prefix=base_prefix,
        python_version=version,
        modules=present,
    )


def is_octarel_own_project(project: ProjectContract) -> bool:
    """True when ``project`` is the Octarel code checkout itself (its environment is Octarel's own)."""

    return os.path.realpath(project.local_repo_root) == os.path.realpath(cp_code_root())
