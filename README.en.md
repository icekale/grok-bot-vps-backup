# Grok Bot VPS Backup

[English](README.en.md) · [中文](README.md)

Automatic VPS backups for Grok Bot. Keep the latest copy on this machine, and the last 3 copies on Quark Drive.

It streams remote directories over SSH into a tar.gz, checks the archive, then stores it locally. A small panel schedules each host on its own interval. Optionally it uploads to Quark Drive and keeps only the newest 3 slots per machine, overwriting the oldest.

## Features

- **Streamed SSH packs**: remote `tar` writes straight to this machine. No temp archive on the VPS.
- **Integrity check**: `tar -tzf` after download; a bad pack is discarded.
- **Safe restore**: unpacks to `/opt/vps-restore/<id>/<timestamp>` on the remote by default. It does not overwrite the live site.
- **In-process scheduler**: the panel runs each host on `interval_hours`, with backoff on failure.
- **Local panel**: binds `127.0.0.1:8787` only. Loopback can skip the token; other clients need `?t=`.
- **Quark 3-slot rotate**: uploads the newest N packs (default 3) as `id-1.tgz` … `id-N.tgz` in one Drive folder. Newer uploads overwrite the oldest slot.
- **Completion notify**: Telegram, Feishu/Lark custom bot, and a generic JSON webhook. Fired after backup, restore, and Quark upload in a background thread. A notify failure never fails the job.

## Requirements

- Linux
- `python3`
- `ssh`, `tar`
- Quark CLI also needs `node` (and the official `quarkclouddrive` skill)

## Install

```bash
git clone https://github.com/icekale/grok-bot-vps-backup.git
cd grok-bot-vps-backup
```

Data directory (default):

```bash
export GROK_BACKUP_ROOT="${GROK_BACKUP_ROOT:-$HOME/.local/share/grok-vps-backup}"
mkdir -p "$GROK_BACKUP_ROOT"
cp hosts.example.json "$GROK_BACKUP_ROOT/hosts.json"
# edit IPs, users, and key paths (use an absolute path, e.g. $HOME/.ssh/id_ed25519)
```

If you already run this on a Grok Bot machine, you can point the data root at the existing directory (optional):

```bash
export GROK_BACKUP_ROOT=/home/box/backups
```

Key path examples: `$HOME/.ssh/id_ed25519` or `/path/to/key.pem`. Never commit private keys.

Start the panel and watchdog:

```bash
./ui/start.sh
./ui/watchdog.sh &
```

Open `http://127.0.0.1:8787`. On first start, if no token exists, one is written to `$GROK_BACKUP_ROOT/ui/token` (mode `600`). For non-local access, append `?t=<token>`. Do not log the token or commit it.

You can also run by hand:

```bash
./backup-all.sh
./backup-one.sh example1
./restore-one.sh example1 example1-20260824-1500.tgz
```

## Quark Drive

1. Install the user-space deps for official [LYISTR2/quark-backup](https://github.com/LYISTR2/quark-backup) (see `quark/install-quark.sh`), or make sure `node` is present and the bundled `quark/quark-backup.sh` can find the official CLI.
2. Sign in (get an auth URL from the panel's Quark page and paste the code, or run `./quark/run.sh login`).
3. On the Quark page, set the Drive folder and whether to auto-upload. Cloud retention defaults to 3 copies per machine.

Config default: `$HOME/.config/quark-backup/config.json`.

## Notifications

The panel's **通知** page (`#notify`) toggles events and the three channels. Config is `$GROK_BACKUP_ROOT/notify.json` (mode `600`, atomic write). `notify.example.json` in the repo is an empty-secret template. Do not commit real tokens or webhook URLs.

Events (all on by default; you can turn successes off):

`backup.success` · `backup.failed` · `restore.success` · `restore.failed` · `quark.success` · `quark.failed`, plus `notify.test` for the test button.

- **Telegram**: `POST https://api.telegram.org/bot<token>/sendMessage` with a short Chinese text.
- **Feishu / Lark**: POST `{"msg_type":"text","content":{"text":"..."}}` to the custom-bot webhook.
- **Generic webhook**: `POST` `application/json` with `User-Agent: grok-bot-vps-backup` and `X-Webhook-Event: <event>`. If a signing secret is set, also `X-Webhook-Signature: sha256=<hex>` (HMAC-SHA256 of the raw body).

```json
{
  "source": "grok-bot-vps-backup",
  "event": "backup.success",
  "ok": true,
  "host_id": "example1",
  "host_name": "example1",
  "file": "example1-20260824-1500.tgz",
  "duration_sec": 12.3,
  "bytes": 1048576,
  "message": "备份成功: example1 · example1-20260824-1500.tgz",
  "error": null,
  "timestamp": "2026-08-24T15:00:00+08:00"
}
```

`event` may also be `backup.failed`, `restore.success`, `restore.failed`, `quark.success`, `quark.failed`, or `notify.test`. Failures set `ok` to `false` and `error` to a string. The status API returns only the last 4 characters of secrets, never the full token or webhook URL.

## Safety

- Restore **does not overwrite** the live site by default. It only writes `/opt/vps-restore/...`. Move files back to production yourself after you check them.
- The panel binds `127.0.0.1` only. Loopback may skip the token. Do not expose the panel to the internet.
- Host IDs, pack names, and restore paths are validated. Restore rejects `..` and absolute-path members.
- `hosts.json` is written atomically. Request bodies are size-capped. Remote tar arguments are `shlex.quote`d.
- **Do not commit** `hosts.json`, `notify.json`, SSH private keys, `ui/token`, backup packs, or logs.

## Layout

| Path | Meaning |
| --- | --- |
| This repo | Scripts and panel code |
| `$GROK_BACKUP_ROOT` | Data root (default `$HOME/.local/share/grok-vps-backup`) |
| `$GROK_BACKUP_ROOT/hosts.json` | Host list |
| `$GROK_BACKUP_ROOT/hosts/<id>/` | Packs for that host |
| `$GROK_BACKUP_ROOT/ui/token` | Panel token |
| `$GROK_BACKUP_ROOT/quark/settings.json` | Quark auto-upload preferences |
| `$GROK_BACKUP_ROOT/notify.json` | Notify channels (do not commit) |

## License

MIT. See [LICENSE](LICENSE).
