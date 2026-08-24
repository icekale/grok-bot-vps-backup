#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("GROK_BACKUP_ROOT") or (Path.home() / ".local/share/grok-vps-backup"))
RUN = CODE / "run.sh"
SETTINGS = ROOT / "quark" / "settings.json"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "quark-backup" / "config.json"
HOSTS = ROOT / "hosts"
STAGE = ROOT / "quark" / "stage"
HOME = str(Path.home())
ENV = {
    **os.environ,
    "HOME": HOME,
    "QUARK_BACKUP_HOME": os.environ.get("QUARK_BACKUP_HOME") or str(Path.home() / ".local/share/quark-backup"),
    "QUARK_SKILL_DIR": os.environ.get("QUARK_SKILL_DIR") or str(Path.home() / ".local/share/quark-backup/vendor/quarkclouddrive"),
    "QUARK_RUNTIME_DIR": os.environ.get("QUARK_RUNTIME_DIR") or str(Path.home() / ".local/share/quark-backup/runtime"),
}


def load_settings():
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except Exception:
            pass
    return {"auto": False, "keep_remote": 3}


def save_settings(data):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_config():
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    CONFIG.chmod(0o600)


def keep_remote():
    try:
        n = int(load_settings().get("keep_remote") or 3)
    except Exception:
        n = 3
    return max(1, min(10, n))


def cmd(*args, timeout=180):
    return subprocess.run(
        [str(RUN), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=ENV,
    )


def stage_latest():
    """Copy each host's newest N tgz files to stable slot names: id-1.tgz (newest) .. id-N.tgz."""
    n = keep_remote()
    if STAGE.exists():
        for p in STAGE.iterdir():
            if p.is_file() or p.is_symlink():
                p.unlink()
    STAGE.mkdir(parents=True, exist_ok=True)
    staged = []
    if not HOSTS.exists():
        return staged
    for d in sorted(p for p in HOSTS.iterdir() if p.is_dir()):
        packs = sorted(d.glob(d.name + "-*.tgz"), key=lambda x: x.stat().st_mtime, reverse=True)
        for i, src in enumerate(packs[:n], start=1):
            dest = STAGE / (d.name + "-" + str(i) + ".tgz")
            shutil.copy2(src, dest)
            staged.append({"host": d.name, "src": src.name, "dest": dest.name, "slot": i, "size": dest.stat().st_size})
    files = [str(STAGE / x["dest"]) for x in staged]
    cfg = load_config()
    cfg["source_paths"] = files or [str(STAGE)]
    cfg["backup_mode"] = "direct"
    cfg.setdefault("remote_dir", "VPS备份")
    cfg.setdefault("version", 2)
    save_config(cfg)
    return staged


def status():
    r = cmd("status", timeout=40)
    text = (r.stdout or "") + "\n" + (r.stderr or "")
    logged = ("未登录" not in text) and ("未授权" not in text) and ("账号：" in text)
    nick = ""
    m = re.search(r"账号：(.+)", text)
    if m:
        nick = m.group(1).strip()
    space = ""
    m = re.search(r"空间：(.+)", text)
    if m:
        space = m.group(1).strip()
    cfg = load_config()
    st = load_settings()
    return {
        "ok": r.returncode == 0 or logged,
        "logged_in": logged,
        "nickname": nick,
        "space": space,
        "remote_dir": cfg.get("remote_dir") or "VPS备份",
        "mode": cfg.get("backup_mode") or "direct",
        "auto": bool(st.get("auto")),
        "keep_remote": keep_remote(),
        "source": str(HOSTS),
        "message": text[-400:],
    }


def login_start():
    r = cmd("login", timeout=40)
    text = (r.stdout or "") + "\n" + (r.stderr or "")
    m = re.search(r"https://pan\.quark\.cn/[^\s]+", text)
    return {"ok": bool(m), "url": m.group(0) if m else "", "message": text[-400:]}


def login_finish(token):
    token = (token or "").strip()
    if not token:
        return {"ok": False, "message": "没有授权码"}
    r = cmd("login", token, timeout=60)
    text = (r.stdout or "") + "\n" + (r.stderr or "")
    ok = "授权成功" in text or "登录成功" in text
    if not ok:
        st = status()
        ok = st.get("logged_in")
    return {"ok": ok, "message": text[-400:]}


def save_prefs(remote_dir=None, auto=None, mode=None, keep=None):
    cfg = load_config()
    if remote_dir is not None:
        cfg["remote_dir"] = remote_dir.strip() or "VPS备份"
    if mode in ("direct", "archive"):
        cfg["backup_mode"] = mode
    cfg.setdefault("version", 2)
    save_config(cfg)
    st = load_settings()
    if keep is not None:
        try:
            st["keep_remote"] = max(1, min(10, int(keep)))
        except Exception:
            st["keep_remote"] = 3
    else:
        st.setdefault("keep_remote", 3)
    if auto is not None:
        st["auto"] = bool(auto)
    save_settings(st)
    return status()


def run_upload():
    staged = stage_latest()
    if not staged:
        return {"ok": False, "message": "本机没有可上传的备份包"}
    r = cmd("run", timeout=600)
    text = (r.stdout or "") + "\n" + (r.stderr or "")
    names = ", ".join(x["host"] + ":" + x["dest"] + "<-" + x["src"] for x in staged)
    ok = r.returncode == 0 and "失败" not in text[-80:]
    return {"ok": ok, "staged": staged, "keep_remote": keep_remote(), "message": "上传 " + names + "\n" + text[-500:]}
