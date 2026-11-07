# Per-user IMA KB 绑定 — Review Notes (待复核)

> 本文档**不是设计文档**，是实现完成后**留给后续 LLM 做 code review** 的怀疑清单 / 风险登记。
>
> 设计意图看 [`IMA_PER_USER_BINDING.md`](./IMA_PER_USER_BINDING.md)；Web UI 用户视角看 [`IMA_WEB_UI.md`](./IMA_WEB_UI.md)。
>
> **代码状态**：已写完 + 主会话做了基础静态验证（语法 / import / grep / 部分 roundtrip），但**未做端到端冒烟测试**、**未做并发 / 竞态测试**、**未做完整 diff 审计**。下面列出所有未充分验证的点，请接手 LLM 重点 review。

---

## 0. 接手 LLM 必读

- **本会话对子代理的"已通过验证"不能完全信任** —— 子代理 B 自报"单测全部通过"实际上 `bind`/`unbind` 是 async，它漏了 `await`，根本是错的测试代码，恰好没崩只是因为 `IMABindings` 内部即使没 await 也部分能跑（详见 §1）。
- **本会话的 roundtrip 也只覆盖 happy path** —— 没测竞态、没测 web UI 流程、没测跨进程、没测 IMA API 调用失败的回退路径。
- **CLAUDE.md 的"Critical invariants"** 必须每条复核 —— 尤其 Portal 隐私、CSRF、HTTP 200≠成功、文件锁这些。

---

## 1. 子代理自检错误（已知问题，已 work-around）

### 1.1 子代理 B 的"单测通过"实际是错的

子代理 B 在 `utils/ima_bindings.py` 写完后自报做了单测：

> Async roundtrip in temp dir:
> - `bind('u1', 'kb1', 'shared KB', 1002)` then lookup returns `kb1` ✓

但 `IMABindings.bind` / `unbind` 是 `async def`（`utils/ima_bindings.py:106` / `:146`），调用时**必须 `await`**。子代理的测试代码漏了 `await`，返回的是 coroutine 对象不是结果。

我用正确的方式重跑了一遍（在 temp dir）：
- ✅ `bind` / `unbind` / `lookup_kb_id` 实际功能正确
- ✅ `kb_type=1001` (`KBT_MINE_KB`) 被 `IMABindings.bind` 拒绝，抛 `ValueError`
- ✅ `unbind` 第一次返回 `True`，第二次返回 `False`
- ✅ 文件落盘、`chmod 0o600`、schema `{"version":1, "bindings":{...}}`

**结论：代码本身 OK，只是子代理自检脚本写错了**。但这给一个信号：**子代理自检不能全信**，所有声称的"测试通过"都要本会话亲自重跑。

### 1.2 子代理 A 跑题风险

子代理 A（设计文档）返回时只花了 13k tokens / 109s，产物 227 行 Markdown 看起来完整，但**没有验证每个引用是否真的对得上行号**。设计文档里的 `bot.py:1334-1347` / `bot_session.py:288` 等行号，**接手 LLM 必须用 `grep -n` 或 Read 复核**。

---

## 2. 未审计的关键代码区域

下面每块**都没在本会话完整 Read 过**，只 grep 过关键词。请接手 LLM 完整 Read 每一块。

### 2.1 `bot.py` 命令分发逻辑（高风险）

**位置**：`bot.py:2021-2162`

```
/bindkb /mykb /unbindkb 命令分发
```

要确认：
- [ ] `from_id == ilink_user_id` 的判断逻辑是否真的能区分 bot 主人 vs 聊天对方
- [ ] 拒绝时的回复文案是否会泄露 `ilink_user_id` 给非主人
- [ ] `kb_id` 前缀匹配逻辑（`bot.py:2123` `prefix_matches = [kb for kb in kbs if kb["kb_id"].startswith(arg)]`）有没有唯一性强制
- [ ] `list_searchable_kbs()` 失败时的回退：会回什么消息？会不会误导用户？
- [ ] `ImaClient` 在命令路径里是怎么构造的？会不会污染 `os.environ`？会不会触发与生产 `ImaClient` 不同的凭据？
- [ ] 命令解析的优先级 —— 是否会跟其他已有命令（`/help` `/time` `/重新连接`）冲突？

### 2.2 `bot.py` `handle_message` KB 透传（中等风险）

**位置**：`bot.py:2183-2203`

```python
owner_id = str(runtime_state.get("ilink_user_id") or "")
kb_id: Optional[str] = None
if owner_id:
    try:
        bindings = await _get_ima_bindings()
        kb_id = bindings.lookup_kb_id(owner_id)
    except Exception as exc:
        log_ai.warning(...)
        kb_id = None
```

要确认：
- [ ] `runtime_state.get("ilink_user_id")` 在哪个时刻一定有值？扫码后 vs 消息到达前
- [ ] `_get_ima_bindings()` 是 lazy singleton 还是每次新建？多次调用会不会 race？
- [ ] `Exception` 兜底会不会吞掉真正的 bug？应该是只兜 `OSError` / `json.JSONDecodeError` 之类
- [ ] `kb_id` 透传到 `ai.chat`（`bot.py:2203` `partial(ai.chat, text, kb_id=kb_id)`）——确认 partial 里 kb_id 是 keyword arg 而不是 positional
- [ ] AI 调用失败时 `kb_id` 是不是要在错误日志里打印出来（方便排查）

### 2.3 `bot.py` `_AIWithIma` 类（中等风险）

**位置**：`bot.py:2413-2640`（按子代理报告）

要确认：
- [ ] `kb_id = kwargs.pop("kb_id", None)`（`bot.py:2595`）位置对吗？是否在其他 pop 之后？
- [ ] `_search_ima_merged` 是否真的把 `kb_id` 透传到每个内部 `ImaClient.search_knowledge(...)` 调用？
- [ ] `local_fallback` / `semantic_fallback` 路径是否也吃 `kb_id`？设计文档说"local/semantic KB 不该被 kb_id gate"（owner-independent），需要确认代码也这么实现
- [ ] log 行（`bot.py:2641, 2800, 2802, 2806`）的 `kb_id=` 字段是否会泄露 kb_id 到不应该看到的地方？日志脱敏 filter 不会遮蔽这个

### 2.4 `bot_session.py` `_apply_login` hook（低风险）

**位置**：`bot_session.py:300-307`

子代理报告说"加 read-only log，不写绑定"。要确认：
- [ ] 真的只读不写
- [ ] log 行不泄露 `ilink_user_id` 全量
- [ ] log 行不会 spam（每次 reconnect 都打）

### 2.5 `shared_web.py` `/ima/bind` 系列路由（**最高风险**）

**位置**：`shared_web.py:740-877`（`ima_bind_get` / `ima_bind_post` / `ima_unbind_post`）+ `:643`（`/state` handler 扩展）+ `:346-352`（INDEX_HTML 卡片）+ `:936-937, 946-947`（路由注册）

要确认：
- [ ] **`_build_ima_client(ima_env)` 的实现**：构造 `ImaClient` 时从哪里读 `ima_env`？是不是真的不污染 `os.environ`？CLAUDE.md 说"`/etc/clawbot/ima.env` 解析进 per-session dict，不污染 os.environ" —— 确认 web UI 路径也是同样的承诺
- [ ] **CSRF 校验时机**：`ima_bind_post` 是不是**先校验 CSRF** 再查 `BotSession.ilink_user_id`？反了就是绕过
- [ ] **`ilink_user_id` 来源**：`ima_bind_post` 里 `owner_id` 是从 `BotSession.ilink_user_id` 读还是从 `runtime_state` 读？两者在 reconnect 后可能不同步
- [ ] **Spoofing 防御**：子代理说"用 `list_searchable_kbs()` 真实数据重写 `kb_name`/`kb_type`" —— 确认这条在每条 POST 路径都覆盖
- [ ] **`/state` 扩展字段**：`ilink_user_id` 是否会泄露给**同浏览器其他 tab**？CLAUDE.md 明确说"无 cookie 访客的页面必须是单一登录按钮，不得渲染任何用户、端口、env 文件路径、bot 状态" —— 主人页面渲染 `ilink_user_id` 本身不在禁止列表，但要确认 `/state` 不会被未登录访客拿到（401 应返）
- [ ] **HTML escape**：KB 名字 / 错误消息如果含 `<script>` 之类会不会 XSS？看 INDEX_HTML 是否有 escape
- [ ] **路由注册的 prefix 双写**（`:936-937` + `:946-947`）—— 确认两个都是真的注册了，还是有死代码
- [ ] **`_render_kb_rows` / `_render_current_label`** 帮手函数：是否真的有 escape？是否处理空 KB 列表？
- [ ] **`state` handler 加的 `ilink_user_id` 字段**：从 `BotSession` 同步读还是从 `runtime_state` dict 读？前者需要 BotSession 已存在；后者可能在 `_apply_login` 之前是 `""`

### 2.6 `utils/ima_bindings.py` 持久化模块（中等风险）

**位置**：`utils/ima_bindings.py`（269 行，全新文件）

要确认：
- [ ] **默认 STATE_DIR 路径**：子代理 B 设的默认是 `.`（cwd）。如果 `CLAWBOT_STATE_DIR` 没设，文件会落到**当前工作目录** —— 本会话的测试已经在 `/opt/weixin-ClawBot-API/ima_bindings.json` 留了空文件。这是个**真问题**：第一次在没有 env var 的环境跑会污染 cwd。接手 LLM 必须：
  - 要么改默认路径为项目根外的固定位置
  - 要么强制要求 `CLAWBOT_STATE_DIR` 必须设（启动时 assert）
  - 至少要在 `__init__` 里 log warning 当默认路径生效时
- [ ] **`_WRITE_LOCK` 是否跨实例共享**：子代理 B 说"class-level asyncio.Lock shared by all instances" —— 在单进程里 OK，**但 `_DEFAULT` 是 process-local**。多进程部署时每个进程一份锁，并发写可能 race。CLAUDE.md 明确说 BrowserSessions 是 process-local，多 worker 部署**本来就不支持** —— 但 ima_bindings.json 是文件层共享的，理论上 race。要么加 `fcntl.flock`，要么明确文档说"多 worker 不支持"
- [ ] **bind 的 upsert 语义**：先 unbind 再 bind，还是合并更新？同一个 `ilink_user_id` 在两个 session 里同时绑不同 KB 时谁赢？
- [ ] **`chmod 0o600`** 是否在容器/某些 fs 上失败？要 try/except 兜底
- [ ] **JSON 文件损坏回退**：子代理测试过写 `this is not json {{{`，确认 warn + 返回空。但**这会静悄悄清掉所有绑定** —— 接手 LLM 应该确认日志是否 audit-grade（带时间戳 + 文件路径）
- [ ] **`kb_type` 校验在 `IMABindings.bind` 内部做了**，但 `kb_type=1002` 字符串别名（`KB_TYPE_SHARED = "KBT_SHARED_KB"`）也接受吗？看代码只接 int，不接字符串别名 —— 接受也好拒绝也好，**必须有明确语义**而不是"看情况"
- [ ] **CLI 子命令**：spec 说"lookup"、"list"、"unbind"，但**没有 "bind"**（应该走 web UI 走 CSRF）。要确认这个决策最终一致
- [ ] **行数超 spec**：269 行 vs spec 150-220。子代理说"删 verbose docstring 或 sys.path shim 才能压" —— docstring 是有意义的，shim 是为了 `python utils/ima_bindings.py list` 独立运行。可以接受。

### 2.7 `ima.py` `list_searchable_kbs()`（低风险，但未深入看）

**位置**：`ima.py:520`（子代理 C 报告）

要确认：
- [ ] 子代理 C 返回时声称"需要更多 context"但还是返回了 —— 它对 OpenAPI 响应字段（`base_type` / 整型 / 字符串映射）的处理是否正确？看 `_BASE_TYPE_TO_INT` dict（`ima.py:313-321`）+ `_KB_TYPE_DISPLAY_NAME` dict（`ima.py:325-328`）的映射是否覆盖所有已知字符串值
- [ ] 网络错误 / 鉴权失败时抛什么？看 `ima.py:538` 的 `raise ImaError(...)` —— 是该模块的根异常还是子异常？
- [ ] 是否会触发 `client_id` / `api_key` 泄露到日志？

---

## 3. 未做的端到端测试（必须有）

接手 LLM 应该跑：

### 3.1 Web UI 流程

```bash
./venv/bin/python bot.py &
sleep 2
# 1. 浏览器扫码（用真的微信 / 用 mock 凭据，看 IMABindings 是否被调用）
# 2. 主页面看到 "知识库：未绑定"
# 3. 点 [绑定] → /ima/bind → 选 KB → 提交
# 4. 检查 ima_bindings.json 落盘
# 5. 微信里发问题 → 日志确认 kb_id=...
# 6. 点 [解绑] → 文件删除该条目
```

### 3.2 微信命令流程

```bash
# 在 WeChat 里以 bot 主人身份发:
/bindkb                # 应该列出可绑定 KB
/bindkb <kb_id>        # 应该成功
/mykb                  # 应该显示当前绑定
/unbindkb              # 应该成功
# 切换到另一个微信号（不是主人），发同样的命令，应该被拒绝
```

### 3.3 跨设备恢复

```bash
# 设备 A 绑定 KB → 清 cookie / 换浏览器 / 重启服务 → 设备 B 扫码
# → 主页面应该直接显示 "知识库：xxx"（自动恢复，不用再绑）
```

### 3.4 鉴权

```bash
# 1. CSRF token 缺失 → POST /ima/bind → 应该 403
# 2. CSRF token 错误 → POST /ima/bind → 应该 403
# 3. cookie 缺失 → POST /ima/bind → 应该 401
# 4. cookie 在但 BotSession 已 evict → POST /ima/bind → 应该 401 / 404
```

### 3.5 并发

```bash
# 同时从两个浏览器绑同一个 ilink_user_id → 应该是后写覆盖前写（upsert），不丢
# 同时从两个浏览器绑不同的 KB → 后写的赢
```

### 3.6 IMA 错误回退

```bash
# 1. /etc/clawbot/ima.env 配错 → /ima/bind 应该 502/500，错误消息不泄露 API key
# 2. 网络断 → /ima/bind 应该 graceful degrade（显示 "IMA 暂不可用"），不崩
# 3. IMA 端 KB 被删 → handle_message 收到错误码 → 应该自动 unbind + 回退默认 KB
```

---

## 4. 设计意图 vs 实现的潜在漂移

下面列几条设计文档 / Web UI 文档里**承诺过但本会话没去 grep 确认**的点：

| 设计承诺 | 文件位置 | 接手 LLM 必须验证 |
|---|---|---|
| `KB_TYPE_MINE_KB` 在 `list_searchable_kbs` 被过滤 | `ima.py:568` | grep `KB_TYPE_INT_MINE` 确认确实走 filter 分支 |
| `kb_id=None` 时回退 `IMA_ILINK_DEFAULT_KB` | `bot.py:_AIWithIma.chat` | 手动传 `kb_id=None` 跑一遍 |
| `local` / `semantic` fallback 不被 `kb_id` gate | `bot.py:_AIWithIma.chat` | 看 KB 切换的分支条件 |
| Web UI 不暴露 `client_id` / `api_key` | `shared_web.py:ima_bind_get` | grep `api_key` / `client_id` 在 HTML 输出 |
| `/healthz` 不暴露任何 KB / user 信息 | `shared_web.py` health handler | grep `healthz` / `kb_id` |
| `RedactFilter` 遮蔽 `kb_id`? | `utils/logging_setup.py` | 实际上 `kb_id` 不算敏感（公开的 KB id），应该**不遮蔽**，但要确认 |
| CSRF `secrets.compare_digest` | `shared_web.py` 新 handler | grep `compare_digest` 确认每条写路径都有 |
| CSRF token 来源 | `BrowserBinding.csrf_token` | 确认是新铸的还是从 cookie 读 |
| `_apply_login` 不写绑定 | `bot_session.py:_apply_login` | grep `bindings.bind\|bindings.unbind` 应该不出现 |

---

## 5. 已知的环境/部署隐患

### 5.1 `CLAWBOT_STATE_DIR` 未设的 fallback

如 §2.6 所述，IMABindings 默认路径是 `.`，会把 `ima_bindings.json` 落到 cwd。**本会话测试已经在仓库根留下了空文件 `ima_bindings.json`** —— 接手 LLM 应该：

1. **删除** `/opt/weixin-ClawBot-API/ima_bindings.json`（空文件，本会话测试残留）
2. **改 IMABindings 默认路径** —— 要么强制要求 `CLAWBOT_STATE_DIR`，要么默认到 `/var/lib/clawbot/state/` 之类固定位置
3. **加 .gitignore**：`echo "ima_bindings.json" >> .gitignore`（防再次污染）

### 5.2 多 worker / 多进程

CLAUDE.md 说"一个 shared service only"。但 ima_bindings.json 文件层共享，**多 worker 部署下 _WRITE_LOCK 失效**。如果部署真的只是单 worker，OK；如果是 gunicorn -w 4 之类，**必须加 fcntl.flock**。

### 5.3 `os.environ` 污染

CLAUDE.md 强调"`shared_runtime.parse_env_file` 不调 `os.environ.update`"。`_build_ima_client(ima_env)` 接手 LLM 必须确认也是同样的承诺 —— 不能让 web UI 请求**意外**写入 `os.environ["IMA_ILINK_CLIENT_ID"]` 然后污染下一个 session。

---

## 6. Git 操作注意

- 当前分支：`cleanup/session-based-20260915`
- 当前 remote：`git@github.com:webheat/weixin-ClawBot-API.git`
- 修改：`bot.py` `bot_session.py` `ima.py` `shared_web.py`
- 新增：`docs/IMA_PER_USER_BINDING.md` `docs/IMA_WEB_UI.md` `utils/ima_bindings.py`
- **污染文件**：`ima_bindings.json`（空文件，**必须删掉** + 加 .gitignore）
- commit 风格：项目近期是 `cleanup(session): ...` / `docs: ...` / `fix(voice): ...` / `fix(session): ...` —— 建议 commit 信息：`feat(ima): per-user KB binding via ilink_user_id`
- push 前先 `git fetch` + 看有没有 remote-side 改动

---

## 7. Review checklist（接手 LLM 直接用）

按优先级排：

- [ ] **P0** 删除 `/opt/weixin-ClawBot-API/ima_bindings.json` + 加 `.gitignore`
- [ ] **P0** Read `shared_web.py:740-900`（三个新 handler + state 扩展 + INDEX_HTML 卡片），重点看 CSRF 顺序、`_build_ima_client` 的 `os.environ` 行为、HTML escape
- [ ] **P0** Read `bot.py:2021-2200`（命令分发 + handle_message 透传），重点看 `from_id == ilink_user_id` 判断、prefix 匹配唯一性
- [ ] **P0** Read `bot.py:2413-2640`（`_AIWithIma` 类），重点看 `kb_id` pop 位置、是否所有 search 调用都吃 `kb_id`、`local`/`semantic` fallback 是否被错误 gate
- [ ] **P1** Read `utils/ima_bindings.py` 全文件（269 行），重点看默认路径、跨实例锁、CLI 子命令
- [ ] **P1** Read `ima.py:520-590`（`list_searchable_kbs`），重点看 `KBT_MINE_KB` 过滤、错误处理
- [ ] **P1** Read `bot_session.py:300-310`（`_apply_login` hook），确认只读
- [ ] **P1** 跑 §3 列的 6 类测试（至少 happy path + 鉴权）
- [ ] **P2** 复核 `docs/IMA_PER_USER_BINDING.md` 和 `docs/IMA_WEB_UI.md` 里的所有 file:line 引用是否对得上
- [ ] **P2** 复核 CLAUDE.md "Critical invariants" 每条有没有破
- [ ] **P2** 复核 `_apply_login` 的 read-only log 不 spam、不泄露

---

## 8. 本会话的盲区（坦白）

- 没有跑过真实微信扫码测试 —— 只能验证代码静态对，**端到端行为未验证**
- 没有看 `bot.py:821`（`extract_message_text`）确认 `/bindkb` 命令走的是同一条 text 抽取路径
- 没有看 `shared_web.py:243-267`（INDEX_HTML）确认子代理加的卡片**没有破坏现有 QR / 配对码 / 切换用户 按钮**
- 没有看 `bot.py:1479-1761`（`message_loop`）确认子代理没动循环结构
- 没有看 `shared_runtime.py` 确认 `_build_ima_client` 的 env 文件读取路径**真的不污染 os.environ**
- 没有看 `tests/` —— 项目没有测试套件（CLAUDE.md 确认），但如果有，应该补 smoke test

**接手 LLM 不要假设本会话的结论"全部 OK"——只假设"语法 / import / 关键词 grep 这一层 OK"**。