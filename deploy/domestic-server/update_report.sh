#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/mytradingsystem}"
WEB_DIR="${WEB_DIR:-/var/www/mytradingsystem}"
LOG_DIR="${LOG_DIR:-/var/log/mytradingsystem}"
LOCK_FILE="${LOCK_FILE:-/tmp/mytradingsystem-update.lock}"

mkdir -p "$LOG_DIR" "$WEB_DIR"

(
  flock -n 9 || exit 0
  cd "$APP_DIR"

  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git fetch --all --prune >>"$LOG_DIR/update.log" 2>&1 || true
    git pull --ff-only >>"$LOG_DIR/update.log" 2>&1 || true
  fi

  python3 work/index-env/market_env.py all >>"$LOG_DIR/update.log" 2>&1

  cp -R outputs/. "$WEB_DIR/"
  date '+%Y-%m-%d %H:%M:%S %Z' > "$WEB_DIR/last_update.txt"
) 9>"$LOCK_FILE"
