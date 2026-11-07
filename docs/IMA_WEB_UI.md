# IMA 知识库绑定 — Web UI 使用说明

> 本文档说明 **Per-user IMA 知识库绑定** 功能的 Web UI 部分。配套设计文档：[`IMA_PER_USER_BINDING.md`](./IMA_PER_USER_BINDING.md)。如果两边有出入，以 `IMA_PER_USER_BINDING.md` 为准。

---

## 1. 这个 Web UI 是给谁用的

给**已经登录过 ClawBot 的 bot 主人**用。Bot 主人的定义：

- 用手机微信扫描了 ClawBot 登录二维码的那个微信号
- 在系统内部对应 `ilink_user_id`（`@im.wechat` 后缀，微信号级别稳定 ID —— 见 `docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410`）
- 一旦绑定，所有发给这个 bot 的消息都会自动查询该 IMA 知识库

> ⚠️ **不是**给聊天对方用的。聊天对方（`from_user_id`）没有自己的 IMA KB，他们发来的问题一律查 bot 主人的 KB。

---

## 2. 入口地址

| 场景 | URL |
|---|---|
| 默认（`CLAWBOT_WEB_PORT=18300`） | `http://<host>:18300/clawbot/` |
| 子路径部署（`CLAWBOT_WEB_PREFIX=/clawbot`） | `http://<host>:<port>/clawbot/` |
| 健康检查（无需鉴权） | `http://<host>:18300/clawbot/healthz` |

前缀 `/clawbot` 在 `shared_web.py:35` 的 `DEFAULT_PREFIX` 定义，可通过 `CLAWBOT_WEB_PREFIX` 覆盖。

---

## 3. 现有 Web UI 的样子（基线）

任何匿名浏览器访问 `/clawbot/` 看到的是单卡片：

```
┌──────────────────────────────┐
│     ClawBot 控制台            │
│  扫码创建一个微信会话           │
│      [ 扫码登录 ]              │
└──────────────────────────────┘
```

点 "扫码登录" → `POST /clawbot/start` → 服务端铸一个 URL-safe session token + 写两个 cookie：

| Cookie | TTL | 用途 |
|---|---|---|
| `clawbot_session`（默认名） | 8 小时 | 浏览器 ↔ session token 绑定 |
| `clawbot_session_resume` | 30 天 | 浏览器清掉主 cookie 后用它恢复 session |

详细见 `shared_web.py:36-41`（`DEFAULT_SESSION_TTL` / `DEFAULT_RESUME_TTL`）。

登录后页面是 QR + 配对码 + "切换用户" 按钮，JS 每秒轮 `/state`。

**当前页面没有 IMA 相关 UI** —— 子代理 2 调研确认。`INDEX_HTML` 是 `shared_web.py:243-267` 里的单字符串常量。

---

## 4. 三个绑定入口

### 4.1 主页面卡片（推荐路径，Web UI）

登录成功后（`status === 'logged_in'`），`INDEX_HTML` 多出一块：

```
┌────────────────────────────────────┐
│ 当前知识库：                         │
│   • 未绑定 → 回退到 IMA_ILINK_DEFAULT_KB │
│   • 产品 FAQ（kb_id: 1098765432）     │
│                                        │
│           [ 更换 ]   [ 解绑 ]         │
└────────────────────────────────────┘
```

- **更换** → 跳到 `/clawbot/ima/bind`
- **解绑** → `POST /clawbot/ima/unbind`，恢复回默认 KB，刷新页面

### 4.2 `/clawbot/ima/bind` 独立绑定页

完整路径：`GET /clawbot/ima/bind`

页面渲染流程：

1. 服务端调 `ImaClient.list_searchable_kbs()`（在 `ima.py:520`），拉回所有 KB
2. **过滤掉 `KBT_MINE_KB`（个人知识库）** —— 因为 search 接口对它返 `code=220004`（见 `docs/IMA_KB.md:126-139` 关键坑 3）
3. 只剩 `KBT_SHARED_KB`（团队）+ `KBT_SUBSCRIBED_CREATE_KB`（知识号）
4. 按 `kb_name` 升序排序渲染表单

```
┌────────────────────────────────────┐
│      选择你的 IMA 知识库              │
│                                        │
│  ( ) 产品 FAQ [知识号]                 │
│  ( ) 团队手册 [团队]                    │
│  (•) 产品 FAQ ← 当前已绑定             │
│                                        │
│           [ 提交 ]   [ 取消 ]         │
└────────────────────────────────────┘
```

提交 → `POST /clawbot/ima/bind {kb_id}` → 写入 `CLAWBOT_STATE_DIR/ima_bindings.json` → 重定向回主页。

#### 鉴权（透明化）

- 必须持有 `clawbot_session` cookie（已被现有 `binding()` 助手校验，`shared_web.py:329-338`）
- 必须带 `X-CSRF-Token`（已有机制，`shared_web.py:79` 铸 + `INDEX_HTML` 模板里的 `__CSRF__`）
- **绑定写入时用的是 `BotSession.ilink_user_id`**，不是 cookie 里的 session token。这意味着：
  - 即使 cookie 是新发的（30 天 resume 后浏览器自动恢复），绑定仍然生效
  - 跨浏览器/跨设备/跨重启都有效（绑定文件独立于 state 文件）

### 4.3 WeChat 命令 `/bindkb`（手机端备选）

手机端用户如果不便访问 web UI，可以直接在微信里给 bot 发命令：

| 命令 | 行为 |
|---|---|
| `/bindkb` | bot 列出所有可绑定的 KB（同样过滤掉 `KBT_MINE_KB`），等用户选 |
| `/bindkb <kb_id>` | 直接绑定该 ID，回复 `✅ 已绑定 KB: <kb_name>（<kb_id>）` |
| `/unbindkb` | 解绑当前 KB，回退默认 |
| `/mykb` | 查询当前绑定状态 |

#### 鉴权（透明化）

- 校验 `from_id == self.ilink_user_id`（即**只有 bot 主人本人**发命令才生效）
- 别人（`from_id` 是其他微信号）发 `/bindkb ...` 会被拒绝 + log warn：
  > ❌ 权限不足：只有 bot 主人能绑定知识库
- 不需要 CSRF（命令 channel 本身就是 WeChat 端到端加密过的）

---

## 5. 跨设备恢复的关键流程

**核心约束**：浏览器 cookie 会丢、`ilink_user_id` 不会丢。

```
设备 A（已绑定）：
  ┌─ 浏览器 cookie → BotSession(ilink_user_id=o9...@im.wechat)
  ├─ ima_bindings.json: { o9...@im.wechat: {kb_id: "X", ...} }
  └─ bot 收到任何消息 → lookup(o9...) → kb_id=X → search_knowledge(knowledge_base_id=X)

设备 B（同一微信号，新浏览器）：
  ┌─ 浏览器没有 cookie → 扫码 → 铸新 session token → BotSession(ilink_user_id=o9...@im.wechat)
  ├─ ima_bindings.json: { o9...@im.wechat: {kb_id: "X", ...} } ← 同一个文件
  └─ bot 收到任何消息 → lookup(o9...) → kb_id=X ✅ 自动恢复
```

这就是为什么绑定文件**必须**独立于 `weixin_state_<session_token>.json`（后者随 cookie 死）。

---

## 6. 用户视角的完整流程（首次绑定）

```
第 1 步：访问 Web UI
   浏览器 → http://host:18300/clawbot/ → 看到 "扫码登录" 按钮
   点 → POST /clawbot/start → 铸 session token → set cookie → 302 回 /

第 2 步：扫码登录
   主页显示 QR → 手机微信扫 → 配对码 → 提交 verify_code → status='logged_in'

第 3 步：绑定 KB
   主页出现 "当前知识库：未绑定" 卡片 → 点 [更换]
   → /clawbot/ima/bind → 看到 KB 列表（已过滤 KBT_MINE_KB）
   → 选 "产品 FAQ" → [提交]
   → POST /clawbot/ima/bind {kb_id: "..."}
   → ima_bindings.bind(self.ilink_user_id, kb_id, ...)
   → 写文件 CLAWBOT_STATE_DIR/ima_bindings.json（原子写 + 文件锁）
   → 302 回主页，卡片变成 "当前知识库：产品 FAQ"

第 4 步：使用
   微信里问 "X 产品怎么用？"
   → handle_message → lookup(ilink_user_id) → kb_id="产品 FAQ"
   → ai.chat(text, kb_id=kb_id)
   → _AIWithIma.chat → ImaClient.search_knowledge(knowledge_base_id="产品 FAQ")
   → logs/clawbot_shared.log: mode=llm+ima hits=... kb_id="产品 FAQ"  ✓

第 5 步：解绑（可选）
   主页 → 点 [解绑]
   → POST /clawbot/ima/unbind → ima_bindings.unbind(self.ilink_user_id)
   → 删文件里这一条 → 回退到 IMA_ILINK_DEFAULT_KB
```

---

## 7. 故障 / 边界情况

| 场景 | 表现 | 原因 / 处理 |
|---|---|---|
| 刚 mint session token，还没扫码 | `self.ilink_user_id == ""` | 主页卡片显示 "请先完成扫码登录后再绑定知识库"；`/ima/bind` 返 412 Precondition Failed |
| KB 在 IMA 端被删除 | 下次 search 返 `code=???` | `_AIWithIma` 走 `mode=llm-only`（已有兜底），并 `ima_bindings.unbind(...)` 自动清掉死绑定 + log warn |
| 用户多个 bot 主人账号切换 | 同一 session 重新扫码 → `ilink_user_id` 变 | 主页卡片自动刷新（按新 `ilink_user_id` lookup ），新主人可重新绑 |
| 跨设备冲突（同一 ilink_user_id 同时在 A、B 设备登录） | 后绑定的覆盖先绑定的 | `bind()` 是 upsert；记 `bound_at` / `bot_id_at_bind` 供审计 |
| 多人共享一个浏览器 cookie（极端） | 不允许 | cookie `httponly + samesite=Lax`，普通 XSS 偷不到 |

---

## 8. 隐私 / 安全约束

| 项 | 设计 |
|---|---|
| API Key / Client ID | **不**暴露给 web UI，只在服务端读 `/etc/clawbot/ima.env`（`shared_runtime.py:68-96` 不污染 `os.environ`） |
| KB 列表 | 只暴露 `{kb_id, kb_name, kb_type_name}`，**不**暴露 KB 内容、所有者、订阅人数 |
| CSRF | 复用 `BrowserBinding.csrf_token`（`shared_web.py:79`） |
| Cookie Secure | 自动跟随请求 scheme；`CLAWBOT_TRUST_PROXY=1` 时跟随 `X-Forwarded-Proto` |
| 速率限制 | `SlidingRateLimiter`（`shared_web.py:294`）默认 `5/60s` 防止 `/ima/bind` 被刷 |
| Log 脱敏 | `RedactFilter`（`utils/logging_setup.py:50-65`）自动遮蔽 `ima_kb_id`、API Key、bot_token 等 |
| `/healthz` 不暴露任何 user/kb 信息 | 见 `CLAUDE.md` "Critical invariants" 关于 Portal 隐私那条 |

---

## 9. CLI 调试入口（开发用）

`utils/ima_bindings.py` 自带三个子命令，便于直接在 shell 看绑定状态、排查问题：

```bash
./venv/bin/python utils/ima_bindings.py list                       # 列出所有绑定
./venv/bin/python utils/ima_bindings.py lookup o9...@im.wechat    # 查单个
./venv/bin/python utils/ima_bindings.py unbind  o9...@im.wechat    # 手动解绑
./venv/bin/python utils/list_ima_kb.py                            # 列 IMA 端所有 KB（含 KBT_MINE_KB，用于诊断）
```

注意：
- `ima_bindings.py` 的子命令是给运维用的，**不**做权限校验（不在浏览器上下文中）
- 正式解绑应该走 `/ima/unbind` 或 `/unbindkb`，会记 `bound_at` / `bot_id_at_bind` 审计字段

---

## 10. 参考

- 设计文档：`docs/IMA_PER_USER_BINDING.md`
- IMA 整体：`docs/IMA_KB.md`（关键坑 3：`KBT_MINE_KB` search 返 220004）
- 稳定性依据：`docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` + 协议文档 `weixin-openclaw-api-py-docs.md:1051`
- Web UI 现状：`shared_web.py:243-267`（`INDEX_HTML`）+ `:329-338`（`binding` 鉴权助手）+ `:619-627`（路由注册）
- 持久化：`utils/ima_bindings.py`（新建）
- AI 路由：`bot.py:_AIWithIma.chat`（在 `bot.py:2374` 透传 `knowledge_base_id`）