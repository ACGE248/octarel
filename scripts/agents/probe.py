"""ENG-AO-06 (issue #15): bounded, non-interactive local provider probes.

A provider CLI status/auth probe (``claude auth status``, ``codex login status`` ...) runs inside the
daemon's advancement loop. It must never be able to hold the daemon hostage: it gets no stdin, no
controlling terminal (its own session, so it can never be stopped by terminal job control or prompt on
``/dev/tty``), and a hard timeout after which it is killed. A timeout surfaces as
``subprocess.TimeoutExpired`` (a ``SubprocessError``), which every caller already maps to a deterministic
"could not probe" result -- never to a paid/API fallback.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any

DEFAULT_PROBE_TIMEOUT = 5.0


def run_probe(
    argv: Sequence[str], *, timeout: float = DEFAULT_PROBE_TIMEOUT, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run one local probe non-interactively with a bounded lifetime.

    Raises ``subprocess.TimeoutExpired`` (child killed and reaped) or ``OSError`` exactly like
    ``subprocess.run``; callers treat both as an unanswered probe.
    """

    if timeout is None or timeout <= 0:
        raise ValueError("a provider probe requires a positive timeout")
    return subprocess.run(  # noqa: S603 - argv is a registry-declared CLI, never shell-interpreted
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        **kwargs,
    )
