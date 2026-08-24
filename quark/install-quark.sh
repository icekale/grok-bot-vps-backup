#!/usr/bin/env bash
# 用户态安装 LYISTR2/quark-backup 的官方 CLI 依赖。
# 不需要 root，不写 /opt 或 /usr/local。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOME="${HOME:-$(cd ~ && pwd)}"
DATA="${QUARK_BACKUP_HOME:-$HOME/.local/share/quark-backup}"
SKILL_DIR="${QUARK_SKILL_DIR:-$DATA/vendor/quarkclouddrive}"
RAW_BASE="${QUARK_BACKUP_RAW_BASE:-https://raw.githubusercontent.com/LYISTR2/quark-backup/v1.1.4}"
SKILL_URL="${QUARK_SKILL_URL:-https://pdds.quark.cn/download/stfile/ssyytvtxsstwsu8uo/quarkclouddrive-1.0.11.zip}"

info() { printf '[信息] %s\n' "$*"; }
die() { printf '[错误] %s\n' "$*" >&2; exit 1; }

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  die "请用普通用户运行，不要用 root。"
fi

for c in bash python3 tar curl unzip; do
  command -v "$c" >/dev/null 2>&1 || die "缺少命令：$c"
done
if ! command -v node >/dev/null 2>&1; then
  die "缺少 node。请先用系统包管理器安装 Node.js，再重试。"
fi

mkdir -p "$DATA"
chmod 700 "$DATA"

if [[ ! -x "$HERE/quark-backup.sh" ]]; then
  info "下载 LYISTR2/quark-backup 脚本……"
  curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 120 \
    -o "$HERE/quark-backup.sh" "$RAW_BASE/quark-backup.sh"
  chmod 755 "$HERE/quark-backup.sh"
fi

if [[ ! -f "$SKILL_DIR/scripts/quark-drive.cjs" ]]; then
  info "安装官方夸克 CLI 到 $SKILL_DIR ……"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 120 \
    -o "$tmp/skill.zip" "$SKILL_URL"
  python3 - "$tmp/skill.zip" "$SKILL_DIR" <<'PY'
import os, stat, sys, zipfile
archive, out = sys.argv[1:]
with zipfile.ZipFile(archive) as z:
    for item in z.infolist():
        name = item.filename
        norm = os.path.normpath(name)
        mode = (item.external_attr >> 16) & 0xFFFF
        if name.startswith(('/', '\\')) or norm == '..' or norm.startswith('../'):
            raise SystemExit(f'压缩包含不安全路径: {name}')
        if stat.S_ISLNK(mode):
            raise SystemExit(f'压缩包含符号链接: {name}')
    os.makedirs(out, exist_ok=True)
    z.extractall(out)
PY
fi

if [[ -f "$SKILL_DIR/scripts/install.sh" ]]; then
  chmod 0755 "$SKILL_DIR/scripts/install.sh" "$SKILL_DIR/scripts/uninstall.sh" 2>/dev/null || true
  if ! bash "$SKILL_DIR/scripts/install.sh"; then
    if [[ -f "$SKILL_DIR/scripts/quark-drive.cjs" ]]; then
      info "官方 CLI 更新检查失败，继续使用已安装版本"
    else
      die "夸克网盘官方 CLI 安装失败"
    fi
  fi
fi

cat <<HINT

用户态安装完成。

  数据目录：$DATA
  Skill：   $SKILL_DIR
  包装脚本：$HERE/run.sh
  配置：    $HOME/.config/quark-backup/config.json

下一步：
  $HERE/run.sh login
  或在面板「夸克网盘」页授权。

官方仓库：https://github.com/LYISTR2/quark-backup
HINT
