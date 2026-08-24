#!/usr/bin/env python3
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
import sys

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR / "quark"))
try:
    import bridge as quark_bridge
except Exception:
    quark_bridge = None

ROOT = Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))
HOSTS_FILE = ROOT / "hosts.json"


def ensure_token():
    token_path = ROOT / "ui" / "token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.exists():
        tok = token_path.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(16)
    token_path.write_text(tok + "\n")
    token_path.chmod(0o600)
    return tok


TOKEN = ensure_token()
PORT = 8787
TZ = timezone(timedelta(hours=8))
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")
FILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}-\d{8}-\d{4}\.tgz$")
PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

_lock = threading.Lock()
_running = {}
_errors = {}


def load_hosts():
    if not HOSTS_FILE.exists():
        return {"hosts": []}
    return json.loads(HOSTS_FILE.read_text())


def save_hosts(data):
    tmp = ROOT / "hosts.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, HOSTS_FILE)
    try:
        HOSTS_FILE.chmod(0o600)
    except Exception:
        pass


def get_host(hid):
    return next((h for h in load_hosts()["hosts"] if h["id"] == hid), None)


def dest_for(hid):
    if not ID_RE.match(hid):
        raise ValueError("bad id")
    root_hosts = (ROOT / "hosts").resolve()
    p = (root_hosts / hid).resolve()
    if not p.is_relative_to(root_hosts) or p == root_hosts:
        raise ValueError("bad dest")
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_items(hid):
    items = []
    for p in sorted(dest_for(hid).glob(f"{hid}-*.tgz"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not FILE_RE.match(p.name):
            continue
        st = p.stat()
        m = re.match(r".*-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\.tgz$", p.name)
        when = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else p.name
        meta = {}
        side = p.with_name(p.name + ".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except Exception:
                meta = {}
        items.append({"name": p.name, "size": st.st_size, "when": when, "seconds": meta.get("seconds")})
    return items


def last_backup(hid):
    items = list_items(hid)
    return items[0]["when"] if items else None


def _parse_when(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except Exception:
        return None


def host_due_at(h):
    hours = max(1, min(24 * 30, int(h.get("interval_hours") or 24)))
    last = _parse_when((read_last(h["id"]) or {}).get("when"))
    fail = read_fail(h["id"]) or {}
    fail_t = _parse_when(fail.get("last_attempt") or fail.get("when"))
    times = []
    if last:
        times.append(last + timedelta(hours=hours))
    if fail_t:
        times.append(fail_t + timedelta(hours=max(float(hours), 0.5)))
    if not times:
        return None
    return max(times)


def host_next(h):
    due_at = host_due_at(h)
    now = datetime.now(TZ)
    if due_at is None or now >= due_at:
        return {"due": True, "label": "现在", "at": due_at.strftime("%Y-%m-%d %H:%M") if due_at else None}
    secs = (due_at - now).total_seconds()
    if secs < 3600:
        label = f"{max(1, int(secs // 60))} 分钟后"
    elif secs < 86400:
        label = f"{int(secs // 3600)} 小时后"
    else:
        label = f"{int(secs // 86400)} 天后"
    return {"due": False, "label": label, "at": due_at.strftime("%Y-%m-%d %H:%M")}


def read_last(hid):
    fp = dest_for(hid) / "last.json"
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text())
    except Exception:
        return {}


def host_bytes(hid):
    return sum(p.stat().st_size for p in dest_for(hid).glob(f"{hid}-*.tgz") if FILE_RE.match(p.name))


def read_fail(hid):
    fp = dest_for(hid) / "fail.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text())
    except Exception:
        return None


def disk_info():
    st = os.statvfs(str(ROOT))
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    pct = round(100.0 * (1 - free / total), 1) if total else 0
    return {
        "total": total,
        "free": free,
        "used_pct": pct,
        "warn": bool(free < 3 * 1024**3 or pct >= 85),
    }


def read_job(hid):
    dest = dest_for(hid)
    job = {"phase": "", "message": "", "bytes": 0, "seconds": 0, "error": "", "log": ""}
    jp = dest / "job.json"
    if jp.exists():
        try:
            job.update(json.loads(jp.read_text()))
        except Exception:
            pass
    lp = dest / "job.log"
    if lp.exists():
        try:
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            job["log"] = "\n".join(lines[-40:])
        except Exception:
            pass
    return job


def ssh_probe(h, key_path=None):
    key = key_path or h["key"]
    cmd = [
        "ssh", "-i", str(key), "-p", str(h.get("port") or 22),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=8",
        f"{h['user']}@{h['host']}",
        "printf '%s\\t%s' \"$(hostname)\" \"$(df -h / | awk 'NR==2{print $4}')\"",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "连接失败").strip()
        return False, err[-240:]
    parts = (r.stdout or "").strip().split("\t", 1)
    name = parts[0] if parts else "ok"
    free = parts[1] if len(parts) > 1 else ""
    msg = name + (f" · 磁盘剩余 {free}" if free else "")
    return True, msg


def run_backup(hid):
    with _lock:
        if _running.get(hid):
            return False
        _running[hid] = True
        _errors[hid] = None

    def _job():
        try:
            r = subprocess.run(
                [str(CODE_DIR / "backup-one.sh"), hid],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "GROK_BACKUP_ROOT": str(ROOT), "GROK_BACKUP_BINDIR": str(CODE_DIR)},
            )
            if r.returncode != 0:
                job = read_job(hid)
                _errors[hid] = job.get("error") or (r.stderr or r.stdout or "备份失败")[-400]
            else:
                _errors[hid] = None
                try:
                    if quark_bridge and quark_bridge.load_settings().get("auto"):
                        threading.Thread(target=quark_bridge.run_upload, daemon=True).start()
                except Exception:
                    pass
        except Exception as e:
            _errors[hid] = str(e)
            dest = dest_for(hid)
            try:
                (dest / "job.json").write_text(json.dumps({
                    "phase": "failed", "message": "备份失败", "bytes": 0,
                    "seconds": 0, "error": str(e),
                }, ensure_ascii=False))
            except Exception:
                pass
        finally:
            with _lock:
                _running[hid] = False

    threading.Thread(target=_job, daemon=True).start()
    return True


def host_is_due(h):
    due_at = host_due_at(h)
    if due_at is None:
        return True
    return datetime.now(TZ) >= due_at


def scheduler_loop():
    while True:
        try:
            for h in load_hosts().get("hosts") or []:
                hid = h.get("id")
                if not hid or not ID_RE.match(hid):
                    continue
                if host_is_due(h) and not _running.get(hid):
                    run_backup(hid)
        except Exception:
            pass
        time.sleep(60)


def run_restore(hid, name):
    with _lock:
        if _running.get(hid):
            return False
        _running[hid] = True
        _errors[hid] = None

    def _job():
        try:
            r = subprocess.run(
                [str(CODE_DIR / "restore-one.sh"), hid, name],
                capture_output=True, text=True, timeout=600,
                env={**os.environ, "GROK_BACKUP_ROOT": str(ROOT), "GROK_BACKUP_BINDIR": str(CODE_DIR)},
            )
            if r.returncode != 0:
                job = read_job(hid)
                _errors[hid] = job.get("error") or (r.stderr or r.stdout or "恢复失败")[-400]
            else:
                _errors[hid] = None
        except Exception as e:
            _errors[hid] = str(e)
            try:
                (dest_for(hid) / "job.json").write_text(json.dumps({
                    "phase": "failed", "message": "恢复失败", "bytes": 0,
                    "seconds": 0, "error": str(e),
                }, ensure_ascii=False))
            except Exception:
                pass
        finally:
            with _lock:
                _running[hid] = False

    threading.Thread(target=_job, daemon=True).start()
    return True


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VPS 备份</title>
<style>
  :root {
    --bg:#0b0b0f; --panel:#121218; --card:#1a1a22; --line:#2a2a34;
    --text:#f3f3f7; --muted:#8d8d9a; --acc:#7c9cff; --acc2:#1c2438;
    --danger:#ff6b7a; --ok:#3dcf9a; --warn:#f5c14a;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; color: var(--text);
    font: 14px/1.45 ui-sans-serif, system-ui, "Noto Sans CJK SC", "PingFang SC", sans-serif;
    background:
      radial-gradient(900px 420px at 0% 0%, #182044 0%, transparent 55%),
      var(--bg);
  }
  .app { display: flex; min-height: 100vh; }
  aside {
    width: 280px; flex-shrink: 0; background: color-mix(in srgb, var(--panel) 92%, transparent);
    border-right: 1px solid var(--line); padding: 22px 14px 16px;
    display: flex; flex-direction: column; backdrop-filter: blur(10px);
  }
  .brand { cursor: pointer; border-radius: 12px; padding: 6px 8px; margin: -6px -8px 12px; }
  .brand:hover { background: #1a1a24; }
  .brand.on { background: #1c2438; }
  .brand h1 { font-size: 16px; margin: 0; font-weight: 650; letter-spacing: .02em; }
  .brand p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
  .back {
    display: inline-flex; align-items: center; gap: 4px; margin: 0 0 8px; padding: 0;
    background: none; color: var(--muted); font-size: 12px;
  }
  .back:hover { color: var(--text); }
  [hidden] { display: none !important; }
  .hosts { display: flex; flex-direction: column; gap: 6px; flex: 1; }
  .host {
    text-align: left; border: 0; background: transparent;
    color: var(--text); border-radius: 12px; padding: 10px 12px; cursor: pointer; width: 100%;
    display: grid; grid-template-columns: 10px 1fr; gap: 10px; align-items: center;
  }
  .host:hover { background: #1a1a24; }
  .host.active { background: var(--acc2); }
  .host b { display: block; font-size: 14px; font-weight: 600; }
  .host span { color: var(--muted); font-size: 12px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #555; }
  .dot.ok { background: var(--ok); box-shadow: 0 0 0 3px #3dcf9a22; }
  .dot.warn { background: var(--warn); box-shadow: 0 0 0 3px #f5c14a22; }
  .dot.err { background: var(--danger); box-shadow: 0 0 0 3px #ff6b7a22; }
  .dot.run { background: var(--acc); box-shadow: 0 0 0 3px #7c9cff33; animation: pulse 1.2s infinite; }
  @keyframes pulse { 50% { opacity: .45; } }
  .add {
    margin-top: 10px; width: 100%; border: 1px dashed var(--line); background: transparent;
    color: var(--muted); border-radius: 11px; padding: 10px; cursor: pointer;
  }
  .add:hover { color: var(--text); border-color: #4a4a58; }
  .aside-foot { margin-top: 12px; color: var(--muted); font-size: 12px; padding: 0 4px; }
  main { flex: 1; padding: 28px 36px 72px; min-width: 0; }
  .hero { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
  .hero-title { display: flex; align-items: center; gap: 10px; }
  .hero h2 { margin: 0; font-size: 22px; font-weight: 650; }
  .sub { color: var(--muted); margin: 6px 0 0; word-break: break-all; }
  .badge {
    font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line);
    color: var(--muted);
  }
  .badge.ok { color: var(--ok); border-color: #2a5c48; background: #123528; }
  .badge.warn { color: var(--warn); border-color: #5c4a22; background: #2a220e; }
  .badge.err { color: var(--danger); border-color: #5c2a32; background: #2a1216; }
  .badge.run { color: var(--acc); border-color: #2a3a5c; background: #121a2e; }
  .row { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
  button {
    appearance: none; border: 0; border-radius: 10px; padding: 8px 12px;
    background: #262632; color: var(--text); cursor: pointer; font-size: 13px;
  }
  button:hover { filter: brightness(1.08); }
  button.primary { background: var(--acc); color: #111; font-weight: 650; }
  button.ghost { background: transparent; color: var(--danger); }
  button:disabled { opacity: .5; cursor: wait; }
  .iconbtn { width: 32px; height: 32px; padding: 0; display: grid; place-items: center; }
  .stats {
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px;
  }
  .stat { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px; }
  .stat i { display: block; color: var(--muted); font-style: normal; font-size: 12px; margin-bottom: 4px; }
  .stat b { font-size: 15px; font-weight: 600; }
  .keepbar {
    display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; margin: 0 0 16px;
  }
  .keepbar input {
    width: 56px; text-align: center; background: #111115; color: var(--text);
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 4px;
  }
  .job {
    background: #0e0e14; border: 1px solid var(--line); border-radius: 14px;
    padding: 12px 14px; margin-bottom: 16px;
  }
  .job-head { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; margin-bottom: 8px; }
  .job pre {
    margin: 0; max-height: 180px; overflow: auto; color: #c8c8d4;
    font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap;
  }
  .list { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 0; list-style: none; }
  .item {
    display: flex; justify-content: space-between; align-items: center; gap: 16px;
    background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px;
  }
  .item.latest { border-color: #35508a; background: #161c2c; }
  .when { font-variant-numeric: tabular-nums; display: flex; align-items: center; gap: 8px; }
  .pill { font-size: 11px; color: var(--acc); background: #1d2a48; border-radius: 999px; padding: 1px 7px; }
  .size { color: var(--muted); font-size: 12px; margin-top: 3px; }
  .actions { display: flex; gap: 10px; align-items: center; flex-shrink: 0; }
  a { color: var(--acc); text-decoration: none; }
  .empty { color: var(--muted); padding: 36px 4px; }
  .toast { min-height: 0; margin: 0 0 12px; color: var(--ok); font-size: 13px; }
  .toast.err { color: var(--danger); }
  .alerts { display: flex; flex-direction: column; gap: 8px; margin: 0 0 14px; }
  .alert {
    border: 1px solid #5c4a22; background: #2a220e; color: var(--warn);
    border-radius: 12px; padding: 10px 12px; font-size: 13px;
  }
  .alert.err { border-color: #5c2a32; background: #2a1216; color: var(--danger); }
  dialog {
    border: 1px solid var(--line); border-radius: 16px; background: var(--panel); color: var(--text);
    width: min(480px, 92vw); max-height: 88vh; padding: 20px 20px 12px; box-shadow: 0 20px 60px #0008; overflow: auto;
  }
  dialog::backdrop { background: #000a; }
  dialog h3 { margin: 0 0 8px; font-size: 16px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
  select,
  input, textarea {
    width: 100%; background: #111115; color: var(--text); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 10px; font: inherit;
  }
  textarea { min-height: 72px; resize: vertical; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
  .chip { background: #232330; border-radius: 999px; padding: 3px 8px 3px 10px; font-size: 12px; display: flex; gap: 6px; align-items: center; }
  .chip button { background: transparent; color: var(--muted); padding: 0; width: 16px; height: 16px; }
  .dlg-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; flex-wrap: wrap; position: sticky; bottom: 0; background: var(--panel); padding: 10px 0 4px; }
  .hint { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .ov-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
  .ov-card {
    text-align: left; background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 14px 16px; cursor: pointer; color: inherit; width: 100%;
  }
  .ov-card:hover { border-color: #3d4a6a; }
  .ov-card .top { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; }
  .ov-card .top b { font-size: 16px; }
  .ov-card .meta { color: var(--muted); font-size: 12px; line-height: 1.65; }
  @media (max-width: 860px) {
    .app { flex-direction: column; }
    aside { width: auto; border-right: 0; border-bottom: 1px solid var(--line); }
    .stats { grid-template-columns: 1fr 1fr; }
    main { padding: 20px 16px 48px; }
  }
</style>
</head>
<body>
<div class="app">
  <aside>
    <div class="brand on" id="brand">
      <h1>VPS 备份</h1>
      <p>按各机频率自动备份</p>
    </div>
    <div class="hosts" id="hosts"></div>
    <a class="host" id="quarknav" href="#quark"><i class="dot"></i><div><b>夸克网盘</b><span>异地副本</span></div></a>
    <button class="add" id="add">添加 VPS</button>
    <div class="aside-foot" id="foot"></div>
  </aside>
  <main>
    <div class="hero">
      <div>
        <button class="back" id="back" hidden type="button">← 总览</button>
        <div class="hero-title">
          <h2 id="title">总览</h2>
          <span class="badge" id="badge" hidden></span>
        </div>
        <p class="sub" id="sub">按设置的频率自动备份，各机各自保留份数</p>
      </div>
      <div class="row">
        <button class="primary" id="run" disabled>立刻备份</button>
        <button id="edit" disabled>设置</button>
        <button class="ghost" id="remove" disabled>移除</button>
      </div>
    </div>
    <p class="toast" id="toast"></p>
    <div class="alerts" id="alerts"></div>
    <div class="stats" id="stats" hidden></div>
    <p class="keepbar" id="keeprow" hidden>
      保留最近
      <button class="iconbtn" id="keepm" type="button">−</button>
      <input id="keep" type="number" min="1" max="30">
      <button class="iconbtn" id="keepp" type="button">+</button>
      份
    </p>
    <div class="job" id="job" hidden>
      <div class="job-head"><span id="jobphase"></span><span id="jobmeta"></span></div>
      <pre id="joblog"></pre>
    </div>
    <div id="list" class="empty">左边选一台，或先添加。</div>
  </main>
</div>
<dialog id="dlg">
  <form method="dialog" id="form">
    <h3 id="dlg-title">添加 VPS</h3>
    <p class="hint" id="dlg-hint">填地址和密钥就能测连通，路径按空格或逗号分隔。</p>
    <input type="hidden" name="id">
    <label>名称</label><input name="name" required placeholder="example1">
    <div class="grid2">
      <div><label>地址</label><input name="host" required placeholder="1.2.3.4"></div>
      <div><label>端口</label><input name="port" value="22"></div>
    </div>
    <div class="grid2">
      <div><label>用户</label><input name="user" value="root"></div>
      <div><label>域名（可选）</label><input name="domain" placeholder="vps.example.com"></div>
    </div>
    <label>备份路径</label>
    <div class="chips" id="pathchips"></div>
    <input id="pathadd" placeholder="opt/mysite 回车添加">
    <textarea name="paths" hidden></textarea>
    <div class="grid2">
      <div>
        <label>保留份数</label><input name="keep" type="number" value="1" min="1" max="30">
      </div>
      <div>
        <label>备份频率</label>
        <select name="interval">
          <option value="12">每 12 小时</option>
          <option value="24" selected>每天一次</option>
          <option value="48">每 2 天</option>
          <option value="168">每 7 天</option>
        </select>
      </div>
    </div>
    <label id="keylabel">SSH 私钥</label><input name="keyfile" type="file">
    <p class="hint" id="testmsg"></p>
    <div class="dlg-actions">
      <button type="button" id="testbtn">测试连接</button>
      <button value="cancel">取消</button>
      <button class="primary" id="save" value="default">保存</button>
    </div>
  </form>
</dialog>
<dialog id="ask">
  <h3 id="ask-title">确认</h3>
  <p class="hint" id="ask-text"></p>
  <div class="dlg-actions" id="ask-actions"></div>
</dialog>
<script>
const token = new URLSearchParams(location.search).get("t") || "";
let selected = (location.hash === "#quark") ? "quark" : "overview";
let hosts = [];
let paths = [];
let mode = "add";
let keepTimer = 0;

function esc(s){
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
function fmt(n){
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n/1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n/1048576).toFixed(1) + " MB";
  return (n/1073741824).toFixed(2) + " GB";
}
function dur(s){
  if (s == null || s === "" || Number(s) <= 0) return "未记录";
  s = Number(s);
  if (s < 60) return (Math.round(s * 10) / 10) + " 秒";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m + " 分 " + String(r).padStart(2, "0") + " 秒";
}
function nextRun(){
  return "按各机器间隔";
}
function intervalLabel(h){
  const n = Number(h.interval_hours || 24);
  if (n % 24 === 0) return n === 24 ? "每天一次" : ("每 " + (n/24) + " 天");
  return "每 " + n + " 小时";
}
function lastSecs(h){
  if (h.last_seconds) return h.last_seconds;
  if (h.job && h.job.phase === "done" && h.job.seconds) return h.job.seconds;
  const it = (h.items || [])[0];
  return it && it.seconds ? it.seconds : null;
}
function rel(when){
  if (!when) return "还没有";
  const t = new Date(when.replace(" ", "T") + "+08:00");
  if (Number.isNaN(t.getTime())) return when;
  const s = (Date.now() - t.getTime()) / 1000;
  if (s < 45) return "刚刚";
  if (s < 3600) return Math.floor(s/60) + " 分钟前";
  if (s < 86400) return Math.floor(s/3600) + " 小时前";
  if (s < 172800) return "昨天 " + when.slice(11);
  return when.slice(0, 10);
}
function diskLabel(d){
  if (!d) return "";
  const free = d.free < 1073741824 ? (d.free/1048576).toFixed(0) + " MB" : (d.free/1073741824).toFixed(1) + " GB";
  return "已用 " + d.used_pct + "% · 剩余 " + free;
}
function renderAlerts(data){
  const box = document.getElementById("alerts");
  const bits = [];
  const disk = data && data.disk;
  if (disk && disk.warn) bits.push(`<div class="alert">磁盘快满了，${diskLabel(disk)}</div>`);
  hosts.forEach(h => {
    const fail = h.fail || (h.job && h.job.phase === "failed" ? {when:"", error: h.job.error || h.error} : null);
    if (h.running || !fail) return;
    bits.push(`<div class="alert err">${esc(h.name)} 上次备份失败${fail.when ? " · " + esc(fail.when) : ""}${fail.error ? " · " + esc(fail.error) : ""}</div>`);
  });
  box.innerHTML = bits.join("");
}
function statusOf(h){
  if (h.running) return {key:"run", label: (h.job && String(h.job.phase||"").startsWith("restor") ? "恢复中" : "备份中")};
  if (h.fail || h.error || h.job.phase === "failed") return {key:"err", label:"失败"};
  if (!h.last) return {key:"warn", label:"还没有备份"};
  const t = new Date(h.last.replace(" ", "T") + "+08:00");
  if ((Date.now() - t.getTime()) > 26 * 3600 * 1000) return {key:"warn", label:"已过期"};
  return {key:"ok", label:"正常"};
}
function api(path, opt={}){
  const join = path.includes("?") ? "&" : "?";
  return fetch(path + join + "t=" + encodeURIComponent(token), opt).then(async r => {
    if (!r.ok) throw new Error(await r.text());
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r;
  });
}
function ask(title, text, actions){
  return new Promise(resolve => {
    const dlg = document.getElementById("ask");
    document.getElementById("ask-title").textContent = title;
    document.getElementById("ask-text").textContent = text;
    const box = document.getElementById("ask-actions");
    box.innerHTML = "";
    actions.forEach(a => {
      const b = document.createElement("button");
      b.textContent = a.label;
      if (a.primary) b.className = "primary";
      if (a.danger) b.className = "ghost";
      b.onclick = () => { dlg.close(); resolve(a.value); };
      box.appendChild(b);
    });
    dlg.showModal();
  });
}
function renderChips(){
  const box = document.getElementById("pathchips");
  box.innerHTML = paths.map((p,i) =>
    `<span class="chip">${esc(p)}<button type="button" data-i="${i}">×</button></span>`
  ).join("");
  box.querySelectorAll("button").forEach(btn => {
    btn.onclick = () => { paths.splice(Number(btn.dataset.i), 1); renderChips(); };
  });
  document.querySelector("#form [name=paths]").value = paths.join(" ");
}

function currentView(){
  if (location.hash === "#quark") return "quark";
  const id = location.hash.replace(/^#/, "");
  if (id && hosts.find(h => h.id === id)) return id;
  if (selected === "quark") return "quark";
  if (selected && hosts.find(h => h.id === selected)) return selected;
  return "overview";
}
function setView(id){
  selected = id || "overview";
  const want = (selected === "overview") ? "" : selected;
  if (location.hash.replace(/^#/, "") !== want) {
    const url = new URL(location.href);
    url.hash = want;
    history.replaceState(null, "", url);
  }
}
async function load(){
  const data = await api("/api/state");
  hosts = data.hosts || [];
  window._disk = data.disk;
  selected = currentView();
  render(data);
}

function renderOverview(){
  const run = document.getElementById("run");
  const rm = document.getElementById("remove");
  const ed = document.getElementById("edit");
  const list = document.getElementById("list");
  const toast = document.getElementById("toast");
  const badge = document.getElementById("badge");
  const stats = document.getElementById("stats");
  const job = document.getElementById("job");
  const sts = hosts.map(statusOf);
  const ok = sts.filter(s => s.key === "ok").length;
  const bad = sts.filter(s => s.key === "err" || s.key === "warn").length;
  const busy = hosts.some(h => h.running);
  const total = hosts.reduce((n,h) => n + (h.bytes||0), 0);
  document.getElementById("back").hidden = true;
  document.getElementById("title").textContent = "总览";
  const nx = (hosts.map(x => x.next).filter(x => x && x.due)[0] || hosts.map(x => x.next).sort((a,b)=>(a&&a.at||"z").localeCompare(b&&b.at||"z"))[0] || {});
  document.getElementById("sub").textContent = "按各机频率自动备份" + (nx.label ? " · 下次 " + nx.label : "");
  badge.hidden = false;
  badge.className = "badge " + (bad ? "warn" : (busy ? "run" : "ok"));
  badge.textContent = bad ? (bad + " 台需要注意") : (hosts.length ? "全部正常" : "还没有机器");
  document.getElementById("keeprow").hidden = true;
  run.disabled = !hosts.length || hosts.every(h => h.running);
  run.textContent = busy ? "备份中…" : "全部备份";
  rm.hidden = true; ed.hidden = true; rm.disabled = true; ed.disabled = true;
  toast.textContent = "";
  toast.className = "toast";
  stats.hidden = false;
  stats.innerHTML = `
    <div class="stat"><i>机器</i><b>${hosts.length} 台</b></div>
    <div class="stat"><i>正常</i><b>${ok} / ${hosts.length}</b></div>
    <div class="stat"><i>本机占用</i><b>${fmt(total)}</b></div>
    <div class="stat"><i>下次备份</i><b>${esc((hosts.map(x => x.next).filter(x => x && x.due)[0] || hosts.map(x => x.next).sort((a,b)=>(a&&a.at||"z").localeCompare(b&&b.at||"z"))[0] || {}).label || nextRun())}</b></div>`;
  job.hidden = true;
  if (!hosts.length){
    list.className = "empty";
    list.textContent = "还没有机器。左边添加一台。";
    return;
  }
  list.className = "";
  list.innerHTML = '<div class="ov-grid">' + hosts.map(h => {
    const st = statusOf(h);
    return `<button class="ov-card" data-id="${esc(h.id)}">
      <div class="top"><b>${esc(h.name)}</b><span class="badge ${st.key}">${st.label}</span></div>
      <div class="meta">${esc(h.host)}${h.domain ? " · " + esc(h.domain) : ""}<br>
      最近 ${esc(h.last ? rel(h.last) : "还没有")} · 占用 ${fmt(h.bytes||0)}<br>
      ${esc(intervalLabel(h))} · 保留 ${h.items.length} / ${h.keep} · 耗时 ${esc(dur(lastSecs(h)))}</div>
    </button>`;
  }).join("") + "</div>";
  list.querySelectorAll(".ov-card").forEach(el => {
    el.onclick = () => { selected = el.dataset.id; render(); };
  });
}

function goOverview(){
  setView("overview");
  render();
}
function goQuark(){
  setView("quark");
  render();
  loadQuark();
}
async function loadQuark(){
  try {
    window._quark = await api("/api/quark/status");
  } catch (e) {
    window._quark = {logged_in:false, message:String(e)};
  }
  if (selected === "quark") renderQuark();
}
function renderQuark(){
  const q = window._quark || {};
  const run = document.getElementById("run");
  const rm = document.getElementById("remove");
  const ed = document.getElementById("edit");
  const list = document.getElementById("list");
  const toast = document.getElementById("toast");
  const badge = document.getElementById("badge");
  const stats = document.getElementById("stats");
  const job = document.getElementById("job");
  document.getElementById("back").hidden = false;
  document.getElementById("title").textContent = "夸克网盘";
  document.getElementById("sub").textContent = "把本机备份目录传到夸克，当作异地副本";
  badge.hidden = false;
  badge.className = "badge " + (q.logged_in ? "ok" : "warn");
  badge.textContent = q.logged_in ? "已登录" : "未登录";
  document.getElementById("keeprow").hidden = true;
  run.hidden = false;
  run.disabled = !q.logged_in;
  run.textContent = "立即上传";
  rm.hidden = true; ed.hidden = true;
  toast.className = "toast";
  toast.textContent = "";
  stats.hidden = false;
  stats.innerHTML = `
    <div class="stat"><i>账号</i><b>${esc(q.nickname || "未登录")}</b></div>
    <div class="stat"><i>空间</i><b>${esc(q.space || "—")}</b></div>
    <div class="stat"><i>网盘目录</i><b>${esc(q.remote_dir || "VPS备份")}</b></div>
    <div class="stat"><i>自动上传</i><b>${q.auto ? "开" : "关"}</b></div>
    <div class="stat"><i>云端保留</i><b>每台 ${q.keep_remote || 3} 份</b></div>`;
  job.hidden = true;
  list.className = "";
  const loginBit = q.logged_in ? `
      <p class="hint" style="margin-top:8px">已授权。云端每台机器留最近 3 份，新的覆盖最旧的。不会覆盖远端 VPS。</p>` : `
      <p class="hint" style="margin-top:8px">打开授权地址，把页面上的授权码贴回来。只在夸克 APP 里扫码登录还不够。</p>
      <p class="hint" id="quarkurl">${q.url ? '<a href="'+esc(q.url)+'" target="_blank">打开授权页</a>' : ""}</p>
      <label>授权码</label>
      <input id="quarkcode" placeholder="粘贴授权码">
      <div class="row" style="margin-top:10px">
        <button type="button" id="quarklogin">获取授权地址</button>
        <button type="button" class="primary" id="quarktoken">提交授权码</button>
      </div>`;
  const logText = q.logged_in ? "" : (q.message || "");
  list.innerHTML = `
    <div class="stat" style="margin-bottom:12px">
      <i>上传来源</i><b>__GROK_HOSTS_DIR__</b>
      ${loginBit}
      <label>网盘目录名</label>
      <input id="quarkdir" value="${esc(q.remote_dir || "VPS备份")}">
      <div class="row" style="margin-top:10px">
        <button type="button" id="quarksave">保存设置</button>
        <button type="button" id="quarkauto">${q.auto ? "关闭自动上传" : "开启自动上传"}</button>
      </div>
      <pre id="quarklog" style="margin-top:12px;white-space:pre-wrap">${esc(logText)}</pre>
    </div>`;
  const loginBtn = document.getElementById("quarklogin");
  const tokenBtn = document.getElementById("quarktoken");
  if (loginBtn) loginBtn.onclick = async () => {
    const r = await api("/api/quark/login-start", {method:"POST"});
    window._quark = Object.assign({}, q, r, {logged_in: false});
    renderQuark();
  };
  if (tokenBtn) tokenBtn.onclick = async () => {
    const code = document.getElementById("quarkcode").value.trim();
    const r = await api("/api/quark/login", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({token: code})});
    await loadQuark();
    document.getElementById("toast").textContent = r.ok ? "登录成功" : (r.message || "登录失败");
    document.getElementById("toast").className = "toast" + (r.ok ? "" : " err");
  };
  document.getElementById("quarksave").onclick = async () => {
    await api("/api/quark/setup", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({remote_dir: document.getElementById("quarkdir").value})});
    loadQuark();
  };
  document.getElementById("quarkauto").onclick = async () => {
    await api("/api/quark/setup", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({auto: !q.auto})});
    loadQuark();
  };
}
function render(data){
  renderAlerts(data || {disk: window._disk, hosts});
  if (data && data.disk) window._disk = data.disk;
  const box = document.getElementById("hosts");
  document.getElementById("brand").className = "brand" + (selected === "overview" ? " on" : "");
  const qn = document.getElementById("quarknav");
  if (qn) qn.className = "host" + (selected === "quark" ? " active" : "");
  box.innerHTML = hosts.map(h => {
    const st = statusOf(h);
    const sub = h.running ? "正在备份" : (h.last ? rel(h.last) : "还没有备份");
    return `<button class="host ${h.id===selected?"active":""}" data-id="${esc(h.id)}">
      <i class="dot ${st.key}"></i>
      <div><b>${esc(h.name)}</b><span>${esc(sub)}</span></div>
    </button>`;
  }).join("");
  box.querySelectorAll(".host").forEach(el => el.onclick = () => { setView(el.dataset.id); render(data); });
  if (qn) qn.onclick = (e) => { e.preventDefault(); goQuark(); };
  const total = hosts.reduce((n,h) => n + (h.bytes||0), 0);
  const disk = (data && data.disk) || window._disk;
  document.getElementById("foot").textContent =
    hosts.length ? `本机占用 ${fmt(total)} · ${hosts.length} 台` + (disk ? " · 盘 " + disk.used_pct + "%" : "") : "还没有机器";

  if (selected === "overview"){
    renderOverview();
    return;
  }
  if (selected === "quark"){
    renderQuark();
    return;
  }
  const h = hosts.find(x => x.id === selected);
  const run = document.getElementById("run");
  const rm = document.getElementById("remove");
  const ed = document.getElementById("edit");
  const list = document.getElementById("list");
  const toast = document.getElementById("toast");
  const badge = document.getElementById("badge");
  const stats = document.getElementById("stats");
  const job = document.getElementById("job");
  if (!h){
    document.getElementById("back").hidden = true;
    document.getElementById("title").textContent = "选择一台机器";
    document.getElementById("sub").textContent = "按设置的频率自动备份";
    badge.hidden = true;
    run.disabled = true; rm.disabled = true; ed.disabled = true;
    document.getElementById("keeprow").hidden = true;
    stats.hidden = true; job.hidden = true;
    list.className = "empty";
    list.textContent = "左边选一台，或先添加。";
    return;
  }
  const st = statusOf(h);
  document.getElementById("back").hidden = false;
  document.getElementById("title").textContent = h.name;
  document.getElementById("sub").textContent =
    h.host + (h.domain ? " · " + h.domain : "") + " · " + intervalLabel(h) +
    (h.next && h.next.label ? " · 下次 " + h.next.label : "");
  badge.hidden = false;
  badge.className = "badge " + st.key;
  badge.textContent = st.label;
  document.getElementById("keeprow").hidden = false;
  const keepEl = document.getElementById("keep");
  if (document.activeElement !== keepEl) keepEl.value = h.keep;
  run.disabled = !!h.running;
  run.textContent = h.running ? ((h.job && String(h.job.phase||"").startsWith("restor")) ? "恢复中…" : "备份中…") : "立刻备份";
  rm.hidden = false; ed.hidden = false; rm.disabled = false; ed.disabled = false;
  toast.className = "toast" + ((h.error || h.job.phase === "failed") ? " err" : "");
  toast.textContent = h.error || (h.job.phase === "failed" ? (h.job.error || h.job.message) : "");
  stats.hidden = false;
  stats.innerHTML = `
    <div class="stat"><i>最近备份</i><b>${esc(h.last ? rel(h.last) : "还没有")}</b></div>
    <div class="stat"><i>占用</i><b>${fmt(h.bytes||0)}</b></div>
    <div class="stat"><i>已保留</i><b>${h.items.length} / ${h.keep}</b></div>
    <div class="stat"><i>上次耗时</i><b>${esc(dur(lastSecs(h)))}</b></div>`;
  const showJob = h.running || h.job.phase === "failed" || (h.job.log && h.job.phase === "done" && h.running === false && (Date.now() - new Date((h.job.updated||"").replace(" ","T")+"+08:00").getTime() < 120000));
  if (h.running || h.job.phase === "failed" || h.job.log){
    job.hidden = !(h.running || h.job.phase === "failed" || (h.job.phase === "done" && h.job.log));
    document.getElementById("jobphase").textContent = h.job.message || (h.running ? "进行中…" : "");
    document.getElementById("jobmeta").textContent =
      (h.job.bytes ? fmt(h.job.bytes) : "") + (h.job.seconds ? " · " + h.job.seconds + "s" : "");
    const pre = document.getElementById("joblog");
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    pre.textContent = h.job.log || "";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } else {
    job.hidden = true;
  }
  if (!h.items.length){
    list.className = "empty";
    list.textContent = "还没有备份。";
    return;
  }
  list.className = "";
  list.innerHTML = '<ul class="list">' + h.items.map((it, i) => `
    <li class="item ${i===0?"latest":""}">
      <div>
        <div class="when">${i===0?'<span class="pill">最新</span>':""}<span>${esc(rel(it.when))}</span></div>
        <div class="size">${fmt(it.size)}${it.seconds ? " · " + dur(it.seconds) : ""} · ${esc(it.when)} · ${esc(it.name)}</div>
      </div>
      <div class="actions">
        <a href="/dl/${encodeURIComponent(h.id)}/${encodeURIComponent(it.name)}?t=${encodeURIComponent(token)}">下载</a>
        <button data-restore="${esc(it.name)}" ${h.running ? "disabled" : ""}>恢复</button>
        <button class="ghost" data-del="${esc(it.name)}">删除</button>
      </div>
    </li>`).join("") + "</ul>";
  list.querySelectorAll("[data-restore]").forEach(btn => {
    btn.onclick = async () => {
      const stamp = btn.dataset.restore.replace(h.id + "-", "").replace(".tgz", "");
      const remote = "/opt/vps-restore/" + h.id + "/" + stamp;
      const ans = await ask("恢复到旁边目录？", "会传到 " + remote + "，不覆盖正在跑的站点。", [
        {label:"取消", value:""},
        {label:"恢复", value:"ok", primary:true}
      ]);
      if (ans !== "ok") return;
      await api("/api/restore?id=" + encodeURIComponent(h.id) + "&name=" + encodeURIComponent(btn.dataset.restore), {method:"POST"});
      load();
    };
  });
  list.querySelectorAll("[data-del]").forEach(btn => {
    btn.onclick = async () => {
      const ans = await ask("删除这份备份？", btn.dataset.del, [
        {label:"取消", value:""},
        {label:"删除", value:"del", danger:true}
      ]);
      if (ans !== "del") return;
      await api("/api/delete?id=" + encodeURIComponent(h.id) + "&name=" + encodeURIComponent(btn.dataset.del), {method:"POST"});
      load();
    };
  });
}

function saveKeep(n){
  n = Math.max(1, Math.min(30, Number(n || 7)));
  document.getElementById("keep").value = n;
  if (!selected || selected === "overview") return;
  clearTimeout(keepTimer);
  keepTimer = setTimeout(async () => {
    await api("/api/hosts/keep?id=" + encodeURIComponent(selected) + "&keep=" + encodeURIComponent(n), {method:"POST"});
    load();
  }, 350);
}

function openDlg(kind, h){
  mode = kind;
  const f = document.getElementById("form");
  f.reset();
  document.getElementById("dlg-title").textContent = kind === "edit" ? "机器设置" : "添加 VPS";
  document.getElementById("dlg-hint").textContent = kind === "edit"
    ? "路径、密钥、频率都可以改。测一下再保存更稳。"
    : "填地址和密钥就能测连通。默认每天备份一次。";
  document.getElementById("testmsg").textContent = "";
  document.getElementById("keylabel").textContent = kind === "edit" ? "更换 SSH 私钥（可选）" : "SSH 私钥";
  if (h){
    f.id.value = h.id;
    f.name.value = h.name;
    f.host.value = h.host;
    f.user.value = h.user || "root";
    f.port.value = h.port || 22;
    f.domain.value = h.domain || "";
    f.keep.value = h.keep || 1;
    f.interval.value = String(h.interval_hours || 24);
    paths = (h.paths || []).slice();
  } else {
    f.id.value = "";
    paths = [];
  }
  renderChips();
  document.getElementById("dlg").showModal();
}

document.getElementById("pathadd").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const v = e.target.value.trim().replace(/^\/+/, "");
  if (!v) return;
  v.split(/[\s,]+/).forEach(p => { if (p && !paths.includes(p)) paths.push(p); });
  e.target.value = "";
  renderChips();
});
document.getElementById("keep").onchange = () => saveKeep(document.getElementById("keep").value);
document.getElementById("keep").oninput = () => saveKeep(document.getElementById("keep").value);
document.getElementById("keepm").onclick = () => saveKeep(Number(document.getElementById("keep").value || 1) - 1);
document.getElementById("keepp").onclick = () => saveKeep(Number(document.getElementById("keep").value || 1) + 1);
document.getElementById("run").onclick = async () => {
  if (!selected) return;
  if (selected === "overview") {
    await api("/api/backup-all", {method:"POST"});
  } else if (selected === "quark") {
    const r = await api("/api/quark/run", {method:"POST"});
    document.getElementById("toast").textContent = r.ok ? "上传完成" : (r.message || "上传失败");
    document.getElementById("toast").className = "toast" + (r.ok ? "" : " err");
    loadQuark();
    return;
  } else {
    await api("/api/backup?id=" + encodeURIComponent(selected), {method:"POST"});
  }
  load();
};
document.getElementById("edit").onclick = () => {
  const h = hosts.find(x => x.id === selected);
  if (h) openDlg("edit", h);
};
document.getElementById("remove").onclick = async () => {
  if (!selected) return;
  const ans = await ask("移除这台 VPS？", "本地备份可以留着，只是不再出现在列表里。", [
    {label:"取消", value:""},
    {label:"只移除机器", value:"keep"},
    {label:"连备份一起删", value:"wipe", danger:true}
  ]);
  if (!ans) return;
  await api("/api/hosts/delete?id=" + encodeURIComponent(selected) + "&wipe=" + (ans==="wipe"?"1":"0"), {method:"POST"});
  selected = null;
  load();
};
document.getElementById("brand").onclick = goOverview;
document.getElementById("back").onclick = goOverview;
document.getElementById("quarknav").onclick = goQuark;
document.getElementById("add").onclick = () => openDlg("add");
document.getElementById("testbtn").onclick = async () => {
  const f = document.getElementById("form");
  const keyfile = f.keyfile.files[0];
  let key_pem = "";
  if (keyfile) key_pem = await keyfile.text();
  const msg = document.getElementById("testmsg");
  msg.textContent = "在测…";
  try {
    const r = await api("/api/hosts/test", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        id: f.id.value || "",
        host: f.host.value.trim(),
        user: f.user.value.trim() || "root",
        port: Number(f.port.value || 22),
        key_pem
      })
    });
    msg.textContent = r.ok ? ("通了 · " + r.message) : ("没通 · " + r.message);
    msg.style.color = r.ok ? "var(--ok)" : "var(--danger)";
  } catch (e) {
    msg.textContent = String(e.message || e);
    msg.style.color = "var(--danger)";
  }
};
document.getElementById("form").onsubmit = async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const f = e.target;
  const keyfile = f.keyfile.files[0];
  let key_pem = "";
  if (keyfile) key_pem = await keyfile.text();
  const body = {
    id: f.id.value,
    name: f.name.value.trim(),
    host: f.host.value.trim(),
    user: f.user.value.trim() || "root",
    port: Number(f.port.value || 22),
    domain: f.domain.value.trim(),
    paths: paths.join(" "),
    keep: Number(f.keep.value || 1),
    interval_hours: Number(f.interval.value || 24),
    key_pem
  };
  if (mode === "edit") {
    await api("/api/hosts/update", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  } else {
    await api("/api/hosts", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  }
  document.getElementById("dlg").close();
  f.reset();
  load();
};

window.addEventListener("hashchange", () => {
  selected = currentView();
  render();
  if (selected === "quark") loadQuark();
});
load();
setInterval(() => {
  if (document.querySelector("dialog[open]")) return;
  if (selected === "quark") return;
  load();
}, 2500);
</script>
</body>
</html>
"""
HTML = HTML.replace("__GROK_HOSTS_DIR__", str(ROOT / "hosts"))


def check_token(qs, handler=None):
    if handler and handler.client_address and handler.client_address[0] in ("127.0.0.1", "::1"):
        return True
    return (qs.get("t") or [""])[0] == TOKEN


class BodyTooLarge(Exception):
    pass


def read_body(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if n > 512_000:
        raise BodyTooLarge()
    if n <= 0:
        return {}
    return json.loads(handler.rfile.read(n).decode())


def slug(name):
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return s[:32] or "vps"


def parse_paths(raw):
    paths = [p.strip().lstrip("/") for p in re.split(r"[\s,]+", raw or "") if p.strip()]
    clean = []
    for p in paths:
        if PATH_RE.match(p) and ".." not in p:
            clean.append(p)
    return clean or ["opt"]


def apply_fields(rec, body):
    if body.get("name"):
        rec["name"] = body["name"].strip()
    if body.get("host"):
        rec["host"] = body["host"].strip()
    if body.get("user"):
        rec["user"] = body["user"].strip()
    if body.get("port") is not None and body.get("port") != "":
        rec["port"] = int(body.get("port") or 22)
    if "domain" in body:
        rec["domain"] = (body.get("domain") or "").strip()
    if body.get("paths") or body.get("paths") == "":
        rec["paths"] = parse_paths(body.get("paths") or "")
    if body.get("keep") is not None and body.get("keep") != "":
        rec["keep"] = max(1, min(30, int(body.get("keep") or 1)))
    if body.get("interval_hours") is not None and body.get("interval_hours") != "":
        rec["interval_hours"] = max(1, min(24 * 30, int(body.get("interval_hours") or 24)))
    pem = body.get("key_pem") or ""
    if pem.strip():
        key_dir = ROOT / "keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        try:
            key_dir.chmod(0o700)
        except Exception:
            pass
        key_path = str(key_dir / f"{rec['id']}.pem")
        Path(key_path).write_text(pem if pem.endswith("\n") else pem + "\n")
        Path(key_path).chmod(0o600)
        rec["key"] = key_path
    return rec


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _deny(self, code=401, msg="unauthorized"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(msg.encode())

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            if not check_token(qs, self):
                return self._deny()
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/state":
            if not check_token(qs, self):
                return self._deny()
            out = []
            for h in load_hosts()["hosts"]:
                hid = h["id"]
                if not ID_RE.match(hid):
                    continue
                out.append({
                    **{k: h.get(k) for k in ("id", "name", "host", "user", "port", "domain", "keep", "paths")},
                    "items": list_items(hid),
                    "last": last_backup(hid),
                    "bytes": host_bytes(hid),
                    "running": bool(_running.get(hid)),
                    "error": _errors.get(hid),
                    "job": read_job(hid),
                    "last_seconds": (read_last(hid) or {}).get("seconds") or (read_job(hid) or {}).get("seconds") or None,
                    "interval_hours": int(h.get("interval_hours") or 24),
                    "next": host_next(h),
                    "fail": read_fail(hid),
                })
            return self._json({"hosts": out, "disk": disk_info()})
        if u.path.startswith("/dl/"):
            if not check_token(qs, self):
                return self._deny()
            parts = [unquote(x) for x in u.path.split("/") if x and x != "dl"]
            if len(parts) != 2 or not ID_RE.match(parts[0]) or not FILE_RE.match(parts[1]):
                return self._deny(400, "bad name")
            p = dest_for(parts[0]) / parts[1]
            if not p.is_file() or not parts[1].startswith(parts[0] + "-"):
                return self._deny(404, "missing")
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f'attachment; filename="{parts[1]}"')
            self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            with p.open("rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        if u.path == "/api/quark/status":
            if not check_token(qs, self):
                return self._deny()
            if not quark_bridge:
                return self._json({"logged_in": False, "message": "夸克组件未装好"})
            return self._json(quark_bridge.status())
        self._deny(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not check_token(qs, self):
            return self._deny()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad length")
        if n > 512_000:
            return self._deny(413, "payload too large")
        if u.path == "/api/hosts/keep":
            hid = (qs.get("id") or [""])[0]
            if not ID_RE.match(hid):
                return self._deny(400, "bad id")
            try:
                keep = int((qs.get("keep") or ["1"])[0])
            except ValueError:
                return self._deny(400, "bad keep")
            keep = max(1, min(30, keep))
            data = load_hosts()
            found = False
            for h in data["hosts"]:
                if h["id"] == hid:
                    h["keep"] = keep
                    found = True
                    break
            if not found:
                return self._deny(404, "no host")
            save_hosts(data)
            return self._json({"ok": True, "keep": keep})
        if u.path == "/api/hosts/update":
            try:
                body = read_body(self)
            except BodyTooLarge:
                return self._deny(413, "payload too large")
            hid = (body.get("id") or "").strip()
            if not ID_RE.match(hid):
                return self._deny(400, "bad id")
            data = load_hosts()
            rec = next((h for h in data["hosts"] if h["id"] == hid), None)
            if not rec:
                return self._deny(404, "no host")
            apply_fields(rec, body)
            save_hosts(data)
            return self._json({"ok": True})
        if u.path == "/api/hosts/test":
            try:
                body = read_body(self)
            except BodyTooLarge:
                return self._deny(413, "payload too large")
            hid = (body.get("id") or "").strip()
            if hid and not ID_RE.match(hid):
                return self._deny(400, "bad id")
            tmp = None
            try:
                if hid and get_host(hid) and not (body.get("key_pem") or "").strip():
                    h = get_host(hid)
                    ok, msg = ssh_probe(h)
                    return self._json({"ok": ok, "message": msg})
                h = {
                    "host": (body.get("host") or "").strip(),
                    "user": (body.get("user") or "root").strip(),
                    "port": int(body.get("port") or 22),
                    "key": "",
                }
                if not h["host"]:
                    return self._deny(400, "host required")
                pem = body.get("key_pem") or ""
                if pem.strip():
                    tmp = Path("/tmp") / f"vps-test-{int(datetime.now().timestamp())}.pem"
                    tmp.write_text(pem if pem.endswith("\n") else pem + "\n")
                    tmp.chmod(0o600)
                    h["key"] = str(tmp)
                elif hid and get_host(hid):
                    h["key"] = get_host(hid)["key"]
                else:
                    return self._deny(400, "key required")
                ok, msg = ssh_probe(h)
                return self._json({"ok": ok, "message": msg})
            finally:
                if tmp and tmp.exists():
                    tmp.unlink()
        if u.path == "/api/quark/status":
            if not quark_bridge:
                return self._json({"logged_in": False, "message": "夸克组件未装好"})
            return self._json(quark_bridge.status())
        if u.path == "/api/quark/login-start":
            if not quark_bridge:
                return self._deny(500, "no quark")
            return self._json(quark_bridge.login_start())
        if u.path == "/api/quark/login":
            if not quark_bridge:
                return self._deny(500, "no quark")
            try:
                body = read_body(self)
            except BodyTooLarge:
                return self._deny(413, "payload too large")
            return self._json(quark_bridge.login_finish(body.get("token") or ""))
        if u.path == "/api/quark/setup":
            if not quark_bridge:
                return self._deny(500, "no quark")
            try:
                body = read_body(self)
            except BodyTooLarge:
                return self._deny(413, "payload too large")
            return self._json(quark_bridge.save_prefs(
                remote_dir=body.get("remote_dir"),
                auto=body.get("auto"),
                mode=body.get("mode"),
            ))
        if u.path == "/api/quark/run":
            if not quark_bridge:
                return self._deny(500, "no quark")
            return self._json(quark_bridge.run_upload())
        if u.path == "/api/backup-all":
            for h in load_hosts()["hosts"]:
                hid = h.get("id") or ""
                if ID_RE.match(hid):
                    run_backup(hid)
            return self._json({"ok": True})
        if u.path == "/api/backup":
            hid = (qs.get("id") or [""])[0]
            if not ID_RE.match(hid):
                return self._deny(400, "bad id")
            if not get_host(hid):
                return self._deny(404, "no host")
            run_backup(hid)
            return self._json({"ok": True})
        if u.path == "/api/restore":
            hid = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            if not ID_RE.match(hid) or not FILE_RE.match(name) or not name.startswith(hid + "-"):
                return self._deny(400, "bad name")
            if not get_host(hid):
                return self._deny(400, "bad name")
            if not (dest_for(hid) / name).is_file():
                return self._deny(404, "missing")
            if not run_restore(hid, name):
                return self._deny(409, "busy")
            return self._json({"ok": True})
        if u.path == "/api/delete":
            hid = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            if not ID_RE.match(hid) or not FILE_RE.match(name) or not name.startswith(hid + "-"):
                return self._deny(400, "bad name")
            p = dest_for(hid) / name
            if p.is_file():
                p.unlink()
            side = p.with_name(p.name + ".json")
            if side.is_file():
                side.unlink()
            return self._json({"ok": True})
        if u.path == "/api/hosts/delete":
            hid = (qs.get("id") or [""])[0]
            if not ID_RE.match(hid):
                return self._deny(400, "bad id")
            root_hosts = (ROOT / "hosts").resolve()
            dest = (root_hosts / hid).resolve()
            if not dest.is_relative_to(root_hosts) or dest == root_hosts:
                return self._deny(400, "bad dest")
            wipe = (qs.get("wipe") or ["0"])[0] == "1"
            data = load_hosts()
            data["hosts"] = [h for h in data["hosts"] if h["id"] != hid]
            save_hosts(data)
            if wipe and dest.is_dir():
                for f in dest.glob("*.tgz"):
                    f.unlink()
                    side = f.with_name(f.name + ".json")
                    if side.is_file():
                        side.unlink()
                for extra in ("job.json", "job.log"):
                    p = dest / extra
                    if p.exists():
                        p.unlink()
                try:
                    dest.rmdir()
                except OSError:
                    pass
            return self._json({"ok": True})
        if u.path == "/api/hosts":
            try:
                body = read_body(self)
            except BodyTooLarge:
                return self._deny(413, "payload too large")
            name = (body.get("name") or "").strip()
            host = (body.get("host") or "").strip()
            if not name or not host:
                return self._deny(400, "name/host required")
            hid = slug(name)
            data = load_hosts()
            if any(h["id"] == hid for h in data["hosts"]):
                hid = hid + "-2"
            rec = {
                "id": hid,
                "name": name,
                "host": host,
                "user": (body.get("user") or "root").strip(),
                "port": int(body.get("port") or 22),
                "key": str(Path.home() / ".ssh" / "id_ed25519"),
                "paths": parse_paths(body.get("paths") or ""),
                "keep": max(1, min(30, int(body.get("keep") or 1))),
                "interval_hours": max(1, min(24 * 30, int(body.get("interval_hours") or 24))),
                "domain": (body.get("domain") or "").strip(),
            }
            apply_fields(rec, body)
            data["hosts"].append(rec)
            save_hosts(data)
            dest_for(hid)
            return self._json({"ok": True, "id": hid})
        self._deny(404, "not found")


if __name__ == "__main__":
    threading.Thread(target=scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("backup-ui listening on 127.0.0.1:8787", flush=True)
    httpd.serve_forever()
