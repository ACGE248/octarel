"""CPX-06: standalone Octarel dashboard service helpers.

Launchd must exec a Unix CLI interpreter, never macOS ``Python.app`` (that
binary hangs during ``Py_Initialize`` with no window server). Duplicate
listens are refused. Secrets stay in the operator's local launchd environment,
never in this module.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8877
PID_FILENAME = "dashboard.pid"
LABEL = "com.octascene.orchestrator-dashboard"
USER_WRAPPER_NAME = "octarel-dashboard-service.sh"


def user_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Octarel"


def installed_wrapper_path() -> Path:
    return user_support_dir() / USER_WRAPPER_NAME


def runtime_pid_path() -> Path:
    """launchd-writable pid path on the boot volume, never the checkout.

    macOS launchd is not permitted to write into an external-volume
    ``OCTAREL_STATE_DIR``. Listener identity still comes from loopback
    health on port 8877; this file is only a convenience for operators.
    """

    return user_support_dir() / PID_FILENAME


def install_user_wrapper(code_root: Path) -> Path:
    """Copy the launchd wrapper onto the boot volume.

    launchd cannot reliably open a script on an external checkout at login
    (``zsh: can't open input file``). The copy contains no secrets.
    """

    source = code_root / "scripts" / USER_WRAPPER_NAME
    if not source.is_file():
        raise FileNotFoundError(f"missing dashboard wrapper: {source}")
    dest = installed_wrapper_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    dest.chmod(0o755)
    return dest


def resolve_cli_python(venv_dir: Path) -> Path:
    """Return a non-GUI Python capable of running under launchd."""

    bin_dir = venv_dir / "bin" if venv_dir.name != "bin" else venv_dir
    for name in ("python3", "python"):
        candidate = bin_dir / name
        if not candidate.exists():
            continue
        resolved = _prefer_frameworks_cli(candidate.resolve())
        if resolved is not None:
            return resolved
    raise FileNotFoundError(f"no CLI python under {bin_dir}")


def _prefer_frameworks_cli(resolved: Path) -> Path | None:
    text = str(resolved)
    if "/Python.app/" in text:
        parts = list(resolved.parts)
        try:
            idx = parts.index("Python.framework")
            version_dir = Path(*parts[: idx + 3])  # .../Python.framework/Versions/3.14
        except (ValueError, IndexError):
            return None
        for name in (resolved.name, "python3", "python3.14", "python3.13", "python3.12", "python3.11"):
            cli = version_dir / "bin" / name
            if cli.is_file() and "/Python.app/" not in str(cli.resolve()):
                return cli
        return None
    return resolved


def listener_pid(port: int, host: str = DEFAULT_HOST) -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP@{host}:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        return int(line[0])
    except ValueError:
        return None


def fetch_health(port: int, host: str = DEFAULT_HOST, timeout: float = 2.0) -> dict | None:
    url = f"http://{host}:{port}/api/cp-status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def is_octarel_listener(port: int, host: str = DEFAULT_HOST, code_root: Path | None = None) -> bool:
    body = fetch_health(port, host=host)
    if not body:
        return False
    if body.get("runtime") != "octarel":
        return False
    if code_root is None:
        return True
    reported = body.get("cp_code_root")
    return reported is not None and Path(reported).resolve() == code_root.resolve()


def pid_file(state_dir: Path) -> Path:
    return state_dir / PID_FILENAME


def write_pid_file(state_dir: Path, pid: int) -> Path:
    path = pid_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def read_pid_file(state_dir: Path) -> int | None:
    path = pid_file(state_dir)
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for_health(
    port: int,
    *,
    host: str = DEFAULT_HOST,
    timeout: float = 20.0,
    code_root: Path | None = None,
) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = fetch_health(port, host=host)
        if body and body.get("runtime") == "octarel":
            if code_root is None or Path(str(body.get("cp_code_root") or "")).resolve() == code_root.resolve():
                return body
        time.sleep(0.25)
    return None


def refuse_if_foreign_listener(port: int, host: str = DEFAULT_HOST, code_root: Path | None = None) -> str | None:
    pid = listener_pid(port, host=host)
    if pid is None:
        return None
    if is_octarel_listener(port, host=host, code_root=code_root):
        return None
    return f"port {port} is already in use by pid {pid} (not Octarel)"


def stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def render_launchd_plist(
    *,
    code_root: Path,
    octascene_root: Path | None = None,
    extra_env: dict[str, str] | None = None,
    label: str = LABEL,
    port: int = DEFAULT_PORT,
    wrapper_path: Path | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
) -> bytes:
    """Zero-secret plist bytes. Remote Access identifiers come from extra_env only."""

    extra = extra_env or {}
    env = {
        "PATH": extra.get("PATH") or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "OCTAREL_CODE_ROOT": str(code_root),
        "OCTAREL_STATE_DIR": extra.get("OCTAREL_STATE_DIR") or str(code_root / ".orchestrator-state"),
        "OCTAREL_DASHBOARD_HOST": extra.get("OCTAREL_DASHBOARD_HOST") or "127.0.0.1",
        "OCTAREL_DASHBOARD_PORT": extra.get("OCTAREL_DASHBOARD_PORT") or str(port),
    }
    if octascene_root is not None:
        env["OCTAREL_OCTASCENE_ROOT"] = str(octascene_root)
        env["OCTAGES_ORCH_CANONICAL_REPO_ROOT"] = str(octascene_root)
    for key in (
        "OCTAGES_ORCH_REMOTE_ENABLED",
        "OCTAGES_ORCH_REMOTE_HOSTNAME",
        "OCTAGES_ORCH_REMOTE_TEAM_DOMAIN",
        "OCTAGES_ORCH_REMOTE_AUD",
        "OCTAGES_ORCH_REMOTE_ALLOWED_EMAILS",
        "OCTAREL_OCTASCENE_ROOT",
        "OCTAGES_ORCH_CANONICAL_REPO_ROOT",
        "OCTAREL_STATE_DIR",
        "OCTAREL_CODE_ROOT",
    ):
        if extra.get(key):
            env[key] = extra[key]
    wrapper = str(wrapper_path or installed_wrapper_path())
    payload = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", wrapper],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ThrottleInterval": 10,
        "StandardOutPath": stdout_path or "/tmp/octarel-orchestrator-dashboard.log",
        "StandardErrorPath": stderr_path or "/tmp/octarel-orchestrator-dashboard.err.log",
    }
    return plistlib.dumps(payload)


def port_free(port: int, host: str = DEFAULT_HOST) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def canonical_state_dir(code_root: Path) -> Path:
    """Live SQLite belongs next to the canonical Octarel checkout, not a feature worktree."""

    return code_root / ".orchestrator-state"


def state_dir_is_canonical(code_root: Path, state_dir: Path | str | None) -> bool:
    if state_dir is None:
        return True
    return Path(state_dir).expanduser().resolve() == canonical_state_dir(code_root).resolve()


def read_launchd_env(plist_path: Path) -> dict[str, str]:
    if not plist_path.is_file():
        return {}
    data = plistlib.loads(plist_path.read_bytes())
    raw = data.get("EnvironmentVariables") or {}
    return {str(k): str(v) for k, v in raw.items()}


def canonicalize_state_dir(
    *,
    source: Path,
    destination: Path,
    dry_run: bool = False,
    replace_unmigrated: bool = True,
):
    """Copy live state onto the canonical checkout path. Never deletes the source."""

    from .state_migration import migrate_orchestrator_state

    return migrate_orchestrator_state(
        source=source,
        destination=destination,
        dry_run=dry_run,
        replace_unmigrated_destination=replace_unmigrated,
    )
