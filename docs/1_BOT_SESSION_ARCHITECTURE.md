# 1 号文档 · 多租户架构：从闭包到 BotSession

> **本文件是 `docs/0_DESIGN_INTENT.md` 的实施蓝图**。
> 读完 0 号文档（"应该多用户共享进程"）再读本文（"怎么实现多用户共享进程"）。
> 核心主张：**不要在 `bot.py` 内部把闭包改成 dict**——那是把单租户代码复杂化。应该把每个微信用户**提升为一等公民对象（`BotSession`）**，让 `bot.py` 退化为 session manager。

---

## 为什么"闭包 → dict"是错路

当前 `bot.py` 的状态在 `main()` 闭包里：

```python
async def main():
    bot_token_ref = [saved_token]                    # 单值
    bot_base_url_ref = [...]                         # 单值
    last_contact = {...}                             # 单值
    typing_ticket_cache = {}                         # 单值
    welcomed_users = set(...)                        # 单值
    qr_state = QrFlowState()                         # 单值
    relogin_event = asyncio.Event()                  # 单值
    reconnect_in_progress = [False]                  # 单值
    login_time_ref = [time.time()]                   # 单值
    manual_reconnect_pending = {}                    # 单值
    _relogin_lock = asyncio.Lock()                   # 单值
    _pending_relogin = [None]                        # 单值

    async def message_loop(): ...                    # 闭包，吃上面 13 个变量
    async def reconnect_timer_task(): ...            # 闭包，吃上面 13 个变量
    async def relogin_listener(): ...                # 闭包，吃上面 13 个变量
    async def request_relogin(): ...                 # 闭包
    async def handle_message(): ...                  # 闭包
    async def apply_new_login(): ...                 # 闭包
    # ... 一个用户 = 7 个任务 + 13 个共享变量
```

**问题清单**（扩到 N 用户时全炸）：

| 闭包变量 | 多租户改造做法 | 风险 |
|---|---|---|
| `bot_token_ref[0]` | 改成 `tokens: dict[user_id, str]` | 漏改一处 = token 串号 |
| `last_contact` | 改成 `last_contacts: dict[user_id, dict]` | 漏改一处 = 给 A 发 B 的消息 |
| `welcomed_users` | 改成 `welcomed: dict[user_id, set]` | 漏改一处 = 重复发送欢迎语 |
| `qr_state` | 改成 `qr_states: dict[user_id, QrFlowState]` | 漏改一处 = A 的 QR 显示在 B 的页面 |
| `relogin_event` | 改成 `relogin_events: dict[user_id, Event]` | 漏改一处 = A 的 /relink 触发 B 的重连 |
| `reconnect_in_progress` | 改成 `reconnect_in_progress: dict[user_id, bool]` | 漏改一处 = B 在重连时 A 误判也跳过 |
| `web_on_qrcode` | 改成 `web_on_qrcodes: dict[user_id, callable]` | 漏改一处 = A 的 QR 写到 B 的 state |
| 7 个 `async def` | 各自改成 `for user_id, sess in sessions.items(): ...` | 闭包 → 函数化 = 重写一半 |

**本质问题**：所有变量、所有协程都是"一个用户"语义写出来的。**N 租户不是改几个变量，是改整套心智模型**。

---

## 正确架构：`BotSession` + `BotManager`

### 核心抽象

```python
class BotSession:
    """一个 BotSession = 一个微信个人号 = 一份完整状态 + 一组生命周期任务。

    不再吃外部闭包；所有 per-user 状态都是 self.xxx。
    """

    def __init__(self, user_id: str, session: aiohttp.ClientSession,
                 config: dict, *, on_event: Optional[Callable] = None):
        self.user_id = user_id
        self.session = session  # 共享的 aiohttp.ClientSession（HTTP 连接池复用）
        self.config = config

        # ---- 协议层状态 ----
        self.bot_token: str = ""
        self.baseurl: str = BASE_URL
        self.ilink_bot_id: str = ""
        self.ilink_user_id: str = ""

        # ---- 业务层状态 ----
        self.contexts: dict[str, str] = {}  # from_id -> context_token
        self.last_contact: dict = {"from_id": None, "context_token": None}
        self.welcomed_users: set[str] = set()
        self.manual_reconnect_pending: dict[str, str] = {}

        # ---- 重连/调度状态 ----
        self.login_time: float = time.time()
        self.relogin_in_progress: bool = False
        self.relogin_event: asyncio.Event = asyncio.Event()
        self.relogin_lock: asyncio.Lock = asyncio.Lock()
        self.pending_relogin: Optional[asyncio.Future] = None

        # ---- Web 登录状态 ----
        self.qr_state: QrFlowState = QrFlowState()
        self.web_on_qrcode: Optional[Callable] = make_web_on_qrcode(
            self.qr_state, session
        )

        # ---- 持久化 ----
        self.runtime_state: dict = load_or_init_state(user_id)
        self._tasks: dict[str, asyncio.Task] = {}
        self._on_event = on_event  # 给 Manager 用的回调（可选）

    # ---- 持久化 ----
    def save_state(self): ...
    def load_state(self): ...

    # ---- 登录流程（从 bot.py 搬过来，self 取代闭包）----
    async def initial_login(self) -> bool: ...
    async def login_with_qr(self, local_token_list=None,
                            existing_state=None,
                            cancel_event=None) -> dict: ...
    async def wait_login_confirmation(self, ...) -> dict: ...
    async def do_reconnect(self, ...) -> Optional[dict]: ...
    async def request_relogin(self, reason: str) -> dict: ...
    async def relogin_listener(self) -> None: ...   # 跑在独立 task 里

    # ---- 消息循环 ----
    async def message_loop(self) -> None: ...        # 跑在独立 task 里
    async def reconnect_timer_task(self) -> None: ...# 跑在独立 task 里
    async def handle_message(self, msg: dict) -> None: ...

    # ---- 生命周期 ----
    async def start(self) -> None:
        """拉起：initial_login + 3 个常驻 task。"""
        ok = await self.initial_login()
        if not ok:
            return
        self._tasks["message"] = asyncio.create_task(
            self.message_loop(), name=f"msg-{self.user_id}"
        )
        self._tasks["timer"] = asyncio.create_task(
            self.reconnect_timer_task(), name=f"timer-{self.user_id}"
        )
        # relogin_listener 必须在 initial_login 之前就有
        self._tasks["relogin"] = asyncio.create_task(
            self.relogin_listener(), name=f"relogin-{self.user_id}"
        )

    async def stop(self) -> None:
        """优雅停机：cancel 所有 task + notifystop + save state。"""
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self.bot_token:
            await notify_lifecycle(self.session, "ilink/bot/msg/notifystop",
                                   self.bot_token, self.baseurl)
        self.save_state()

    # ---- 单租户 → 多租户的"隔离契约" ----
    # 类内没有共享 dict[user_id, ...]；所有 self.xxx 都是 scalar / per-user dict。
    # 两个 session 实例之间不共享任何可变状态（除了 self.session 那个 aiohttp session）。
```

### Manager

```python
class BotManager:
    """所有 BotSession 的容器。负责新增 / 查找 / 停止 / 持久化索引。"""

    def __init__(self, aiohttp_session: aiohttp.ClientSession):
        self.sessions: dict[str, BotSession] = {}
        self.http = aiohttp_session  # 所有 session 共享
        self._lock = asyncio.Lock()

    async def get_or_create(self, user_id: str, config: dict) -> BotSession:
        async with self._lock:
            if user_id in self.sessions:
                return self.sessions[user_id]
            sess = BotSession(user_id, self.http, config)
            self.sessions[user_id] = sess
            await sess.start()
            return sess

    async def stop(self, user_id: str) -> None:
        async with self._lock:
            sess = self.sessions.pop(user_id, None)
        if sess:
            await sess.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            all_sess = list(self.sessions.values())
            self.sessions.clear()
        await asyncio.gather(
            *(s.stop() for s in all_sess), return_exceptions=True
        )

    async def touch(self, user_id: str) -> None:
        """portal 反代命中时刷新 last_used_at（给 ephemeral TTL 用）。"""
        sess = self.sessions.get(user_id)
        if sess:
            sess.last_used_at = time.time()
```

### main() 缩成 30 行

```python
async def main():
    config = load_config()
    async with aiohttp.ClientSession() as http:
        manager = BotManager(http)

        # 启动 web server（HTTP API + 反代到 BotManager）
        app = build_web_app(manager)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", config["web_port"])
        await site.start()

        # 启动时把命名用户都拉起来（OAuth 绑定的也要拉）
        for user_id in load_named_users():
            await manager.get_or_create(user_id, config)

        # 跑直到被取消
        try:
            await asyncio.Event().wait()  # 永久 sleep，等 cancel
        except asyncio.CancelledError:
            pass
        finally:
            await manager.stop_all()
            await runner.cleanup()
```

**对比**：
- 现在 main() 约 250 行（`bot.py:1572-1800`），全是闭包 + 状态初始化
- 目标 main() 约 30 行，纯调度

---

## 哪些代码**原样不动**

| 层 | 现状 | 多租户后 |
|---|---|---|
| iLink 协议（`api_get` / `api_post` / `ensure_business_success`） | 同步函数 + 异步封装 | **不动**。本来就是 stateless，只是参数里多带个 `bot_token` / `baseurl` |
| `login_with_qrcode` / `wait_login_confirmation` / `fetch_login_qrcode` / `poll_login_status` | 同步 → 异步函数 | **不动函数体**，从 module-level 移到 `BotSession` 方法（self 取代闭包） |
| AI 层（`dusapi.py` / `deepseek.py` / `ima.py` / `_AIWithIma`） | stateless 类 | **不动**。本来就每次调用 new instance |
| `qr_web.py` | 单一 `QrFlowState` | `BotSession.qr_state` 替它；web app 接收 `user_id` 路由到对应 session |
| `qr_portal.py` | 反代到 user 端口 | **大量简化**：反代改成进程内调用 `manager.get_or_create(user_id)` |

---

## 改造分期

### Phase 1：把闭包拆成类（不引入 Manager）

**目标**：`bot.py --user alice` 行为完全不变，但内部状态从闭包迁到 `BotSession`。

1. 新建 `bot_session.py`，定义 `BotSession` 类，把 13 个闭包变量 + 7 个 `async def` 全部迁入（机械搬迁，self 取代所有闭包引用）。
2. `bot.py` 的 `main()` 简化为：构造 `BotSession` → `await session.start()` → `await asyncio.Event().wait()`。
3. `qr_web.py` 接收 `qr_state` 注入（已经是这样了，但要从单例改成 per-session 实例）。
4. 跑通：alice 的 bot 用新代码启动，扫码登录、收发消息、24h 重连全过。

**风险**：搬迁漏一个变量就是 bug。**缓解**：diff 应该几乎全是"加 self."，没有逻辑改动；上 dev 用户灰度 24h。

### Phase 2：引入 BotManager + 单进程多用户

**目标**：同一个 `bot.py` 进程跑 N 个 `BotSession`。

1. 新建 `bot_manager.py`，定义 `BotManager` 类。
2. `main()` 改成 `BotManager` + 启动命名用户。
3. `web_app.py`（新建）把 web 路由改成 `user_id → manager.get_or_create()`。
4. `qr_portal.py` 反代逻辑改成进程内调用（保留 HTTP 反代作为兜底，给跨进程/跨机器场景用）。
5. 跑通：起 `bot.py` 不带 `--user`，从 web 端创建 alice + bob + ephemeral 三个 session，全部能收发消息。

**风险**：iLink 服务端对多 token 并发有未文档化的限流（之前一个进程一个 token 时未触发）。**缓解**：先 5 个并发试一晚上；日志监控 `iLink getupdates ret` 看是否被限。

### Phase 3：ephemeral GC 改造

**目标**：`bot_launcher.reap_ephemeral` 改为 GC `BotManager.sessions` 里的 `eph_*`。

1. `bot_launcher.py` 简化为"配置 + 索引"层（端口分配逻辑消失——端口现在由 `BotSession` 内部 bind）。
2. `utils/bot_launcher.reap_ephemeral(manager, ttl)` 改成 `manager.sessions` 内的清理。
3. 删 `_start_systemd` / `_start_subprocess`——`BotManager` 自己 start session。

**风险**：低。Phase 2 已经把所有状态机搬进 `BotSession`，GC 只是遍历 + `session.stop()`。

### Phase 4（可选）：systemd 单元清理

`/etc/clawbot/eph_*.env` 全部消失，named user 仍然保留 systemd（ops 习惯 + 跨机器场景）。`clawbot@<name>.service` 改为可选（推荐继续用，systemd 管 portal 即可）。

---

## 数据隔离的"硬保证"

类化后，跨用户泄漏只有这几种途径：

| 泄漏途径 | 防御 |
|---|---|
| `BotSession` 之间共享可变对象 | `__init__` 内所有 mutable 默认值都用 `default_factory`（已有 `dataclass` 实践） |
| 持久化文件互写 | `weixin_state_<user_id>.json` 路径在 `BotSession.save_state` 内部硬编码 |
| 日志串号 | `setup_logging` 加 `user_id` 字段；log filter 按 user 隔离 |
| `aiohttp.ClientSession` 共享 | OK——HTTP 连接池本就共享；只要不塞 per-user cookie 就安全（iLink 用 `Authorization` header，per-call） |
| 全局 dict 缓存 | `typing_ticket_cache` 必须从 module-level dict 改成 `self.typing_ticket_cache` |

每加一个 `BotSession` 字段，**默认问一句**：这个值是 per-user 吗？是 → 放 self；不是（譬如 `BASE_URL`、iLink 协议路径前缀）→ 放 module-level 常量。

---

## 关键文件 / 函数映射（搬迁清单）

| 当前位置 | 迁入位置 | 备注 |
|---|---|---|
| `bot.py:1572-1800` `main()` 闭包定义 | `BotSession.__init__` | 13 个变量 → self.xxx |
| `bot.py:1608-1714` `relogin_listener` 闭包 | `BotSession.relogin_listener` | 闭包变量 → self |
| `bot.py:1625-1646` `request_relogin` | `BotSession.request_relogin` | 同上 |
| `bot.py:1980-2112` `message_loop` 闭包 | `BotSession.message_loop` | 同上 |
| `bot.py:1107-1177` `reconnect_timer_task` 函数 | `BotSession.reconnect_timer_task` | self 取代闭包 |
| `bot.py:934-1104` `do_reconnect` 函数 | `BotSession.do_reconnect` | 同上 |
| `bot.py:1261-1301` `fetch_login_qrcode` | `BotSession.fetch_login_qrcode` | module 函数，迁过来即可 |
| `bot.py:1304-1321` `poll_login_status` | `BotSession.poll_login_status` | 同上 |
| `bot.py:984-1327` `login_with_qrcode` | `BotSession.login_with_qrcode` | self 取代闭包 |
| `bot.py:1445-1542` `wait_login_confirmation` | `BotSession.wait_login_confirmation` | 同上 |
| `bot.py:1874-...` `handle_message` 闭包 | `BotSession.handle_message` | 同上 |
| `qr_web.py:38-102` `QrFlowState` | `BotSession.qr_state` 字段 | 类不搬，实例化进 self |
| `qr_web.py:145-169` `make_web_on_qrcode` | `BotSession.__init__` 内部调用 | 返回的 callback 存 self.web_on_qrcode |
| `qr_web.py:451-489` `start` (aiohttp app) | `web_app.py:build_web_app(manager)` | app 接收 manager，按 user_id 路由 |
| `utils/bot_launcher.py` 全部 | Phase 3 改；Phase 1-2 保持 | 搬迁期间共存 |

---

## 反例（不要做的事）

### ❌ 把所有闭包变量改成 `self._state[user_id]`

```python
class Bot:
    state: dict[str, Any] = {}  # 千万不要这样
    
    def get_token(self, user_id):
        return self.state[user_id]["bot_token"]  # 漏一处就是串号 bug
```

这是"闭包 → dict"的等价物，只是搬了个家。所有问题都还在。

### ❌ 搞一个全局 `UserContext` 单例

```python
class UserContext:
    _instance = None
    @classmethod
    def get(cls): return cls._instance
```

多租户场景下 `UserContext` 不是单例，是 N 例。任何"全局唯一"的设计都是反模式。

### ❌ 试图一次性重构 + 上线

`bot.py` 是热路径；任何回归 = 真实用户掉线。**Phase 1 必须先跑通单用户场景**才能动 Phase 2。

---

## 验证方式

每完成一个 Phase：

- Phase 1：alice bot 用新代码跑 24h，对照旧代码看 reconnect / 收发消息 / 状态持久化日志
- Phase 2：5 个并发 session（3 named + 2 ephemeral）跑 8h，看 iLink 服务端是否有限流
- Phase 3：ephemeral TTL 触发 → session.stop() 优雅停机 → 新访客能分到新 session
- Phase 4（可选）：named user 保留 systemd，portal 用单进程，跨机器场景文档化

---

## 变更日志

- 2026-09-15 · 初版，Phase 1-4 改造蓝图
