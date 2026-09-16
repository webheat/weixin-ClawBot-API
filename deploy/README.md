# 部署：单进程多用户共享 runtime

2026-09-15 · 取代旧的 `clawbot.service`（legacy 单租户）和 `clawbot@.service` 模板（per-user systemd unit）。

## 部署前

```bash
# 确认新代码已拉取（commit 8a88d6f 起）
git log --oneline -1
# 8a88d6f docs(shared): 同步多租户架构文档与 CLAUDE/README
```

## 安装步骤

```bash
# 1. 复制 unit 文件到 systemd
sudo cp deploy/clawbot-shared.service /etc/systemd/system/
sudo systemctl daemon-reload

# 2. 停止并禁用旧 service（4 service → 0）
sudo systemctl stop clawbot-portal.service
sudo systemctl stop 'clawbot@*.service'    # 历史残留：旧 launcher 曾写 per-session systemd unit
sudo systemctl stop clawbot.service
sudo systemctl disable clawbot-portal.service
sudo systemctl disable clawbot.service

# 3. 删除旧 unit 文件
sudo rm /etc/systemd/system/clawbot.service
sudo rm /etc/systemd/system/clawbot-portal.service
sudo rm /etc/systemd/system/clawbot@.service
sudo systemctl daemon-reload

# 4. 启动新 service
sudo systemctl enable --now clawbot-shared.service

# 5. 验证
curl -s http://127.0.0.1:18300/healthz
# 期望：{"ok": true, "shared_process": true}
```

## .env 配置

确保 `/opt/weixin-ClawBot-API/.env` 含以下关键项：

```bash
# 单进程入口（绑 :18300）
CLAWBOT_WEB_ENABLED=1
CLAWBOT_WEB_HOST=127.0.0.1
CLAWBOT_WEB_PORT=18300
CLAWBOT_WEB_TOKEN=<openssl rand -hex 32>

# 登录模式（OR 关系，两者都关 → 503）
WXOAPP_ENABLED=0              # 0=OAuth 关；1=OAuth 开（需 APP_ID/SECRET）
WXOAPP_SESSION_ENABLED=1      # 1=无 cookie 访客自动分配临时 session
WXOAPP_SESSION_TTL=28800      # 临时 session cookie 有效期（秒，默认 8h）
```

## session 持久化

共享 runtime **不**为 session 写任何磁盘 env 文件；它们的元数据
只活在内存里，由 `BotManager.sessions` 维护，TTL 到期由 `shared_web` 周期
GC。

旧 launcher 时代残留的 `/etc/clawbot/<session_token>.env` 已成为孤儿，shared_web 不再写；
清理脚本：

```bash
sudo rm -f /etc/clawbot/sess_*.env
```

共享 ima 凭据（`/etc/clawbot/ima.env`）按需保留——shared_runtime 仍会在启动时
读取它作为兜底，所有 session 共享同一份 IMA 配置。

## 验证 multi-task 并行

启动后 `journalctl -u clawbot-shared -f` 应该看到多个 `[user=xxx]` 日志交错
（每个 session 独立的 message_loop / reconnect_timer / relogin_listener 在同一个进程内并发）：

```
INFO  [clawbot.qr] [user=sess_a1b2c3] qr fetched via POST ...
INFO  [clawbot.qr] [user=sess_d4e5f6] qr fetched via POST ...
INFO  [clawbot.qr] [user=sess_789xyz] qr fetched via POST ...
```

## 端口 / 反代

nginx 配置**不需要改**：
- `location ^~ /clawbot/` → `proxy_pass http://127.0.0.1:18300/`
- shared_web 在 :18300 监听，与旧 portal 端口一致

## 旧 env 文件清理

旧 service 停掉后，`/etc/clawbot/sess_*.env` 成为孤儿（shared_web 不再写）。
可以一次性删掉：

```bash
sudo rm /etc/clawbot/sess_*.env
```

`ima.env` 保留——shared_runtime 启动时会作为兜底读取，所有 session 共享同一份 IMA 配置。

## 回滚

如果新 service 启动后出问题，回滚到旧架构：

```bash
sudo systemctl disable --now clawbot-shared.service
git checkout ac2bdd6 -- bot.py  # 旧单租户版
# 重建旧 unit 文件（从 git 历史拉 /opt/weixin-ClawBot-API/deploy/ 不存在，
# 只能手写或从别处恢复）
```

**注意**：回滚后所有 session 全部需要重新扫码（iLink 端 session 已在
service 切换时失效）。
