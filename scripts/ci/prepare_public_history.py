#!/usr/bin/env python3
"""Build a public-safe orphan snapshot of the current Octarel tree.

Current ``main`` history is not safe to publish: GitHub merge commits record
a private author email, and an earlier ENG-CP-05 blob recorded an operator
filesystem path. This script copies the *current tracked tree* into a brand
new ``git init`` repository as a single commit with a public-safe identity.

It never clones the unsafe object store (so unreachable historical blobs
cannot hide in pack files), never force-pushes, and never changes
``origin/main``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

PUBLIC_NAME = "Octarel contributors"
PUBLIC_EMAIL = "octarel@users.noreply.github.com"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or " ".join(command))
    return result


def prepare_public_history(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(f"destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=destination)
    # Stream only the current commit's tracked tree. Ignored runtime/state
    # (``.orchestrator-state``, ``.venv``, ``node_modules``, ``.local-gate``,
    # ``.agent-output``) never enters the snapshot.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if archive.returncode:
        raise RuntimeError(archive.stderr.decode("utf-8", errors="replace") or "git archive failed")
    extract = subprocess.run(
        ["tar", "-xf", "-"],
        cwd=destination,
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if extract.returncode:
        raise RuntimeError(extract.stderr.decode("utf-8", errors="replace") or "tar extract failed")
    _run(["git", "add", "-A"], cwd=destination)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": PUBLIC_NAME,
        "GIT_AUTHOR_EMAIL": PUBLIC_EMAIL,
        "GIT_COMMITTER_NAME": PUBLIC_NAME,
        "GIT_COMMITTER_EMAIL": PUBLIC_EMAIL,
    }
    _run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "Octarel 0.1.0 public-safe snapshot (CPX-07).\n\n"
            "This commit is the entire public history. It replaces unpublished "
            "history that contained a private GitHub merge-commit email and an "
            "operator filesystem path.",
        ],
        cwd=destination,
        env=env,
    )
    _run(["git", "config", "user.name", PUBLIC_NAME], cwd=destination)
    _run(["git", "config", "user.email", PUBLIC_EMAIL], cwd=destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a public-safe Octarel history snapshot")
    parser.add_argument("--source", default=".")
    parser.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    path = prepare_public_history(Path(args.source), Path(args.destination))
    print(f"public_safe_clone={path}")
    print("does_not_push=true")
    print("object_store=fresh-git-init")
    print("next=run public-safety --publication-gate against this clone before replacing private main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
