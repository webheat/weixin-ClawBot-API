# 多用户支持现状与未来规划

## 当前架构（单租户）

`bot.py` 启动一个 Python 进程，同一时间只持有一个 iLink `bot_token`，对应一个微信个人号。`qr_web.py` 在同一个进程里起 `aiohttp` 服务（绑 `127.0.0.1:18300`），共享一份 `QrFlowState` 单例和一份 `weixin_state.json`。

iLink OpenClaw 协议本身允许多个 `bot_token` 并发，但当前实现没有用到这个能力。**协议侧群聊入口固定 `ChatType: direct`**（见 `weixin-openclaw-api-py-docs.md:486-490`），所有入站消息都按私聊处理。

## 两种多用户语义

### 场景 A：多人共用同一个 bot 账号（已支持，无需改动）

一个 bot 进程登录一个微信账号 X，X 的任意微信好友发消息都会收到 AI 回复。`welcomed_users` / `last_contact` / `manual_reconnect_pending` 都是 per-peer 设计，互不干扰。

适用：把 bot 当作群助手、家庭号、企业号对外客服。

### 场景 B：每个用户各自扫码绑定自己的微信（**当前不支持**）

每个用户要独立登录自己的微信个人号、看到自己的 QR、用自己的 token 鉴权。当前问题：

- `weixin_state.json` 单文件，多用户会互相覆盖 token
- `QrFlowState` 是模块级单例，多用户的 QR 互相串
- `:18300` 单端口，反代也只能指向一个进程
- `CLAWBOT_WEB_TOKEN` 单值，无法隔离用户

## 多用户共享（场景 A）下的体验痛点（已复核）

下面按真实严重度排序。早期版本有 3 处错误，已在本节末尾用 `~~删除线~~` 标注并说明纠正依据。

### P0 — 用户感知断线

**1. 重连期间 30-60s 完全无响应**

`message_loop` 在检测到 `-14`（bot.py:1531）后 `await login_with_qrcode(...)` 阻塞，期间没有任何 `getupdates` 长轮询在飞。用户的请求堆积在 iLink 服务端直到 token 恢复。

**消息不丢，但延迟**：`get_updates_buf` 是 iLink 服务端的 cursor，重连成功后新一轮长轮询会用旧 cursor 拉取，gap 期间到达的消息会一并补发。**除非** `apply_new_login` 触发账号切换（清空 cursor），才会真丢。

早期结论"真空期消息永久丢失"——错了。iLink 服务端的 cursor 设计就是为了这个场景；只有账号切换才真丢。

**2. `last_contact` 单值，重连通知只发给最近一个用户**

`bot.py:1264` 只跟踪"最近一个发消息的人"；`reconnect_timer_task:868` 倒计时 2h 警告 → 只发 `last_contact`。其他活跃用户完全不知道 bot 即将到期；到点被强制踢下线，没人收到提醒。

多人共享场景下，必须改成"24h 内所有活跃用户都通知"才合理。

### P1 — 影响多人并发体验

**3. `message_loop` 串行 await → 一人慢全员等**

`bot.py:1507` `for msg in result.get("msgs"): await handle_message(msg)` 串行。用户 A 触发 8s 长回答 → 用户 B 的消息在 queue 里等 8s。并发用户越多，尾延迟叠加越严重。

缓解：单次 `getupdates` 返回的多个消息可以用 `asyncio.gather` 并发处理；不同长轮询周期天然不阻塞。

### P2 — 长期或边缘问题

**4. `send_msg_safe` 静默吞错（welcome/help/重连警告）**

`bot.py:588-590` catch 所有 Exception 只 print 不通知。早期版本我说"AI 回复被吞"——错了。**AI 回复走 `api_post + ensure_business_success`**（bot.py:1492），失败抛 `ILinkAPIError` → `message_loop` 走重连 / backoff 路径，不会静默吞。

`send_msg_safe` 只用在 4 类消息：
- 首次欢迎语（`COMMANDS_MSG`）
- `/help` / `/指令` 回复
- 非文本消息提示
- `reconnect_timer_task` / `do_reconnect` 的重连通知

这些非核心消息被吞后，用户感知"命令列表不显示""重连没人提醒"，不影响对话主链路。

**5. `runtime_state["contexts"]` 无限膨胀**

每个新 `from_id` 都 append，从不 GC。1000 用户后 `weixin_state.json` 几十 MB；`save_runtime_state` 每次全量重写 → IO 慢 + 启动加载慢。长期运维地雷。

缓解：切到 SQLite 或 per-user 分文件存储。

### ~~P1 — 已降级为 P3~~

~~**6. typing_ticket_cache 跨 context_token 复用**~~

`bot.py:621` 缓存 key = `user_id`，不含 `context_token`。用户重连/重装微信后 context_token 轮换，cache 仍返回旧 ticket → iLink 拒绝 → "正在输入"显示但不发送。

实际影响：只导致 UI 上"对方正在输入"提示偶尔不出现，**消息本身仍正常送达**。从 P1 降为 P3。

## 参考实现对比（XTmai/WeChat-iLinkBot）

参考仓库：https://github.com/XTmai/WeChat-iLinkBot — 是一个 iLink 协议的 CLI 客户端（login + listen + send），不是 bot 实现。它对我们当前架构的指导意义：

### 设计一致（说明方向对）

| 点 | XTmai 实现 | 我们的实现 |
|---|---|---|
| 消息类型过滤 | `if msg.get("message_type") != 1: continue` | `bot.py:1320 if msg.get("message_type") != 1: return` |
| 联系人/上下文存储 | `contacts.json` keyed by `from_user_id`，存 `context_token + last_text + last_seen_at` | `runtime_state["contexts"][from_id]` 存 `context_token` |
| `get_updates_buf` 处理 | "opaque blob，原样回传" | `runtime_state["get_updates_buf"]` 原样保存转发 |

### XTmai 没做、我们做得更好

- **重连机制**：XTmai `cmd_listen` 异常就 `time.sleep(3)` 接着跑，没有 `-14` 兜底。我们有 stale_token 检测 + 受控重新登录。
- **Web UI 登录二维码**：XTmai 只有终端 `print_terminal_qr`。
- **配对码 web 输入**：XTmai 没做。

### XTmai 也没做、我们同样缺（协议 + 应用层通用缺口）

- **per-user 对话历史**：XTmai 只存 `last_text`（最近一条），不存 history。协议 SDK 层不提供，是应用层自建功能，跟我们 TODO 里写的方案一致。
- **并发用户隔离**：XTmai 是 CLI 工具一次性 `cmd_listen`，没考虑多用户并发场景。
- **消息可靠性 / 重发**：两边都没做。

**结论**：上面 1-5 的痛点不是某家实现的偶发问题，而是 iLink 协议 + 应用层通用设计缺口，必须我们自己修。

## 推荐路径

### 场景 B 触发后的多进程方案

1. `bot.py` 增加 `--user <name>` CLI 参数，派生 `STATE_FILE = f"weixin_state_{name}.json"`、`CLAWBOT_WEB_PORT = 18300 + idx`、独立的 `CLAWBOT_WEB_TOKEN`（放在 `/etc/clawbot/<user>.env`）
2. 新建 systemd 模板单元 `/etc/systemd/system/clawbot@.service`，`%i` 替换为用户名，开用户 = `systemctl enable --now clawbot@<user>`
3. nginx 加 `location ^~ /clawbot/([^/]+)/ { proxy_pass http://127.0.0.1:1830${1}; }` 路由
4. ima 凭据可以共用（一份 `.env` 由 systemd `EnvironmentFile` 指向 `/etc/clawbot/ima.env`），节省管理成本

**不推荐单进程多租户**：复杂度高、长轮询并发可能踩协议限制、故障域变大，性价比不如多进程。

### 当前（场景 A）下应优先修的痛点

按 ROI 排序：
- **#2 last_contact 单发 → 改 broadcast 到所有活跃用户**：改动小（`runtime_state["contexts"]` 已有 from_id 列表），收益大
- **#3 串行 → gather 并发**：改动很小（`asyncio.gather(*[handle_message(m) for m in msgs])`），尾延迟明显改善
- **#4 send_msg_safe 静默 → 加 retry 或日志告警**：低风险

不建议先动：
- **#5 runtime_state 膨胀**：用户量到 100+ 再切 SQLite
- **#1 重连无响应**：协议侧无解，只能缓解（缩短 reconnect 时长、im 端做 retry）

## 触发重新评估（场景 B）的信号

- 单账号下的活跃用户数 > 50 且彼此消息需要完全隔离
- 出现"我希望用我自己的微信号登录，不是某个公用号"的明确需求
- 需要给不同用户配置不同的 AI provider / system prompt（顺带做 per-user config）
