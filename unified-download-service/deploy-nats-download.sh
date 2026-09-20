#!/usr/bin/env bash
# deploy-nats-download.sh
# Usage:
#   ./deploy-nats-download.sh install      # install deps + systemd unit + start
#   ./deploy-nats-download.sh redeploy     # same as install (idempotent)
#   ./deploy-nats-download.sh reload       # daemon-reload + restart (after editing unit)
#   ./deploy-nats-download.sh restart      # restart service
#   ./deploy-nats-download.sh stop         # stop service
#   ./deploy-nats-download.sh uninstall    # stop, disable, remove unit
#   ./deploy-nats-download.sh status       # service status
#   ./deploy-nats-download.sh help
#
# Env overrides:
#   NATS_URL   default nats://192.168.12.111:4222
#   NATS_COOKIE_FILE  exported Netscape cookie file; skips browser refresh
set -euo pipefail

APP="nats-download-service"
SRC_PY="$(cd "$(dirname "$0")" && pwd)/nats_download_service.py"

BIN_DIR="$HOME/bin"
APP_DIR="$HOME/.local/share/$APP"
VENV="$APP_DIR/venv"
SYSTEMD_DIR="$HOME/.config/systemd/user"
UNIT="$SYSTEMD_DIR/$APP.service"
# ── NATS auth (override via env when calling the deployer) ─────────
NATS_URL="${NATS_URL:-nats://192.168.12.111:4222}"
NATS_USER="${NATS_USER:-zboxnats}"
NATS_PASSWORD="${NATS_PASSWORD:-zboxpswd}"
NATS_TOKEN="${NATS_TOKEN:-}"
NATS_CREDS="${NATS_CREDS:-}"
NATS_NKEY_SEED="${NATS_NKEY_SEED:-}"
NATS_COOKIE_FILE="${NATS_COOKIE_FILE:-}"

help() {
  cat <<EOF
NATS download service deployer

Actions:
  install | redeploy   Install deps, copy service, install & (re)start unit
  reload               systemctl --user daemon-reload + restart (unit was edited)
  restart              Restart running service
  stop                 Stop service (unit kept)
  uninstall            Stop, disable and remove unit + bin copy
  status               Show service status
  help                 Show this help

Env:
  NATS_URL             NATS server URL (default: $NATS_URL)
  NATS_COOKIE_FILE     Netscape cookie file for yt-dlp (optional)

Requires: python3, python3-venv, aria2c, ffmpeg, ffprobe
EOF
}

require() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { echo "missing required tool: $c" >&2; exit 1; }
  done
}

install_deps() {
  echo "==> installing python deps into $VENV"
  mkdir -p "$APP_DIR"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install --upgrade nats-py yt-dlp
  # install the yt-dlp CLI to ~/bin so the service (and you) can call it
  mkdir -p "$BIN_DIR"
  "$VENV/bin/pip" show yt-dlp >/dev/null && ln -sf "$VENV/bin/yt-dlp" "$BIN_DIR/yt-dlp"
}

copy_service() {
  echo "==> copying service to $BIN_DIR/$APP.py"
  mkdir -p "$BIN_DIR"
  install -m 0755 "$SRC_PY" "$BIN_DIR/$APP.py"
}

write_unit() {
  echo "==> writing $UNIT"
  mkdir -p "$SYSTEMD_DIR"

  # Build the Environment= lines, omitting empties.
  env_lines=""
  add_env() {  # add_env KEY VALUE
    if [ -n "$2" ]; then
      env_lines+="Environment=\"$1=$2\""$'\n'
    fi
  }
  add_env NATS_URL        "$NATS_URL"
  add_env NATS_USER       "$NATS_USER"
  add_env NATS_PASSWORD   "$NATS_PASSWORD"
  add_env NATS_TOKEN      "$NATS_TOKEN"
  add_env NATS_CREDS      "$NATS_CREDS"
  add_env NATS_NKEY_SEED  "$NATS_NKEY_SEED"
  add_env NATS_COOKIE_FILE "$NATS_COOKIE_FILE"

  # systemd 'Environment=' with a password → keep the unit file private.
  cat > "$UNIT" <<EOF
[Unit]
Description=NATS Download Service (queue-based, single worker)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$VENV/bin/python $BIN_DIR/$APP.py
${env_lines}Restart=always
RestartSec=5
WorkingDirectory=$HOME

[Install]
WantedBy=default.target
EOF

  chmod 600 "$UNIT"   # contains credentials
}

install_all() {
  require python3 aria2c ffmpeg ffprobe
  install_deps
  copy_service
  write_unit
  systemctl --user daemon-reload
  systemctl --user enable --now "$APP.service"
  systemctl --user restart "$APP.service"
  echo "==> installed & running: $APP.service"
  systemctl --user --no-pager status "$APP.service" | head -n 15 || true
}

reload_all() {
  [ -f "$UNIT" ] || { echo "unit not installed; run 'install' first" >&2; exit 1; }
  copy_service
  systemctl --user daemon-reload
  systemctl --user restart "$APP.service"
  echo "==> reloaded & restarted: $APP.service"
}

restart_only() {
  systemctl --user restart "$APP.service"
  echo "==> restarted: $APP.service"
}

stop_only() {
  systemctl --user stop "$APP.service" || true
  echo "==> stopped: $APP.service"
}

uninstall_all() {
  systemctl --user disable --now "$APP.service" 2>/dev/null || true
  rm -f "$UNIT"
  rm -f "$BIN_DIR/$APP.py"
  systemctl --user daemon-reload
  echo "==> uninstalled: $APP.service"
}

status_only() {
  systemctl --user --no-pager status "$APP.service" || true
}

action="${1:-install}"
case "$action" in
  install|redeploy) install_all ;;
  reload)           reload_all ;;
  restart)          restart_only ;;
  stop)             stop_only ;;
  uninstall)        uninstall_all ;;
  status)           status_only ;;
  -h|--help|help)   help ;;
  *) help; exit 1 ;;
esac