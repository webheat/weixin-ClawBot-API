# 2026-10-27 多账户共享隔离审计

> 起因：用户要求「集中处理多账户共享问题」
> 范围：`shared_runtime.py` 单进程 / 1 个 `BotManager` / N 个 `BotSession` /
> 1 个共享 `aiohttp.ClientSession` 连接池
> 结论：修掉 3 个「今天就在影响生产」的真问题，其余记入 `TODO.md`

---

## 0. 一句话

审计把「多用户共享」拆成两类问题：

- **数据串了**（跨账户看到对方的东西）—— 找到 **1 处**：per-user 的 IMA 云端
  KB 绑定完全失效，所有账号共用同一个知识库。已修。
- **互相拖累**（一个账号的动作让另一个账号变慢）—— 找到 **2 处**：限流是
  「全站一个桶」、共享线程池互堵。已修。

另有 1 处**安全缺陷**是顺带发现的（限流键可被伪造绕过），也一并修了。

**已核查确认没问题的**（不是"没查"，是查过）：`BotSession` 的全部 per-account
状态、账号切换时的显式清理、`_adopt_pending_account_state` 的同账号校验、
本地/语义检索的目录隔离与 `media_id` 前缀、共享连接池的 Cookie 隔离
（`DummyCookieJar` + 每请求 header 传 per-session token）、`ima_bindings.json`
的分键与浅拷贝返回、`xiaolvshu` 的 `--user` 隔离、`QrFlowState` 的实例隔离。

---

## 1. per-user 的 IMA 云端 KB 绑定完全失效（P0 · 跨账户数据泄漏）

### 现象

`docs/USER_PROVISIONING_AND_KB_ISOLATION.md` §5 早就记了「per-user 绑定在共享
运行时里没接线」，但当时被当成「可选项，按需再补」。实际影响比那更大：
**它不是"没接线"，是"接了但永远走默认值"**。

### 根因链

```
bot_session._ai_chat_kwargs()          # 只传 kb_scope，从不传 kb_id
        ↓
bot._AIWithIma.chat():
    kb_id = kwargs.pop("kb_id", None)  # → None
        ↓
ImaClient.search_knowledge(knowledge_base_id=None)
        ↓
ImaConfig.default_knowledge_base_id    # = IMA_ILINK_DEFAULT_KB
```

`IMA_ILINK_DEFAULT_KB` 是全局的一个库。**所有账号检索的是同一个云端知识库。**

如果某个账号把只属于他的内容装进了那个默认库，**所有人都会检索到**。

生产日志实测：`kb_id=-` 出现 3 次，恒为 `-`（没有一次是真实绑定的 id）。
`ima_bindings.json` 文件根本不存在 —— 没有任何账号绑过。

### 修法

`bot_session._ai_chat_kwargs_async()`：按 `ilink_user_id`（微信账号级稳定）
查 `ima_bindings.json`，把 `kb_id` 传下去。查不到就不传 → 仍走默认，行为不变。

隔离键用 `ilink_user_id` 而不是 `ilink_bot_id`（每次扫码轮换）或
`from_user_id`（聊天对端，不是 bot 主人）。

### 回归测试

`tests/test_bot_session_contract.py::test_per_user_ima_kb_binding_reaches_the_ai_call`
—— 造一个 `ilink_user_id → kb-OWNER` 的绑定，断言 AI 调用收到 `kb_id=kb-OWNER`。
**已验证修复前失败**（`kwargs={'kb_scope': ...}`，没有 `kb_id`）、修复后通过。

---

## 2. 限流是「全站一个桶」（P0 · 跨账户互相拖累）

### 现象

应用只监听 `127.0.0.1:18300`（外部无法直连），nginx 是唯一入口。
但 `CLAWBOT_TRUST_PROXY` 默认 `0` → `_client_key()` 退回 `request.remote`
→ **所有请求的 `request.remote` 都是 `127.0.0.1`**。

限流是 5 次 / 60 秒（`CLAWBOT_WEB_RATE_LIMIT` 默认值）。于是：

> 一个用户狂点 `/clawbot/start`，**其他所有用户全被 429**。

### 修法

`.env` 开 `CLAWBOT_TRUST_PROXY=1`。前提是「可信边缘会**覆盖**转发头」——
本项目满足：nginx 用 `proxy_set_header X-Real-IP $remote_addr`，客户端自带的
该头会被丢弃。

### ⚠️ 顺带修掉一个安全缺陷

原 `_client_key()` 在 `trust_proxy` 开启时取 XFF 的**第一个**元素：

```python
return forwarded.split(",", 1)[0].strip()
```

但 nginx 用 `$proxy_add_x_forwarded_for` —— 它把 `$remote_addr` **追加**到
客户端自带的 XFF **后面**：

```
客户端发 XFF:  9.9.9.9          ← 伪造
nginx 发出:    "9.9.9.9, 203.0.113.7"
                  ↑第一个        ↑真实对端（最后一个）
```

**取第一个 = 取到伪造值** → 可以绕过限流、也可以污染日志里的客户端 IP。

改成：优先读 nginx 覆盖过的 `X-Real-IP`（不可伪造）；没有它再取 XFF 的
**最后**一段（最靠近 nginx 那一跳 = 真实对端）。

### 回归测试

`tests/test_shared_web.py::ClientKeyTests`（4 个）：
- `test_trusted_proxy_takes_the_last_forwarded_for_not_the_first`
  —— **已验证修复前失败**（取到伪造的 `9.9.9.9`）、修复后通过
- `test_trusted_proxy_prefers_nginx_overwritten_x_real_ip`
- `test_untrusted_proxy_falls_back_to_remote`
- `test_trusted_proxy_ignores_client_supplied_x_real_ip_when_absent`

---

## 3. 共享线程池互堵（P0 · 跨账户互相拖累）

### 现象

默认 executor 是 `min(32, cpu+4)`。本机 **2 核 → 只有 6 个线程**。
池子里挤着所有阻塞调用：

| 使用者 | 单次阻塞时长 | 位置 |
|---|---|---|
| 发稿桥子进程 | **最多 240 秒** | `utils/xiaolvshu.py`（出图 + 上传 + 调接口） |
| AI 调用 | **最多 ~200 秒** | DusAPI 30s 超时 × 5 次重试 + 62s 退避 |
| fastembed 模型首次加载 | ~11 秒 | `utils/semantic_kb.py` |
| 每账号首次嵌入 150 篇基座 | 更久 | `utils/semantic_kb.py` |

一条发稿请求占住一个线程 4 分钟，就吃掉 6 个里的 1 个；再来两个慢 AI 调用，
**剩下所有人的 AI 回答都在排队**。

症状是「bot 不理人」—— 但跟 bot 本身没关系，是线程被占满了。

### 修法

1. **桥挪到独立线程池**（`utils/xiaolvshu.py`）：
   `_BRIDGE_EXECUTOR`，`XIAOLVSHU_WORKERS` 默认 2。
   桥的慢不会影响 AI 路径，反过来也一样。
2. **默认池放大**（`shared_runtime._configure_default_executor`）：
   `max(8, cpu*4)`，可用 `CLAWBOT_WORKER_THREADS` 覆盖。
   这些调用都是 I/O 等待，线程数可以远大于核数。

### 回归测试

- `test_bridge_uses_its_own_executor_not_the_shared_default` —— 桥池独立、线程名可辨
- `test_slow_bridge_does_not_starve_ai_calls` —— 把**默认池占满**（按
  `_default_executor._max_workers` 个任务），AI 调用仍须 < 1.5s 返回。
  **已验证修复前失败**（AI 被拖到 1.9s）、修复后通过（0.26s）。

> 注：这条测试最初只起 2 个桥任务，修复前也通过 —— 因为 2 个任务只占 6 个
> 默认线程里的 2 个。必须按池容量起任务才能真正测出饥饿。

---

## 4. 没修的（记入 `TODO.md`）

| # | 问题 | 严重程度 | 触发条件 |
|---|---|---|---|
| A2 | 语义库 `db_path` 退化成共享的 `docs/.semantic_kb.sqlite3` | 跨用户泄漏 + 写竞争 | `CLAWBOT_USER_KB_DIR` 未配或 scope 为空时 |
| A3 | `LocalKBIndex` 的 `extra_dirs` 永久缓存 → 私有目录后建就永远检索不到 | 功能缺口 | 私有目录在索引已建之后才出现 |
| A4 | 私有 KB 目录没有任何人自动创建（只有 semantic 的 mkdir 副作用会建空壳） | 功能缺口 | 跟发稿桥的 `ensure_user` 相比缺一个对应的自动化 |
| A7 | 连接池无 per-account 配额，`limit_per_host=0` → 单账号可占满 300 条连接 | 瓶颈 | 活跃会话数接近 300 时 |
| A10 | `_kb_scope` 净化非单射（`-` 和 `_` 都映射成 `_`） | 低概率跨用户泄漏 | 需要两个真实账号 ID 只差 `-`/`_` |
| A11 | `CLAWBOT_STATE_DIR` 未设 → `ima_bindings.json` 落 CWD，与文档不符 | 文档不一致 | 一直 |

---

## 5. 顺带记录的日志问题（由并行的日志 worktree 处理）

审计时发现、但**不属于本轮范围**（日志由另一个 worktree 在做）：

- `clawbot.ai` / `clawbot.ima` / `clawbot.semantic` 的日志全部 `[user=-]`
  —— executor 线程不继承 contextvars。已修，见 `fix/log-user-dimension`。
- `BotManager.stop_all()` 没 set/reset `USER_LOG_CONTEXT` → 关机时所有会话
  日志丢维度（生产日志里可见 4 组 `[user=-]` 的 stop/notifystop）。
- `shared_web.py` 全链路没设 `USER_LOG_CONTEXT` → web 层日志前缀恒为 `-`。
- README 的 5 条诊断配方都指向 `logs/clawbot.log`（**生产早就不写它了**，
  最后修改停在 9 月 16 日）；生产写的是 `logs/clawbot_shared.log`。
  配方 3 提到的 `ai call start` / `ai call done` / `reply sent`
  **生产一条都不产生**。

---

## 6. 相关文件

- `bot_session.py` — `_ai_chat_kwargs_async()`（KB 绑定）、AI 调用点
- `shared_web.py` — `_client_key()`（限流键）
- `shared_runtime.py` — `_configure_default_executor()`（默认池容量）
- `utils/xiaolvshu.py` — `_BRIDGE_EXECUTOR`（桥的独立池）
- `docs/USER_PROVISIONING_AND_KB_ISOLATION.md` §5 — 已知限制（已更新）
- `TODO.md` — 本轮未修项 + IMA 默认 KB 失效
- 分支：`fix/multi-account-isolation`（已并入 main）
