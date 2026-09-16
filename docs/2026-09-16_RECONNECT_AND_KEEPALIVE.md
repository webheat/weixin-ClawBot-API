# 2026-09-16 事故复盘 + 无感重连设计分析

> 接手的 LLM 请先读 §1 TL;DR 和 §3 当前事故状态,再按需跳读 §5–§7。
> 关联文档:[`2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) · [`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md)

---

## 1. TL;DR

1. **当前(2026-09-16 13:22 CST)Bot 已死锁约 5 小时**。`pid 2365525` 还活着、web 心跳正常,但 `getupdates` / `sendmessage` 调用为零,`bot_token` 为空字符串。用户感知:"暂时无法连接openclaw"(用户对"bot 无回复"的口语化转述,代码里**没有**这个字面字符串)。
2. **触发原因**:运维在 Web UI 点了"切换账号"(`POST /switch`)。3 张 QR 60s 内无人扫码,`MAX_QR_REFRESH_COUNT=3` 触发 `RuntimeError`,`request_relogin` future 收到异常后**不会自愈**。
3. **代码层根因复合**:`78e1dfd` 清 `bot_token` 后 QR 失败 → 永久空 token;`0bed146` 删了所有用户可见的重连提醒 → 沉默;`request_relogin` 一次性触发、失败即终态。
4. **立即恢复**:在 Web UI 再点一次"切换账号",或微信端发 `/重新连接`,扫一次 QR 即可恢复。无需改代码。
5. **长期根除**(尚未实现):iLink 2.4.6 协议里**没有**任何心跳端点(`weixin-openclaw-api-py-docs.md:60-69` 全 8 个端点,无 `/heartbeat` / `/ping` / `/refresh_token`)。客户端活动**不能延长**服务端 token 有效期(`§2.7.3` 明确)。所以"周期性心跳"路线不通。"让用户无感"只能靠两条:
   - **A. in-flight 竞态互斥**(修 `IN_FLIGHT_RELOGIN_RACE.md §5.2` 的 `_state_lock`)— 保证重连期不丢消息
   - **B. 预测式重连**(`proactive_relogin=True`,在 token 预计失效前若干小时提前扫码) — 仍需用户配合,但比"被动 -14 后手动恢复"体验好

---

## 2. 三句话背景

- 协议事实:iLink 2.4.6 的 `bot_token` 由服务端独立计时,客户端**任何活动都不能延长**。`-14` 是 server-side token 过期判断,不是 idle reset。官方客户端收到 `-14` 会**冻结账号全部请求 1 小时**(协议 §2.7.3)。
- 当前设计假设(已证伪):`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md §3` 写 "`getupdates` 长轮询本身就是 iLink 连接保活机制"。本次事故 + 07:52 的 `-14 session timeout` 双重证伪。
- 完整 API 列表(共 8 个,无心跳):`/ilink/bot/{getupdates,getconfig,sendtyping,sendmessage,notifystart,notifystop,getuploadurl,get_bot_qrcode}` + `GET /ilink/bot/get_qrcode_status`。`grep -iE "heartbeat|keepalive|ping"` 在协议文档里零命中。

---

## 3. 当前事故状态(2026-09-16 13:22 CST)

### 3.1 进程与连接

| 项 | 值 | 含义 |
|---|---|---|
| pid | `2365525` | 08:16 启动,已运行 5h06m |
| 状态 | `Ssl`,7 线程,212 MB | 健康,**未崩溃**,`ep_poll` 空闲 |
| TCP 连接 | **零条**到 `ilinkai.weixin.qq.com` 4 个 IP | 长轮询完全停止 |
| 残留连接 | `10.1.0.11:60656 → 109.244.194.209:443` | `CLOSE_WAIT`,5h 未关闭,32 字节未读(可疑 socket leak,**不阻塞恢复但应修**) |
| Web | `:18300` 监听,`/state` 每 2s 心跳 | 健康 |

### 3.2 状态文件与日志

| 路径 | 内容 | 含义 |
|---|---|---|
| `weixin_state_96Up7nuU68ZEX1VqMmVGOKgdlFlyEpDI87NVPg-S_DU.json` | `bot_token=""`, `ilink_bot_id=3783fdd31be8@im.bot`, `ilink_user_id=o9cq806m1rtXSvyUgFcUc_KO_N7I@im.wechat` | **token 已空**,但 owner / bot_id 仍在 |
| `logs/clawbot_shared.log` | 最后写入 **10:37:02** | 已 2h45m 无新业务日志 |
| `logs/clawbot.log` | 510 字节,只 3 行 | 07:33:22 的 ima_bindings 测试残留,**不是**当前活跃日志 |

### 3.3 因果时间线(精确到秒)

| 时间 | 事件 | 来源 |
|---|---|---|
| 07:52:10 | `getupdates` → `errcode=-14 session timeout` | `clawbot.api` |
| 08:28:04 | QR 重新扫码成功,新 `ilink_bot_id=3783fdd31be8@im.bot` | `clawbot.qr` |
| 08:28:52 | 收到一条语音消息,08:28:57 成功回复 | `clawbot.message` |
| **08:30:27** | **运维 `POST /switch` → `request_relogin("web switch")`** | `clawbot.shared_web` + `clawbot.reconnect` |
| 08:30:27 → 08:36:31 | 3 张 QR(`refresh_count=1/3/2/3/3/3`),60s 内无人扫码,全部 `status="expired"` | `clawbot.qr` |
| **08:36:31** | `MAX_QR_REFRESH_COUNT` 命中 → `RuntimeError("二维码多次失效或登录失败,请稍后重试。")` → `relogin_failed` | `bot.py:1558` + `bot_session.py:377` |
| 08:36:31 → 13:22 | **零** `getupdates`、**零** `sendmessage`、**零** `recv msg`,只有 web `/state` 心跳 + `ima_bindings lookup miss` 探针 | `clawbot.shared_web` + `clawbot.ima_bindings` |

### 3.4 立即恢复步骤(运维手动,无需改代码)

任选其一即可:

1. **Web UI**:浏览器 → `http://<host>:18300/clawbot/` → 找到当前 session 标签 → 点 **"切换用户"** → 微信扫码
2. **微信端**:给 bot 发 `/重新连接`,按提示回 `Y` → 扫码
3. **强制重启**:`systemctl restart clawbot-shared.service` → 必须重新扫码(会丢 in-flight 历史上下文,但 `runtime_state.json` 已持久化关键状态)

恢复后**必须**的 sanity check:

```bash
# 1) 确认 token 已恢复
python3 -c "import json; print(json.load(open('/opt/weixin-ClawBot-API/weixin_state_96Up7nuU68ZEX1VqMmVGOKgdlFlyEpDI87NVPg-S_DU.json'))['bot_token'][:12]+'...')"

# 2) 确认 getupdates 重新开始
grep "POST ilink/bot/getupdates" /opt/weixin-ClawBot-API/logs/clawbot_shared.log | tail -3

# 3) 确认没有 pending_messages 残留
python3 -c "import json; s=json.load(open('/opt/weixin-ClawBot-API/weixin_state_96Up7nuU68ZEX1VqMmVGOKgdlFlyEpDI87NVPg-S_DU.json')); print('pending=',s.get('pending_messages'))"
```

---

## 4. 代码层根因剖析(已合并的代码)

### 4.1 复合三个改动造成"半死"状态

| 提交 | 文件:行 | 改动 | 副作用 |
|---|---|---|---|
| `78e1dfd` `fix(session): preserve controlled reauth` | `bot_session.py:336-381` | `_reconnect()` 在 `_login()` 前**原子清空** `self.bot_token` / `_token_ref[0]` / `runtime_state["bot_token"]` | QR 失败时 token **永久留空**,`_send_reliable` 拿到空 token 直接 -14 |
| `0bed146` `feat(reconnect): 后台静默重连 + 终态单条通知` | `bot.py:991-1058` | 删除所有用户可见的 `warn_msg` / `remind_msg`,只发终态单条 | 用户**完全收不到**"正在重连 / 需要扫码"信号,微信端只有沉默 |
| `62bff45` `fix(session): decouple web TTL from iLink keepalive` | `bot_session.py:760-779`,`shared_web.py:~487` | `_timer_loop` 仅 `proactive_relogin=True` 时启动;sweeper 不再回收有 token 的 session | 用户用 `/TIME` 探测活跃度,得到 "当前连接由后台持续维护,服务端 token 失效时会自动恢复" 这种**被动答案** |

### 4.2 `request_relogin` 是一次性的,不重试

`bot_session.py:358-369` 当前实现:

```python
async def request_relogin(self, reason: str = "manual") -> dict[str, Any]:
    if self._stopped:
        raise RuntimeError("session stopped")
    async with self._relogin_lock:
        future = self._pending_relogin
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self._pending_relogin = future
        self._initial_cancel.set()
        self._relogin_event.set()
    await self._emit("relogin_requested", reason=reason)
    return await future
```

→ 触发后**只跑一次**;`future` 拿到异常即终态,长轮询 task 不会自动重新触发 `login_with_qrcode`。

### 4.3 没有 `request_relogin` 日志

`reason` 只走 `_emit` hook,**没有 `log_session.info(...)`**,共享日志里看不到触发源。`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md §5.1` 已经指出,但**未合并**。

---

## 5. "让用户无感"的方案对比

### 5.1 评估"周期性心跳"思路 —— **不可行**

iLink 2.4.6 协议里**没有**心跳端点(`weixin-openclaw-api-py-docs.md:60-69` 全 8 个端点)。即便周期性调:

| 候选 | 是否延长 token 有效期 | 副作用 |
|---|---|---|
| `getupdates` | ❌ 协议 §2.7.3 明确否 | 已经在用,无效 |
| `sendtyping` | ❌ 协议 §2.7.3 明确否 | 需要 `user_id` + `typing_ticket`,会让 WeChat 联系人看到"正在输入..."(bot 莫名冒泡) |
| `sendmessage` | ❌ 协议 §2.7.3 明确否 | **真的会送达一条消息**,必须有接收方 |
| `getconfig` | ❌ 协议 §2.7.3 明确否 | 需要 user context |

**结论**:客户端任何活动都不能延长服务端 token 有效期。这是用户记忆中的错误部分。

### 5.2 三条真正可走的路

#### 方案 A:in-flight 竞态互斥(根治"重连期丢消息") ★ 推荐

来源:[`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) §5.2 + §5.3。

- **改动**:`bot_session.py` 加 `self._state_lock = asyncio.Lock()`,`_drain_batch` 整段持锁,`_reconnect` 进入前 `await lock.acquire()`
- **用户感知成本**:浏览器点"切换账号"后最坏等当前 LLM 算完(5–15s)才看到新 QR。比当前"断流 + 卡死"强
- **状态**:§5.1 加 reason 日志、§5.2 lock、§5.3 `_send_reliable` 失败保留 `pending`,全部**未合并**
- **实现量**:~30 行 + 1 个 lock + 测试

#### 方案 B:预测式重连窗口(`proactive_relogin=True`)

来源:[`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) §4 `bot_session.py` 部分 + §3 设计原则。

- **改动**:`RECONNECT_CONFIG.proactive_relogin=True` 启用 `_timer_loop`,在 `session_duration - warning_before` 时主动弹 QR
- **用户感知成本**:用户不在手机旁时**仍**会失败,落到 `max_refresh_exceeded`,回到"半死状态"
- **状态**:代码已实现,只是默认 `False`(`bot.py:107`)
- **风险**:会让用户在深夜被弹 QR,体验**未必**比当前好

#### 方案 C:加 `request_relogin` 失败后自动重试(backoff)

- **改动**:`request_relogin` 在 `future` 拿到异常时,调度一次性 backoff 重试(60s / 300s / 900s),最多 3 次
- **用户感知成本**:0,后台尝试
- **风险**:若 iLink 整个 token 池已黑名单该账号,重试只会打满接口(协议 §2.7.3 "官方插件会暂停该账号全部请求 1 小时")
- **建议**:仅在 `reason ∈ {"stale-token", "session-expiry"}` 时启用;`{"web switch", "manual"}` 不重试(尊重用户取消意图)

### 5.3 推荐顺序

1. **方案 A §5.1**(5 行):`request_relogin` 加 reason 日志 —— 立刻可合,无悔改动
2. **方案 A §5.2 + §5.3**:in-flight 互斥 + `_send_reliable` 失败保留 `pending` —— **真根治**,但需要测试
3. **方案 C**(可选):自动重试 backoff,作为最后一层兜底
4. **方案 B**:**不建议**作为默认,仅作 named-user 配置项开放

---

## 6. 顺手要修的小问题(incident 暴露)

| 问题 | 文件:行 | 优先级 |
|---|---|---|
| `request_relogin` 不写日志 | `bot_session.py:358-369` | P0(同 §5.1) |
| `send_typing_safe` 失败不写日志 | `bot.py:730-755` | P1(`IN_FLIGHT_RELOGIN_RACE.md §8`) |
| `_send_reliable` 无脑抛,跟 `send_msg_safe` 行为不一致 | `bot_session.py:473-501` | P1(同 doc §8) |
| `109.244.194.209:443` socket 泄漏(`CLOSE_WAIT` 5h,32 字节未读) | runtime(`aiohttp.TCPConnector`) | P2(不阻塞恢复) |
| `ILINK_STORE_SQLITE_PATH=./data/ilink_bot.db` 指向不存在的 dir | env | P2(无关本事故,但应清理) |
| `account_changed=True` 无条件清 `contexts` / `welcomed_users`,与 named-user 保活意图冲突 | `bot_session.py:_apply_login` | P2(同 `IN_FLIGHT_RELOGIN_RACE.md §10`) |

---

## 7. 验证步骤(方案 A 落地后跑)

```bash
# 1) request_relogin 必须能在日志里 grep 到 reason
grep "request_relogin reason=" /opt/weixin-ClawBot-API/logs/clawbot_shared.log | tail -5

# 2) in-flight 锁定行为:见 IN_FLIGHT_RELOGIN_RACE.md §6.1 test_relogin_waits_for_drain

# 3) 端到端复现:
#    a) 起一个 session,扫一次码让它进 logged_in
#    b) 微信侧连发两条消息(间隔 < LLM 响应时间)
#    c) 在第二条的 LLM 计算期间,浏览器点 #switch
#    d) 观察:修复前看到 -14 + login start 几乎同时;修复后 login start 在 _send_reliable 之后
#    e) 重连成功后 weixin_state_*.json 里 pending_messages 应为空

# 4) 回归:浏览器绑定过期后,已认证 BotSession 仍然存在(62bff45 已有)
```

---

## 8. 引用

- 协议:`weixin-openclaw-api-py-docs.md:60-69`(API 列表),`:444-460`(超时与 token 失效语义)
- 代码:
  - `bot.py:1558` `RuntimeError("二维码多次失效或登录失败,请稍后重试。")` raise 点
  - `bot.py:1018` `print("[重连] 服务端提示已连接过此 OpenClaw,继续沿用当前连接")` —— 仅运维终端可见,**不是**用户消息
  - `bot.py:730-815` `send_typing_safe` / `get_typing_ticket_safe` —— 当前只在 AI 处理期间调,无定时任务
  - `bot_session.py:336-381` `_reconnect()` 清 token 后 QR
  - `bot_session.py:358-369` `request_relogin()` —— 当前实现,无 reason 日志,无重试
  - `bot_session.py:473-501` `_send_reliable()` —— 失败无脑抛,`pending` 不保留
  - `shared_web.py:543-549` `web switch` 端点,触发 `request_relogin("web switch")`
- 提交:
  - `78e1dfd` `fix(session): preserve controlled reauth` —— 清 token 修复,但没解决 in-flight 竞态
  - `0bed146` `feat(reconnect): 后台静默重连 + 终态单条通知` —— 删了所有用户提醒
  - `62bff45` `fix(session): decouple web TTL from iLink keepalive` —— `/TIME` 回复改被动措辞
  - `c60aeae` `refactor(session): drop eph_<hex>` —— 与本事故无关(纯重命名)
- 历史 incident 文档:
  - [`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) —— "long-poll 即保活" 设计假设(已证伪)
  - [`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) —— 竞态分析 + 三层修复方案(未合)
- 当前进程数据:
  - `pid 2365525`,`/proc/2365525/{status,fd,net/tcp}`
  - `weixin_state_96Up7nuU68ZEX1VqMmVGOKgdlFlyEpDI87NVPg-S_DU.json`(`bot_token=""`)
  - `logs/clawbot_shared.log`(最后写入 10:37:02)
- 协议引用文本(§2.7.3):

  > 2.4.5 将内部命名从"session expired"改为"stale token",明确 `ret=-14` 或 `errcode=-14` 表示 bot token 已失效/过期,而不只是普通会话超时。官方插件会暂停该账号全部请求 1 小时,避免快速重试打满接口。