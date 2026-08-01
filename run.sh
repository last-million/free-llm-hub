#!/usr/bin/env bash
# Calvoun Free LLM Hub — THE launcher (Linux / macOS / Git Bash).
#
# Deliberately the ONLY .sh in the project root. There used to be a second one
# (autostart.sh) beside it, and two runnable scripts with no way to tell which
# one starts the thing is a coin flip for anyone who did not write them.
# Everything else is a subcommand of this file:
#
#   ./run.sh                    start the hub
#   ./run.sh autostart          also start it at login, and self-heal
#   ./run.sh autostart remove   undo that
#   ./run.sh autostart status   show what is installed
#
# Idempotent: creates a venv on first run, reuses it afterwards. Installs
# Python itself if the machine has none.
set -e
cd "$(dirname "$0")"

case "${1:-}" in
  autostart) shift; exec ./scripts/autostart.sh "$@" ;;
  help|-h|--help)
    echo "  ./run.sh                    start the hub"
    echo "  ./run.sh autostart          also start it at login, and self-heal"
    echo "  ./run.sh autostart remove   undo that"
    echo "  ./run.sh autostart status   show what is installed"
    exit 0 ;;
esac

PORT="${PORT:-8787}"

# A dashboard Stop is intentional, not a crash. Supervisors set
# HUB_SUPERVISED=1; in that mode the marker makes this launcher a clean no-op.
# A person running this script explicitly clears the marker and starts again.
if [ -n "${FREE_LLM_HUB_CONFIG:-}" ]; then
  CONFIG_FILE="${FREE_LLM_HUB_CONFIG/#\~/$HOME}"
else
  CONFIG_FILE="$HOME/.free-llm-hub/config.json"
fi
STOP_MARKER="$(dirname "$CONFIG_FILE")/intentional-stop"
if [ "${HUB_SUPERVISED:-}" = "1" ] && [ -f "$STOP_MARKER" ]; then
  echo "[free-llm-hub] Intentionally stopped from the dashboard - supervisor restart skipped."
  exit 0
fi
if [ "${HUB_SUPERVISED:-}" != "1" ]; then
  rm -f "$STOP_MARKER"
fi

# --- refuse to double-bind -------------------------------------------------
# Werkzeug sets SO_REUSEADDR, so on some platforms a SECOND process can bind a
# port that is already served. You then get two hubs alive at once and requests
# land on whichever won - typically the OLD one, so code changes appear not to
# take effect and any check you run "passes" against a stale process. Cheaper to
# refuse than to debug. HUB_FORCE=1 overrides.
if [ -z "${HUB_FORCE:-}" ]; then
  # No -f: any completed HTTP response counts as "already running" -- since
  # SEC-001 added a control-token requirement to every /api/* route (this one
  # included), an unauthenticated request now gets a 401, and -f treats that
  # as failure. That silently broke this exact double-bind check (it always
  # said "not running" once a token existed, so a second copy would start).
  if command -v curl >/dev/null 2>&1 && curl -sS -m 2 -o /dev/null "http://127.0.0.1:${PORT}/api/providers"; then
    echo "[free-llm-hub] Already running and healthy on port ${PORT} - nothing to do."
    echo "               Dashboard: http://127.0.0.1:${PORT}"
    echo "               (restart it instead of starting a second copy; HUB_FORCE=1 to override)"
    exit 0
  fi
fi

# --- find python, and INSTALL it if this machine has none --------------------
# "Download it, run one file" only holds if the one file also handles a machine
# with no Python on it. Sending someone off to install Python first is exactly
# the wall this script exists to remove.
find_python() {
  PY=""
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' >/dev/null 2>&1; then
    PY=python3
  elif command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
    PY=python
  fi
  # Debian and Ubuntu ship venv as a SEPARATE package, so python3 exists while
  # `python3 -m venv` fails. Treat that as "not usable yet" rather than letting
  # it blow up three lines later with a message nobody can act on.
  if [ -n "$PY" ] && ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    NEEDS_VENV=1
  fi
}

install_python() {
  # sudo only if we are not already root and it exists; a machine without it
  # gets a clear instruction instead of a permission error.
  SUDO=""
  if [ "$(id -u)" != "0" ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  fi
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq || true
    $SUDO apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 python3-pip
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm python python-pip
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper install -y python3 python3-pip
  elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add --no-cache python3 py3-pip
  elif command -v brew >/dev/null 2>&1; then
    brew install python           # never with sudo: Homebrew refuses outright
  else
    return 1
  fi
}

NEEDS_VENV=""
find_python
if [ -z "$PY" ] || [ -n "$NEEDS_VENV" ]; then
  if [ -z "$PY" ]; then
    echo "[free-llm-hub] Python was not found on this machine - installing it."
  else
    echo "[free-llm-hub] Python is missing its venv module - installing it."
  fi
  install_python || true
  NEEDS_VENV=""
  find_python
fi
if [ -z "$PY" ]; then
  echo "ERROR: could not install Python automatically." >&2
  echo "       Install Python 3.9+ with your package manager (or from" >&2
  echo "       https://www.python.org/downloads/) and run this file again." >&2
  exit 1
fi

# --- venv (create once, reuse forever) ---
if [ ! -d ".venv" ]; then
  echo "[free-llm-hub] Creating virtual environment..."
  "$PY" -m venv .venv
fi

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
  # Git Bash on Windows
  # shellcheck disable=SC1091
  . .venv/Scripts/activate
fi

# --- dependencies: install ONLY when actually missing or changed -------------
# This ran `pip install` on EVERY start, adding 60-200s of network round-trips
# before the hub bound its port. The stamp holds a hash of requirements.txt, so
# a pinned-version bump still triggers a real install; nothing else does.
DEPS_STAMP=".venv/.deps-stamp"
if python - <<'PYCHK' >/dev/null 2>&1
import hashlib, os, sys
h = hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest()
p = os.path.join(".venv", ".deps-stamp")
ok = os.path.exists(p) and open(p).read().strip() == h
import flask, requests          # noqa: F401 — must be importable, not just stamped
sys.exit(0 if ok else 1)
PYCHK
then
  echo "[free-llm-hub] Dependencies already installed - skipping pip."
else
  echo "[free-llm-hub] Installing dependencies (flask, requests)..."
  pip install -q -r requirements.txt
  python -c "import hashlib;open('$DEPS_STAMP','w').write(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())" 2>/dev/null || true
fi

echo ""
echo "=========================================================="
echo "  Calvoun Free LLM Hub is starting"
echo "  Dashboard:  http://127.0.0.1:${PORT}"
echo "=========================================================="
echo ""
exec python app.py
