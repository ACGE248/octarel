"""Issue #148: self-heal a worker CLI's ``--agent <name>`` preset file onto a

candidate worktree whose branch predates it, generically -- not a V1-05-
specific patch.

OpenCode2's ``opencode`` CLI discovers named agent presets (``.opencode/
agents/<name>.md``) relative to its own working directory, which for a
review Task is the *candidate* worktree, not the Control Plane daemon's own
checkout. A historical branch (video-editor/v1-05-visual-items predates
commit bc2864d, which added ``.opencode/agents/reviewer.md``) simply never
has that file in its tracked tree, so ``opencode run --agent reviewer``
silently substitutes its own default agent -- one that never received the
reviewer role's strict output contract -- instead of failing loudly. This
mirrors ENG-AGENT-16's insight exactly: Control Plane-owned tooling state
must never depend on a candidate branch's own tracked content to exist, and
must never be allowed to contaminate that candidate's identity either.

The fix materializes the *current* canonical preset file (read from wherever
this running code's own repository checkout is -- always up to date, since
the daemon's own checkout is what gets merged/fast-forwarded) into the
target worktree as an untracked file, only when that worktree does not
already have its own copy, and registers that exact path in the worktree's
local (never-committed) git excludes so it can never be staged into that
candidate's tree -- reusing the identical mechanism ENG-AGENT-16 already
established and tested for CP runtime/evidence directories.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from scripts.ci.runtime_paths import append_missing_local_excludes

AGENT_PRESET_RELATIVE_DIR = Path(".opencode") / "agents"


def _canonical_root() -> Path:
    # scripts/ci/reviewer_presets.py -> parents[2] is the repository root of
    # whatever checkout this running code actually lives in.
    return Path(__file__).resolve().parents[2]


def ensure_opencode_agent_preset(worktree: Path, agent_name: str) -> bool:
    """Ensure ``worktree`` can resolve the named OpenCode agent preset.

    Returns ``True`` when the preset is available at ``worktree`` afterward
    (whether it was already there or was just materialized), ``False`` when
    it is unavailable even in this running code's own canonical checkout --
    a real configuration gap, never silently papered over.

    Never overwrites a file the target worktree already has (tracked or
    not): a worktree with its own copy of the preset -- even an older one --
    keeps it untouched, exactly like every other candidate-identity
    boundary in this codebase never mutates real candidate content.
    """

    relative = AGENT_PRESET_RELATIVE_DIR / f"{agent_name}.md"
    target = worktree / relative
    if target.is_file():
        return True
    source = _canonical_root() / relative
    if not source.is_file():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError:
        return False
    append_missing_local_excludes(
        worktree,
        (relative.as_posix(),),
        comment=(
            "ENG-AGENT-17 (issue #148): self-healed reviewer agent preset, never candidate identity"
        ),
    )
    return True


def ensure_opencode_agent_preset_for_command(worktree: Path, command: list[str]) -> bool:
    """Self-heal whichever ``--agent <name>`` preset ``command`` requests, if any.

    A no-op (returns ``True``) for a command that is not an ``opencode``
    invocation or does not use ``--agent`` at all. Accepts both the
    two-token (``--agent reviewer``) and single-token (``--agent=reviewer``)
    forms yargs (opencode's CLI parser) allows for any flag.
    """

    if not command or command[0] != "opencode":
        return True
    for index, token in enumerate(command):
        if token == "--agent" and index + 1 < len(command):
            return ensure_opencode_agent_preset(worktree, command[index + 1])
        if token.startswith("--agent="):
            return ensure_opencode_agent_preset(worktree, token[len("--agent=") :])
    return True
