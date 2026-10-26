# 2026-10-25 事故:「切换账号」把刚登录好的会话打死

> 关联文档: [`2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md) · [`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) · [`2026-09-16_RECONNECT_AND_KEEPALIVE.md`](2026-09-16_RECONNECT_AND_KEEPALIVE.md)

---

## 1. 症状

用户在网页上**刚点「开始」扫码登录**,**2 秒后又点了「切换账号」**。切换的二维码扫码成功、账号也拿下了 —— 但几分钟后会话还是死了,页面回到「无法连接」。

日志里最刺眼的一行:

```
ERROR [clawbot.qr] login aborted refresh_count=3 reason=max_refresh_exceeded
INFO  [clawbot.reconnect] session.stop ... had_token=True ... caller=start-<session>
```

`had_token=True` —— **会话是在已经持有 token 的情况下被 stop 掉的**。这正是"明明登录成功了却还是连不上"的原因。

## 2. 时间线(`logs/clawbot_shared.log.2026-10-25`)

| 时间 | 事件 | 日志 |
|---|---|---|
| 18:41:57 | `POST /start` → 新建会话,**初始二维码登录开始** | `login start refresh_count=1/3 deadline_in_s=480` |
| 18:41:58 | 初始二维码取到(seq=1) | `qr cycle refresh=1 qr_len=32` |
| 18:41:59 | **`POST /switch`** → 解耦切换 QR 启动(**距上一步仅 2 秒**) | `relogin_requested reason=web switch` / `qr_switch initiate` |
| 18:41:59 | 切换二维码取到(seq=2) | `qr_switch fetched qr_len=32` |
| 18:42:49 | 用户扫的是切换码 → **凭据已原子 swap 成功** | `qr_switch confirmed and applied bot_id=10119704` |
| 18:45:59 | **初始二维码自己又过期了**(它一直在并行轮询) | `poll status=expired` → `qr expired, regenerating (#2)` |
| 18:48:00 | 初始二维码第 3 次过期 → 放弃 | `login aborted refresh_count=3 reason=max_refresh_exceeded` |
| 18:48:00 | `BotSession.start()` 抛出 → 会话被 stop | `session.stop ... had_token=True caller=start-...` |
| 18:48:01 | iLink 侧收到 notifystop | `notify_lifecycle ok endpoint=ilink/bot/msg/notifystop` |

**18:42:49 已经拿到的 token,在 18:48:01 被 notifystop 亲手掐掉。**

## 3. 根因

三条独立的缺陷叠在一起:

### 3.1 主因:初始登录和切换登录**并行**跑,谁先死谁说了算

`BotSession.start()` 的初始扫码流程(`_login()` → `login_with_qrcode`)是一个**最长 480 秒**的长任务;`/switch` 走的是完全独立的 `_request_qr_switch` 路径。两者互不知情:

- 切换路径成功后 → `_apply_login` 原子 swap,**token 已经就位**
- 初始路径**毫不知情**,继续轮询它自己那个没人扫的二维码 → 3 次过期 → `RuntimeError`
- `RuntimeError` 从 `start()` 抛出来 → `BotManager` 回收会话 → `session.stop()` → **连同刚拿到的 token 一起扔掉**

### 3.2 放大器:`_initial_cancel` 只对**服务端驱动**的重登录生效

`_initial_cancel` 这个 Event 原本只在 `request_relogin()` 的**服务端驱动**分支里被 `set()`(`bot_session.py:472`)。用户驱动的 `"web switch"` / `"manual"` 走 `_request_qr_switch`,**从不碰它** —— 所以初始扫码流程没有任何办法被提前叫停。

### 3.3 掩盖:`_pending_qr_task` 一结束就置空,晚到的调用方看不到结果

`_run_qr_switch` 在 `finally` 里把 `self._pending_qr_task = None`。`start()` 这类"晚到"的调用方醒来时,如果 task 已经跑完,就**无从判断切换到底成功了没有** —— 只能靠一个新字段把结果留下来。

## 4. 修复

### 4.1 切换一开始,立刻取消初始登录(`bot_session.py:_request_qr_switch`)

```python
if self._initializing:
    self._initial_cancel.set()
```

放在**创建 switch task 之后**,这样 `start()` 醒来时一定能通过 `_pending_qr_task` 找到这次切换。

### 4.2 `start()` 醒来后,等切换的结果而不是自己判死(`bot_session.py:start`)

原来的逻辑只有服务端驱动的一条路(`pending is not None`)。现在补上解耦路径:

```python
if pending is not None:
    result = await asyncio.shield(pending)
    already_applied = True
else:
    result = await self._await_qr_switch_result()
    if result.get("bot_token"):
        already_applied = True          # 切换已应用凭据,直接接管
    else:
        self._initial_cancel.clear()
        result = await self._login()     # 切换二维码没人扫 → 回到初始扫码
        already_applied = False
```

### 4.3 切换结果要留痕(`bot_session.py:_pending_qr_result`)

`_pending_qr_task` 结束后会被置空,所以新增 `_pending_qr_result` 记住最后一次切换的结果(成功 = 含 `bot_token`;失败 = `{}`)。`_await_qr_switch_result()` 先看 task,再看这个字段。

### 4.4 附带修复:`ImaConfig.configured()` 缺失(`ima.py`)

`configured()` 原本只挂在 `ImaClient` 上,但有两处调用方拿到的是 **`ImaConfig` 实例**:

- `shared_web.py:444` —— 在 `try/except Exception` 里 → `AttributeError` 被吞掉 → **整个网页绑定路径静默降级成「IMA 未配置」**
- `bot.py:2095` —— `/BINDKB` 命令 → 抛 `AttributeError` 而不是那句友好的「❌ IMA 未配置」

修法:把 `configured()` 下沉到 `ImaConfig`,`ImaClient.configured()` 改为转发。

## 5. 新增不变量(有测试守住)

| 不变量 | 测试 |
|---|---|
| 初始扫码期间按「切换账号」**不得**杀死会话 | `test_switch_during_initial_login_keeps_applied_token` |
| 初始扫码**只轮询一次**就被切换取消(不能烧完 3 个 QR) | 同上(`calls.count("poll-init") == 1`) |
| 切换二维码没人扫 → **回落**到初始扫码,而不是留下一个没有凭据的死会话 | `test_switch_during_initial_login_falls_back_when_qr_not_scanned` |
| `ImaConfig.configured()` 与 `ImaClient.configured()` 语义一致 | `ima.py` 内联 |

## 6. 验证

```bash
./venv/bin/pytest tests/ --ignore=tests/test_shared_web.py -q   # 39 passed
./venv/bin/python -m unittest tests.test_shared_web               # 8 tests OK
```

两个新测试在**修复前会失败**(pre-fix: `TimeoutError` / `RuntimeError`),修复后通过 —— 见 §5 表格。

## 7. 下次事故直接 grep

```bash
# 切换是否确认 + 是否应用(应该看到 qr_switch confirmed and applied)
grep -E "qr_switch (initiate|fetched|confirmed|expired)" logs/clawbot_shared.log

# 初始扫码是否被切换取消(修复后应该出现 "login cancelled by external event")
grep -E "login cancelled by external event" logs/clawbot_shared.log

# 会话是不是"带着 token 被 stop 掉"(出现了就是本 bug 的残留)
grep -E "session.stop .*had_token=True" logs/clawbot_shared.log

# 兜底:initial 扫码烧完 3 个 QR(修复后不应再出现)
grep -E "max_refresh_exceeded" logs/clawbot_shared.log
```

## 8. 与既有设计的关系

- [`2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md) §2 的「QR 是显示物」原则**没有被破坏**:切换二维码依然是显示物,没扫就过期、没扫就不动 token。
- 这次补的是**另一个维度**:解耦路径和初始登录路径**之间的互斥**。之前只考虑了「切换 vs 服务端驱动重登录」,没考虑「切换 vs 初始登录」。
- [`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) §10 提到的「QR 循环切断数据面上下文」——现在初始扫码循环**也**不会切断已经建立的数据面了。
- `2026-10-26` 的 `47fd45a`(重启不掉线)与本次修复互补:前者保证**进程重启**后能恢复会话,本次保证**会话内部**的登录流程切换不会自杀。
