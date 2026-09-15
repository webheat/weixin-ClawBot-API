# 0 号文档 · 设计预期

> **优先级最高**。接手这个项目的 LLM **必须先读完本文档**再去读 `CLAUDE.md` 和其他文档。
> 任何代码改动如果偏离下面 3 条预期，先回这里确认意图。

---

## 设计预期（来源：用户原话，2026-09-15）

### 预期 1：基于 iLink_bot 协议的多微信用户共享进程

> 基于 iLink_bot 协议，实现多微信用户共享进程，而且用户数据严格隔离。

**展开：**

- 多个微信个人号**共用同一个 `bot.py` 进程**。
- 每个微信用户的数据**严格隔离**：各自的 `bot_token` / `baseurl` / `contexts` / `last_contact` / `weixin_state_*.json` / `config_*.json` / 日志，互不可见。
- iLink_bot 2.4.6 协议本身支持多 token 并发（每个 token 对应一个微信个人号），本项目应充分利用这一点，**不要**靠"一个用户一个进程"来隔离。
- 新加入的微信用户**不需要单独的 systemd unit**，直接通过 iLink_bot 协议在共享进程里登录、收发消息。

### 预期 2：页面 UX

> 页面显示 iLink_bot 协议的二维码扫码显示之后，会显示登录成功字样，同时在页面有"切换用户"开关，有防抖功能，点击之后，会生成新的扫码登录二维码，让新的微信用户登录并使用 iLink_bot 服务。

**展开：**

- QR 码来自 iLink_bot 2.4.6 `ilink/bot/get_bot_qrcode` 端点（不是自造的）。
- 扫码成功后页面立即显示"登录成功"（状态字段同步到 `web_state.status = "logged_in"`）。
- 页面常驻"切换用户"开关（portal 注入的右上角 bar），点击触发新一轮 QR 展示。
- 切换用户有**防抖**（当前实现：1.5s 内重复点击无效），防止 QR 反复刷新让用户扫到一半死码。

### 预期 3：登录后直接文字对话

> 登录后的微信用户，可以直接输入语言，调用 iLink_bot 来直接转化为文字指令，让后端处理。

**展开：**

- 微信用户扫码登录后，私聊发文字或语音 → bot 进程通过 iLink_bot `getupdates` 长轮询收到；语音读取 `voice_item.text` 转写文本 → 与文字走同一条 LLM（DeepSeek / Claude / GPT，可选）→ 回写 `sendmessage`。
- 全程文本透传，不做额外结构化（除非用户主动用 `/help` 等指令）。
- 用户不需要知道有 LLM、IMA 知识库、四档路由这些实现细节——**只看到微信对话**。

---

## 架构核心词

| 词 | 含义 |
|---|---|
| **共享进程** | 一个 `bot.py` 进程服务 N 个微信用户（**不是**一个用户一个进程） |
| **多进程并行** | 在**一个进程内**通过 asyncio 并行处理 N 个用户的长轮询 / 收发消息 / 定时重连（协程级并行） |
| **数据隔离** | 每个用户的 `bot_token` / `contexts` / 持久化文件按 user 维度独立，互不可见 |
| **iLink_bot 协议** | Tencent OpenClaw iLink 2.4.6 HTTP（`get_bot_qrcode` / `getupdates` / `sendmessage` 等） |
| **新用户接入** | portal 给新用户分配一个 user_id（如 `alice` / `bob` / `eph_xxx`），进程内为该 user_id 启动一个长轮询协程，**不**新开 systemd unit |

---

## 当前实现状态（2026-09-15 改造后）

| 预期 | 实现状态 | 差距 |
|---|---|---|
| 1 · 多用户共享进程 | ✅ **已实现** | `python bot.py` 默认进入 `shared_runtime`；一个 `BotManager` 管理 N 个 `BotSession`，共享一个无 Cookie 的 HTTP 连接池。旧 `--user` 路径只作兼容。 |
| 1 · 数据隔离 | ✅ **已实现** | token、baseurl、上下文、QR、AI/IMA 配置、游标和 state 均为 session 私有；文件名防碰撞，状态原子落盘；共享日志带 `user` 维度。 |
| 2 · 页面 QR + 登录成功 | ✅ **已实现** | `shared_web` 直接读取对应 session 的 `QrFlowState`，同时兼容保留或剥离 `/clawbot` 前缀的 nginx 配置。 |
| 2 · 切换用户 + 防抖 | ✅ **已实现** | `POST /switch` 使用 CSRF 校验和服务端 1.5 秒原子防抖，后台触发该 session 的新 QR，不阻塞 HTTP 请求。 |
| 3 · 登录后文字对话 | ✅ **已实现** | 每个 session 独立长轮询；普通文字进入各自 AI/IMA 栈，再用该账号 token/context 回写。 |
| 3 · 文本指令 | ✅ **保持兼容** | `/help`、`/time`、`/重新连接` 保留；首条普通问题在发送欢迎语后仍继续交给 AI。 |
| 架构 · 新用户无 systemd unit | ✅ **已实现** | `/ephemeral/start` 只在当前进程注册 `eph_<hex>` session，不调用 launcher、systemctl 或 subprocess；网页 TTL 到期只停止未完成扫码的 session，已登录连接继续由后台维护。 |
| 架构 · 单进程多任务并行 | ✅ **已实现** | 同用户并发创建去重，不同用户启动互不持锁；每个用户各自运行消息、定时和重连任务。 |

### 已落实的关键安全与可靠性约束

1. 浏览器只持有随机 opaque session id；不能把 cookie 伪造成 user id 越权访问别人的 QR。
2. `/switch` 与配对码提交必须携带绑定级 CSRF token；创建接口同时有 IP 限流和全局容量限制。
3. 消息批次先持久化，回复确认成功后才记录 message id 并推进 `get_updates_buf`；重放使用稳定 client id。
4. 启动任务和常驻任务受监督；失败 session 会从 Manager 摘除并优雅停止，避免僵尸会话。
5. 未完成扫码的 ephemeral 会话随浏览器 TTL 回收；已登录连接不受浏览器闲置影响；不写 env、不占独立端口、不创建 systemd unit。

### 哪些不需要改

- iLink_bot 2.4.6 协议层（`bot.py:api_get` / `api_post` / `login_with_qrcode` / `do_reconnect`）—— 多 token 并发本来就是协议支持的，只需在调用层加 user 维度。
- AI 层（`dusapi.py` / `deepseek.py` / `ima.py` / `_AIWithIma`）—— 单调用就是单调用，无状态，多 user 各自调用即可。
- 持久化格式（`weixin_state_*.json`）—— 已经是文件级隔离，多租户后改成"每个 user 一个文件"或"一个文件内多 user dict"。
- 日志（`logs/clawbot_*.log`）—— 加 user 字段即可，不用拆文件。

---

## 改造路径（如果要走多租户）

> 详见后续专门的设计文档，本节只是总览。

```
现状（一用户一进程）：
  portal (:18300) → 反代 → bot --user alice (:18301)
                  → 反代 → bot --user bob   (:18302)
                  → 反代 → bot --user eph_xxx (:18xxx)

目标（一进程多用户）：
  portal (:18300) → 内部 API → bot.py 单进程
                              ├─ user "alice" 长轮询协程
                              ├─ user "bob"   长轮询协程
                              └─ user "eph_xxx" 长轮询协程
```

代码改造量：~~中等~~。`bot.py` 状态机要重写（单例闭包 → dict-per-user），`utils/bot_launcher.py` 可以删半，`qr_portal.py` 的反代逻辑换成进程内调用。**不动 iLink 协议层、不动 AI 层**。

---

## 相关文档

- `CLAUDE.md` — 项目总览（本文档优先）
- `docs/EPHEMERAL_BOT_LIFECYCLE.md` — ephemeral 与 systemd 依赖（共享进程改造的伏笔）
- `docs/multi-user.md` — 当前多用户架构（**只是过渡形态**）
- `docs/WECHAT_OAUTH.md` — OAuth 接入
- `docs/IMA_KB.md` — 知识库

---

## 变更日志

- 2026-09-15 · 初版，3 条设计预期 + 当前实现盘点
- 2026-09-15 · 完成 `BotSession + BotManager + shared_web + shared_runtime` 单进程多租户改造
