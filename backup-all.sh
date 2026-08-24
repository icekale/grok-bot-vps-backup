#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$HERE"
mkdir -p "$ROOT"
python3 - << 'PY'
import json, os, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

tz = ZoneInfo("Asia/Shanghai")
root = Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))
bindir = Path(os.environ.get("GROK_BACKUP_BINDIR") or root)
st = os.statvfs(str(root))
total = st.f_frsize * st.f_blocks
free = st.f_frsize * st.f_bavail
pct = 100.0 * (1 - free / total) if total else 0
warn = free < 3 * 1024**3 or pct >= 85
print(f"DISK {'WARN' if warn else 'ok'} {pct:.1f}% free {free}")

cfg = json.loads((root / "hosts.json").read_text())
rc = 0
ran = 0
for h in cfg.get("hosts") or []:
    hid = h["id"]
    hours = max(1, min(24 * 30, int(h.get("interval_hours") or 24)))
    lastp = root / "hosts" / hid / "last.json"
    due = True
    if lastp.exists():
        try:
            last = json.loads(lastp.read_text())
            t = datetime.strptime(last["when"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            due = datetime.now(tz) >= t + timedelta(hours=hours)
        except Exception:
            due = True
    if not due:
        print("==", hid, "SKIP")
        continue
    print("==", hid, "BACKUP")
    r = subprocess.run([str(bindir / "backup-one.sh"), hid])
    ran += 1
    if r.returncode != 0:
        rc = r.returncode
if ran == 0:
    print("NOTHING_DUE")
sys.exit(rc)
PY
