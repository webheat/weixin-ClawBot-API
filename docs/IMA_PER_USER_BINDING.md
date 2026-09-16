# Per-user IMA 知识库绑定设计

> 状态：设计文档（未实现）。本文档描述一个把"每个微信账号绑定到自己的 IMA KB"这一新特性的目标、架构、数据流、关键坑与实施清单，作为后续 patch 的 canonical reference。
>
> 与现行实现的差异：当前所有 `BotSession` 共用同一个全局 `IMA_ILINK_DEFAULT_KB`（`bot_session.py:191-202` 解析后塞进 `ImaConfig.default_knowledge_base_id`，`ima.py:55/116/507-512` 透传给 `ImaClient.search_knowledge`）。多账号同进程场景下，账号 A 看到的资料 = 账号 B 看到的资料 = 所有人共享同一个 KB。
>
> 关联 commit / 文档：
> - `docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` —— 确立 `ilink_user_id` 是微信号级稳定 id
> - commit `f298d82` `docs: clarify iLink bot_id rotation in 2026-09-15 incident report` —— 同上
> - `weixin-openclaw-api-py-docs.md:1051` —— `ilink_bot_id` 在每次 QR 重新分配
> - `docs/IMA_KB.md:126-139` —— `KBT_MINE_KB` 不能 search 的 220004 坑

---

## 1. 背景 / 动机

CLAUDE.md "Product context: IMA vs Obsidian" 决策表里第一行就指出：

> Multi-user editing, cross-device access, official AI answering → **ima (production)** — Service-side KB; supports `KEYWORD_EXTRACT` + `FETCH_BODY` + `RERANK`. Shared creds via `/etc/clawbot/llm.env` + `/etc/clawbot/ima.env` (no per-user env files)

但 "Shared creds via `/etc/clawbot/ima.env`" 是 **凭据** 共享，不是 **KB** 共享。`/etc/clawbot/ima.env` 里的 `IMA_ILINK_DEFAULT_KB` 是一段**全局字符串**（`ima.py:116` 读一次、`ima.py:508` 当 fallback），整个 process 共一份。当前共享运行时下：

- 匿名访客拿到一个 session token，扫一次码 → 分配一个 BotSession → 用 `IMA_ILINK_DEFAULT_KB` 这个 KB 检索
- 换一个浏览器、换个 session token、再扫码 → 拿到**另一个** BotSession，**仍然查同一个 KB**

这意味着：「翼claw 助手」运维方预装了 150 条 Q&A 灌进某个 KB（见 `docs/SESSION_2026-09-07_ima_pipeline.md`），但任何扫了同一个 QR 的人看到的是同一份资料。**对单一部署 owner 的「个人助理」场景不致命；对「我自己的微信、想接我自己的 KB」的诉求不成立。**

本特性的目标就是把 `kb_id` 这一维从「进程级常量」提升为「微信号级绑定」。

## 2. 设计目标

- 每个 bot 主人（手机扫码登录的那个微信号，`ilink_user_id`）能绑定一个 IMA KB。
- 绑定关系跨浏览器、跨设备、跨服务重启都有效。**不**依赖浏览器 cookie / opaque session token，**不**依赖 session 状态文件。
- 第一次扫码登录后自动套用绑定；之后该用户的所有消息都查该 KB。
- `KBT_MINE_KB` 不能 `search_knowledge`（`docs/IMA_KB.md:126-139`：实测 `code=220004 invalid knowledge_base_id`），所以绑定页只能选 `KBT_SHARED_KB` / `KBT_SUBSCRIBED_CREATE_KB`。
- 单一配置回退：未绑定时继续用旧的 `IMA_ILINK_DEFAULT_KB`，不破坏现有部署。
- 运行时不影响未配置 IMA 凭据的部署（`mode=llm-only reason=ima-not-configured`，`bot.py:2410-2411`）。

## 3. 稳定身份键的选择

绑定键必须满足两个条件：

1. **跨 QR 轮转稳定** —— 用户每周甚至每天重连一次，键不变。
2. **与"聊天对方"无关** —— `from_user_id` 是用户消息里的"对方"，不是 bot 主人自己。

候选字段（iLink 协议）：

| 字段 | 形态 | 是否可用 | 原因 |
|---|---|---|---|
| `ilink_user_id` | `o9cq806m1rtXSvyUgFcUc_KO_N7I@im.wechat` | ✅ | 微信号级稳定 id；同一微信号每次扫码**始终相同**（`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` 实测） |
| `ilink_bot_id` | `077b77e37683@im.bot` | ❌ | **每个 QR 生命周期独立生成**；同微信号连续两次扫码 iLink 分配两个不同 bot 实体（`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` + commit `f298d82`） |
| `from_user_id` (msg 里) | `o9XXXX@im.wechat` | ❌ | 是聊天对方（user → bot 的另一侧），不是 bot 主人 |
| `bot_token` | 64 hex chars | ❌ | 轮转更快，且会污染日志 |
| opaque browser session token | URL-safe hex string | ❌ | 浏览器 cookie 死了就丢，与「跨设备/重启有效」目标直接冲突 |

**结论：用 `ilink_user_id` 作键。**

绑定时机：在 `BotSession._apply_login` 第一次拿到非空 `ilink_user_id` 时记录（`bot_session.py:288`），通过 `_emit("logged_in", account_changed=account_changed)`（`bot_session.py:299`）这个已有 hook 暴露给 `BotManager` 层；hook 不直接写绑定（绑定只能由用户在 web UI 主动设置，runtime 不应默默改 `ima_bindings.json`）。

## 4. 架构图 / 数据流

```
首次扫码
─────────────────────────────────────────────────────
浏览器访问 /clawbot/start
  └─ shared_web.py:397   mint opaque URL-safe session token (secrets.token_hex-like)
  └─ shared_web.py:401   store.bind(session_token) → opaque cookie
  └─ shared_web.py:416   BotManager.get_or_create(session_token)

BotSession.start()
  └─ _login → login_with_qrcode → poll_login_status
       └─ status="confirmed" → bot.py:1334-1347
          返回 { bot_token, baseurl, ilink_bot_id, ilink_user_id }
  └─ _apply_login  (bot_session.py:265-299)
       └─ self.ilink_user_id = result["ilink_user_id"]   ← bot_session.py:288
       └─ await self._emit("logged_in", account_changed=...)   ← bot_session.py:299
  └─ (manager 侧 handler)  ima_bindings.lookup(ilink_user_id)     ← OPTIONAL pre-warm
       └─ 命中 → 缓存到 self._kb_id 以备 handle_message 直查
       └─ 未命中 → self._kb_id = None → 后续走 IMA_ILINK_DEFAULT_KB

每条入站消息
─────────────────────────────────────────────────────
handle_message(msg)  (bot_session.py:503)
  └─ from_id = msg["from_user_id"]                       ← bot_session.py:514
  └─ owner  = self.ilink_user_id                         ← bot_session.py:288 (已存)
  └─ kb_id  = ima_bindings.lookup(owner) or None         ← NEW
  └─ loop.run_in_executor(None, self.ai.chat, text,
                          kb_id=kb_id)                   ← bot_session.py:613 + NEW kwarg
  └─ _AIWithIma.chat(message, kb_id=kb_id)               ← bot.py:2386 + NEW kwarg
       └─ self._ima.search_knowledge(query,
                                     knowledge_base_id=kb_id)   ← ima.py:489-545
       └─ kb_id 为 None 时 ↓
          self._ima.search_knowledge(query)              ← 走 ImaConfig.default_knowledge_base_id
                                                         ← ima.py:507-512
```

注意：`_AIWithIma.chat` 当前签名只接 `message` + `kwargs.pop("prompt")`（`bot.py:2405`），要扩成接 `kb_id` 必须新增一个 kwarg；调用链上 `handle_message` → `ai.chat` 需要把这个 kwarg 透传下去。同时 `local_kb` 和 `semantic_kb` **不应**受 `kb_id` 影响（它们是本地资源，全 KB 兜底，跟"主人是谁"无关）。

## 5. 持久化设计

`weixin_state_<session_token>.json` 跟浏览器 cookie 同生共死（`bot_session.py:91-94` 用 `_safe_name(user_id)` 拼文件名，cookie 一过期 `BrowserSessions.expire()` 会 `manager.stop()`；`shared_web.py:133-141`），**不能**用来存绑定。绑定必须独立成一个文件：

```
CLAWBOT_STATE_DIR/ima_bindings.json
```

格式：

```json
{
  "o9cq806m1rtXSvyUgFcUc_KO_N7I@im.wechat": {
    "kb_id": "AUNyKAq7e0i2iguEL-XVEa6xcrbqxhr3yeonvLdFdJ0=",
    "kb_name": "我的私人 KB",
    "bound_at": "2026-09-16T11:23:45Z",
    "bot_id_at_bind": "077b77e37683@im.bot"
  }
}
```

模块：`utils/ima_bindings.py`（**未实现**；本文档列 API 表面）。`IMABindings` 类公开：

| 方法 | 行为 |
|---|---|
| `load() -> dict` | 读文件；不存在 → `{}`；解析失败 → 备份 `.corrupt-<ts>.json` 后 `{}`，WARN 日志 |
| `lookup(ilink_user_id) -> Optional[dict]` | 返回 `{kb_id, kb_name, bound_at, bot_id_at_bind}` 或 `None` |
| `bind(ilink_user_id, kb_id, kb_name, bot_id_at_bind="") -> None` | 写入；`bound_at = now()`；`os.replace(tmp, final)` 原子 |
| `unbind(ilink_user_id) -> bool` | 删一条；返回 True/False 表示是否真有这条 |
| `list() -> list[tuple[str, dict]]` | 给 web UI 调试 / 列出所有绑定（**默认不暴露给匿名访客**） |

并发：`BotManager` 下 N 个 `BotSession` 共享同一个 `IMABindings` 实例（构造时由 `shared_runtime.py` 注入，类似 `ima_env` 的处理路径，`shared_runtime.py:282-284`）。`bind` / `unbind` 必须原子：

- 选项 A：`fcntl.flock(fd, LOCK_EX)` —— POSIX 强一致，但只读模式下进程级阻塞
- 选项 B：单 `asyncio.Lock` 保护 `dict` + 后台任务 `os.replace` —— 与项目 asyncio-first 风格一致（参考 `BotSession.save_state` 的 `threading.Lock` 模式，`bot_session.py:95`）

**推荐选项 B**：`asyncio.Lock` + `os.replace` 已经覆盖所有调用点（都是 `await` 上下文），零额外依赖。文件锁不需要。

## 6. 改动清单

| Layer | 文件 | 改动 |
|---|---|---|
| 持久化 | `utils/ima_bindings.py`（**NEW**） | `IMABindings` 类：`load / save / lookup / bind / unbind / list`；`asyncio.Lock` + `os.replace` 原子写；放在 `CLAWBOT_STATE_DIR/ima_bindings.json` |
| IMA 客户端 | `ima.py` | 新增 `list_searchable_kbs()` 方法：调 `search_knowledge_base(query_user=False)`（`ima.py:405-428`），过滤 `base_type == "KBT_MINE_KB"`（`ima.py:299-301`），只返 `KBT_SHARED_KB` / `KBT_SUBSCRIBED_CREATE_KB`。给 web UI 用 |
| AI 路由 | `bot.py:_AIWithIma.chat`（`bot.py:2386-2626`） | `chat` 接受新 kwarg `kb_id`；非空时透传给 `self._ima.search_knowledge(..., knowledge_base_id=kb_id)`（`ima.py:489-545`）；空时走 `self._ima.cfg.default_knowledge_base_id` 旧路径。`mode=llm+ima` 日志加 `kb_id=` 字段便于 grep |
| 消息分发 | `bot_session.py:_handle_message`（`bot_session.py:503-622`） | 在 `loop.run_in_executor(None, self.ai.chat, text)`（`bot_session.py:613`）前查 `kb_id = ima_bindings.lookup(self.ilink_user_id)`；把 `kb_id=kb_id` 作为 kwarg 透传给 `self.ai.chat(text, kb_id=kb_id)` |
| 生命周期 | `bot_session.py:_apply_login`（`bot_session.py:265-299`） | **不直接写绑定**（运行时不应偷偷改用户的 KB 选择）；通过已有 `await self._emit("logged_in", account_changed=account_changed)`（`bot_session.py:299`）暴露 `ilink_user_id` 给 manager，manager 可选择性把 `_kb_id` 缓存进 session（避免每次消息都查文件）。`BotSession.__init__` 增加 `ima_bindings: IMABindings` 参数 |
| Manager | `bot_manager.py` | 构造 `BotSession` 时注入共享的 `IMABindings` 实例；监听 `_emit("logged_in")` 把 `ilink_user_id → kb_id` 缓存到 `BotSession._kb_id` |
| Web UI | `shared_web.py:INDEX_HTML`（`shared_web.py:243-267`）+ 新路由 | 新增 `GET /ima/bind`：列出可绑定 KB（`list_searchable_kbs()`）+ 当前绑定；`POST /ima/bind`：写绑定，需校验 `X-CSRF-Token` 与 `b.csrf_token`（复用 `shared_web.py:510-512` 的 `secrets.compare_digest`）。`INDEX_HTML` 加一个"知识库绑定"按钮，弹一个折叠的 `<select>` + 提交按钮 |
| 文档 | `docs/IMA_KB.md` | 加 §"Per-user binding" 章节引用本文件；点明"个人 KB 不能绑"的 220004 坑 |

## 7. 关键坑 / 风险

- **`KBT_MINE_KB` search 返回 `220004`**（`docs/IMA_KB.md:126-139`）—— `list_searchable_kbs()` **必须在 web 端过滤**，否则用户选了个人 KB 后 `_AIWithIma` 每次对话打 6 行 `code=220004` 重试日志（`docs/IMA_KB.md:138`）。在 `ima.py:299-301` 的 `KB_TYPE_MINE` 常量处判断 `base_type` 字段。
- **`ilink_bot_id` 不能作为键** —— 会随 QR 轮转；绑定写入时 `bot_id_at_bind` 只是元数据，不参与 lookup。**测试要点**：连续两次扫码，看 `lookup(ilink_user_id)` 仍然返回同一个 `kb_id`（即便 `bot_id_at_bind` 字段已经变了）。
- **绑定写入需 CSRF + cookie 校验** —— 复用 `BrowserBinding.csrf_token`（`shared_web.py:50, 79, 510-512`）。**不能**靠 `ilink_user_id` 反查（攻击者拿到别人的 `ilink_user_id` 字符串就能改 KB，绕过 cookie）。**必须**：`secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), b.csrf_token)`。
- **文件锁** —— `asyncio.Lock` 足够；不需要 `fcntl.flock`（多 worker 不在当前架构里，CLAUDE.md 明确单进程 `BotManager`）。
- **回退路径：KB 被 IMA 端删除** —— `_AIWithIma.chat` 检测到 `search_knowledge` 抛 `code=220004`，自动 `ima_bindings.unbind(ilink_user_id)`，记 `log_ima.warning("kb invalidated ilink_user=%s kb_id=%s; auto-unbound", ...)`，下一次走 `IMA_ILINK_DEFAULT_KB`。
- **隐私** —— `/ima/bind` 只暴露 KB 的 `kb_id` + `kb_name` + `base_type`，**绝不**暴露 `client_id` / `api_key`（前者是 ima 内部标识，后者是 LLM 凭据）。`IMABindings.list()` 在 web 路由层**不调用**；仅供调试 / CLI（未来 `utils/list_ima_bindings.py` 留口）。
- **Owner ≠ 聊天对方** —— bot 主人的 `ilink_user_id` 在 `_apply_login` 时确定；用户消息里的 `from_user_id` 是**对方**。不要把 `from_user_id` 当成"该用哪个 KB"的查询条件。
- **`from_id` 不是绑定的 key** —— 在 `bot_session.py:514` `from_id = msg["from_user_id"]`，与 `self.ilink_user_id`（`bot_session.py:288`）完全不同。本特性的 lookup 只用 `self.ilink_user_id`，**永远不用** `from_id`。

## 8. 测试 / 验证步骤

```bash
# 1) 确认有可绑定的 shared / subscribed KB（utils/list_ima_kb.py 现状会列出全部 KB，需人工过滤 ★号）
./venv/bin/python utils/list_ima_kb.py --query "共享"
./venv/bin/python utils/list_ima_kb.py --query "订阅"

# 2) 起服务，扫码登录
python bot.py
# 浏览器访问 http://localhost:18300/clawbot/ → 扫码 → 登录成功

# 3) 看 log 拿 ilink_user_id
grep "poll status=confirmed" logs/clawbot_shared.log | tail -1
# 例：poll status=confirmed bot_id=xxxx baseurl=...  ← 后面紧跟 ilink_user_id=<oXXX@im.wechat>

# 4) 访问绑定页（auth cookie 已有），选 KB，提交
#    浏览器：http://localhost:18300/clawbot/ima/bind
#    选 KB → 点"绑定"
#    期望：返回 200 {ok: true}，日志一行 ima_bindings bind ilink_user=<oXXX> kb_id=<kb>

# 5) 检查文件落地
cat CLAWBOT_STATE_DIR/ima_bindings.json | jq .
# 期望：{ "oXXX@im.wechat": { "kb_id": "...", "kb_name": "...", ... } }

# 6) 在 WeChat 发消息，看 log
# 期望出现：ai route mode=llm+ima reason=hits-injected kb_id=<kb> hits=...
# 注意：bot.py:2606-2614 当前不输出 kb_id，需顺手在 chat 末尾加上

# 7) 重启服务、清 cookie、再扫码 → 应自动套用之前绑定的 KB
sudo systemctl restart clawbot.service
# 清浏览器 cookie（关掉所有标签页即失效）
# 重新扫码 → handle_message 看到 self.ilink_user_id 是同一个 → 命中 lookup → 同一 KB

# 8) 解绑
# 浏览器：http://localhost:18300/clawbot/ima/bind → 点"解绑"
# 期望：weixin_state 还在，ima_bindings.json 里那条被删
# 后续消息：ai route mode=llm+ima reason=hits-injected kb_id=<IMA_ILINK_DEFAULT_KB> hits=...

# 9) 删 IMA 端 KB / 把 KB 改成 KBT_MINE_KB
# 期望：第一次 search 拿到 220004 → _AIWithIma 自动 unbind → 后续走 DEFAULT_KB
# 日志：kb invalidated ilink_user=<oXXX> kb_id=<old>; auto-unbound
```

## 9. 未做 / 后续

- 群聊 per-user KB 区分 —— 当前群消息也用 bot 主人 KB（`from_user_id` 是群，owner 是主人，绑哪个取决于"谁付钱"；保持单 KB 是更合理的默认）。
- 多 bot 主人共享一个进程下的 multi-account switch UI —— `BotManager` 现在支持，但 web 只暴露 `/clawbot/start` 入口；named user 路径已在 2026-09-15 cleanup 删除（commit `d54398c`）。恢复"同一浏览器管理多个 KB"属于独立特性。
- KB 内容同步 / 双向同步 —— "IMA 当检索源、Obsidian 当编辑源"方案 A（`docs/SESSION_2026-09-07_ima_pipeline.md:236`）属于更大的内容工作流，超出本特性范围。
- `utils/list_ima_bindings.py` 诊断 CLI —— 不在 v1，但 `IMABindings.list()` API 已留好；后续加 CLI 时直接调用即可。
- 跨 `BotSession` 的 `kb_id` 缓存一致性 —— `BotSession._kb_id`（manager 侧在 `_emit("logged_in")` 里写入）只在 session 生命周期内有效；用户在另一浏览器解绑后，**当前 session** 的 `_kb_id` 不会失效（直到下次重连）。**可接受**：5–15 s 的人工解绑操作不要求立刻影响在飞消息；重启 / 重连后必然最新。生产补强：每隔 60 s 重新 `lookup` 一次（简单加 `BotSession._kb_id_refresh_at`）。

## 10. 参考

- `bot.py:_AIWithIma.chat` `bot.py:2386-2626` —— 4 档 AI 路由 state machine
- `bot.py:handle_message` `bot.py:1884-2015`（legacy main）；`bot_session.py:_handle_message` `bot_session.py:503-622`（shared runtime）
- `ima.py:ImaClient.search_knowledge` `ima.py:489-545` —— `knowledge_base_id` kwarg 已存在
- `ima.py:ImaConfig` `ima.py:50-123` —— `default_knowledge_base_id` 是 fallback 字段
- `bot_session.py:_apply_login` `bot_session.py:265-299` —— `ilink_user_id` 写入点（line 288）
- `shared_web.py:session_start` `shared_web.py:378-481` —— session token 铸造（line 397）
- `shared_web.py:INDEX_HTML` `shared_web.py:243-267` —— 当前 web UI（需扩 KB 绑定按钮）
- `shared_web.py:verify_code` CSRF 校验 `shared_web.py:506-528` —— 绑定写操作可直接复用 `secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), b.csrf_token)`（line 510-512）
- `shared_runtime.py:282-284` —— `ima_env` 注入模式参考，新模块按同样方式接入 `IMABindings`
- `docs/IMA_KB.md:126-139` —— `KBT_MINE_KB` 220004 坑
- `docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` —— `ilink_user_id` 稳定性的实测证据
- `weixin-openclaw-api-py-docs.md:1051` —— iLink `ilink_bot_id` 轮转语义
- `CLAUDE.md` "Product context: IMA vs Obsidian" —— 选 ima 的决策依据
- `CLAUDE.md` "Multi-user layout (session-based single-process model)" —— session 状态文件生命周期
