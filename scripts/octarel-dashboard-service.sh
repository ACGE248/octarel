#!/bin/zsh
# CPX-06: launchd-safe Octarel dashboard entrypoint.
# Never exec macOS Python.app (it hangs in Py_Initialize without a window
# server). Never print secrets. Bind stays 127.0.0.1.
set -euo pipefail

CODE_ROOT="${OCTAREL_CODE_ROOT:-}"
if [[ -z "$CODE_ROOT" ]]; then
  echo "error: OCTAREL_CODE_ROOT is required" >&2
  exit 2
fi
HOST="${OCTAREL_DASHBOARD_HOST:-127.0.0.1}"
PORT="${OCTAREL_DASHBOARD_PORT:-8877}"

for _ in {1..30}; do
  if [[ -d "$CODE_ROOT" ]]; then
    break
  fi
  sleep 1
done
cd "$CODE_ROOT" || {
  echo "error: cannot cd to OCTAREL_CODE_ROOT=$CODE_ROOT" >&2
  exit 2
}
export OCTAREL_CODE_ROOT
export PYTHONUNBUFFERED=1

status_json="$(curl -fsS --max-time 1 "http://${HOST}:${PORT}/api/cp-status" 2>/dev/null || true)"
if [[ "$status_json" == *'"runtime": "octarel"'* || "$status_json" == *'"runtime":"octarel"'* ]]; then
  echo "octarel already healthy on ${HOST}:${PORT}"
  exit 0
fi
if [[ -n "$status_json" ]]; then
  echo "error: ${HOST}:${PORT} is in use by a non-Octarel listener" >&2
  exit 2
fi

py="${CODE_ROOT}/.venv/bin/python3"
if [[ ! -e "$py" ]]; then
  py="${CODE_ROOT}/.venv/bin/python"
fi
if [[ ! -e "$py" ]]; then
  echo "error: no venv python under ${CODE_ROOT}/.venv/bin" >&2
  exit 2
fi
while [[ -L "$py" ]]; do
  target="$(readlink "$py")"
  if [[ "$target" == /* ]]; then
    py="$target"
  else
    py="$(dirname "$py")/$target"
  fi
done
if [[ "$py" == *"/Python.app/"* ]]; then
  version_root="${py%%/Resources/Python.app/*}"
  if [[ -x "${version_root}/bin/python3" ]]; then
    py="${version_root}/bin/python3"
  fi
fi
if [[ "$py" == *"/Python.app/"* ]]; then
  echo "error: refused to exec Python.app under launchd: $py" >&2
  exit 2
fi

state_dir="${OCTAREL_STATE_DIR:-${CODE_ROOT}/.orchestrator-state}"
mkdir -p "$state_dir" 2>/dev/null || true
# launchd on macOS cannot write a pid file onto an external-volume checkout.
# The wrapper already lives on the boot volume under Application Support;
# the pid file must live there too. Health/lsof on 127.0.0.1:8877 remains
# the listener authority. Never abort the service for a pid-file failure.
support_dir="${HOME}/Library/Application Support/Octarel"
mkdir -p "$support_dir" 2>/dev/null || true
echo $$ > "${support_dir}/dashboard.pid" 2>/dev/null || true

exec "$py" -m octarel dashboard --host "$HOST" --port "$PORT"
