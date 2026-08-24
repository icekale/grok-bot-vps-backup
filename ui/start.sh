#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$(cd "$HERE/.." && pwd)"
mkdir -p "$ROOT/ui" "$ROOT/hosts"
if ss -lntp 2>/dev/null | grep -q ':8787 '; then
  echo already
  exit 0
fi
nohup python3 "$HERE/app.py" >> "$HERE/ui.log" 2>&1 &
echo started $!
