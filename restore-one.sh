#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$HERE"
ID="${1:?host id}"
NAME="${2:?backup file}"
python3 - "$ID" "$NAME" << 'ENDPY'
import json, os, sys, subprocess, datetime, time, fcntl, re, shlex
from pathlib import Path
from zoneinfo import ZoneInfo

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")
FILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}-\d{8}-\d{4}\.tgz$")
STAMP_RE = re.compile(r"^\d{8}-\d{4}$")

hid, name = sys.argv[1], sys.argv[2]
if not ID_RE.match(hid):
    sys.exit("bad id")
if not FILE_RE.match(name):
    sys.exit("bad name")
if not name.startswith(hid + "-") or not name.endswith(".tgz"):
    sys.exit("bad name")

root = Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))
cfg = json.loads((root / "hosts.json").read_text())
h = next((x for x in cfg["hosts"] if x["id"] == hid), None)
if not h:
    sys.exit(f"unknown host {hid}")
src = root / "hosts" / hid / name
if not src.is_file():
    sys.exit("missing backup")

dest = root / "hosts" / hid
tz = ZoneInfo("Asia/Shanghai")
stamp = name[len(hid) + 1 : -4]
if not STAMP_RE.match(stamp):
    sys.exit("bad stamp")
remote = f"/opt/vps-restore/{hid}/{stamp}"
key = h["key"]
host = f"{h['user']}@{h['host']}"
port = str(h.get("port") or 22)
started = time.time()
log_path = dest / "job.log"
status_path = dest / "job.json"
lock_path = dest / "job.lock"
log_path.write_text("", encoding="utf-8")
lockf = lock_path.open("w")
try:
    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit("host busy")


def now_s():
    return datetime.datetime.now(tz).strftime("%H:%M:%S")


def write_status(phase, message, bytes_n=0, error=""):
    payload = {
        "phase": phase,
        "message": message,
        "bytes": int(bytes_n),
        "seconds": round(time.time() - started, 1),
        "error": error,
        "remote": remote,
        "updated": datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
    }
    status_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def log(msg):
    line = f"{now_s()}  {msg}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


write_status("checking", "检查备份包…")
log(f"校验 {name}")
chk = subprocess.run(["tar", "-tzf", str(src)], capture_output=True)
if chk.returncode != 0:
    err = ((chk.stderr.decode("utf-8", "replace") if chk.stderr else "tar 校验失败"))[-300:]
    write_status("failed", "备份包损坏", 0, err)
    log(err)
    sys.exit(1)

raw = chk.stdout or b""
if b"\0" in raw:
    write_status("failed", "备份包含非法路径", 0, "nul in listing")
    log("拒绝：归档列表含 NUL")
    sys.exit(1)
listing = raw.decode("utf-8", "surrogateescape")
for member in listing.splitlines():
    if not member:
        continue
    if ".." in member or member.startswith("/") or "\0" in member:
        write_status("failed", "备份包含非法路径", 0, "bad member")
        log("拒绝：非法成员 " + member[:80])
        sys.exit(1)

total = src.stat().st_size
write_status("restoring", "传到旁边目录…", 0)
log(f"恢复到 {host}:{remote}")
log("不会覆盖正在跑的站点")

qremote = shlex.quote(remote)
cmd = [
    "ssh", "-i", key, "-p", port,
    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=20",
    host, "--", f"mkdir -p -- {qremote} && tar -C {qremote} --no-absolute-names -xzf -",
]
written = 0
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
last = 0.0
with src.open("rb") as f:
    while True:
        chunk = f.read(256 * 1024)
        if not chunk:
            break
        try:
            p.stdin.write(chunk)
        except BrokenPipeError:
            break
        written += len(chunk)
        now = time.time()
        if now - last >= 0.4:
            last = now
            write_status("restoring", "传到旁边目录…", written)
if p.stdin:
    p.stdin.close()
err = (p.stderr.read() if p.stderr else b"").decode("utf-8", "replace").strip()
rc = p.wait()
if rc != 0:
    write_status("failed", "恢复失败", written, err[-400:] if err else f"exit {rc}")
    if err:
        log(err[-400:])
    sys.exit(rc)
write_status("done", f"已放到 {remote}", total)
log(f"完成 {remote}")
print(remote)
ENDPY
