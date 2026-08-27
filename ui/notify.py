#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

TZ = timezone(timedelta(hours=8))
TIMEOUT = 8
SOURCE = "grok-bot-vps-backup"
UA = "grok-bot-vps-backup"

EVENTS = (
    "backup.success",
    "backup.failed",
    "restore.success",
    "restore.failed",
    "quark.success",
    "quark.failed",
)
CHANNELS = ("telegram", "feishu", "webhook")
_TG_TOKEN_RE = re.compile(r"^[0-9A-Za-z_:-]{20,200}$")
_CHAT_RE = re.compile(r"^@?[A-Za-z0-9_-]{1,64}$")
_PACK_WHEN_RE = re.compile(r"(?:^|[^0-9])(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(?:[^0-9]|$)")
_lock = threading.Lock()


def data_root():
    return Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))


def notify_path():
    return data_root() / "notify.json"


def default_config():
    return {
        "events": {e: True for e in EVENTS},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "feishu": {"enabled": False, "webhook": ""},
        "webhook": {"enabled": False, "url": "", "secret": ""},
    }


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _clip(v, n=240):
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s[:n]


def _merge(raw):
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    ev = raw.get("events")
    if isinstance(ev, dict):
        for e in EVENTS:
            if e in ev:
                cfg["events"][e] = _as_bool(ev[e])
    for name in CHANNELS:
        src = raw.get(name)
        if not isinstance(src, dict):
            continue
        for key in cfg[name]:
            if key not in src:
                continue
            if key == "enabled":
                cfg[name][key] = _as_bool(src[key])
            elif src[key] is not None:
                cfg[name][key] = str(src[key])
    return cfg


def load_config():
    path = notify_path()
    if not path.exists():
        return default_config()
    try:
        return _merge(json.loads(path.read_text()))
    except Exception:
        return default_config()


def save_config(cfg):
    path = notify_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _merge(cfg)
    tmp = path.with_name("notify.json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return data


def mask_secret(value):
    s = str(value or "")
    if not s:
        return {"configured": False, "last4": ""}
    return {"configured": True, "last4": s[-4:]}


def public_status(cfg=None):
    cfg = cfg or load_config()
    return {
        "events": {e: bool(cfg["events"].get(e, True)) for e in EVENTS},
        "telegram": {
            "enabled": bool(cfg["telegram"]["enabled"]),
            "chat_id": cfg["telegram"].get("chat_id") or "",
            "bot_token": mask_secret(cfg["telegram"].get("bot_token")),
        },
        "feishu": {
            "enabled": bool(cfg["feishu"]["enabled"]),
            "webhook": mask_secret(cfg["feishu"].get("webhook")),
        },
        "webhook": {
            "enabled": bool(cfg["webhook"]["enabled"]),
            "url": mask_secret(cfg["webhook"].get("url")),
            "secret": mask_secret(cfg["webhook"].get("secret")),
        },
    }


def setup(body):
    body = body if isinstance(body, dict) else {}
    with _lock:
        cfg = load_config()
        ev = body.get("events")
        if isinstance(ev, dict):
            for e in EVENTS:
                if e in ev:
                    cfg["events"][e] = _as_bool(ev[e])
        _apply_channel(cfg, body, "telegram", ("enabled", "bot_token", "chat_id"), ("bot_token",))
        _apply_channel(cfg, body, "feishu", ("enabled", "webhook"), ("webhook",))
        _apply_channel(cfg, body, "webhook", ("enabled", "url", "secret"), ("url", "secret"))
        save_config(cfg)
        return public_status(cfg)


def _apply_channel(cfg, body, name, keys, secret_keys):
    src = body.get(name)
    if not isinstance(src, dict):
        return
    for key in keys:
        if key not in src:
            continue
        val = src[key]
        if key == "enabled":
            cfg[name]["enabled"] = _as_bool(val)
            continue
        if val is None:
            continue
        text = str(val).strip()
        if key in secret_keys and text == "":
            continue
        cfg[name][key] = text


def _http_url(url):
    u = (url or "").strip()
    if len(u) > 800:
        return ""
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return u


def _post(url, raw, headers):
    """POST raw bytes. Never raise, never include the URL or body in the error text."""
    req = urllib.request.Request(url, data=raw, method="POST")
    for key, val in headers.items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = resp.getcode()
            body = resp.read(2048)
            if 200 <= code < 300:
                return True, body, ""
            return False, body, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2048)
        except Exception:
            body = b""
        return False, body, f"HTTP {e.code}"
    except TimeoutError:
        return False, b"", "timeout"
    except Exception:
        return False, b"", "send failed"


def _send_telegram(cfg, text):
    token = (cfg.get("bot_token") or "").strip()
    chat_id = (cfg.get("chat_id") or "").strip()
    if not token or not chat_id:
        return {"ok": False, "error": "未配置"}
    if not _TG_TOKEN_RE.match(token) or not _CHAT_RE.match(chat_id):
        return {"ok": False, "error": "配置无效"}
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    raw = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode()
    ok, body, err = _post(url, raw, {"Content-Type": "application/json", "User-Agent": UA})
    if ok:
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
            if data.get("ok") is False:
                return {"ok": False, "error": "rejected"}
        except Exception:
            pass
        return {"ok": True, "error": None}
    return {"ok": False, "error": err or "send failed"}


def _send_feishu(cfg, text):
    url = _http_url(cfg.get("webhook"))
    if not url:
        return {"ok": False, "error": "未配置"}
    raw = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode()
    ok, body, err = _post(url, raw, {"Content-Type": "application/json", "User-Agent": UA})
    if ok:
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
            code = data.get("code", data.get("StatusCode", data.get("status_code")))
            if code not in (None, 0, "0"):
                return {"ok": False, "error": "rejected"}
        except Exception:
            pass
        return {"ok": True, "error": None}
    return {"ok": False, "error": err or "send failed"}


def _webhook_payload(event, ctx):
    ok = bool(ctx.get("ok")) if "ok" in ctx else event.endswith(".success") or event == "notify.test"
    err = ctx.get("error")
    if ok:
        err = None
    elif err is not None:
        err = _clip(err, 300) or "failed"
    ts = datetime.now(TZ).isoformat(timespec="seconds")
    return {
        "source": SOURCE,
        "event": event,
        "ok": ok,
        "host_id": _clip(ctx.get("host_id"), 64),
        "host_name": _clip(ctx.get("host_name"), 80),
        "file": _clip(ctx.get("file"), 200),
        "duration_sec": _qty(ctx.get("duration_sec")),
        "bytes": int(_num(ctx.get("bytes"))),
        "message": _clip(ctx.get("message") or _zh_text(event, ctx), 300),
        "error": err,
        "when": _when_display(ctx),
        "timestamp": ts,
    }


def _num(v):
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        return 0
    return n


def _qty(v):
    n = _num(v)
    r = round(n)
    return int(r) if abs(n - r) < 1e-9 else n


def _send_webhook(cfg, payload):
    url = _http_url(cfg.get("url"))
    if not url:
        return {"ok": False, "error": "未配置"}
    raw = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "X-Webhook-Event": str(payload.get("event") or ""),
    }
    secret = (cfg.get("secret") or "").strip()
    if secret:
        sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature"] = "sha256=" + sig
    ok, _body, err = _post(url, raw, headers)
    if ok:
        return {"ok": True, "error": None}
    return {"ok": False, "error": err or "send failed"}


def _parse_when(*sources):
    """Shanghai wall time from last.json `when` or pack YYYYMMDD-HHMM. Not the send clock."""
    for raw in sources:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=TZ)
            except ValueError:
                pass
        m = _PACK_WHEN_RE.search(text)
        if not m:
            continue
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), tzinfo=TZ,
            )
        except ValueError:
            continue
    return None


def _fmt_when(dt):
    if not dt:
        return ""
    return f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"


def _when_display(ctx):
    existing = _clip(ctx.get("when"), 32)
    if existing and _PACK_WHEN_RE.search(existing) is None:
        parsed = _parse_when(existing)
        if parsed:
            return _fmt_when(parsed)
        if re.match(r"^\d{1,2}/\d{1,2} \d{2}:\d{2}$", existing):
            return existing
    dt = _parse_when(ctx.get("when"), ctx.get("file"))
    return _fmt_when(dt)


def _fmt_dur(sec):
    n = _num(sec)
    if n <= 0:
        return ""
    return f"{int(round(n))}s"


def _fmt_size(n):
    n = int(_num(n))
    if n <= 0:
        return ""
    units = ((1073741824, "GB"), (1048576, "MB"), (1024, "KB"))
    for step, suffix in units:
        if n >= step:
            v = n / step
            if abs(v - round(v)) < 0.05:
                return f"{int(round(v))}{suffix}"
            return f"{v:.1f}{suffix}"
    return f"{n}B"


def _line(verb, ctx, tails):
    head = [verb]
    name = _clip(ctx.get("host_name") or ctx.get("host_id"), 40)
    if name:
        head.append(name)
    when = _when_display(ctx)
    if when:
        head.append(when)
    text = " ".join(head)
    bits = [b for b in tails if b]
    if bits:
        text += "  " + "  ".join(bits)
    return text


def _zh_text(event, ctx):
    if event == "notify.test":
        return "通知测试：VPS 备份"
    err = _clip(ctx.get("error"), 80)
    if event == "backup.success":
        return _line("备份成功", ctx, [_fmt_dur(ctx.get("duration_sec")), _fmt_size(ctx.get("bytes"))])
    if event == "backup.failed":
        return _line("备份失败", ctx, [err])
    if event == "restore.success":
        return _line("恢复成功", ctx, [_fmt_dur(ctx.get("duration_sec")), _fmt_size(ctx.get("bytes"))])
    if event == "restore.failed":
        return _line("恢复失败", ctx, [err])
    if event == "quark.success":
        return _line("夸克上传成功", ctx, [_fmt_size(ctx.get("bytes"))])
    if event == "quark.failed":
        return _line("夸克上传失败", ctx, [err])
    return event


def send_event(event, ctx=None, channels=None):
    ctx = dict(ctx or {})
    cfg = load_config()
    if event != "notify.test" and event not in EVENTS:
        return {}
    if event != "notify.test" and not cfg["events"].get(event, True):
        return {}
    if "ok" not in ctx:
        ctx["ok"] = event.endswith(".success") or event == "notify.test"
    ctx["when"] = _when_display(ctx)
    if not ctx.get("message"):
        ctx["message"] = _zh_text(event, ctx)
    payload = _webhook_payload(event, ctx)
    text = _zh_text(event, ctx)
    want = [c for c in (channels or CHANNELS) if c in CHANNELS]
    out = {}
    for name in want:
        if not (cfg.get(name) or {}).get("enabled"):
            continue
        try:
            if name == "telegram":
                out[name] = _send_telegram(cfg["telegram"], text)
            elif name == "feishu":
                out[name] = _send_feishu(cfg["feishu"], text)
            else:
                out[name] = _send_webhook(cfg["webhook"], payload)
        except Exception:
            out[name] = {"ok": False, "error": "send failed"}
    return out


def emit(event, ctx=None):
    def _run():
        try:
            send_event(event, ctx)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def emit_job(kind, host, ok, job=None, last=None, error=None, pack="", fail=None):
    job = job or {}
    last = last or {}
    fail = fail or {}
    hid = (host or {}).get("id") or ""
    name = (host or {}).get("name") or hid
    event = (("backup" if kind == "backup" else "restore") + (".success" if ok else ".failed"))
    fname = pack or last.get("name") or ""
    seconds = job.get("seconds")
    if seconds in (None, "", 0):
        seconds = last.get("seconds") or 0
    size = job.get("bytes")
    if not size:
        size = last.get("size") or 0
    err = None if ok else (error or job.get("error") or "failed")
    if kind == "restore":
        when_dt = _parse_when(fname, last.get("when"), job.get("updated"))
    elif ok:
        when_dt = _parse_when(last.get("when"), fname, job.get("updated"))
    else:
        when_dt = _parse_when(
            fail.get("when") or fail.get("last_attempt"),
            job.get("updated"),
            fname,
        )
    emit(event, {
        "ok": bool(ok),
        "host_id": hid,
        "host_name": name,
        "file": fname,
        "duration_sec": seconds,
        "bytes": size,
        "error": err,
        "when": _fmt_when(when_dt),
    })


def emit_quark(result):
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("ok"))
    staged = result.get("staged") or []
    names = []
    total = 0
    for item in staged:
        if not isinstance(item, dict):
            continue
        names.append(item.get("src") or item.get("dest") or "")
        try:
            total += int(item.get("size") or 0)
        except (TypeError, ValueError):
            pass
    fname = ", ".join(n for n in names if n)[:200]
    emit("quark.success" if ok else "quark.failed", {
        "ok": ok,
        "host_id": "",
        "host_name": "",
        "file": fname,
        "duration_sec": 0,
        "bytes": total,
        "error": None if ok else (result.get("message") or "上传失败"),
        "when": _fmt_when(_parse_when(fname)),
    })


def test(body=None):
    body = body if isinstance(body, dict) else {}
    channel = (body.get("channel") or "").strip()
    channels = [channel] if channel in CHANNELS else None
    cfg = load_config()
    if channel in CHANNELS and not (cfg.get(channel) or {}).get("enabled"):
        return {"ok": False, "channels": {channel: {"ok": False, "error": "未启用"}}}
    if channels is None:
        enabled = [c for c in CHANNELS if (cfg.get(c) or {}).get("enabled")]
        if not enabled:
            return {"ok": False, "channels": {}, "message": "没有启用的通道"}
    ctx = {
        "ok": True,
        "host_id": "",
        "host_name": "",
        "file": "",
        "duration_sec": 0,
        "bytes": 0,
        "message": "通知测试",
        "error": None,
    }
    results = send_event("notify.test", ctx, channels=channels)
    if channel in CHANNELS and channel not in results:
        results[channel] = {"ok": False, "error": "未配置"}
    ok = bool(results) and all(v.get("ok") for v in results.values())
    return {"ok": ok, "channels": results}
