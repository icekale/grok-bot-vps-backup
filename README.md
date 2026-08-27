[English](README.en.md) · 中文

# GrokKeep

GrokKeep 是面向 Grok 相关主机的本机 VPS 备份面板：经 SSH 把远端目录流式打成 `tar.gz`，本机按各机保留份数，可选再传到夸克网盘。

备份/恢复/上传完成后可推 Telegram、飞书或 webhook。数据根：`GROK_BACKUP_ROOT`（默认 `~/.local/share/grok-vps-backup`）。面板默认 `127.0.0.1:8787`。

## 安装与运行

依赖：Linux、`python3`、`ssh`、`tar`。夸克上传另外需要 `node` 和官方 `quarkclouddrive` CLI（见 `quark/install-quark.sh`）。

```bash
git clone https://github.com/icekale/grokkeep.git
cd grokkeep

export GROK_BACKUP_ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
mkdir -p "$GROK_BACKUP_ROOT"
cp hosts.example.json "$GROK_BACKUP_ROOT/hosts.json"
# 改地址、用户、密钥绝对路径，例如 $HOME/.ssh/id_ed25519
```

旧仓库名 `icekale/grok-bot-vps-backup` 会重定向到 `icekale/grokkeep`。环境变量 `GROK_BACKUP_ROOT` 与默认数据目录不变。

```bash
./ui/start.sh
./ui/watchdog.sh &
```

`start.sh` 若找不到 `ssh`，会尝试非交互安装 `openssh-client`；apt 失败不阻止面板启动。看门狗只是循环调用 `start.sh`。

浏览器打开 `http://127.0.0.1:8787`。也可以：

```bash
./backup-all.sh
./backup-one.sh example1
./restore-one.sh example1 example1-20260824-1500.tgz
```

恢复默认写到远端 `/opt/vps-restore/<id>/<时间戳>`，不覆盖正在跑的站点。

## 访问控制

| 来源 | 行为 |
| --- | --- |
| `127.0.0.1` / `::1` | 免 token |
| Tailscale 网段（仅当设置里打开） | 同样免 token。网段为 `100.64.0.0/10` 和 `fd7a:115c:a1e0::/48` |
| 其他 | URL 加 `?t=`，token 在 `$GROK_BACKUP_ROOT/ui/token`（`600`） |

设置里的 Tailscale 开关写在 `$GROK_BACKUP_ROOT/access.json`，发布版默认 `false`。打开后额外监听 `tailscale0` 的 IPv4（`ip -4 addr show tailscale0`），并尽量从 `tailscale status --json` 读 MagicDNS。关则只绑 `127.0.0.1`，也不信任上述网段。永远不绑 `0.0.0.0`。不要把面板暴露到公网。

## 夸克

设置页登录、填网盘目录、开关自动上传、立即上传。云端默认每台 3 份（`id-1.tgz` … `id-N.tgz`，新的覆盖最旧槽）。

上传有单飞锁：已有任务在跑时，第二次自动上传直接跳过，也不会清暂存目录。超时按小时计，适合几个 GB 的包。CLI 配置默认 `$HOME/.config/quark-backup/config.json`。

## 通知

设置页开关事件和三个通道。配置在 `$GROK_BACKUP_ROOT/notify.json`（`600`）。`notify.example.json` 是空模板。

事件：`backup.success` / `backup.failed` / `restore.success` / `restore.failed` / `quark.success` / `quark.failed`，测试为 `notify.test`。发送失败不影响备份。

- Telegram：`POST https://api.telegram.org/bot<token>/sendMessage`
- 飞书：自定义机器人 webhook，`{"msg_type":"text","content":{"text":"..."}}`
- Webhook：`POST application/json`，`User-Agent: grok-bot-vps-backup`，`X-Webhook-Event`。若填了签名密钥，另带 `X-Webhook-Signature: sha256=<hex>`（HMAC-SHA256 of body）

Telegram / 飞书时间取备份本身的上海时间（`last.json` 的 `when` 或包名 `YYYYMMDD-HHMM`），不是发送时钟。状态接口只回密钥末四位。

旧书签 `#quark`、`#notify` 会跳到 `#settings`。

## 数据目录

| 路径 | 含义 |
| --- | --- |
| 本仓库 | 脚本和面板 |
| `$GROK_BACKUP_ROOT/hosts.json` | 主机列表 |
| `$GROK_BACKUP_ROOT/hosts/<id>/` | 该机备份包 |
| `$GROK_BACKUP_ROOT/ui/token` | 面板 token |
| `$GROK_BACKUP_ROOT/access.json` | Tailscale 开关 |
| `$GROK_BACKUP_ROOT/quark/settings.json` | 夸克自动上传等 |
| `$GROK_BACKUP_ROOT/notify.json` | 通知通道 |

不要提交 `hosts.json`、`access.json`、`notify.json`、私钥、token、备份包或日志。

MIT，见 [LICENSE](LICENSE)。
