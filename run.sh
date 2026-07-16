#!/usr/bin/env bash
# Calvoun Free LLM Hub — one-command launcher (Linux / macOS / Git Bash)
# Idempotent: creates a venv on first run, reuses it afterwards.
set -e
cd "$(dirname "$0")"

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
  if command -v curl >/dev/null 2>&1 && curl -fsS -m 2 "http://127.0.0.1:${PORT}/api/providers" >/dev/null 2>&1; then
    echo "[free-llm-hub] Already running and healthy on port ${PORT} - nothing to do."
    echo "               Dashboard: http://127.0.0.1:${PORT}"
    echo "               (restart it instead of starting a second copy; HUB_FORCE=1 to override)"
    exit 0
  fi
fi

# --- find python ---
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "ERROR: Python 3.9+ not found. Install it from https://www.python.org/downloads/" >&2
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

echo "[free-llm-hub] Installing dependencies (flask, requests)..."
pip install -q -r requirements.txt

echo ""
echo "=========================================================="
echo "  Calvoun Free LLM Hub is starting"
echo "  Dashboard:  http://127.0.0.1:${PORT}"
echo "=========================================================="
echo ""
exec python app.py
