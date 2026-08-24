#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export HOME="${HOME:-$(cd ~ && pwd)}"
export QUARK_BACKUP_HOME="${QUARK_BACKUP_HOME:-$HOME/.local/share/quark-backup}"
export QUARK_SKILL_DIR="${QUARK_SKILL_DIR:-$QUARK_BACKUP_HOME/vendor/quarkclouddrive}"
export QUARK_RUNTIME_DIR="${QUARK_RUNTIME_DIR:-$QUARK_BACKUP_HOME/runtime}"
if [[ -x "$HERE/quark-backup.sh" ]]; then
  exec "$HERE/quark-backup.sh" "$@"
fi
if command -v quark-backup >/dev/null 2>&1; then
  exec quark-backup "$@"
fi
echo "未找到 quark-backup。请先运行 quark/install-quark.sh 或安装 LYISTR2/quark-backup。" >&2
exit 1
