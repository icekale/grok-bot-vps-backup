# Changelog

## 0.3.0

### 中文

- 产品更名为 **GrokKeep**。面板 `<title>` 与侧栏标题改为 GrokKeep；仓库说明与文档同步。
- `GROK_BACKUP_ROOT`、默认数据目录、webhook `User-Agent` 保持兼容，不做破坏性改名。

### English

- Renamed the product to **GrokKeep**. Panel `<title>` and sidebar heading are GrokKeep; repo description and docs match.
- `GROK_BACKUP_ROOT`, the default data directory, and the webhook `User-Agent` stay compatible. No breaking path rename.

## 0.2.0

### 中文

- 侧栏改为「主机列表 + 设置」。设置页集中：添加 VPS、可选 Tailscale、夸克、通知。旧 `#quark` / `#notify` 跳到设置。
- Tailscale 默认关闭。打开后额外监听 `tailscale0` IPv4，并把 Tailscale 网段当成本机；关闭则只绑 `127.0.0.1`。不绑 `0.0.0.0`。
- 夸克上传加单飞锁：进行中的第二次自动上传直接跳过，且不会清暂存。上传超时改为数小时。
- 备份/恢复子进程超时同样加长，避免几个 GB 的包被 10 分钟掐断。
- `start.sh` 缺少 `ssh` 时尝试安装 `openssh-client`，失败不挡面板。
- 收紧 token 比较、下载/删除路径解析、SSH 探测临时密钥、数据文件权限和响应头。

### English

- Sidebar is host list + Settings. Settings stacks Add VPS, optional Tailscale, Quark, and notifications. `#quark` / `#notify` redirect there.
- Tailscale is off by default. When on, also bind `tailscale0` IPv4 and treat the Tailscale ranges as local; when off, bind `127.0.0.1` only. Never bind `0.0.0.0`.
- Quark uploads are single-flight: a second auto-upload is a no-op and staging is left alone. Upload timeout is hours.
- Backup/restore subprocess timeouts match, so multi-GB jobs are not killed at 10 minutes.
- `start.sh` tries to install `openssh-client` if `ssh` is missing; apt failure does not stop the panel.
- Tighter token compare, resolved download/delete paths, SSH probe temp keys, file modes, and response headers.
