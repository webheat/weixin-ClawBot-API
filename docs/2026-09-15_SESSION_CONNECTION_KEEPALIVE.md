# 2026-09-15 会话记录：iLink 连接保活与无感恢复

## 1. 用户问题

用户反馈：微信扫码登录成功后，如果长时间不使用，应用会自动退出，之后无法继续与 OpenClaw 交互。目标是让系统在后台自动、无感地维护登录后的连接状态。

## 2. 排查结论

问题来自两个生命周期被错误地绑在一起：

1. 共享 Web 入口的 `BrowserSessions` 有浏览器闲置 TTL。TTL 到期后，`shared_web.py` 的 sweeper 会调用 `manager.stop(user_id)`，直接停止已经扫码成功并正在运行的 `BotSession`。
2. `bot.py` 和 `bot_session.py` 原本还按本地 24 小时计时器主动进入二维码重连流程。这个计时器不是 iLink 服务端的 token 生命周期承诺；它会把仍然有效的连接误判为需要重新扫码。

因此，用户即使只是没有打开网页，微信侧的数据连接也可能被停止；或者到达本地计时窗口后被要求重新扫码。

## 3. 采用的设计

将系统拆成两个独立生命周期：

```text
浏览器控制面：登录页、二维码、配对码、切换用户
        │
        │ 浏览器 TTL 到期只清理网页绑定
        ▼
微信数据面：BotSession + getupdates 长轮询 + 消息收发
        │
        └─ 仅服务端明确返回 -14 时进入重新扫码恢复
```

- 登录成功后，后台 `BotSession` 独立于浏览器页面继续运行。
- `getupdates` 长轮询本身就是 iLink 连接保活机制；正常超时、空消息、短暂网络故障都不触发扫码。
- `ret=-14` 或 `errcode=-14` 表示服务端明确判定 `bot_token` 已失效，才启动统一的受控重登录流程。
- 真正失效的 token 仍然需要用户重新扫码，这是服务端认证要求，无法由客户端无感伪造恢复。
- 未完成扫码的临时会话仍按浏览器 TTL 回收，避免 QR 登录会话无限占用资源。

## 4. 具体实现

### `bot.py`

- `RECONNECT_CONFIG` 增加 `proactive_relogin=False`，默认关闭旧的本地计时主动扫码。
- legacy 单用户入口仅在显式开启 `proactive_relogin` 时创建 `reconnect_timer_task`。
- `/time` 在默认模式下改为提示“当前连接由后台持续维护”。
- 保留 `getupdates` 收到 `-14` 后的统一 `request_relogin()` 路径。

### `bot_session.py`

- 增加 `has_authenticated_connection` 属性，用于区分“已拿到 token 的后台连接”和“仍在扫码的临时会话”。
- 受控重扫码会显式标记为已认证会话的过渡态：即使为避免复用失效凭据而暂时清空
  `bot_token`，web sweeper 也不会把该 session 当成未认证会话回收；二维码超时后仍可重试。
- 默认只启动消息长轮询，不启动本地到期重连计时器。
- `/TIME` 与 legacy 行为保持一致：默认说明后台持续维护，兼容模式才显示本地计时窗口。

### `shared_web.py`

- 浏览器绑定 TTL 到期后仍移除 opaque cookie 绑定，保持 Web 控制面的安全边界。
- 首次临时登录额外下发独立、随机且 HttpOnly 的恢复 cookie；浏览器绑定过期后重新打开控制页，
  服务端会在确认后台 session 仍存在后自动建立新的短期绑定。恢复 cookie 不携带 raw `user_id`。
- 控制页轮询遇到绑定过期的 `401` 会自动刷新页面，触发上述恢复路径。
- sweeper 检查关联 session 是否已经拥有有效 token：
  - 已认证：保留 `BotManager` 中的后台 session，不调用 `manager.stop()`。
  - 未认证：继续停止未完成扫码的临时 session。

### 文档与测试

- 同步更新 `README.md`、`CLAUDE.md`、`docs/0_DESIGN_INTENT.md`、`docs/1_BOT_SESSION_ARCHITECTURE.md` 和 `docs/multi-user.md`，明确浏览器 TTL 与微信连接生命周期的区别。
- 新增回归测试：浏览器绑定过期后，已认证 BotSession 仍然存在；受控重扫码期间 session 不被回收；
  恢复 cookie 可重新绑定原 session；未认证临时 session 仍被回收。

## 5. 验证结果

```text
30 passed
python -m py_compile bot.py bot_session.py shared_web.py bot_manager.py shared_runtime.py
git diff --check
```

当前实现不再因为用户长时间不访问网页或本地 24 小时计时而中断 OpenClaw 交互。只有 iLink 服务端返回 `-14` 时，系统才会生成新的二维码请求真实重新认证。

## 6. 后续复核：修复失效 token 被重复复用

在语音功能上线后的运行反馈中再次出现“暂无法连接 OpenClaw”。复核确认仓库代码已经更新，但发现多用户 `BotSession` 的 `-14` 重连路径仍保留旧 token：

```text
-14
  → request_relogin()
  → login_with_qrcode(local_token_list=[旧 token])
  → binded_redirect 可能复用旧 token
  → getupdates 再次收到 -14
```

现在 `BotSession._reconnect()` 会在申请新二维码前原子清空内存和持久化状态中的旧 token；新的登录流程不再携带失效 token，`binded_redirect` 也不会被当成旧凭据恢复。新 token 登录成功后再写回状态并恢复长轮询。该路径新增了“失效 token 不得复用”的回归测试。

## 7. Sol 复核后的第二轮修复

高阶模型复核认为上一轮修复虽已切断 stale token 死循环，但仍有两个线上风险：

1. 清空 token 到二维码重新认证之间存在窗口，浏览器 TTL sweeper 可能误把正在受控恢复的 session 回收。
2. 浏览器绑定过期后，用户重新打开控制页没有安全地找回原后台 session 的入口。

本轮以 `_reauthenticating` / `_reauthentication_required` 表示已认证 session 的恢复过渡态，
并增加独立随机恢复 cookie（默认有效 30 天，仅服务端映射到 session）。因此浏览器控制绑定可以短期过期，
但后台数据连接和后续 `-14` 二维码恢复不会丢失；恢复过程不把 `user_id` 放进客户端 cookie。

复核同时要求并已补充：真正从状态文件读取的 token 清空断言、非 `-14` 错误不被分类为 stale token、
并发重连只执行一次以及绑定过期后恢复原 session 的回归测试。

---

## 8. 后续追加(2026-09-16):多事故闭环 + 协议"无心跳"确认

> 本 doc §3 中 "`getupdates` 长轮询本身就是 iLink 连接保活机制" 的设计假设在 **2026-09-16 08:30** 事故中被部分证伪(07:52 与 08:36 的 `-14` 都发生在长轮询活跃期间)。但**长轮询仍是 keepalive 的核心** —— 它维持 TCP 连接、把消息送进 drain 流水线;只是**不能**被视为"token 续命器"。

### 8.1 协议"无心跳"事实

iLink 2.4.6 完整 API 列表(`weixin-openclaw-api-py-docs.md:60-69`)共 8 个端点:

```
POST /ilink/bot/getupdates      # 长轮询
POST /ilink/bot/getconfig       # typing_ticket
POST /ilink/bot/sendtyping      # 输入状态
POST /ilink/bot/sendmessage     # 发消息
POST /ilink/bot/notifystart     # 生命周期
POST /ilink/bot/notifystop
POST /ilink/bot/getuploadurl    # 媒体
POST /ilink/bot/get_bot_qrcode  # 登录
GET  /ilink/bot/get_qrcode_status
```

**没有 `/heartbeat` / `/ping` / `/refresh_token`**。客户端任何活动**都不能延长**服务端 token 有效期(协议 §2.7.3)。"`getupdates` 长轮询保活 token"是错误假设;token 由服务端独立计时,`-14` 即"已过期",客户端无能为力。

### 8.2 2026-09-16 08:30 事故 + 5 层防御

[`docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md`](2026-09-16_RECONNECT_AND_KEEPALIVE.md) §3 详述了 08:30 事故(用户点"切换账号" + 3 张 QR 不扫 → `MAX_QR_REFRESH_COUNT` → `RuntimeError` → 长轮询永久沉默)。该事故 100% 已修复,5 层防御全部部署:

| 层 | Commit | 修复 |
|---|---|---|
| 1 | `4f563a5` | `_drain_lock` 互斥 + pending 保留 |
| 2 | `708ec8c` | 重连失败回滚旧 token |
| 3 | `f2eeb12` | server-driven 失败自动 backoff (60s/300s/900s) |
| 4 | `3ec232b` | observability + **解耦 QR 切换**(架构层根治) |
| 5 | `e6939f3` | `qr_status` 日志 |

详见 [`docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md`](2026-09-16_RECONNECT_AND_KEEPALIVE.md) §0 / §5。

### 8.3 本文 §3 设计原则仍正确

| §3 原则 | 仍正确? |
|---|---|
| 登录成功后 `BotSession` 独立于浏览器继续运行 | ✅ 是 |
| `getupdates` 长轮询本身就是 keepalive(连接级) | ⚠️ **部分**:长轮询维持连接,但**不**续命 token |
| `-14` 才触发受控重扫码 | ✅ 是 |
| 未完成扫码的临时会话按浏览器 TTL 回收 | ✅ 是 |

**修正**:把"`getupdates` 长轮询本身就是 iLink 连接保活机制"理解为**连接级保活**(TCP / 客户端状态),不是**token 级保活**(服务端 token 有效期)。token 由服务端独立计时,客户端无能为力。
