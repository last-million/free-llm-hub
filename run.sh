#!/usr/bin/env bash
# Calvoun Free LLM Hub — THE launcher (Linux / macOS / Git Bash).
#
# Deliberately the ONLY .sh in the project root. There used to be a second one
# (autostart.sh) beside it, and two runnable scripts with no way to tell which
# one starts the thing is a coin flip for anyone who did not write them.
# Everything else is a subcommand of this file:
#
#   ./run.sh                    start the hub
#   ./run.sh restart            stop whatever is on the port, then start fresh
#   ./run.sh autostart          also start it at login, and self-heal
#   ./run.sh autostart remove   undo that
#   ./run.sh autostart status   show what is installed
#
# Idempotent: creates a venv on first run, reuses it afterwards. Installs
# Python itself if the machine has none.
set -e
cd "$(dirname "$0")"

HUB_RESTART=""
case "${1:-}" in
  autostart) shift; exec ./scripts/autostart.sh "$@" ;;
  restart) HUB_RESTART=1; shift ;;
  help|-h|--help)
    echo "  ./run.sh                    start the hub"
    echo "  ./run.sh restart            stop whatever is on the port, then start fresh"
    echo "  ./run.sh autostart          also start it at login, and self-heal"
    echo "  ./run.sh autostart remove   undo that"
    echo "  ./run.sh autostart status   show what is installed"
    exit 0 ;;
esac

PORT="${PORT:-8787}"

# --- restart: stop whatever is serving PORT, then fall through to a normal start
# Restarting by hand -- kill whichever pid you happened to find, start a new one
# -- is exactly how you end up with several hubs alive at once. FOUND LIVE
# 2026-08-06: four orphaned `python app.py` processes, only one actually owning
# the port, so every check "passed" against whichever happened to answer. The
# double-bind guard below only refuses a SECOND copy; it cannot clean up after a
# manual kill that missed one. This asks the OS who owns the port instead of
# guessing from a process name, so it can never kill an unrelated python.
hub_pids_on_port() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser "${PORT}/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ano -p TCP 2>/dev/null | grep LISTENING | grep ":${PORT} " \
      | awk '{print $NF}' | grep -E '^[0-9]+$' | sort -u || true
  fi
}

# Git Bash on Windows: `kill` CANNOT reliably signal a native Windows process --
# MSYS emulates signals for its own children only. MEASURED 2026-08-06: both
# `kill` and `kill -9` reported success while the hub kept right on serving, so
# the restart silently did nothing and the guard below then said "already
# running - nothing to do", which reads exactly like success. taskkill is the
# only thing Windows actually honours.
hub_kill_pid() {
  if [ -n "${WINDIR:-}${SYSTEMROOT:-}" ] && command -v taskkill >/dev/null 2>&1; then
    # // is MSYS's escape for a leading / in a native-tool flag.
    taskkill //F //PID "$1" >/dev/null 2>&1 || taskkill /F /PID "$1" >/dev/null 2>&1 || true
  else
    kill "$1" 2>/dev/null || true
  fi
}

if [ -n "$HUB_RESTART" ]; then
  for pid in $(hub_pids_on_port); do
    echo "[free-llm-hub] Stopping the hub on port ${PORT} (pid ${pid})..."
    hub_kill_pid "$pid"
  done
  # Let it drain, then insist. Windows/macOS can hold the socket briefly after
  # the process is gone, so the start below would otherwise race the release.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -z "$(hub_pids_on_port)" ] && break
    sleep 0.5
  done
  for pid in $(hub_pids_on_port); do
    echo "[free-llm-hub] pid ${pid} did not exit - forcing."
    if [ -n "${WINDIR:-}${SYSTEMROOT:-}" ]; then hub_kill_pid "$pid"; else kill -9 "$pid" 2>/dev/null || true; fi
  done
  sleep 1
  # A restart that could not actually stop the old hub must FAIL LOUDLY. Falling
  # through would hit the double-bind guard below, which reports "already running
  # and healthy - nothing to do" and exits 0 -- indistinguishable from a
  # successful restart, while the new code never loaded.
  still="$(hub_pids_on_port)"
  if [ -n "$still" ]; then
    echo "ERROR: could not stop the hub serving port ${PORT} (pid(s): ${still})." >&2
    echo "       Nothing was restarted - the OLD process is still running." >&2
    echo "       Stop it yourself, then run this again." >&2
    exit 1
  fi
fi

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

# First successful setup ever: auto-persist, so a closed terminal or a reboot
# never silently drops the hub again for someone who has no reason to know
# "./run.sh autostart" exists. Once only -- a marker in the state dir (same
# directory as STOP_MARKER above), fully best-effort: never blocks or slows
# the actual start, never touches ~/.free-llm-hub/'s config or history, stays
# silent instead of adding noise to every plain start. autostart.sh's
# `set -e` must not take this whole script down if it fails (e.g. no systemd
# on a minimal box) -- the `if ...; then` form is the standard set -e-safe
# pattern. Called from TWO places: right before the final exec, and from the
# "already running" branch below (a live hub IS proof setup already
# succeeded, and that branch exits before the python/venv checks -- without
# this second call site, the single most common case, re-running the script
# while the hub is already up, would never trigger it).
maybe_autopersist() {
  local marker
  marker="$(dirname "$CONFIG_FILE")/autostart-auto-installed"
  if [ ! -f "$marker" ]; then
    if ./scripts/autostart.sh >/dev/null 2>&1; then
      mkdir -p "$(dirname "$marker")" 2>/dev/null || true
      echo "installed automatically on first successful start -- delete this file to let run.sh try again, or run \"./run.sh autostart remove\" to fully uninstall it" \
        > "$marker" 2>/dev/null || true
    fi
  fi
}

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
    maybe_autopersist
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

maybe_autopersist

echo ""
echo "=========================================================="
echo "  Calvoun Free LLM Hub is starting"
echo "  Dashboard:  http://127.0.0.1:${PORT}"
echo "=========================================================="
echo ""
exec python app.py
