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
sudo systemctl stop clawbot@alice.service        # 如果有命名用户
sudo systemctl stop 'clawbot@eph_*.service'     # 如果有 ephemeral 子进程
sudo systemctl stop clawbot.service
sudo systemctl disable clawbot-portal.service
sudo systemctl disable clawbot@alice.service
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
WXOAPP_EPHEMERAL_ENABLED=1    # 1=无 cookie 访客自动分配临时 session
WXOAPP_EPHEMERAL_TTL=28800    # 临时 session cookie 有效期（秒，默认 8h）
```

## 命名用户持久化

命名用户（OAuth 绑定或手动指定的 user_id）通过 `/etc/clawbot/<name>.env` 配置；
新 service 启动时由 `shared_runtime` 读取并自动恢复 `weixin_state_<name>.json`。

ephemeral session **不**写 `/etc/clawbot/eph_*.env`（与旧架构的根本区别）；
它们的元数据只活在内存里，TTL 到期 GC。

## 验证 multi-task 并行

启动后 `journalctl -u clawbot-shared -f` 应该看到多个 `[user=xxx]` 日志交错
（每个 user 独立的 message_loop / reconnect_timer / relogin_listener 在同一个进程内并发）：

```
INFO  [clawbot.qr] [user=eph_abc123] qr fetched via POST ...
INFO  [clawbot.qr] [user=eph_def456] qr fetched via POST ...
INFO  [clawbot.qr] [user=alice] qr fetched via POST ...
```

## 端口 / 反代

nginx 配置**不需要改**：
- `location ^~ /clawbot/` → `proxy_pass http://127.0.0.1:18300/`
- shared_web 在 :18300 监听，与旧 portal 端口一致

## 旧 env 文件清理

旧 service 停掉后，`/etc/clawbot/eph_*.env` 成为孤儿（shared_web 不再写）。
可以一次性删掉：

```bash
sudo rm /etc/clawbot/eph_*.env
```

`alice.env` / `oauth.env` 等命名用户 env 保留（shared_runtime 仍会读）。

## 回滚

如果新 service 启动后出问题，回滚到旧架构：

```bash
sudo systemctl disable --now clawbot-shared.service
git checkout ac2bdd6 -- bot.py  # 旧单租户版
# 重建旧 unit 文件（从 git 历史拉 /opt/weixin-ClawBot-API/deploy/ 不存在，
# 只能手写或从别处恢复）
```

**注意**：回滚后 ephemeral session 全部需要重新扫码（iLink 端 session 已在
service 切换时失效）。
