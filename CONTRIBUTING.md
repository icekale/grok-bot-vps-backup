# 参与

改备份脚本、面板或夸克桥接都可以。这是给单人操作员用的本机工具，补丁请保持小、可审。

不要提交 `hosts.json`、`access.json`、`notify.json`、私钥、token、备份包或日志。示例主机继续用文档网段（`203.0.113.0/24`）和假路径，不要写真实 IP、域名或家目录。数据一律走 `GROK_BACKUP_ROOT`。

改完至少跑：

```bash
bash -n ui/start.sh ui/watchdog.sh backup-one.sh backup-all.sh restore-one.sh quark/run.sh
python3 -m py_compile ui/app.py ui/notify.py quark/bridge.py
```
