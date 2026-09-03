# 腾讯 ima 知识库集成

> `bot.py` 在每次 LLM 调用前会自动去腾讯 ima 拉相关知识片段，拼到
> system prompt 后再问 LLM。本文档记录**实战验证**出来的行为、坑与运维
> 步骤，供未来换 KB / 调试时参考。

## 1. 接入点

- 模块：`ima.py`（与 `dusapi.py` / `deepseek.py` 同风格：dataclass +
  同步 `requests` + 5 次重试 + 未配置时静默返回空）
- 启动挂钩：`bot.py` 的 `_AIWithIma` 包装层。`handle_message` 调
  `ai.chat(text)` 不变；包装层内部先做 `ImaClient.search_knowledge`，
  把命中的片段以 `### 参考资料 N：<title>\n<snippet>` 格式追加到
  system prompt
- 凭据：项目**首个**使用 `.env` 的消费者，由 `python-dotenv` 加载
  （`requirements.txt` 增加了 `python-dotenv` 依赖）

## 2. `.env` 关键变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `IMA_ILINK_BASE_URL` | 否 | 默认 `https://ima.qq.com` |
| `IMA_ILINK_CLIENT_ID` | **是** | 在 https://ima.qq.com/agent-interface 创建智能体后拿 |
| `IMA_ILINK_API_KEY` | **是** | 同上 |
| `IMA_ILINK_DEFAULT_KB` | 否 | 默认检索的知识库 ID，**支持逗号分隔多个**（自动去重） |
| `IMA_ILINK_TIMEOUT` | 否 | 单次请求超时（秒），默认 15 |
| `IMA_ILINK_SEARCH_LIMIT` | 否 | 单次返回条数上限，默认 5 |

未配置 `CLIENT_ID` / `API_KEY` 时 bot 启动会打印 `[ima] 知识库检索未启用`，
LLM 调用**完全不受影响**，只是少了知识库上下文。

## 3. 列出当前账户下所有知识库

```bash
./venv/bin/python utils/list_ima_kb.py
```

输出形如：

```
=== ima 知识库（共 7 个）===
  1.   _Ci0fxH0LZX0uoCQVLiujGiDcvsl12KEYRk_Xt4zRgw=  1000个AI工具推荐（持续更新） | 普通成员 | 我加入的订阅知识库
  2.   AUNyKAq7e0i2iguEL-XVEa6xcrbqxhr3yeonvLdFdJ0=  AI一人公司内容自动化行动营-AI助教 | 普通成员 | 共享知识库
  ...
  6. ★ TnJsS0t-ULm4ubQ63ySeD0YJJ8R-iBn-_PUZPZjiyv8=  追梦成真的知识库 | 创建者 | 个人知识库
```

`★` 标记的是 `.env` 里当前配置的 `IMA_ILINK_DEFAULT_KB`。

### 切换默认 KB（最常用操作）

```bash
# 模糊匹配唯一名字 → 打印可粘到 .env 的那一行 + 备份展示
./venv/bin/python utils/list_ima_kb.py --pick AI工程化

# 一步到位：原地改写 .env
./venv/bin/python utils/list_ima_kb.py --pick AI工程化 --update
```

其它子命令：`--query <子串>` 过滤，`--json` 给后续脚本用，
`--env <path>` 指定非默认 `.env` 路径。

### 故障排查快捷方式

| 现象 | 怎么查 |
|---|---|
| 启动时 `[ima] 知识库检索未启用` | `grep IMA_ILINK_ .env` 看是否漏配 |
| 调用时 `[ima] 检索异常: code=40001 ...` | 凭据错，登录 https://ima.qq.com/agent-interface 重新复制 |
| 调用时 `code=220004 invalid knowledge_base_id` | KB ID 错 / KB 已下架 / KB 是"个人知识库"（见 §6） |
| 检索返回 0 命中 | 见 §4、§5 |

## 4. ⚠️ 关键坑 1：`search_knowledge` 是**精确词匹配**，不是语义检索

实测同一 KB 内只有 1 篇文档（标题 `AIOT平台`）：

| 查询 | 命中数 |
|---|---|
| `AIOT平台` | ✅ 1 |
| `AIOT` | ✅ 1 |
| `AIOT 平台` | ✅ 1 |
| `AIOT平台是什么` | ❌ 0 |
| `AIOT平台介绍` | ❌ 0 |
| `介绍一下AIOT平台` | ❌ 0 |

**结论：腾讯 ima OpenAPI 的 `search_knowledge` 走的是关键词匹配，不是
embedding 检索。** 用户拿自然语言问问题，命中率会非常低。

实际缓解办法：

- 让 LLM 在拿到 `search_knowledge` 的结果后，**只用**匹配到的标题/片段
  作为线索；当 0 命中时回退到纯 LLM 回答（当前 `_AIWithIma` 默认行为）
- 上传 KB 内容时尽量用 Q&A 短句而不是长文（便于切出可命中的关键词）
- 长期方案：等腾讯支持向量检索，或自建一层 embedding + rerank

## 5. ⚠️ 关键坑 2：`search_knowledge` **不返回文档正文**

实际响应里每条命中只有：

```json
{
  "media_id": "note_...",
  "title": "AIOT平台",
  "parent_folder_id": "...",
  "highlight_content": "",   // ← 经常是空字符串
  "media_type": 11
}
```

**没有 `content` / `snippet` / `score` / `url` 字段。** Go 版本的
`internal/ima/types.go` 里 `SearchHit.Content/Snippet/URL` 在真实接口里
全是空的。

`build_context_prompt()` 在 `display_snippet` 为空时**整块不注入**到
prompt，LLM 只会看到 "### 参考资料 N：<title>" 而没有任何内容。**bot
现阶段只能告诉 LLM"哪些文档相关"，不能直接喂文档正文。**

如要拿到正文，目前唯一接近的途径是：

1. `get_knowledge_list` 拿到 `media_id`
2. 再调一个未公开的"读文档内容"端点（`ima.openapi.v1.ImportDoc` 是写
   入方向，不是读取）

短期建议：在 ima 后台用 Q&A 对的形式建 KB，每条 `highlight_content`
本身就是答案的浓缩（虽然经常空）。长期建议用其他 RAG 方案。

## 6. ⚠️ 关键坑 3："创建者 / 个人知识库" 不能被 `search_knowledge`

`search_knowledge_base`（列 KB）能看到个人 KB，但 `search_knowledge`
会报：

```
code=220004 message=invalid knowledge_base_id.
You MUST correct the value before retrying. Do not retry with the same value.
```

实测 `role=创建者` + `base_type=个人知识库` 的 KB 全部 220004，**共享**
或**订阅**的 KB 都能正常检索。`.env` 里默认 KB 选了个人 KB 的话，
`_AIWithIma` 会在每次对话时打 6 行 `code=220004` 重试日志（5 次重试），
不影响主流程但很吵。

`utils/list_ima_kb.py` 现在没有标出"哪些 KB 是 search 友好的"，未来可
以在 `--pick` / `--update` 之前加一个"个人 KB 不可检索"的提示。

## 7. ⚠️ 关键坑 4：API 响应字段名因端点而异

下面三个端点都返回"列表"，但 list 字段名各不相同 —— 复制 Go 代码时
容易踩：

| 端点 | list 字段名 |
|---|---|
| `search_knowledge_base` | `info_list` |
| `get_addable_knowledge_base_list` | `addable_knowledge_base_list` |
| `get_knowledge_list` | `knowledge_list` |
| `search_knowledge` | `info_list` 或 `list`（两套历史 API） |

另外两条元数据字段名也不一致：

- `search_knowledge_base` 列表项字段：`kb_id` / `kb_name` / `role_type` / `base_type`
- `get_addable_knowledge_base_list` 列表项字段：`id` / `name`（更短）

`KnowledgeBaseSummary.from_dict` 已经两路兼容。

## 8. ⚠️ 关键坑 5：成功响应是套娃结构

```json
{ "code": 0, "msg": "success", "data": { "...真正数据..." }, "request_id": "..." }
```

`code != 0` 时整段是错误。`code == 0` 时**真正的载荷在 `data` 里**。
`ima.py._unwrap_envelope()` 已经自动拆开，但任何后续接手的人改这块
逻辑时务必先打一条 `print(data)` 看清楚结构。

## 9. 凭据安全

- `.env` 已被 `.gitignore` 排除，**不会**进 git
- `config.json` 同样被忽略（含 LLM API key），由 `load_or_create_config()`
  启动时交互生成
- `weixin_state.json` 含 iLink token / 上下文，也被忽略
- `requirements.txt` 加了 `python-dotenv` 是项目首个 env 消费者，新增
  模块时可直接 `from dotenv import load_dotenv; load_dotenv(".env")`

## 10. 已知遗留 / 未来工作

- [ ] `get_knowledge_list` 翻页已支持 cursor / `is_end`，但
  `search_knowledge_base` / `addable_knowledge_bases` 单次最多 20 条
  （API 硬上限），如果账户下 KB 数 > 20，需要手动 `cursor` 翻页
- [ ] `utils/list_ima_kb.py` 可以加 `--pick` 时检测"个人 KB 不可检索"
  并拒绝 `--update`
- [ ] `_AIWithIma` 当前对每条消息都同步检索。10k 消息规模时建议加
  TTL 缓存（同一 `from_id + 相同 query 字符串 → 5s 复用）
- [ ] 如果腾讯后续支持向量检索，强烈建议把 `build_context_prompt` 的
  数据源从 `search_knowledge` 切到那个新端点，并加 rerank
