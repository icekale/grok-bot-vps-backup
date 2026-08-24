#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
export GROK_BACKUP_ROOT="$ROOT"
export GROK_BACKUP_BINDIR="$HERE"
mkdir -p "$ROOT"
ID="${1:?host id}"
python3 - "$ID" << 'ENDPY'
import json, os, sys, subprocess, datetime, time, fcntl, re, shlex
from pathlib import Path
from zoneinfo import ZoneInfo

ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

hid = sys.argv[1]
if not ID_RE.match(hid):
    sys.exit("bad id")

root = Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))
cfg = json.loads((root / "hosts.json").read_text())
h = next((x for x in cfg["hosts"] if x["id"] == hid), None)
if not h:
    sys.exit(f"unknown host {hid}")

paths = h.get("paths") or []
if not paths:
    sys.exit("bad path")
for pth in paths:
    if not pth or not PATH_RE.match(pth) or ".." in pth:
        sys.exit("bad path")

dest = root / "hosts" / hid
dest.mkdir(parents=True, exist_ok=True)
dest.chmod(0o700)
tz = ZoneInfo("Asia/Shanghai")
stamp = datetime.datetime.now(tz).strftime("%Y%m%d-%H%M")
out = dest / f"{hid}-{stamp}.tgz"
key = h["key"]
host = f"{h['user']}@{h['host']}"
port = str(h.get("port") or 22)
keep = int(h.get("keep") or 1)
started = time.time()
log_path = dest / "job.log"
status_path = dest / "job.json"
log_path.write_text("", encoding="utf-8")
lock_path = dest / "job.lock"
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
        "updated": datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
    }
    status_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        status_path.chmod(0o600)
        log_path.chmod(0o600)
    except Exception:
        pass


def write_fail(error):
    when = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    fp = dest / "fail.json"
    fp.write_text(json.dumps({
        "when": when,
        "error": error,
        "last_attempt": when,
    }, ensure_ascii=False), encoding="utf-8")
    try:
        fp.chmod(0o600)
    except Exception:
        pass


def log(msg):
    line = f"{now_s()}  {msg}"
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


write_status("connecting", "正在连接…")
log(f"连接 {host}:{port}")
log("打包 " + " ".join(paths))

remote_tar = (
    "tar -C / --warning=no-file-changed --ignore-failed-read -czf - -- "
    + " ".join(shlex.quote(p) for p in paths)
)
cmd = [
    "ssh", "-i", key, "-p", port,
    "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=20",
    host, "--", remote_tar,
]

tmp = Path(str(out) + ".tmp")
written = 0
try:
    with tmp.open("wb") as f:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        last = 0.0
        write_status("archiving", "正在打包…", 0)
        assert p.stdout is not None
        while True:
            chunk = p.stdout.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            now = time.time()
            if now - last >= 0.4:
                last = now
                write_status("archiving", "正在打包…", written)
        err = (p.stderr.read() if p.stderr else b"").decode("utf-8", "replace").strip()
        rc = p.wait()
    if rc != 0:
        tmp.unlink(missing_ok=True)
        if err:
            log(err[-400:])
        write_status("failed", "备份失败", written, err[-400:] if err else f"exit {rc}")
        write_fail(err[-400:] if err else f"exit {rc}")
        sys.exit(rc)
    write_status("saving", "正在保存…", written)
    tmp.replace(out)
    size = out.stat().st_size
    write_status("checking", "校验备份包…", size)
    log("校验 " + out.name)
    chk = subprocess.run(["tar", "-tzf", str(out)], capture_output=True, text=True)
    if chk.returncode != 0:
        err = (chk.stderr or "tar 校验失败")[-300:]
        out.unlink(missing_ok=True)
        write_status("failed", "备份包损坏", size, err)
        log(err)
        write_fail(err)
        sys.exit(1)
    log("校验通过")
    write_status("rotating", "清理旧备份…", size)
    olds = sorted(dest.glob(f"{hid}-*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in olds[keep:]:
        p.unlink()
        side = p.with_name(p.name + ".json")
        if side.exists():
            side.unlink()
        removed += 1
    kept = len(list(dest.glob(f"{hid}-*.tgz")))
    log(f"完成 {out.name}  {size} bytes")
    log(f"保留 {kept} 份" + (f"，删了 {removed} 份旧的" if removed else ""))
    elapsed = round(time.time() - started, 1)
    last_payload = {"seconds": elapsed, "size": size, "when": datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M"), "name": out.name}
    last_fp = dest / "last.json"
    side_fp = dest / (out.name + ".json")
    last_fp.write_text(json.dumps(last_payload, ensure_ascii=False), encoding="utf-8")
    side_fp.write_text(json.dumps(last_payload, ensure_ascii=False), encoding="utf-8")
    (dest / "fail.json").unlink(missing_ok=True)
    dest.chmod(0o700)
    for fp in (out, last_fp, side_fp, status_path, log_path):
        try:
            fp.chmod(0o600)
        except Exception:
            pass
    write_status("done", "备份完成", size)
    print(f"{out} {size}")
    print(f"kept {kept} copies")
except Exception as e:
    tmp.unlink(missing_ok=True)
    write_status("failed", "备份失败", written, str(e))
    log(str(e))
    write_fail(str(e))
    raise
ENDPY
