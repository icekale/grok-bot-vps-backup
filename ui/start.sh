#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$(cd "$HERE/.." && pwd)"
mkdir -p "$ROOT/ui" "$ROOT/hosts"
if ! command -v ssh >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq openssh-client || true
  fi
fi
if ss -lntp 2>/dev/null | grep -q ':8787 '; then
  echo already
  exit 0
fi
nohup python3 "$HERE/app.py" >> "$HERE/ui.log" 2>&1 &
echo started $!
