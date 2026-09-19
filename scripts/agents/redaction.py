"""Secret redaction for delegated-worker commands and captured output.

The orchestrator records the exact invocation it ran and the worker's output as
audit evidence. That evidence must never contain credentials, so every logged
command and every captured stdout/stderr chunk is passed through
:func:`redact_text` (or :func:`redact_command`) before it is written to disk.

Redaction is intentionally conservative: it prefers to mask a harmless token over
leaking a real one.
"""

from __future__ import annotations

import re
from typing import Sequence

PLACEHOLDER = "***REDACTED***"

# Substrings that mark an option/key name as secret-bearing.
_SECRET_NAME_HINTS = (
    "api-key",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth-header",
    "auth_header",
    "bearer",
    "access-key",
    "access_key",
    "private-key",
    "private_key",
    "credential",
)

# Value-shaped secrets, matched regardless of the surrounding key name.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),  # GitHub personal token
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained token
    re.compile(r"\bgho_[A-Za-z0-9]{20,}"),  # GitHub OAuth token
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}"),  # Google API key
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),  # Hugging Face token
    re.compile(r"\br8_[A-Za-z0-9]{20,}"),  # Replicate token
    re.compile(r"\bxai-[A-Za-z0-9]{20,}"),  # xAI / Grok key
    re.compile(r"\bfal_[A-Za-z0-9_-]{20,}"),  # fal.ai key
    re.compile(r"\bAKIA[A-Z0-9]{16}"),  # AWS access-key id
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)

# ``NAME=value`` / ``"name": "value"`` where NAME contains a secret hint.
_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.\"'-]*(?:" + "|".join(re.escape(h) for h in _SECRET_NAME_HINTS) + r")[A-Za-z0-9_.\"'-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"',]+)",
    re.IGNORECASE,
)


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value[:1] in {'"', "'"} else ""
    return f"{match.group('key')}{match.group('sep')}{quote}{PLACEHOLDER}{quote}"


def _looks_like_secret_flag(arg: str) -> bool:
    if not arg.startswith("-"):
        return False
    stripped = arg.lstrip("-").lower()
    return any(hint in stripped for hint in _SECRET_NAME_HINTS)


def redact_text(text: str) -> str:
    """Return ``text`` with credential-shaped substrings masked."""

    if not text:
        return text
    redacted = _ASSIGNMENT_RE.sub(_redact_assignment, text)
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(PLACEHOLDER, redacted)
    return redacted


def redact_command(command: Sequence[str]) -> list[str]:
    """Redact a command given as an argv list.

    Handles both ``--api-key VALUE`` (separate argument) and inline
    ``API_KEY=VALUE`` / value-shaped tokens.
    """

    out: list[str] = []
    redact_next = False
    for arg in command:
        if redact_next and not arg.startswith("-"):
            out.append(PLACEHOLDER)
            redact_next = False
            continue
        redact_next = False
        if _looks_like_secret_flag(arg) and "=" not in arg and ":" not in arg:
            # Bare secret flag: the following argument is its value.
            out.append(arg)
            redact_next = True
            continue
        out.append(redact_text(arg))
    return out
