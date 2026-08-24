# 参与

欢迎改进备份脚本、面板和夸克桥接。

- 不要提交 `hosts.json`、`notify.json`、私钥、`ui/token`、备份包或日志。
- 示例主机请继续使用文档网段（如 `203.0.113.0/24`），不要写真实 IP 或域名。
- 数据目录一律走 `GROK_BACKUP_ROOT`，不要写死家目录。
- 改脚本后请至少跑：`bash -n` 相关 shell，以及 `python3 -c "import ast; ast.parse(open('ui/app.py').read()); ast.parse(open('quark/bridge.py').read()); ast.parse(open('ui/notify.py').read())"`。
