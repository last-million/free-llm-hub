#!/usr/bin/env bash
# Calvoun Free LLM Hub — one-command launcher (Linux / macOS / Git Bash)
# Idempotent: creates a venv on first run, reuses it afterwards.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8787}"

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
