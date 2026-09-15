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

- 微信用户扫码登录后，私聊发文字 → bot 进程通过 iLink_bot `getupdates` 长轮询收到 → 转给 LLM（DeepSeek / Claude / GPT，可选）→ 回写 `sendmessage`。
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

## 当前实现状态（2026-09-15 盘点）

| 预期 | 实现状态 | 差距 |
|---|---|---|
| 1 · 多用户共享进程 | ❌ **未实现** | 当前是 **一用户一进程**：`bot.py --user alice` 起一个进程，独占 `bot_token` / 端口 / state 文件。多个用户 = 多个 `clawbot@<user>.service` 单元。`bot.py` 内部状态都是单租户闭包（`bot_token_ref[0]` / `last_contact` / `welcomed_users` 全是单值，不是 dict）。 |
| 1 · 数据隔离 | ✅ **已实现**（在多进程前提下） | 每个 `--user` 进程独享 `weixin_state_<name>.json` / `config_<name>.json` / `logs/clawbot_<name>.log` / 端口。OAuth 绑定的 bot 永不被 GC（`utils/bot_launcher.py:reap_ephemeral`）。 |
| 2 · 页面 QR + 登录成功 | ✅ **已实现** | QR 来自 `ilink/bot/get_bot_qrcode`；扫码成功 `do_reconnect` 末尾同步 `web_state.status = "logged_in"`（`bot.py:1099-1102`）；前端 `render(s)` 看到 `status === "logged_in"` 渲染"登录成功 ✓"。 |
| 2 · 切换用户 + 防抖 | ✅ **已实现** | portal 注入右上角"切换账号"按钮 → `/switch` → `POST /relink`；`handle_relink` 有 1.5s 防抖（`qr_web.py:418-424`）。 |
| 3 · 登录后文字对话 | ✅ **已实现** | `message_loop` 长轮询 → `handle_message` 抽文本 → `_AIWithIma` 4 档路由 → `sendmessage` 回写。 |
| 3 · 文本指令 | ⚠️ **部分** | 大部分普通消息走 AI；`/help` / `/time` / `/重新连接` 等斜杠指令直发预定义文本（不经 AI）。但**没有**"结构化指令"层 —— 用户说"打开灯"是 LLM 自由回答，不是预定义动作。 |
| 架构 · 新用户无 systemd unit | ❌ **未实现** | 当前 ephemeral 路径也要 `systemctl start clawbot@eph_<hex>.service`（`utils/bot_launcher.py:_start_systemd`）。共享进程改造后这里会消失。 |
| 架构 · 单进程多任务并行 | ⚠️ **半实现** | 单进程内**已经**是 asyncio 并行（`message_loop` + `reconnect_timer_task` + `relogin_listener` + `web_task` 一起 `asyncio.gather`），但只服务 1 个用户。要扩到 N 个用户 = N 倍的这些协程。 |

### 关键差距（按重要性排序）

1. **`bot.py` 单租户 → 多租户**。最核心。所有单值闭包（`bot_token_ref[0]` / `last_contact` / `welcomed_users` / `qr_state` / `web_on_qrcode`）要改造成 `dict[user_id, ...]`。`message_loop` 要按 user 维度 N 份。
2. **`utils/bot_launcher.py` 的 systemd 路径对 ephemeral 失去意义**。共享进程里没有"启新进程"这回事，新用户只是进程内一个新协程。详见 `docs/EPHEMERAL_BOT_LIFECYCLE.md`。
3. **`qr_portal.py` 的反代**目前直接转发到 `:port`（用户进程）。多租户后不需要反代，portal 直接调用共享进程的内部 API（HTTP 或 in-process）。
4. **`/etc/clawbot/*.env`** 多租户后只剩命名用户（alice / bob）的持久配置；ephemeral 不再写 env。
5. **`/switch` 语义不变**（保留 cookie 触发同 user 的新一轮 QR），但实现路径从"调另一个端口"变成"调共享进程的某个 endpoint"。

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
