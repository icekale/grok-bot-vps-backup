#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$(cd "$HERE/.." && pwd)"
exec >> "$HERE/watchdog.log" 2>&1
if [ -f "$HERE/watchdog.pid" ] && kill -0 "$(cat "$HERE/watchdog.pid")" 2>/dev/null; then
  echo already "$(cat "$HERE/watchdog.pid")"
  exit 0
fi
echo $$ > "$HERE/watchdog.pid"
while true; do
  "$HERE/start.sh" || true
  sleep 20
done
