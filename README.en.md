# GrokKeep

[English](README.en.md) · [中文](README.md)

GrokKeep is a local VPS backup panel for Grok-related hosts: it streams remote directories over SSH into `tar.gz`, keeps N copies per host on this machine, and can optionally copy them to Quark Drive.

After backup, restore, or upload it can notify via Telegram, Feishu/Lark, or a JSON webhook. Data root: `GROK_BACKUP_ROOT` (default `~/.local/share/grok-vps-backup`). Panel default: `127.0.0.1:8787`.

## Install and run

Needs Linux, `python3`, `ssh`, `tar`. Quark upload also needs `node` and the official `quarkclouddrive` CLI (`quark/install-quark.sh`).

```bash
git clone https://github.com/icekale/grokkeep.git
cd grokkeep

export GROK_BACKUP_ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
mkdir -p "$GROK_BACKUP_ROOT"
cp hosts.example.json "$GROK_BACKUP_ROOT/hosts.json"
# edit host, user, and an absolute key path, e.g. $HOME/.ssh/id_ed25519
```

The previous slug `icekale/grok-bot-vps-backup` redirects to `icekale/grokkeep`. The `GROK_BACKUP_ROOT` env var and default data directory are unchanged.

```bash
./ui/start.sh
./ui/watchdog.sh &
```

If `ssh` is missing, `start.sh` tries a noninteractive `openssh-client` install. An apt failure does not stop the panel. The watchdog is a loop that calls `start.sh`.

Open `http://127.0.0.1:8787`. Or run by hand:

```bash
./backup-all.sh
./backup-one.sh example1
./restore-one.sh example1 example1-20260824-1500.tgz
```

Restore writes to `/opt/vps-restore/<id>/<timestamp>` on the remote by default. It does not overwrite the live site.

## Access

| Client | Token |
| --- | --- |
| `127.0.0.1` / `::1` | not required |
| Tailscale CGNAT / ULA (only if enabled in Settings) | not required (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) |
| anything else | `?t=` from `$GROK_BACKUP_ROOT/ui/token` (mode `600`) |

The Tailscale flag lives in `$GROK_BACKUP_ROOT/access.json` and defaults to `false`. When on, the panel also binds the IPv4 on `tailscale0` (`ip -4 addr show tailscale0`) and tries MagicDNS from `tailscale status --json`. When off, it binds `127.0.0.1` only and does not trust those ranges. It never binds `0.0.0.0`. Do not publish the panel on the internet.

## Quark

Settings: login, remote folder, auto-upload, run now. Default is 3 slots per host (`id-1.tgz` … `id-N.tgz`; newer uploads overwrite the oldest slot).

Uploads are single-flight: a second auto-upload is a no-op while one is running, and staging is not cleared mid-upload. The timeout is hours, not minutes. CLI config default: `$HOME/.config/quark-backup/config.json`.

## Notifications

Settings toggles events and the three channels. Config: `$GROK_BACKUP_ROOT/notify.json` (mode `600`). `notify.example.json` is an empty template.

Events: `backup.success` / `backup.failed` / `restore.success` / `restore.failed` / `quark.success` / `quark.failed`, plus `notify.test`. A notify failure never fails the job.

- Telegram: `POST https://api.telegram.org/bot<token>/sendMessage`
- Feishu/Lark: custom-bot webhook, `{"msg_type":"text","content":{"text":"..."}}`
- Webhook: `POST application/json`, `User-Agent: grok-bot-vps-backup`, `X-Webhook-Event`. Optional `X-Webhook-Signature: sha256=<hex>` (HMAC-SHA256 of the body)

Telegram/Feishu time is the backup's Shanghai time (`when` in `last.json`, or `YYYYMMDD-HHMM` in the pack name), not the send clock. The status API returns only the last four characters of secrets.

Old `#quark` and `#notify` hashes redirect to `#settings`.

## Layout

| Path | Meaning |
| --- | --- |
| this repo | scripts and panel |
| `$GROK_BACKUP_ROOT/hosts.json` | host list |
| `$GROK_BACKUP_ROOT/hosts/<id>/` | packs for that host |
| `$GROK_BACKUP_ROOT/ui/token` | panel token |
| `$GROK_BACKUP_ROOT/access.json` | Tailscale flag |
| `$GROK_BACKUP_ROOT/quark/settings.json` | Quark auto-upload prefs |
| `$GROK_BACKUP_ROOT/notify.json` | notify channels |

Do not commit `hosts.json`, `access.json`, `notify.json`, private keys, the token, packs, or logs.

MIT. See [LICENSE](LICENSE).
