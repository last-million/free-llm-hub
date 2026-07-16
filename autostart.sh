#!/usr/bin/env bash
# Calvoun Free LLM Hub - install/remove autostart (Linux systemd / macOS launchd)
#
# WHY: the hub is a foreground process. Close the terminal, log out, or reboot
# and it is gone - and then every CLI pointed at it silently loses the free
# fleet (or falls back to a paid path). This installs a per-user service that
# starts it at login and restarts it if it dies.
#
# Per-USER, no root/sudo: it runs as you, in your session, and reads your keys
# from ~/.free-llm-hub. A system-wide unit would run as another user and could
# not see them.
#
#   ./autostart.sh           install (or refresh)
#   ./autostart.sh remove    uninstall
#   ./autostart.sh status    show current state
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
PORT="${PORT:-8787}"
LABEL="com.calvoun.free-llm-hub"
ACTION="${1:-install}"

case "$(uname -s)" in
  Darwin) PLATFORM=macos ;;
  Linux)  PLATFORM=linux ;;
  *)      echo "ERROR: unsupported platform $(uname -s). On Windows use autostart.bat." >&2; exit 1 ;;
esac

# --------------------------------------------------------------------------- #
if [ "$PLATFORM" = linux ]; then
  command -v systemctl >/dev/null 2>&1 || {
    echo "ERROR: systemctl not found. No systemd on this box - start the hub from" >&2
    echo "       your own init system, or just run ./run.sh." >&2; exit 1; }
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT="$UNIT_DIR/free-llm-hub.service"

  case "$ACTION" in
    remove)
      systemctl --user disable --now free-llm-hub.service 2>/dev/null || true
      rm -f "$UNIT"; systemctl --user daemon-reload 2>/dev/null || true
      echo "[autostart] Removed. The hub will no longer start automatically."; exit 0 ;;
    status)
      systemctl --user status free-llm-hub.service --no-pager 2>/dev/null || echo "[autostart] Not installed."
      exit 0 ;;
  esac

  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=Calvoun Free LLM Hub (local gateway for free LLM providers)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HERE
Environment=PORT=$PORT
Environment=HUB_SUPERVISED=1
ExecStart=$HERE/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now free-llm-hub.service
  echo
  echo "  Installed (systemd user unit). Starts at login, restarts on crash."
  echo "  Dashboard: http://127.0.0.1:$PORT"
  echo
  echo "  Survive logout too:  sudo loginctl enable-linger $USER"
  echo "  Logs:                journalctl --user -u free-llm-hub -f"
  echo "  Remove:              ./autostart.sh remove"
  exit 0
fi

# --------------------------------------------------------------------------- #
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
case "$ACTION" in
  remove)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "[autostart] Removed. The hub will no longer start automatically."; exit 0 ;;
  status)
    launchctl list 2>/dev/null | grep -q "$LABEL" && echo "[autostart] Loaded: $LABEL" || echo "[autostart] Not installed."
    exit 0 ;;
esac

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$HERE/run.sh</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key><string>$PORT</string>
    <key>HUB_SUPERVISED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>/tmp/free-llm-hub.log</string>
  <key>StandardErrorPath</key><string>/tmp/free-llm-hub.err</string>
</dict>
</plist>
EOF
chmod +x "$HERE/run.sh" 2>/dev/null || true
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo
echo "  Installed (launchd agent). Starts at login, restarts on crash."
echo "  Dashboard: http://127.0.0.1:$PORT"
echo
echo "  Logs:   /tmp/free-llm-hub.log  and  /tmp/free-llm-hub.err"
echo "  Remove: ./autostart.sh remove"
