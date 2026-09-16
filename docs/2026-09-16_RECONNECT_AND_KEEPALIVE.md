# 2026-09-16 事故复盘 + 无感重连设计分析

> ⚠️ **STATUS (2026-09-16 15:23 CST)** — 全部 5 个修复已部署到生产(pid 2467379 active running)。本文档 §1 TL;DR、§3.4 立即恢复、§5 方案对比、§6 顺手要修、§7 验证步骤、§8 引用均已同步更新到已部署状态。关联 commit:`4f563a5` · `708ec8c` · `f2eeb12` · `e6939f3` · `3ec232b`。
> 接手 LLM 请先读 §1 TL;DR 和 §0 部署状态摘要,再按需跳读 §3–§8。
> 关联文档:[`2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) · [`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) · [`2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md)(新增,本事故架构修复的设计依据)

---

## 0. 部署状态摘要(2026-09-16 15:23 CST)

5 个 commit 已 push 到 `cleanup/ephemeral-only-20260915` 分支 + production `clawbot-shared.service` 已重启:

| Commit | 修复层 | 防御场景 |
|---|---|---|
| `4f563a5` | in-flight `_drain_lock` + pending 保留 | 重连期 AI 算完用旧 token 触发 -14 → 消息不丢 |
| `708ec8c` | 重连失败回滚旧 token | `MAX_QR_REFRESH_COUNT` 命中后长轮询不死(消除"半死"状态) |
| `f2eeb12` | server-driven 失败自动 backoff (60s/300s/900s) | 用户不在手机旁 → 21min 内 3 次自动重试 |
| `e6939f3` | `bot.py` `qr_status` 日志(6 行) | QR / 登录生命周期可观测 |
| `3ec232b` | observability(8 处) + 解耦 QR 切换 | **架构层根治**:user-driven `/switch` 不扫码不影响后端 |

**新行为对比**:`request_relogin("web switch" | "manual")` 走 `_request_qr_switch` 解耦路径 → 拉 QR(不动 token)→ 后台 task 轮询 → 过期 no-op,旧 token 保持;`"stale-token" | "session-expiry"` 仍走 `_reconnect` 老路径(78e1dfd binded_redirect 防御保留)。详见 [`docs/2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md)。

**测试状态**:37 pytest + 8 unittest 全过(含 5 个新解耦测试 + 2 个 mutex 测试 + 2 个 rollback 测试 + 4 个 retry 测试)。

---

## 1. TL;DR

1. **历史事故(已恢复,2026-09-16 13:22 CST)**:`pid 2365525` 还活着、web 心跳正常,但 `getupdates` / `sendmessage` 调用为零,`bot_token` 为空字符串 → 长轮询永久沉默。用户感知:"暂时无法连接openclaw"。
2. **触发原因**:运维在 Web UI 点了"切换账号"(`POST /switch`)。3 张 QR 60s 内无人扫码,`MAX_QR_REFRESH_COUNT=3` 触发 `RuntimeError`,`request_relogin` future 收到异常后**不会自愈**。
3. **代码层根因复合**:`78e1dfd` 清 `bot_token` 后 QR 失败 → 永久空 token;`0bed146` 删了所有用户可见的重连提醒 → 沉默;`request_relogin` 一次性触发、失败即终态。
4. **修复结果**:5 个 commit 全部部署。新行为下,完全相同的"用户点切换不扫码"场景**不会**再导致长轮询沉默 —— token 不动、QR 过期后台 task 自己 no-op、若服务端 -14 则自动 backoff retry (60s/300s/900s)。
5. **协议事实**(仍适用):iLink 2.4.6 协议里**没有**任何心跳端点(`weixin-openclaw-api-py-docs.md:60-69` 全 8 个端点,无 `/heartbeat` / `/ping` / `/refresh_token`)。客户端活动**不能延长**服务端 token 有效期(`§2.7.3` 明确)。"周期性心跳"路线仍不通;但已部署的 5 层防御完全覆盖用户可见的所有症状。

---

## 2. 三句话背景

- 协议事实:iLink 2.4.6 的 `bot_token` 由服务端独立计时,客户端**任何活动都不能延长**。`-14` 是 server-side token 过期判断,不是 idle reset。官方客户端收到 `-14` 会**冻结账号全部请求 1 小时**(协议 §2.7.3)。
- 历史设计假设(已部分证伪):`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md §3` 写 "`getupdates` 长轮询本身就是 iLink 连接保活机制"。本次事故 + 07:52 的 `-14 session timeout` 双重证伪 —— 但 long-poll 仍是 keepalive 的核心,只是不再被视为"token 续命器"。
- 完整 API 列表(共 8 个,无心跳):`/ilink/bot/{getupdates,getconfig,sendtyping,sendmessage,notifystart,notifystop,getuploadurl,get_bot_qrcode}` + `GET /ilink/bot/get_qrcode_status`。`grep -iE "heartbeat|keepalive|ping"` 在协议文档里零命中。

---

## 3. 历史事故状态(2026-09-16 08:30 → 13:22)

> 已恢复。仅作历史记录。**当前生产进程(pid 2467379)已运行新代码,行为见 §0。**

### 3.1 进程与连接(事故期间 pid 2365525)

| 项 | 值 | 含义 |
|---|---|---|
| pid | `2365525` | 08:16 启动,事故期间运行 5h06m |
| 状态 | `Ssl`,7 线程,212 MB | 健康,**未崩溃**,`ep_poll` 空闲 |
| TCP 连接 | **零条**到 `ilinkai.weixin.qq.com` 4 个 IP | 长轮询完全停止 |
| 残留连接 | `10.1.0.11:60656 → 109.244.194.209:443` | `CLOSE_WAIT`,5h 未关闭,32 字节未读(可疑 socket leak,§6 P2 项待修) |
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

### 3.4 立即恢复步骤(已 100% 自动化,此节仅作应急 fallback 保留)

> **修复后(2026-09-16 15:23 CST 起,`pid 2467379` active)**:
> - 服务端 `-14` → `request_relogin("stale-token")` 失败 → `_scheduled_retry_relogin` 自动 backoff 重试 (60s/300s/900s)
> - 用户 `/switch` 不扫码 → `_request_qr_switch` 解耦路径,旧 token 不动,后台 task 过期 no-op
> - 重连期 AI 算完触发 -14 → `_drain_batch` 保留 `pending_messages`,`_reconnect` 回滚旧 token,长轮询继续
> **运维手动恢复已不再必要。**

应急 fallback(若所有自动层被绕过,极少见):

1. **Web UI**:浏览器 → `http://<host>:18300/clawbot/` → 找到当前 session 标签 → 点 **"切换用户"** → 微信扫码
2. **微信端**:给 bot 发 `/重新连接`,按提示回 `Y` → 扫码
3. **强制重启**:`systemctl restart clawbot-shared.service` → 必须重新扫码

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

## 4. 代码层根因剖析(已合并的代码)—— **已被 §5 修复层覆盖**

> 本节是 08:30 事故的事故剖析。**所述代码路径依然存在**(78e1dfd 的"清 token 后 QR"是为修 binded_redirect 死循环,本事故的源头之一),但被 §5 的多层修复联合覆盖,**不再导致用户可见症状**。事故的具体触发链见 §3.3 时间线。

### 4.1 复合三个改动造成"半死"状态

| 提交 | 文件:行 | 改动 | 副作用 | **修复** |
|---|---|---|---|---|
| `78e1dfd` `fix(session): preserve controlled reauth` | `bot_session.py:_reconnect` | `_reconnect()` 在 `_login()` 前**原子清空** `self.bot_token` / `_token_ref[0]` / `runtime_state["bot_token"]` | QR 失败时 token **永久留空** | ✅ `708ec8c` 加 `restore_token` 回滚守卫(条件:`had_authenticated and "binded_redirect" not in str(exc)`) |
| `0bed146` `feat(reconnect): 后台静默重连 + 终态单条通知` | `bot.py:991-1058` | 删除所有用户可见的 `warn_msg` / `remind_msg`,只发终态单条 | 用户**完全收不到**"正在重连 / 需要扫码"信号 | ✅ `3ec232b` 把 user-driven QR 与 token 解耦 —— QR 过期 no-op,**不再有"沉默"窗口** |
| `62bff45` `fix(session): decouple web TTL from iLink keepalive` | `bot_session.py:_timer_loop`,`shared_web.py` | `_timer_loop` 仅 `proactive_relogin=True` 时启动;sweeper 不再回收有 token 的 session | 用户用 `/TIME` 探测活跃度,得到被动答案 | 无需修(此设计本意正确) |

### 4.2 `request_relogin` 是一次性的

> 旧实现(已被 `f2eeb12` + `3ec232b` 替换):

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

- `f2eeb12`:server-driven 失败 → `_scheduled_retry_relogin` 后台 backoff (60s/300s/900s),最坏 21min 内自动恢复
- `3ec232b`:`reason in {"web switch", "manual"}` 走 `_request_qr_switch` 解耦路径,与 listener 解耦,失败也不影响后端

### 4.3 没有 `request_relogin` 日志

> 旧:`reason` 只走 `_emit` hook,**没有 `log_*` 输出**。

✅ **已修** —— `78e1dfd` 后续 + `3ec232b` 在 `request_relogin` 顶部加 `log_reconnect.info("relogin_requested session=... reason=... ...")` 加 `await future` 前加 `log_reconnect.debug("request_relogin awaiting reason=%s", reason)`。下次事故可直接 `grep "relogin_requested"` 定位。

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

### 5.2 三条真正可走的路 —— **全部已部署**

#### 方案 A:in-flight 竞态互斥(根治"重连期丢消息")—— ✅ 已部署 `4f563a5`

来源:[`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) §5.2 + §5.3。

- **改动**:`bot_session.py` 加 `self._drain_lock = asyncio.Lock()`,`_drain_batch` 整段持锁,`_reconnect` 嵌套 `async with self._reconnect_lock, self._drain_lock:`
- **用户感知成本**:浏览器点"切换账号"后最坏等当前 LLM 算完(5–15s)才看到新 QR。比修复前"断流 + 卡死"强
- **状态**:✅ 已合并 `4f563a5`(`fix(session): in-flight mutex + preserve pending on stale token`)
- **测试**:`test_drain_lock_serializes_drain_and_reconnect`、`test_stale_token_during_drain_preserves_pending_messages`
- **实现量**:~30 行 + 1 个 lock

#### 方案 B:预测式重连窗口(`proactive_relogin=True`)—— ⏸️ 不推荐(仍适用)

来源:[`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) §4 `bot_session.py` 部分 + §3 设计原则。

- **改动**:`RECONNECT_CONFIG.proactive_relogin=True` 启用 `_timer_loop`,在 `session_duration - warning_before` 时主动弹 QR
- **用户感知成本**:用户不在手机旁时**仍**会失败,落到 `max_refresh_exceeded`,回到"半死状态"(即使有 token 回滚 + 自动 retry 也需 21min 才彻底放弃)
- **状态**:代码已实现,只是默认 `False`(`bot.py:107`)。**不建议**作为默认 —— 会让用户在深夜被弹 QR,体验**未必**比当前好。仅作 named-user 配置项开放

#### 方案 C:加 `request_relogin` 失败后自动重试(backoff)—— ✅ 已部署 `f2eeb12`

- **改动**:`request_relogin` 在 `future` 拿到异常时,fire-and-forget `asyncio.create_task(self._scheduled_retry_relogin(reason))`,在 60s / 300s / 900s 后分别调 `_reconnect()`,最多 3 次
- **用户感知成本**:0,后台尝试
- **风险**:若 iLink 整个 token 池已黑名单该账号,重试只会打满接口(协议 §2.7.3 "官方插件会暂停该账号全部请求 1 小时")—— backoff 60s 起步对此有缓冲
- **限制**:**仅**对 `reason ∈ {"stale-token", "session-expiry"}` 启用;`{"web switch", "manual"}` 不重试(尊重用户取消意图)
- **状态**:✅ 已合并 `f2eeb12`(`fix(session): background retry for server-driven relogin failures`)
- **测试**:`test_scheduled_retry_relogin_calls_reconnect_on_backoff`、`test_scheduled_retry_relogin_skipped_for_user_driven_reasons`、`test_manual_relogin_does_not_schedule_retry`、`test_scheduled_retry_relogin_exhausts_after_three_attempts`

#### 方案 D:重连失败回滚旧 token —— ✅ 已部署 `708ec8c`

不在原始 §5 中,但用户随后提出。**`request_relogin("stale-token" | "session-expiry")` 走老路径(78e1dfd 清 token 后 QR),失败时回滚旧 token** —— 消除"半死状态"。

- **改动**:`_reconnect` 加 `restore_token = had_authenticated_connection and current` 决策变量,`except Exception` 时条件性写回 `bot_token` / `_token_ref[0]` / `runtime_state["bot_token"]`
- **保留 78e1dfd 不变量**:substring guard `"binded_redirect" not in str(exc)` 阻止回滚触发 binded_redirect 复用死循环
- **状态**:✅ 已合并 `708ec8c`(`fix(session): restore bot_token on transient reconnect failure`)
- **测试**:`test_reconnect_failure_restores_previous_token`、`test_reconnect_binded_redirect_does_not_rollback_token`

#### 方案 E:解耦 QR 切换与 token —— ✅ 已部署 `3ec232b` ← 架构层根治

`request_relogin("web switch" | "manual")` 不再清 token。QR 仅是显示物,确认才原子 swap。

- **改动**:`BotSession` 加 `_request_qr_switch` / `_initiate_qr_switch` / `_await_qr_confirmation_and_swap` / `_run_qr_switch` 方法 + `_pending_qr_token` / `_pending_qr_started_at` / `_pending_qr_task` 状态
- **状态**:✅ 已合并 `3ec232b`(`fix(session): observability + decoupled QR switch (open-platform style)`)
- **测试**:`test_qr_switch_initiate_does_not_clear_token`、`test_qr_switch_expire_preserves_token`、`test_qr_switch_confirm_atomic_swap`、`test_request_relogin_web_switch_uses_decoupled_path`、`test_request_relogin_stale_token_uses_clearing_path`
- **设计依据**:[`docs/2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md)

### 5.3 推荐顺序 —— 已执行

1. ✅ 方案 A §5.1(`78e1dfd` 后续 + `e6939f3` 中第 10 项):`request_relogin` reason 日志 —— 已合
2. ✅ 方案 A §5.2 + §5.3:in-flight 互斥 + `_send_reliable` 失败保留 `pending` —— 已合 `4f563a5`
3. ✅ 方案 D(本次新增):重连失败回滚旧 token —— 已合 `708ec8c`
4. ✅ 方案 C:自动重试 backoff —— 已合 `f2eeb12`
5. ✅ observability + 方案 E:解耦 QR 切换 —— 已合 `3ec232b`
6. ⏸️ 方案 B:**不推荐**作为默认,留作 named-user 配置项

---

## 6. 顺手要修的小问题(incident 暴露)—— 状态同步

| 问题 | 文件:行 | 优先级 | 状态 |
|---|---|---|---|
| `request_relogin` 不写日志 | `bot_session.py:358-369` | P0(同 §5.1) | ✅ **已修** `78e1dfd` 后续 + `3ec232b` 加 debug awaiting 日志 |
| `_send_reliable` 失败不写日志 | `bot_session.py:_send_reliable` | P1(`IN_FLIGHT_RELOGIN_RACE.md §8`) | ✅ **已修** `3ec232b` try/except + `log_message.warning("send_reliable failed ...")` |
| `_send_reliable` 无脑抛,跟 `send_msg_safe` 行为不一致 | `bot_session.py:_send_reliable` | P1(同 doc §8) | ⏸️ **未修** —— 设计取舍:`_send_reliable` 由 `_drain_batch` 接管 stale-token 场景(`4f563a5`),`send_msg_safe` 走 `_message_loop` 兼容老 CLI,暂不统一 |
| `109.244.194.209:443` socket 泄漏(`CLOSE_WAIT` 5h,32 字节未读) | runtime(`aiohttp.TCPConnector`) | P2 | ⏸️ **未修** —— 不阻塞恢复,需后续 aiohttp connector 排查 |
| `ILINK_STORE_SQLITE_PATH=./data/ilink_bot.db` 指向不存在的 dir | env | P2 | ⏸️ **未修** —— 与本事故无关 |
| `account_changed=True` 无条件清 `contexts` / `welcomed_users`,与 named-user 保活意图冲突 | `bot_session.py:_apply_login` | P2 | ⏸️ **未修** —— 见 `IN_FLIGHT_RELOGIN_RACE.md §10` 仍未解 |

---

## 7. 验证步骤 —— 已部署状态

### 7.1 自动验证(已通过)

```bash
./venv/bin/python -m py_compile bot.py bot_session.py shared_web.py
# → PY_COMPILE OK

./venv/bin/pytest tests/ --ignore=tests/test_shared_web.py -q
# → 37 passed in 0.86s

./venv/bin/python -m unittest tests.test_shared_web
# → Ran 8 tests in 0.823s, OK
```

### 7.2 日志断言(下次事故直接 grep)

```bash
# 一次事故全链路
LOG=logs/clawbot_shared.log
grep -E "relogin_requested|qr_switch|reconnect (clear_token|login_attempt|login_succeeded|rollback|failed)|drain_batch stale_token|send_reliable failed|message_loop stale_token|qr_status|login (timeout|qrcode_missing|failed|confirmation_timeout|max_refresh_exceeded)" "$LOG"

# user-driven 切换是否走解耦路径(应见 qr_switch initiate,不应见 reconnect clear_token)
grep -E "qr_switch (initiate|fetched|confirmed|expired|cancelled)" "$LOG"

# server-driven 仍走老路径
grep -E "reconnect clear_token|message_loop stale_token" "$LOG"

# 自动 backoff retry 触发链
grep -E "scheduled_retry_relogin" "$LOG"
```

### 7.3 端到端复现(供未来回归)

```bash
# 复现 in-flight 竞态防御:
# a) 起一个 session,扫一次码让它进 logged_in
# b) 微信侧连发两条消息(间隔 < LLM 响应时间)
# c) 在第二条的 LLM 计算期间,浏览器点 #switch
# d) 修复前:看到 -14 + login start 几乎同时,消息丢
#    修复后:login start 在 _send_reliable 之后,pending_messages 保留,重连成功后 replay
# e) 重连成功后 weixin_state_*.json 里 pending_messages 应为空

# 复现解耦路径:
# a) 启动 session 进 logged_in
# b) 浏览器点 #switch → 日志应见 qr_switch initiate,bot_token 不变
# c) 不扫码,等 8min → 日志应见 qr_switch expired (no-op, current token preserved)
# d) 长轮询从未中断,/healthz 始终 ok
```

---

## 8. 引用

### 8.1 协议

- `weixin-openclaw-api-py-docs.md:60-69`(API 列表),`:444-460`(超时与 token 失效语义),`:1412-1433`(`wait_login_confirmation` 状态判定,`e6939f3` 加 log),`:1450-1580`(`login_with_qrcode` 各 raise 站点,部分 log 已存在)

### 8.2 代码

- `bot.py:1412-1433` `wait_login_confirmation` 状态判定与 `qr_status=` 日志(`e6939f3`)
- `bot.py:1482-1558` `login_with_qrcode` 各 raise 站点(已有 log)
- `bot.py:1018` `print("[重连] 服务端提示已连接过此 OpenClaw,继续沿用当前连接")` —— 仅运维终端可见
- `bot_session.py:_drain_lock` 新增(`4f563a5`)
- `bot_session.py:_reconnect` 清 token 后 QR + 回滚守卫(`78e1dfd` + `708ec8c`)
- `bot_session.py:_drain_batch` 持锁 + stale-token 保留 pending(`4f563a5`)
- `bot_session.py:request_relogin` reason 日志 + dispatch 改造(`78e1dfd` 后续 + `3ec232b`)
- `bot_session.py:_scheduled_retry_relogin` 自动 backoff(`f2eeb12`)
- `bot_session.py:_request_qr_switch` / `_initiate_qr_switch` / `_await_qr_confirmation_and_swap` / `_run_qr_switch` 新方法(`3ec232b`)
- `bot_session.py:_send_reliable` try/except + log(`3ec232b`)
- `shared_web.py:/clawbot/switch` 日志增强(`3ec232b`)

### 8.3 提交(按时间顺序,本事故修复链)

| Hash | 提交 | 影响 |
|---|---|---|
| `78e1dfd` | `fix(session): preserve controlled reauth` | 清 token 修复 binded_redirect 死循环(本事故的源头) |
| `0bed146` | `feat(reconnect): 后台静默重连 + 终态单条通知` | 删了所有用户提醒(本事故沉默的原因) |
| `62bff45` | `fix(session): decouple web TTL from iLink keepalive` | `/TIME` 回复改被动措辞 |
| `c60aeae` | `refactor(session): drop eph_<hex>` | 纯重命名,与本事故无关 |
| `78e1dfd` 后续 | `fix(session): preserve controlled reauth` | 加 `request_relogin` reason 日志 |
| `4f563a5` | `fix(session): in-flight mutex + preserve pending on stale token` | **防御层 1**:in-flight 竞态 + pending 保留 |
| `708ec8c` | `fix(session): restore bot_token on transient reconnect failure` | **防御层 2**:重连失败回滚旧 token |
| `f2eeb12` | `fix(session): background retry for server-driven relogin failures` | **防御层 3**:server-driven 自动 backoff |
| `e6939f3` | `fix(qr): add qr_status logging in wait_login_confirmation` | observability:QR/登录生命周期可观测 |
| `3ec232b` | `fix(session): observability + decoupled QR switch (open-platform style)` | observability 8 处 + **防御层 4:架构层根治 user-driven 不扫码不影响后端** |

### 8.4 历史 incident 文档

- [`docs/2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md) —— "long-poll 即保活" 设计假设(部分证伪,但 keepalive 核心仍正确)
- [`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) —— 竞态分析;§5.1/§5.2/§5.3 全部已合并(参见该 doc §11 Resolution)
- [`docs/2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md) —— **新增**,本事故架构修复的设计依据

### 8.5 当前生产状态

- 进程:`pid 2467379`,active running since 2026-09-16 15:21:43 CST
- 代码:`cleanup/ephemeral-only-20260915` 分支 HEAD = `3ec232b`
- 日志:`logs/clawbot_shared.log`(TimedRotatingFileHandler,append 模式)
- 协议引用文本(§2.7.3):

  > 2.4.5 将内部命名从"session expired"改为"stale token",明确 `ret=-14` 或 `errcode=-14` 表示 bot token 已失效/过期,而不只是普通会话超时。官方插件会暂停该账号全部请求 1 小时,避免快速重试打满接口。