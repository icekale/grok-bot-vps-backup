[English](README.en.md) · 中文

# Grok Bot VPS 备份

给 Grok Bot 用的 VPS 自动备份，本机留最新一份，夸克网盘留最近 3 份。

通过 SSH 把远端目录流式打成 tar.gz，校验后落在本机；面板按各机间隔调度；可选再传到夸克网盘，每台机器只保留最近 3 个槽位并覆盖最旧的。

## 功能

- **SSH 流式打包**：远端 `tar` 直接打到本机，不在 VPS 上落临时包。
- **校验**：下载后 `tar -tzf` 检查，损坏则丢弃。
- **安全恢复**：默认解压到远端 `/opt/vps-restore/<id>/<时间戳>`，不覆盖正在跑的站点。
- **进程内调度**：面板按每台机器的 `interval_hours` 自动备份；失败有退避。
- **本机面板**：只监听 `127.0.0.1:8787`。本机访问可免 token；非本机请求需要 `?t=`。
- **夸克 3 槽覆盖**：把每台机器最新 N 份（默认 3）以 `id-1.tgz` … `id-N.tgz` 上传到同一网盘目录，新的覆盖旧槽。

## 依赖

- Linux
- `python3`
- `ssh`、`tar`
- 夸克 CLI 还需要 `node`（以及官方 `quarkclouddrive` Skill）

## 安装

```bash
git clone https://github.com/icekale/grok-bot-vps-backup.git
cd grok-bot-vps-backup
```

数据目录默认是：

```bash
export GROK_BACKUP_ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
mkdir -p "$GROK_BACKUP_ROOT"
cp hosts.example.json "$GROK_BACKUP_ROOT/hosts.json"
# 按实际改 IP、用户、密钥路径（请写成绝对路径，例如 $HOME/.ssh/id_ed25519）
```

已有 Grok Bot 现场安装时，可把数据根指回原来的目录（可选）：

```bash
export GROK_BACKUP_ROOT=/home/box/backups
```

密钥路径示例：`$HOME/.ssh/id_ed25519` 或 `/path/to/key.pem`。不要把私钥提交进仓库。

启动面板和看门狗：

```bash
./ui/start.sh
./ui/watchdog.sh &
```

浏览器打开 `http://127.0.0.1:8787`。首次启动若没有 token，会在 `$GROK_BACKUP_ROOT/ui/token` 生成一个（权限 `600`）。从非本机访问时在 URL 后加 `?t=<token>`。不要把 token 打进日志或提交到 Git。

也可以手动跑：

```bash
./backup-all.sh
./backup-one.sh example1
./restore-one.sh example1 example1-20260824-1500.tgz
```

## 夸克网盘

1. 安装官方 [LYISTR2/quark-backup](https://github.com/LYISTR2/quark-backup) 的用户态依赖（见 `quark/install-quark.sh`），或保证 `node` 已装且本仓库自带的 `quark/quark-backup.sh` 能找到官方 CLI。
2. 登录夸克账号（面板「夸克网盘」页获取授权地址并回填授权码，或 `./quark/run.sh login`）。
3. 在面板夸克页设置网盘目录、是否自动上传。云端默认每台保留 3 份。

配置默认位置：`$HOME/.config/quark-backup/config.json`。

## 安全说明

- 恢复**默认不会覆盖**线上站点，只写到 `/opt/vps-restore/...`。要换回生产目录请自己确认后再动手。
- 面板只绑 `127.0.0.1`。本机可跳过 token；不要把面板暴露到公网。
- 主机 ID、备份文件名、恢复路径都有正则校验；恢复拒绝 `..` 和绝对路径成员。
- `hosts.json` 原子写入；请求体有大小限制；远端 tar 参数经过 `shlex.quote`。
- **不要提交** `hosts.json`、SSH 私钥、`ui/token`、备份包或日志。

## 目录约定

| 路径 | 含义 |
| --- | --- |
| 仓库本身 | 脚本和面板代码 |
| `$GROK_BACKUP_ROOT` | 数据根（默认 `$HOME/.local/share/grok-vps-backup`） |
| `$GROK_BACKUP_ROOT/hosts.json` | 主机列表 |
| `$GROK_BACKUP_ROOT/hosts/<id>/` | 该机备份包 |
| `$GROK_BACKUP_ROOT/ui/token` | 面板 token |
| `$GROK_BACKUP_ROOT/quark/settings.json` | 夸克自动上传等偏好 |

## 许可

MIT。见 [LICENSE](LICENSE)。
