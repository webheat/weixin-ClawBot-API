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

---

## 两个仓库：桥（提供者）先发，ClawBot（消费者）后发

发稿桥在**另一个仓库**：`/opt/obsidian-xiaolvshu`（origin =
`/opt/git/obsidian-xiaolvshu.git`，本地裸库；GitHub 仓库待建）。两边靠
「子进程 + stdout 最后一行 JSON」耦合 —— 桥改了 JSON 形状而 ClawBot 没跟上，
用户的「发贴图号」就会静默变成「❌ 发布链路出错」。

### 契约由谁保证

`tests/test_xiaolvshu_bridge_contract.py`（**在消费者这边**）跑桥的无副作用
命令（`info` / `search` / `status`），断言：

- 子命令还在（search / publish / select / status）；
- 每个命令该带的 key 还在（`info`→uid/slug/vault、`search`→`results`、
  `status`→`entries`）；
- 消费者认识的 7 个错误码桥还产出（need_select / duplicate / not_found /
  no_creds / cross_vault / no_pending / bad_index）；
- 每个错误码分支需要的字段桥还带（need_select→candidates、no_creds→hint…）；
- 契约测试自己**不碰生产目录**（STORE / CREDS_DIR 指向临时目录）。

桥那边自己的 CI 只能测它自己 —— **只有消费者这边能测出契约是否还成立**。

### 版本钉住

`.env` 里的 `XIAOLVSHU_BRIDGE_REF=<sha 或 tag>` 记录「ClawBot 期望的桥是
哪一版」。生产上装的桥跟它对不上时，`utils/xiaolvshu.py` 会在
`clawbot.message` 日志里记一条版本漂移告警；桥的工作区有未提交改动也会告警
（装到生产的不是 pin 那版）。

### 部署

```bash
./deploy/deploy.sh                # 按 .env 里的 pin 部署（桥 → 契约测试 → A → 重启）
./deploy/deploy.sh --bridge-only  # 只发桥
./deploy/deploy.sh --app-only     # 只发 A
./deploy/deploy.sh --no-restart   # 不重启
```

脚本干的事：

1. 桥：`git fetch` + `git checkout $XIAOLVSHU_BRIDGE_REF`（有未提交改动会告警）
2. 跑契约测试 —— 不绿就别往下走
3. ClawBot：`git pull --ff-only`
4. `systemctl restart clawbot-shared.service`

### 发布顺序与回滚

- **顺序**：桥先发（保持向后兼容：新桥还得认旧的 JSON 形状），A 后发
  （升 pin）。
- **回滚**：A 可以单独回滚到上一版，只要桥保持向后兼容；桥回滚则要同时把
  A 的 pin 降回去。

### 部署记录

每次部署在下面追加一行 —— 「线上跑的是哪一版」必须是可查的事实，不能靠记忆。
（`deploy.sh` 只把 commit `echo` 到 stdout，退出后不留痕，所以要手工记。）

| 时间 (CST) | A（ClawBot） | 桥 | 备注 |
|---|---|---|---|
| 2026-10-26 20:42:42 | `ce8ce14` | `1840340` | 诊断埋点上线（`fix/wechat-connection-diagnostics-20261026`） |
| 2026-10-27 06:17:44 | `b36d4b0` | `1840340` | `--app-only` 重启，让进程拾取 `.env` 里更新的 pin |
| 2026-10-27 06:22:45 | `4e897ce` | `1840340` | `--app-only` 重启（部署审计补录） |
| 2026-10-27 06:30:48 | `fc5d35b` | `1840340` | `--app-only`（`reply_sent` 埋点下沉到 `_send_reliable`） |
| 2026-10-27 06:48:28 | `e1d1400` | `1840340` | `--app-only`（多账户隔离修复：KB 绑定 / 限流 / 线程池互堵） |
| 2026-10-27 07:12:14 | `3b33424` | `1840340` | `--app-only`（发稿闭环流程日志：flow id 贯穿全链路） |

⚠️ **改完 `.env` 必须重启服务**。systemd 的 `EnvironmentFile` 只在进程启动时读一次，
之后改 `.env` 不会进已运行进程的 env。`utils/xiaolvshu.py` 的 `_check_bridge_version()`
读的是 `os.environ`，所以 pin 改了不重启 → 拿旧 pin 比对 → 误报 version drift。

`--no-restart` 只改代码不重启 —— 那时 `.env` 的改动同样不会生效。

### 发稿闭环的流程日志（`3b33424` 起）

部署后日志里会多一个 `clawbot.publish` logger，每条带 `flow=<8位hex>`：

```bash
grep -E "flow=[0-9a-f]{8}" /opt/weixin-ClawBot-API/logs/clawbot_shared.log
```

一次发稿的完整链路（认领 → 调桥 → 桥的结果 → 回信）都在同一个 flow 下。
阶段表和排查对照见 [`docs/PUBLISH_FLOW_LOGGING.md`](../docs/PUBLISH_FLOW_LOGGING.md)。
