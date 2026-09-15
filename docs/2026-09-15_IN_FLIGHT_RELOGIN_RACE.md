# 2026-09-15 复盘：in-flight AI reply 与 request_relogin 竞态

> 严重等级：P0（用户体验直接断流）
> 影响面：共享运行时下所有 `eph_*` / `o*` / named 用户的「处理中消息」都会被无差别打断
> 触发面：`request_relogin()` 的全部 4 个调用点（manual / stale-token / session-expiry / web switch）
> 修复状态：本文档只含分析与方案；代码补丁待 review 后单独立 commit

---

## 1. TL;DR

`BotSession._drain_batch()` 与 `BotSession._reconnect()` 是**两个独立 asyncio task**，没有互斥。前者正在 `run_in_executor` 跑同步的 LLM 调用（5–15 s），后者在等待用户的 Web 重新扫码；扫码确认的瞬间，`_reconnect` 会**原子清空** `_token_ref[0]` 和磁盘上的 `bot_token`，但**不会通知、也不会等待** in-flight 中的 `_send_reliable`。

5 秒后 LLM 算完，`_send_reliable` 用已经被清掉的旧 token 发 `sendmessage`，iLink 返回 `errcode=-14 session timeout`；该 msg id 此时还**没有**被写入 `runtime_state["processed_message_ids"]`，于是它永远卡在 `runtime_state["pending_messages"]` 里。

用户感知：「我刚才那句话你回了吗？」→ 没有 → 重连后机器人只回新消息，旧问题被吞。

## 2. 事故时间线（本次 `eph_656ef35f...` 实例）

| 时间 (CST) | 事件 | 来源 |
|---|---|---|
| 18:41:44 | `login start refresh_count=1/3`，QR 首次拉取 | clawbot.qr |
| 18:42:43 | `poll status=confirmed bot_id=077b77e3`，首次登录完成 | clawbot.qr |
| 18:42:43 | `notifystart` 成功 | clawbot.reconnect |
| 18:42:55.222 | `getupdates` 返回 seq=1 语音「介绍一下你自己吧」（已转写） | clawbot.api |
| 18:42:55.343 | `sendtyping 1` → 200；IMA 搜索开始 | clawbot.api / clawbot.ima |
| 18:42:56.115 | IMA 0 命中 → local 0 命中 → semantic 5 命中；`mode=llm+semantic` | clawbot.ima / clawbot.ai |
| 18:42:58.888 | `sendmessage` → 200，**msg #1 成功送达**（message_id=7505576895791177096） | clawbot.api |
| 18:42:58.981 | `sendtyping 2` → 200 | clawbot.api |
| 18:43:15.222 | `getupdates` 返回 seq=2 语音「那你具体能查阅什么资料呢」 | clawbot.api |
| 18:43:15.226 | `recv msg ... type=voice_transcript` | clawbot.message |
| 18:43:15.343 | `sendtyping 1` → **200**（旧 token 此时还有效） | clawbot.api |
| 18:43:15.343 | IMA 检索再次开始（0 命中） | clawbot.ima |
| 18:43:16.115 | semantic 5 命中，prompt 拼好（`ctx_chars=1272`） | clawbot.ai |
| 18:43:16.130 | LLM 调用开始（`run_in_executor`） | clawbot.ai |
| **18:43:16.887** | **⛔ `login start refresh_count=1/3`** —— 有人触发了 `request_relogin` | clawbot.qr |
| 18:43:16.982 | `_reconnect` 已清空旧 token；新 `get_bot_qrcode` 成功 | clawbot.api |
| 18:43:21.216 | LLM 算完，`_send_reliable` 用旧 token 发 `sendmessage` → **errcode=-14** | clawbot.api |
| 18:43:21.252 | `sendtyping 2` → **errcode=-14** | clawbot.api |
| 18:43:47 → 18:49:19 | 客户端轮询新 QR；3 次过期后 `max_refresh_exceeded` | clawbot.qr |
| 18:49:19 | `login aborted refresh_count=3 reason=max_refresh_exceeded` | clawbot.qr |
| 18:49:19 | `web relogin failed user=eph_656ef35f...` | clawbot.shared_web |

> 关键证据：18:43:15.343 `sendtyping` 仍返回 `ret:0`，**说明旧 token 在 18:43:15 还是好的**。18:43:16.887 的 `login start` 与 18:43:15.343 之间**只有 1.5 s 间隔**，LLM 调用不可能在那 1.5 s 内完成（第一次正常 AI 回复用了 3.4 s）。因此 `login start` 不是 `_drain_batch` 自己发起的。

## 3. 触发源定位

`request_relogin` 在仓内共 4 个调用点：

| 调用点 | 位置 | 本次是否触发？ |
|---|---|---|
| `request_relogin("stale-token")` | `bot_session.py:708`，`getupdates` 拿到 -14 时 | ❌ 日志中没看到 `getupdates → -14` |
| `request_relogin("session-expiry")` | `bot_session.py:736`，`_timer_loop` | ❌ `proactive_relogin=False`（CLAUDE.md 明确默认关闭） |
| `request_relogin("manual")` | `bot_session.py:551`，用户回复 `Y` 给 `/重新连接` | ❌ 微信侧没收到「确认要立即重新连接吗？」这条 prompt |
| `request_relogin("web switch")` | `shared_web.py:543-549`，浏览器点 #switch 按钮 | ⚠️ **唯一剩下的可能**；`request_relogin` 的 `reason` 没写日志，无法直接确认 |

**`request_relogin` 当前实现（`bot_session.py:358-369`）**：

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
    await self._emit("relogin_requested", reason=reason)   # ← hook 不是 logger
    return await future
```

> `reason` 只被 `_emit` 消费（hook），没有 `log_session.info(...)` 行 → 共享日志里**完全看不到**是谁触发的，定位只能靠排除法。这是本事故**诊断层**的根因。

## 4. 根因：两个 task 共享 mutable state，零协调

### 4.1 状态写入顺序

`bot_session.py:631-649` `_drain_batch`：

```python
async def _drain_batch(self, messages, cursor):
    processed = list(map(str, ...))[-1000:]
    seen = set(processed)
    for msg in messages:
        mid = self._message_id(msg)
        if mid in seen:
            continue
        await self.handle_message(msg)              # ← 包含 AI 调用（5–15 s）
        seen.add(mid)
        processed.append(mid)
        processed = processed[-1000:]
        self.runtime_state["processed_message_ids"] = processed   # ← 仅成功后才记录
        self.save_state()
    self.runtime_state["pending_messages"] = []     # ← 仅全部成功才清空
    if cursor:
        self.runtime_state["get_updates_buf"] = cursor
    self.save_state()
```

`bot_session.py:473-501` `_send_reliable`（实际发送回复）：

```python
result = await api_post(
    self.http,
    "ilink/bot/sendmessage",
    {...},
    self._token_ref[0],                              # ← 读的是 mutable ref
    self._base_url_ref[0] or None,
    timeout=API_TIMEOUT,
)
```

`bot_session.py:311-356` `_reconnect`：

```python
async def _reconnect(self) -> dict[str, Any]:
    async with self._reconnect_lock:
        ...
        current = self.bot_token
        ...
        self._reauthenticating = had_authenticated_connection
        self.bot_token = ""                          # ← 内存清零
        self._token_ref[0] = ""                      # ← _drain_batch 看到的也是这个
        self.runtime_state["bot_token"] = ""         # ← 磁盘清零
        self.save_state()                            # ← 持久化清零
        try:
            result = await self._login(reconnect=True)
            ...
            await self._apply_login(result)          # ← 拿到新 token
            ...
```

**两个 task 的交错**（红色是 _reconnect 写入的空 token）：

```
t=0.0  _drain_batch  sendtyping 1     读 token_ref[0] = "valid-AAA"     ✓
t=0.1  _drain_batch  ai.chat(...) 开始 run_in_executor（线程池，5–15 s 阻塞）
t=1.5  _reconnect    _token_ref[0] = ""     （用户点 Switch / 触发了 request_relogin）
t=1.5  _reconnect    save_state()      （磁盘 token 也空了）
t=1.5  _reconnect    login_with_qrcode()  （开始新 QR 轮询）
t=5.0  _drain_batch  LLM 算完
t=5.0  _drain_batch  _send_reliable     读 token_ref[0] = ""            ✗ → -14
t=5.0  _drain_batch  raise ILinkAPIError(-14)   _drain_batch 退出循环
                                                            ↑ 不会写 processed_message_ids
                                                            ↑ 不会清 pending_messages
                                                            ↑ 不会推进 get_updates_buf
```

### 4.2 现有保护为何没拦住

| 已有保护 | 为什么没拦住 |
|---|---|
| `_apply_login` 在 `account_changed=True` 时清 `pending_messages` | 本次 `ilink_bot_id` 不变（同账号重连），分支不进 |
| `_reconnect` 用 `_reconnect_lock` 串行化多次重连 | **只防多次重连，不防「重连 vs 处理中」**；`_drain_batch` 不持这把锁 |
| `send_msg_safe` 失败会 fallback 到 console | `_send_reliable` 没有 fallback，直接 `ensure_business_success` 抛异常 |
| `message_loop` 捕获 `ILinkAPIError.is_stale_token` | 只覆盖 `getupdates` 路径，**不覆盖 `_send_reliable` 路径** |
| `_initial_cancel` 在 `request_relogin` 中被 set | 只在 `_login`（初次登录）里被检查，reconnect 路径不看它 |

## 5. 修复方案（三层）

### 5.1 P0 立刻打：让 `request_relogin` 在日志里留痕

**改动范围**：`bot_session.py:358-369`，**5 行内**。

```python
async def request_relogin(self, reason: str = "manual") -> dict[str, Any]:
    from utils.logging_setup import get_logger
    log_session = get_logger("session")

    if self._stopped:
        raise RuntimeError("session stopped")
    async with self._relogin_lock:
        future = self._pending_relogin
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self._pending_relogin = future
        self._initial_cancel.set()
        self._relogin_event.set()
    log_session.info(                                # ← 新增
        "request_relogin reason=%s pending_msgs=%d processed=%d",
        reason,
        len(self.runtime_state.get("pending_messages") or []),
        len(self.runtime_state.get("processed_message_ids") or []),
    )
    await self._emit("relogin_requested", reason=reason)
    return await future
```

> 效果：以后出同样的事，`grep "request_relogin" logs/clawbot_shared.log` 一行就能定位。**不解决 bug，只解决诊断盲区**。

### 5.2 P1 主修复：`_drain_batch` 与 `_reconnect` 互斥

**思路**：在 `BotSession` 上加一把 `_state_lock`（或复用 `_reconnect_lock` 改名），让 `_drain_batch` 整段持锁；`_reconnect` 进入临界区前先 `await lock.acquire()`，并设置 `_reauthenticating=True` 让 `request_relogin` 重入时**等待**而不是**重复触发**。

**实现草案**（`bot_session.py`）：

```python
class BotSession:
    def __init__(self, ...):
        ...
        # 现有：
        self._reconnect_lock = asyncio.Lock()
        # 新增：
        self._state_lock = asyncio.Lock()         # 覆盖整段「拿 token → 调 API → 改状态」
        self._drain_in_flight = 0                 # 调试可读；可选

    async def _drain_batch(self, messages, cursor):
        async with self._state_lock:              # ← 整个 _drain_batch 持锁
            processed = list(map(str, self.runtime_state.get("processed_message_ids") or []))[-1000:]
            seen = set(processed)
            for msg in messages:
                mid = self._message_id(msg)
                if mid in seen:
                    continue
                await self.handle_message(msg)    # AI 调用也持锁（用户会感知到「重连更慢」）
                seen.add(mid)
                processed.append(mid)
                processed = processed[-1000:]
                self.runtime_state["processed_message_ids"] = processed
                self.save_state()
            self.runtime_state["pending_messages"] = []
            if cursor:
                self.runtime_state["get_updates_buf"] = cursor
            self.save_state()

    async def _reconnect(self) -> dict[str, Any]:
        # 关键修改：等 _drain_batch 跑完（最多 15 s 量级），再清 token
        async with self._reconnect_lock:
            await self._state_lock.acquire()      # ← 阻塞到 in-flight 处理完
            try:
                ...  # 原有的清 token / login / apply_login 逻辑
            finally:
                self._state_lock.release()
```

**用户感知成本**：用户在浏览器点「切换用户」后，最坏情况下要等当前 LLM 算完（5–15 s）才看到新 QR 出现。比当前的「断流 + 卡死」强。

**退路方案**（如果业务上不能接受 LLM 阻塞重连）：只锁 `_send_reliable` 那一段，不锁 AI 计算 —— 复杂度更高，需要 token 「双 ref」（old + new）切换，**不推荐**。

### 5.3 P2 防御：在 `_send_reliable` 失败时重试或诚实上报

让 `_send_reliable` 拿到 -14 时**不抛**，而是返回失败状态，由 `_drain_batch` 决定下一步：

```python
async def _send_reliable(self, msg, to_id, context_token, text, kind) -> bool:
    """返回 True=已送达，False=未送达（_drain_batch 应保留 pending）。"""
    from bot import API_TIMEOUT, api_post, base_info, ensure_business_success, ILinkAPIError
    try:
        result = await api_post(
            self.http, "ilink/bot/sendmessage",
            {"msg": {...}, "base_info": base_info()},
            self._token_ref[0], self._base_url_ref[0] or None,
            timeout=API_TIMEOUT,
        )
        ensure_business_success(result, "sendmessage")
        return True
    except ILinkAPIError as exc:
        if exc.is_stale_token:
            return False                          # ← 不抛，drain 看到 False 就保留 pending
        raise                                      # 别的错误照旧抛
```

`_drain_batch` 配套调整：

```python
ok = await self._send_reliable(msg, from_id, context, reply, "ai-reply")
if not ok:
    log_session.warning("sendmessage stale-token, msg %s requeued (relogin pending)", mid)
    self.runtime_state["pending_messages"] = list(messages[i:]) + list(messages[:i])
    self.runtime_state["pending_cursor"] = cursor or self.runtime_state.get("pending_cursor")
    self.save_state()
    return                                       # 退出本轮 drain，等下次 replay
```

> 5.2 + 5.3 一起做，可以做到「重连期间消息不丢，重连后自动 replay」。

## 6. 验证步骤

### 6.1 单元/集成

`tests/test_bot_session_contract.py` 已有 91 行骨架（`git show 78e1dfd -- tests/`），需要新增：

```python
async def test_relogin_waits_for_drain():
    """模拟：_drain_batch 持锁 5 s，request_relogin 必须在 5 s 后才清 token。"""
    session = make_test_session()
    drain_started = asyncio.Event()
    drain_release = asyncio.Event()

    async def slow_drain():
        async with session._state_lock:
            drain_started.set()
            await drain_release.wait()

    drain_task = asyncio.create_task(slow_drain())
    await drain_started.wait()

    relogin_task = asyncio.create_task(session.request_relogin("test"))
    await asyncio.sleep(0.1)
    assert session._token_ref[0] != "", "token was cleared while drain in flight"

    drain_release.set()
    await drain_task
    await relogin_task
    assert session._token_ref[0] == "", "token was not cleared after drain released"
```

### 6.2 手动复现

```bash
# 1) 起一个 ephemeral 会话，扫一次码让它进 logged_in
# 2) 微信侧连发两条消息（中间间隔 < LLM 响应时间）
# 3) 在第二条的 LLM 计算期间，浏览器点 #switch
# 4) 观察 logs/clawbot_shared.log
#    修复前：看到 -14 + login start 几乎同时
#    修复后：login start 在 _send_reliable 之后
# 5) 重连成功后 weixin_state_*.json 里 pending_messages 应为空
```

### 6.3 日志断言（5.1 落地后就能跑）

```bash
# 任意一次重连都必须能搜到 reason
grep -c "request_relogin reason=" logs/clawbot_shared.log
# 必须 >= BotManager 中实际发生过 request_relogin 的次数
```

## 7. 立即恢复：当前 `eph_656ef35f...` 会话

**当前状态**（18:50 CST）：
- QR 已耗尽（refresh_count=3 都 expired）
- `weixin_state_eph_656ef35f563969087b7c6459f48b8b68.json` 里仍有 stale 的 `pending_messages[0]`（seq=2 语音「那你具体能查阅什么资料呢」）
- 浏览器侧显示「二维码多次失效或登录失败，请稍后重试」

**用户侧步骤**（必须由用户在浏览器里完成）：

1. 在浏览器 `http://<host>:18300/clawbot/` 找到这个 `eph_656ef35f...` 标签
2. 点页面上的 **「切换用户」** 按钮（id=`#switch`）
   → 触发 `request_relogin("web switch")` → `_relogin_listener` 重新进入 QR 轮询（refresh_count 归零）
3. 用绑定的微信扫描新 QR
   → WeChat 返回**同一** `ilink_bot_id=077b77e37683@im.bot`（同账号）→ `_apply_login` 不会清 `pending_messages`
4. 重连成功后，把**这次的 msg #2 当作「已丢」** —— 重新发问即可

**服务端清理步骤**（我这边执行）：

> 时序关键：必须在 `_message_loop` 拿到新 token 后**第一次 `getupdates` 之前**完成，否则 bot 会按 startup hook 之外的逻辑（实际上不会）或者干脆没机会 replay。

```bash
# 等到 log 里看到：
#   poll status=confirmed bot_id=077b77e3
# 之后立即执行（一行原子重写）：
python - <<'PY'
import json, os
p = "/opt/weixin-ClawBot-API/weixin_state_eph_656ef35f563969087b7c6459f48b8b68.json"
s = json.load(open(p))
s["pending_messages"] = []
s["pending_cursor"] = ""
# 保留 processed_message_ids（msg #1 真实送达过；不能让它被重放）
json.dump(s, open(p + ".tmp", "w"), ensure_ascii=False, indent=2)
os.replace(p + ".tmp", p)
print("cleaned: pending_messages=[], pending_cursor=''; processed_message_ids preserved")
PY
```

如果第 4 步用户扫码时换了微信号（`ilink_bot_id` 变了），`_apply_login` 自己会清 `pending_messages`，不需要我们手动处理 —— 但用户会失去之前的对话上下文。

## 8. 相关问题 / 后续

- **5.1 是无悔的，立刻可以提交**；5.2 + 5.3 是真修复，需要过 P1 review。
- `_send_reliable` 不区分 -14 与其他 ILinkAPIError 的现状与 `send_msg_safe`（`bot.py:722-832`）不一致 —— `send_msg_safe` 对 -14 是重抛，对其他是降级 console；`_send_reliable` 是无脑抛。建议统一。
- `_handle_message` 的 `try/finally` 里 `send_typing_safe(..., 2, ...)` 在 `_send_reliable` 抛异常时仍会跑，但用的是已被清空的 token（`send_typing_safe` 看到 -14 也会再抛一次）。日志里能看到两次 -14 但**没有任何 INFO/WARN 行**说明「这是 stale token」。**`send_typing_safe` 也应该写一行 warn**。
- `request_relogin` 的 4 个调用点除了 `shared_web.py:543-549` 之外，其它 3 个 (`bot.py:1884`, `bot_session.py:551/708/736`) 也都没写 reason 日志；5.1 的修复顺手能盖到这 3 个。
- TODO P0「per-user conversation history」落地后，本 bug 的影响会**更严重**：replay 出来的「历史」会包含那条永远没送达的 AI 回复，从而污染后续上下文。所以 5.2 必须在 P0 history 之前落地。

## 9. 关联 commit / 文档

- 78e1dfd `fix(session): preserve controlled reauth` —— 上一轮修复，让 `_reconnect` 显式清空失效 token，但**没考虑 in-flight handler**
- 18f0f83 `fix(voice): separate transcript from message text` —— 加重了 `voice_item.text` 路径；该路径走的就是 `ai.chat`，**所以本 bug 第一次能被用户感知到是在语音功能上线之后**
- `docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md` —— 上一轮「连接保活」的设计文档；本 doc 是其续篇，**专门处理受控重连与消息处理的并发**

---

**结论**：本事故是受控重连路径与消息处理路径的**第一类竞态**。Phase 1（加日志）即可在下次复发时把定位时间从「排除 3 个不可能」缩短到「1 行 grep」；Phase 2（互斥）才是根治，且实现量不大（~30 行 + 一个 lock）。建议两个 commit 分开发，Phase 1 立刻合，Phase 2 等 review。
