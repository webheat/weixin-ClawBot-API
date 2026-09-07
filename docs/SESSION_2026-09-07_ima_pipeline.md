# 会话记录：2026-09-07 IMA 知识库接入 + 路由加固

> 起始动机："调试阶段需要看清用户问题走的是 IMA 还是裸 LLM"，
> 终点状态：4 个开关齐备、链路全闭环、IM/本地双 KB 兜底、llm-only 自标记。

## 时间线速览

| 阶段 | 时间 | 产出 |
|---|---|---|
| 1. 加 AI 路由日志 | 上午 | `_AIWithIma.chat` 输出 `mode=` / `reason=` / `hits=` / `ctx_chars=`；命中 3 状态机 |
| 2. 探查 IMA 实际行为 | 中午 | 3 个子代理并行（GitHub / ima.qq.com / 公开 WebSearch）拼出 OpenAPI 真实路径 |
| 3. 修代码 + 灌库 | 下午 | `ima.py` 4 类 bug 修 + 150 条 Q&A 灌入新 KB（个人 KB） |
| 4. 加 4 个开关 | 傍晚 | `keyword_extract` / `fetch_body` / `local_fallback` / `llm_caveat` |
| 5. push 到 main | 19:00 | commit `b20d8f1`，162 files / 5076+ 行 |

---

## 1. 起因与初始诊断

用户原话："当前应用，在调试阶段，添加日志功能。例如当前用户在 clawbot 中提问时，不知道是否查询了 IMA 知识库，还是只是向 LLM 提问，还是两者都有。需要通过日志明确。"

最初对 `_AIWithIma.chat` 加了一行 INFO 日志：

```python
log_ai.info("ai route mode=%s reason=%s ...", mode, reason, ...)
```

但立即发现：**实际生产中 IMA 永远 `hits=0`、`ctx_chars=0`**，LLM 在"裸答"。排查发现：
- `docs/IMA_KB.md §5` 已记录 search 不返回 body（`highlight_content` 经常空）
- 用户当前默认 KB `GG-7KUJK-...` 里只有 1 篇文档《AIOT平台》—— 5 段问句全部 0 命中
- `ima.py:281` 的 import_doc 路径 `ima.openapi.v1.ImportDoc` 实际是 404（gRPC 风格）
- `ima.py:568` 的 `data.get("media_id", "")` 实际响应字段是 `note_id`

也就是说：**IMA 写入端在生产代码里就是坏的，从未真正工作过**。这引发了后续整个会话的"用子代理去找官方文档"。

---

## 2. 三个子代理并行调研

`ImaClient.import_doc` 路径 404 之后，需要确定真实端点。3 个子代理从不同角度并行查：

| Agent ID | 角度 | 关键发现 |
|---|---|---|
| `a32213abe1464e81c` | 公开 WebSearch 找 ima OpenAPI 官方文档 | 拿到 `github.com/tencent/ima-openapi-docs`、`openakita/openakita/.../kb-api.md`、`EdwardWason/web-to-FIM/.../ima-setup.md` 候选 |
| `af15e53d0e9125ea0` | GitHub 找 proto / spec | `Tencent/WeKnora`（IMA 内核开源重写版，但类型用 string 常量）、`Tencent/openclaw-weixin` 不含 ima |
| `a605eb931abb79279` | ima.qq.com 控制台 + cloud.tencent.com 文档 | WebFetch 全部被环境拦截，结论"官方未公开" |

**WebFetch 在本环境被企业策略拦截**（`github.com` / `raw.githubusercontent.com` / `ima.qq.com` 等），所以子代理全部基于 WebSearch 摘要 + 本地 ima.py 旁路交叉印证。三家一致的核心结论：

- **ImportDoc 真实路径**：`POST /openapi/note/v1/import_doc`（snake_case + 斜杠），不是 `ima.openapi.v1.ImportDoc`
- **Headers**：`ima-openapi-clientid` + `ima-openapi-apikey`（不是 `Authorization: Bearer`）
- **`type` 字段**（`create_knowledge_base`）：字符串枚举 `KBT_MINE_KB` / `KBT_SHARED_KB` / `KBT_SUBSCRIBED_CREATE_KB` 或整型 1001/1002/1004
- **search 不返回 body**（`highlight_content` 经常空）→ 需另调 `get_doc_content(note_id)` 拿正文

## 3. 端到端实测验证（与文档交叉印证）

子代理结论后用现有凭证**实测**（不是只信文档）：

| 实测 | 结论 |
|---|---|
| `curl POST /openapi/note/v1/import_doc` body 包含 `？` | 返回 `service codec Unmarshal: unexpected EOF` |
| 改 `data=json.dumps(...)` 为 `json=` kwarg | ✅ 成功 → 确认是 Python 端编码问题 |
| `create_knowledge_base` type=`1`（旧版小整数）| 返回 `code=51` "must be in list [KBT_MINE_KB KBT_SHARED_KB KBT_SUBSCRIBED_CREATE_KB]" |
| type=`"KBT_MINE_KB"` / `"KBT_SHARED_KB"` / `1001` / `1002` | 全部成功（字符串/整型双形式都接受） |
| type=`"KBT_SUBSCRIBED_CREATE_KB"` / `1004` | "未开通知识号，无法创建订阅知识库" |
| `get_doc_content(note_id)` | 返回 `data.content`（完整 Markdown；换行被服务端规范化） |
| `search_knowledge` 对 `media_type=11` (Note) 命中 | `highlight_content` 始终为空字符串（40+ 分钟后仍空） |

最关键的两个发现：
- **`import_doc` 在 production 代码里 404 是个一直存在的 bug**——CLAUDE.md 提到 ima.py 是 "Mirrors Go engine.retrieveMulti"，但实际 REST 端点写错了
- **`get_doc_content` 是绕开 §5 关键坑 2 的唯一方法**——`highlight_content` 不会自己填

---

## 4. 修过的 4 类 bug（ima.py / bot.py）

### 4.1 `_post()` 显式 UTF-8 字节

旧代码用 `data=json.dumps(payload, ensure_ascii=False)`（Unicode str），body 里有 `？`/`：` 等全角标点时，ima 服务端 Go decoder 报 EOF。改：

```python
body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
headers = {"Content-Type": "application/json; charset=utf-8"}
```

### 4.2 `PATH_IMPORT_DOC` 路径修正

```diff
- PATH_IMPORT_DOC = "ima.openapi.v1.ImportDoc"          # gRPC 风格 404
+ PATH_IMPORT_DOC = "openapi/note/v1/import_doc"        # 真实 REST 路径
```

### 4.3 `import_doc` 响应字段修正

```diff
- return data.get("media_id", "")                       # 拿到空串
+ return data.get("note_id") or data.get("media_id") or ""
```

### 4.4 新增 `add_knowledge()` / `get_doc_content()` / `KBT_*` 枚举

- `add_knowledge(kb_id, title, note_id=..., media_type=11)`：把 note 挂到 KB
- `get_doc_content(note_id)`：拿 note 完整正文（仅作者本人能拿，第三方会 210005）
- `ImaClient.KBT_MINE_KB` / `KB_TYPE_SHARED` / `KB_TYPE_SUBSCRIBED` + 整型别名

### 4.5 media_id 解析（bot.py）

`search_knowledge` 返回的 `media_id` 格式：
```
note_<32-hex>_<16-digit-note-id><16-digit-folder-id>
```

最初 regex 写成 `^note_[^_]+_(\d+)_` 失败——`_` 之后没有分隔符。改成：
```python
re.search(r"^note_[0-9a-f]{32}_(\d{16})", h.media_id or "")
```

---

## 5. 4 个新开关（默认全部 OFF，老用户零影响）

```bash
# 1. search 前用 LLM 抽 1-3 个关键词（让"IMA是什么？"类自然问句能命中）
IMA_ILINK_KEYWORD_EXTRACT=1

# 2. search 后逐条 get_doc_content 拿正文注入 prompt
IMA_ILINK_FETCH_BODY=1

# 3. IMA 0 命中时查本地 docs/knowledge/*.md 兜底
CLAWBOT_LOCAL_FALLBACK=1
CLAWBOT_LOCAL_KB_DIR=docs/knowledge

# 4. mode=llm-only 时给 LLM 拼"无参考资料"提示，让它自我降自信 + 加 "（未参考知识库）" 标记
CLAWBOT_LLM_CAVEAT=1
```

### 5.1 路由决策模式

| mode | reason | 触发条件 | 上下文 |
|---|---|---|---|
| `llm+ima` | `hits-injected` | IMA 检索命中 + 拼接 ctx | 5 段 Q&A 全文 |
| `llm+local` | `local-fallback` | IMA 0 → 本地 .md 命中 | 5 段 Q&A 全文 |
| `llm-only` | `ima-no-match` | IMA 0 + 本地 0 | 无（拼 caveat） |
| `llm-only` | `ima-search-failed` | IMA API 报错 | 无 |
| `llm-only` | `ima-not-configured` | 凭据缺失 | 无 |

### 5.2 LLM 关键词抽取 prompt 设计权衡

最初 prompt："1-3 个关键词，空格分隔"。但 LLM 在 `"微信的IMA知识库服务。"` 上输出 `"IMA知识库"`（连写）→ IMA 当 phrase 搜 → 0 命中。

修法（"最稳"组合）：
- **Prompt 强约束单字**（每个词/字必须独立，"ima知识库"是错的，"ima 知识库"才是对的）
- **代码兜底**：`_extract_keywords` 返回 `list[str]`（按相关性排序），`_search_ima_merged` 逐 term 搜 + 合并去重
- **加 1 次小 LLM 调用**（~1.5s 抽取）+ 1~2 次 IMA 搜索（每词 ~800ms）

### 5.3 本地 MD 兜底（方案 C）

为什么选这个：用户原话"毕竟用户提问之后，最终还是要有答案反馈的"。IMA 0 命中时不能"沉默"。

- `utils/local_kb.py`：零依赖 substring 索引，标题权重×2，命中次数 ×10 + 命中 term 种类数
- `utils/seed_ima_kb.py`：同时写 IMA + 本地 .md，幂等（按 title 去重）
- 路由日志：`[local] query='...' hits=N titles=[...]`，INFO 级别

---

## 6. 端到端验证 trace

`alice` 进程（PID 3654473，活跃）的 4 种 trace：

```
# 场景 1: "重连" → IMA 命中
recv msg from=m.wechat type=text len=2 preview='重连'
keyword_extract ok q_in='重连' terms=['重连'] elapsed_ms=0
ima search q='重连' hits=10 (truncated to 5) elapsed_ms=887
fetch_body total=5 enriched=5 elapsed_ms=1319
ai route mode=llm+ima reason=hits-injected hits=5 ctx_chars=1179
reply sent chars=75 preview='日志在 `logs/clawbot.log`（单租户）或者 ...'

# 场景 2: "verify_code 是干嘛的？" → IMA 0 → 本地兜底
recv msg from=m.wechat type=text len=10 preview='verify_code 是干嘛的？'
keyword_extract ok q_in='verify_code 是干嘛的？' terms=['verify_code']
ima search q='verify_code' hits=0
local_kb fallback hits=5 (ima was 0)
ai route mode=llm+local reason=local-fallback hits=5 ctx_chars=1344

# 场景 3: "clawbot" → IMA 命中
recv msg from=m.wechat type=text len=7 preview='clawbot'
keyword_extract ok q_in='clawbot' terms=['clawbot']
ima search hits=100 (truncated to 5)
ai route mode=llm+ima reason=hits-injected hits=5 ctx_chars=1159
reply sent chars=75 preview='日志在 `logs/clawbot.log`（单租户）或者 ...'

# 场景 4: "量子纠缠" → 全 0 → llm-only (含 caveat)
recv msg from=m.wechat type=text len=4 preview='量子纠缠'
keyword_extract ok q_in='量子纠缠' terms=['量子纠缠']
ima search q='量子纠缠' hits=0
local_kb fallback none (ima=0, local=0)
ai route mode=llm-only reason=ima-no-match
ai route caveat injected (llm-only mode)
```

LLM 拿到 1159 字符的 5 段 Q&A 全文后，输出准确答案（"日志在 logs/clawbot.log..."），证据是用户只发了 `clawbot` 一个词，LLM 不可能自己知道这个项目细节。

---

## 7. 配置文件最终状态

`/opt/weixin-ClawBot-API/.env` 和 `/etc/clawbot/ima.env`：

```bash
IMA_ILINK_DEFAULT_KB=-jDsbK-UEdF5xYyP7kZaoWbMrTyDUDuMNuvOt3oorvI=
IMA_ILINK_TIMEOUT=15
IMA_ILINK_SEARCH_LIMIT=5
IMA_ILINK_FETCH_BODY=1
IMA_ILINK_KEYWORD_EXTRACT=1
CLAWBOT_LOCAL_FALLBACK=1
CLAWBOT_LOCAL_KB_DIR=docs/knowledge
CLAWBOT_LLM_CAVEAT=1
```

默认 KB 从老的 `GG-7KUJK-...`（订阅型，只有一篇 AIOT 文档）切到新的 `-jDsbK-...`（个人 KB，150 条 Q&A）。

---

## 8. 教训与未来工作

### 教训
- **生产代码里的 bug 可能沉默多年**——`import_doc` 404 一直在，但没人用过写入路径。教训：写完 IMA 集成后没做过 e2e 测试
- **第三方文档不可全信，必须实测**——子代理给的"type 是整数 1004"和实际 `probe` 返回的 `must be in list [KBT_*]` 矛盾。`curl` 是最便宜的验证
- **WebFetch 不可达时不靠盲信**——三个子代理都明说"没亲眼看到页面"，结论仅供参考。EMA（可执行最小假设）原则：用现有凭证打一发就知道
- **关键路径必须有可见日志**——`mode=llm-only reason=ima-no-match ctx_chars=0` 一行就把 4 类问题定位了

### 未来工作
- **per-user 对话历史**（TODO.md P0）—— 当前 `contexts` 只在 bot 内存，进程重启即丢
- **`update_knowledge` / `delete_knowledge`** 端点（agent 1 提到存在）—— 用户想"未来还会不断要增加内容"，可以加 `utils/update_ima_kb.py` 一键改/删
- **Obsidian 双向同步**（方案 A）—— 把 Obsidian 当编辑源、IMA 当检索源，内容双向同步
- **IMA 索引进度查询**—— `highlight_content` 5-15 分钟才填满，需要给种子脚本加"等索引完成"选项
- **rerank 调优**—— `IMA_ILINK_RERANK=1` 已实现但默认 OFF，hits>1 时可让 LLM 二次排序
- **Caveat 行为校准**—— 需观察真实 DusAPI/DeepSeek 是否严格在 llm-only 时输出 "（未参考知识库）" 标记，必要时在 prompt 里加更多约束

---

## 9. 关联 commit

- `b20d8f1` —— 本次会话主 commit（162 files / 5076+ 行）
- `b9c6d93` —— `feat(logging): 在主链路关键节点加结构化日志`（上一会话留下的 logging 基建）

## 10. 关联文件

| 路径 | 作用 |
|---|---|
| `bot.py:1859-2168` | `_AIWithIma.chat` 4 模式路由 + keyword_extract + fetch_body + local_fallback + caveat |
| `bot.py:21-58` | `_KEYWORD_EXTRACT_PROMPT` / `_LLM_ONLY_CAVEAT_PROMPT` 两个关键 prompt |
| `ima.py:280-290` | `PATH_*` 端点 + `KB_TYPE_*` 枚举 |
| `ima.py:555-620` | `import_doc` / `add_knowledge` / `get_doc_content` 三个写读方法 |
| `utils/seed_ima_kb.py` | 一键灌 IMA + 本地双源，幂等 |
| `utils/local_kb.py` | 零依赖本地 .md 索引 |
| `docs/knowledge/qa-001..150-*.md` | 150 条 clawbot 项目演示 Q&A |
| `docs/IMA_KB.md §5` | 关键坑 2 文档（已补充 get_doc_content 方案） |
