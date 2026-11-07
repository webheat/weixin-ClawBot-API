# IMA 批量导入实战 Playbook

> 沉淀自 2026-09-07 把 `/opt/business-lang/02.客服沟通话术汇总/`（11 个文件，5 种格式）灌进腾讯 ima 个人 KB 的全过程。所有坑都附带修法，照搬即可用于下一批语料。

## ⚠️ 战略定位（写在最前面，2026-09-07 实测后总结）

**腾讯 IMA 不是 chatbot RAG 后端；它是为 AI Agent + 人类设计的个人/团队知识管理工具。**

来源：ima.qq.com/agent-interface 的官方"使用场景"——所有例子都是 "帮我记下..."、"帮我查..."、"帮我监控..."，调用方是 **小龙虾 AI agent**（不是 chatbot 实时对话）。它做的是：

- 给 AI agent 提供 `read_note / write_note / search_kb / import_url` 等工具调用
- 给人类提供微信扫码登录的移动端/Web 知识库
- 给定时任务提供"每日自动归档"能力
- 与 OpenClaw / 企业微信 / 公众号互通

**我们之前错用的地方**：把 IMA 当成 clawbot 微信聊天机器人的 RAG 检索引擎，撞上了它的天花板（关键词匹配、3 字长词不分词、限流、无 delete API）。

**正确分工（2026-09-07 实测得出）**：

| 场景 | 用谁 | 理由 |
|---|---|---|
| clawbot 实时回话 RAG | **LocalKBIndex**（本地） | substring 命中 100%、5ms、零依赖 |
| 用户说"帮我记下..." → 存个人 KB | **IMA** | IMA 本行：随手记、跨设备同步 |
| 用户翻"我去年记的合同" | **IMA** | 个人/团队检索，关键词足够 |
| 运营主管在手机上查话术库 | **IMA** | IMA 移动端入口，无可替代 |
| clawbot 每天自动归档 | **IMA** | IMA 定时任务场景天然适配 |
| 批量处理 ~200 文档生成候选 | **本地** | 不被 IMA 限流，免费 |
| 真正的语义检索（理解"运费险怎么赔" ≈ "退货运费险怎么用"）| **需要新凭据**（Tencent VectorDB / Hunyuan embedding）| 当前 OpenAPI 没暴露 |

**这条原则改变了所有后续设计取舍**。例如：凌晨 1 点的蒸馏任务不是为了"提升 bot 检索率"（那是本地 KB 的事），而是为了：
- 把杂乱的客服话术**结构化成 Q&A**（管理员可读性）
- 灌进 IMA 给**人**看（移动端/微信里查话术）
- 蒸馏版本同时是 bot prompt 注入的更好源（单条短、命中率高 5 倍）

未来批次、架构决策、是否继续蒸馏、要不要上 VectorDB——所有判断都从这条战略出发。

## 0. 一句话总览

```
源文档 → 转换（catdoc / LO）→ Markdown 切块 → 加 # title H1 → IMA import_doc + add_knowledge
```

代码骨架在 `utils/import_business_lang.py`（691 行）。**新批次只改源目录和 KB 名就行**，别的逻辑不用动。

## 1. 目录结构与工具版本

服务器实测环境（与脚本强耦合）：

| 工具 | 版本 | 备注 |
|---|---|---|
| Python | 3.11+ | 系统 `/usr/bin/python3` 即可，不依赖 venv |
| LibreOffice | 7.3.7 | **没装 MD 导出过滤器**，`.doc` 走 catdoc 不要走 LO |
| catdoc | 0.95 | 专治二进制 CFB `.doc`，输出 UTF-8 |
| pandas | 3.0+ | 读 xlsx/xls/csv |
| openpyxl | 3.1+ | xlsx 引擎 |
| xlrd | 2.0+ | **2.0 不再支持 .xls**，统一走 LO 转 xlsx |
| chardet | 5.x | CSV 编码探测（注意 §5 的 GB2312 坑） |
| requests | 任意 | ImaClient 同步 HTTP |

## 2. 文件格式转换矩阵

| 输入 | 处理方式 | 为什么 |
|---|---|---|
| `.doc` | `catdoc -d utf-8 file.doc > out.txt` | LO 没装 MD 导出过滤器（实测报 `Error: no export filter for ... .md found`）；catdoc 是为 CFB 二进制设计的，速度 < 100ms |
| `.xls` | `libreoffice --headless --convert-to xlsx --outdir staging/` | xlrd 2.0+ 已不支持 .xls；LO 转 xlsx 是当前最稳路径。**每个 worker 独立 UserInstallation profile**（`_lo_user_profile(staging, key=worker_key)`），不然并发会 rc=1 互锁 |
| `.xlsx` | `pandas.ExcelFile(path, engine="openpyxl")` | 直接读；`dtype=str` + `keep_default_na=False` 保留空 cell |
| `.csv` | `chardet.detect` → `pandas.read_csv(encoding=enc)` | 见 §5 的 GB2312→GB18030 升级 |
| `.md` | 直接复用 | 当作已经手工准备好的素材 |

并发用 `ThreadPoolExecutor(workers=4)`：每个文件一个独立任务，catdoc 是子进程自然释放 GIL，LO 也类似。注意 `catdoc`/`libreoffice` 要传 `text=True` 给 `subprocess.run`，否则 stdout 是 bytes，`write_text` 会报 `data must be str, not bytes`。

## 3. 切块：自然段 + 表格

### 散文（`.doc` / `.md`）

```python
# 1) 先 normalize：catdoc 把中文按 40 字换行，单 \n 不是段落边界
text = re.sub(r"(?<=\S)\n(?=\S)", " ", text)
# 2) 列表项前补空行，避免 1、xxx 和上一段被合并
text = re.sub(r"\n(?=\d+[、.]\s)", "\n\n", text)
text = re.sub(r"\n(?=[•·]\s)", "\n\n", text)
text = re.sub(r"\n(?=[一二三四五六七八九十]+、\s)", "\n\n", text)
# 3) 多空行合一
text = re.sub(r"\n{3,}", "\n\n", text)
# 4) 按空行切段
paragraphs = re.split(r"\n\s*\n", text)
# 5) 累积合并到 max_chars = 1500 封顶
```

### 表格（`.xlsx` / `.xls` / `.csv` → Markdown）

每个 sheet / CSV 一张 Markdown 表：
- 表头 + 分隔行 + 数据行
- `|` 在 cell 里要转义成 `\|`
- 换行替换成空格（避免破坏表行）

切片策略：`TABLE_ROWS_PER_CHUNK = 20` 行一块。超大单行（cell 几百字）兜底按字符硬切。

### 上限

`max_chars = 1500` 是 `import_doc` 服务端的实测硬上限附近。**留点 buffer**：本批用了 `# title` H1 前缀后实际最大 1531 字符，IMA 没拒绝，但理论上略超，所以新批次如果某条 chunk 接近 1500，考虑把 max_chars 调到 1450。

## 4. ⭐ IMA 表格首格覆写 title 坑（49/57 翻车）

**症状**：灌完后 `get_knowledge_list` 拿到的 `title` 字段被覆写。例如：

```
原始 title: 客服回复快捷语 · Sheet1 · 片段001
IMA 实际存: 快捷短语编码
```

**根因**：IMA 的 `import_doc` 服务端解析 content 时，如果第一行不是 Markdown H1，会把表格首行第一格当 title 覆写 API 参数。`.doc` 转的纯散文没踩到（第一行是中文句子不是表格），所有 `.xlsx / .csv` 全中招。

**修法**（必加）：

```python
def _wrap_with_h1(title: str, content: str) -> str:
    if content.lstrip().startswith("# "):
        return content  # 已经有了就不重复
    return f"# {title}\n\n{content}"
```

每条 note 的 content 在送 `import_doc` 之前先 `wrap_with_h1(title, content)`。修后 57/57 title 全部对齐 API 参数。

兼容 LocalKBIndex：它的 `_extract_title` 也认 H1（`if line.startswith("# "): return line[2:]`），所以这个修法对离线 fallback KB 同样有效。

## 5. CSV 编码探测坑：chardet 把 GBK 判成 GB2312

**症状**：`pd.read_csv(path, encoding="gb2312")` 报 `UnicodeDecodeError: 'gb2312' codec can't decode byte 0x84 in position 934`。

**根因**：chardet 对中文 encoding 区分度低，倾向返回 GB2312（严格子集）。GB2312 只覆盖 6763 个汉字 + ASCII，0x84 是扩展区的字，必须 GBK / GB18030 才能解。

**修法**：

```python
def _detect_csv_encoding(path):
    raw = path.read_bytes()
    guess = chardet.detect(raw[:200_000])
    enc = (guess.get("encoding") or "").lower()
    # 把 gb2312 / hzgb / iso-2022-cn 等严格子集都升级到 gb18030
    if enc in ("gb2312", "gbk", "gb18030", "hz-gb-2312", "hzgb", "iso-2022-cn"):
        enc = "gb18030"
    # confidence < 0.7 时按 utf-8 → gb18030 兜底
    ...
```

`gb18030` 是 GBK 的超集，最稳。所有国内 CSV 默认用它。

## 6. IMA API 行为表（实测）

| 操作 | API | 备注 |
|---|---|---|
| 搜索 | `POST /openapi/wiki/v1/search_knowledge` | **精确词匹配**，不是 embedding。短字面（如「客服」「亲」）命中好；自然问句或长词几乎 0 |
| 列 KB | `POST /openapi/wiki/v1/search_knowledge_base` | 空 query 列出全部 |
| 列 KB 内 note | `POST /openapi/wiki/v1/get_knowledge_list` | `limit` 上限 **50**，超出要翻 `cursor` |
| 取正文 | `POST /openapi/note/v1/get_doc_content` | **只有作者本人能取**；用 `doc_id` 替代 `note_id` 会 210005 `GetNoteContent not author` |
| 建 KB | `POST /openapi/wiki/v1/create_knowledge_base` | `type`: `KBT_MINE_KB`=1001（个人）/ `KBT_SHARED_KB`=1002 / `KBT_SUBSCRIBED_CREATE_KB`=1004。**1/2 这种短整型已被服务端拒绝（code=51）** |
| 建 note | `POST /openapi/note/v1/import_doc` | `content_format=1` 是 Markdown。单次约 1500 字符上限（实测可超一点） |
| 加到 KB | `POST /openapi/wiki/v1/add_knowledge` | `media_type=11`（Note），传 `note_id` 落到 `note_info.content_id` |
| 删 KB | **没有 API 端点** | 见 §7 |
| 改 KB 名 | **没有 API 端点** | 同上 |

**重试行为**：`ImaClient` 自带 5 次重试、backoff = `[2, 4, 8, 16, 32]`s。遇到限流（`code=200001`）会自愈，但耗时线性上涨。

## 7. ⚠️ IMA 不提供的能力（必须知道）

1. **没有 `delete_knowledge_base` API** — 试了 15+ 路径变体（`/openapi/wiki/v1/delete_knowledge_base`、`/openapi/wiki/v2/...`、`/api/wiki/...`、`/wiki/...` 等）全部 HTTP 404
2. **没有 `delete_note` API** — 想删单条 note 也不行
3. **没有 `rename_knowledge_base` API** — KB 名一旦创建就锁死
4. **Web 控制台强制微信扫码登录** — 装「打开电脑版」会提示下载原生客户端，**没有任何账号密码路径**
5. **没有公开的 OpenAPI 文档站点** — `ima.qq.com/agent-interface` 在 Claude Code WebFetch 黑名单里；社区文档（GitHub openakita、ruiyongwang 等）都不列 delete/rename

**唯一清理途径**：人工浏览器登录 → 我的知识库 → 找到 KB → 删除。Headless 浏览器 + AI 帮不上忙（微信扫码这关过不去）。

## 8. IMA 限流（code=200001）的处理

4 worker 并行灌 57 条 note 时大约 1/3 调用遇到 403。`ImaClient` 5 次重试全接住，**最终全部 57 条都成功**，总耗时 ~90s（vs dry-run 的 3.6s）。

如果重试 ladder 也不够（极端情况），降级方案：

```bash
# 单 worker 串行：稳但慢
python3 utils/import_business_lang.py --workers 1

# 或者批量 sleep（脚本里加 if i % 10 == 0: sleep(5)）
```

### 8.1 大批量限流实战（2026-09-07 converted/ 1540 条）

首次跑 1540 条蒸馏笔记，按 4 worker + 200 条/批一次性灌，结果：
- **Stage 1 OK**（cache 全在，秒过）
- **Stage 3 卡死**：1540 条 × 4 worker，全部触发 `code=200001`
- 现象：4 个 worker 全部进入 `hrtimer_nanosleep`（retry backoff），单次 sleep 32s
- 跑了 39 分钟仅部分上传，仍未完成

**根因**：4 worker 并发请求触发了 IMA 全局限流，所有 worker 同时 sleep 等 backoff。

**修复**（已落地 `qa_distill.py`）：

| 参数 | 之前 | 修复后 |
|---|---|---|
| `--workers` | 4 | **2** |
| `--batch-size` | （全量） | **80** |
| `--batch-pause` | 0 | **15s** |

机制：Stage 3 把 notes 分成 N 批（每批 ≤80），每批上传完成后 sleep 15s 再继续下一批。IMA 有时间冷却，`code=200001` 基本不出现。

实测估算：1540 条 / 80 = 20 批 × 15s = 5 分钟 sleep + 实际 upload 时间约 20 分钟，总 ~25 分钟跑完。

### 8.2 `at` 排程（大任务必备）

不要在前台跑大任务。`at` 或 systemd timer：

```bash
# 1 小时后跑（大任务不要现在跑，给 IMA 充分冷却）
echo "/usr/bin/python3 utils/qa_distill.py \
    --topic-filter clawbot-客服话术-补充 \
    --kb-name clawbot-客服话术-补充 \
    --workers 2 --batch-size 80 --batch-pause 15 \
    >> logs/qa_distill_converted.log 2>&1" | at 23:45

# 查看排程
atq

# 取消（如果需要）
atrm <job_id>
```

systemd timer 模板见 `/etc/systemd/system/qa-distill.{service,timer}`（已配置 `--workers 2 --batch-size 80 --batch-pause 15`）。

### 8.3 限流时的应急操作

| 现象 | 操作 |
|---|---|
| 进程跑了几十分钟但 cache 不再增长 | 大概率在 Stage 3 retry backoff；看 `/proc/<pid>/task/*/wchan`，多个 `hrtimer_nanosleep` 表明被卡 |
| 4 worker 全卡 5+ 分钟 | 杀进程，按 §8.1 保守参数重跑（cache 命中，Stage 1 跳过，只重传 Stage 3） |
| IMA 持续返回 200001 >30 min | 真的撞硬限流了；杀进程，等 1-2 小时，按 batch=50, pause=30s 重跑 |

## 9. 检索命中率基线（实测数据，2026-09-07）

跑 `python3 utils/test_search_quality.py`，18 条 query 分 4 类（高频字 / 中等关键词 / 自然问句 / 边界）：

| 路径 | 应该命中数 | 实际命中 | 命中率 | 误命中 |
|---|---|---|---|---|
| **A. IMA 直搜** `search_knowledge(q)` | 16 | **1** | **6%** | 0/2 |
| **B. IMA 抽关键词**（模拟 `KEYWORD_EXTRACT=1`） | 16 | **1** | **6%** | 0/2 |
| **C. LocalKBIndex substring 索引** `utils/local_kb.py` | 16 | **16** | **100%** | 1/2 |
| **全管线 B ∪ C 兜底**（bot 实际行为） | 16 | **16** | **100%** | 1/2 |
| **D. IMA qa-v3（Q&A 蒸馏 + title 含 Q）**（2026-09-07 跑同 16 条 query） | 16 | **6** | **37.5%** | 0/2 |

> **D vs A 真实提升**：约 6.25 倍（6% → 37.5%）。title 含 Q 关键短词这次抓到了 `快递`/`运费险` 等之前完全漏的字面。

> **术语澄清**：「LocalKBIndex substring 索引」= `docs/knowledge/*.md` 经 `utils/local_kb.LocalKBIndex` 做 substring 计数检索。这是测试路径 C 的实际后端，**不是** Obsidian vault（`/opt/ob-vault/` 那只是文档镜像，不参与检索）。Obsidian 镜像只是给"人"看的；`LocalKBIndex` 才是给 bot 用的搜索引擎。三者关系：
>
> ```
> 源文档 ─import_business_lang.py ─┬─→ docs/knowledge/*.md  ─→ LocalKBIndex 检索  ★ 100% 命中
>                                  └─→ IMA KB (clawbot-客服话术-v2) ─→ search_knowledge  ✗ 6% 命中
>                                  └─→ /opt/ob-vault/19.智能客服/*.md (人类阅读副本, 不参与检索)
> ```

**反直觉发现**：灌完的 IMA KB **几乎不能用**——只有 `客服` 这个高频单字命中，其他 15 条（"运费险"、"催付款"、"5分好评"、自然问句、字面短语）全部 0 命中。即使拆关键词再合并也没救，因为 IMA 自己就 match 不上长词（`docs/IMA_KB.md §4` 老问题）。

**`LocalKBIndex` substring 索引 是搜索的真正主力**（下文简称"LocalKBIndex 索引"）：
- 命中率高（16/16）— `LocalKBIndex` 用 substring 计数匹配，中文长词友好
- 延迟极低（3-5ms）— 纯字符串扫描
- 误命中 1/2（`DS 快速` 命中了 seed_ima_kb 的 qa-027/056/085 about `welcomed_users`，因为里面出现"DS"），但 bot 后续 LLM 兜底会判断相关性

**实操建议**：
- `.env` **必须**保留 `CLAWBOT_LOCAL_FALLBACK=1`，否则 15/16 query 直接 0 命中
- 跑过 import_business_lang.py 一定接着 `--only-local`，让 `docs/knowledge/` 同步落地
- 灌完 IMA 后跑一次 `test_search_quality.py`，不要相信"灌了就能搜到"
- 下批次若 KB 内容**天然是 Q&A 短句**（每条标题/正文都是关键词串），IMA 命中率会好；如果是长段落话术/散文，本地兜底才是出路

`test_search_quality.py` 的设计：三路径对照 + 模拟 LLM 抽关键词 + 5 类 query 覆盖。新批次只要改 QUERIES 列表里的 query 就能复用。

## 10. 复现一批新语料的标准流程

```bash
cd /opt/weixin-ClawBot-API

# 0) 把源目录指过去（也可以 --src 自定义）
SRC=/path/to/new-corpus

# 1) Dry-run 验证切块（~5s）
python3 utils/import_business_lang.py --src $SRC --dry-run
# 看输出："共切出 N 条 note"，按文件分组合理
# 看抽样：title / content 格式没问题

# 2) 只落本地 docs/knowledge/，人工抽检 2-3 条
python3 utils/import_business_lang.py --src $SRC --only-local
# ls docs/knowledge/qa-NNN-*.md  找几个打开看

# 3) 真灌
python3 utils/import_business_lang.py \
    --src $SRC \
    --kb-name "clawbot-新语料名" \
    --workers 4

# 4) 拿到 KB ID 后，立刻跑 search 抽样
KB=<paste-id-here>
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
for q in ['高频字1', '高频字2', '关键长词1', '关键长词2']:
    hits = c.search_knowledge(q, knowledge_base_id='$KB', limit=3)
    print(f'{q!r:15s} hits={len(hits)}', [h.title[:40] for h in hits])
"

# 5) 写进 .env
echo "IMA_ILINK_DEFAULT_KB=$KB" >> .env
# 或多 KB 逗号分隔：IMA_ILINK_DEFAULT_KB=$KB,-jDsbK-UEdF5xYyP7kZaoWbMrTyDUDuMNuvOt3oorvI=
```

## 11. 关键命令清单（Cheatsheet）

```bash
# 列所有 KB
python3 utils/list_ima_kb.py

# 模糊选 KB（带回显可粘到 .env 的那一行）
python3 utils/list_ima_kb.py --pick 客服话术

# 一步改写 .env
python3 utils/list_ima_kb.py --pick 客服话术 --update

# 看 KB 内 note（API 限制 50/页，要翻 cursor）
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
KB='<id>'
for it in c.get_knowledge_list(KB, folder_id='', limit=50):
    print(it.get('title'), '·', it.get('media_id'))
"

# 验证某条 note 内容
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
print(c.get_doc_content('<note_id>')[:500])
"
```

## 12. 验收 Checklist（新批次跑完后照着勾）

- [ ] Dry-run 切块数与文件数大致匹配（`xlsx` 多 sheet 会爆增；`doc` 通常 1-5 块/篇）
- [ ] `--only-local` 落地的 `.md` 抽样 2-3 条肉眼可读
- [ ] 真灌后 `get_knowledge_list` 抽查 title 全部对（关键：先看 5 条）
- [ ] 真灌后 `search_knowledge` 抽查 3-5 个关键词命中率 ≥ 60%
- [ ] KB ID 写到 `.env` 后重启 bot 验证一次：`grep "ima search" logs/clawbot.log` 看到 hits>0
- [ ] 把这次踩到的新坑补到本 playbook（保持活的）

## 13. 已知陷阱速查

| 现象 | 原因 | 修法 |
|---|---|---|
| 49/57 title 被改成「快捷短语编码」这种列名 | IMA 把表格首格当 title | `wrap_with_h1` 加 `# title` 前缀 |
| `'gb2312' codec can't decode byte 0x84` | chardet 误判 + GB2312 严格子集 | 升级到 `gb18030` |
| LibreOffice rc=1 在并发下偶发 | 多 worker 共用 UserInstallation | 每个 worker 独立 profile（`key=f"w{i%workers}"`） |
| `data must be str, not bytes` | `subprocess.run` 没传 `text=True` | 加 `text=True` |
| `no export filter for ... .md` | LO 没装 writer MD filter | `.doc` 走 `catdoc` 不走 LO |
| 57 条 note 灌了 90s | IMA 触发 `code=200001` 限流 | `ImaClient` 自带 5 次重试，自愈 |
| 灌完后 0 命中 | IMA 是关键词匹配不是 embedding；长中文词命中率极低 | **走LocalKBIndex substring 索引 兜底**（`CLAWBOT_LOCAL_FALLBACK=1`），IMA 这层基本是装饰。详见 §9 实测数据 |
| 微信扫码挡路无法删 KB | IMA Web 只有 QR 登录 | 手工去 ima.qq.com 删 |
| IMA OpenAPI 没有 delete/rename | IMA 没暴露 | 同上 |
| `xlrd` 读 .xls 报错 | xlrd 2.0+ 不再支持 .xls | `.xls` 一律走 LO 转 xlsx |
| 大批量（>500）灌 IMA 卡死 30+ 分钟 | 4 worker 触发硬限流，全在 retry backoff | `kill` 进程 → `--workers 2 --batch-size 80 --batch-pause 15` 重跑（cache 命中）→ 详见 §8.1 |
| `at` 排程没触发 | `atd` daemon 未启；或时间已过 | `systemctl status atd`；`atq` 看排程 |

## 14. 相关文件

- `utils/import_business_lang.py` — 主脚本（691 行，含全部修法）
- `utils/list_ima_kb.py` — KB 列表工具
- `utils/seed_ima_kb.py` — 灌纯 Q&A markdown（语料已经是 Q&A 格式时用这个更顺手）
- `docs/IMA_KB.md` — IMA 接入既有文档（关键词匹配、search 不返回正文等）
- `docs/business-lang-import-report.md` — 2026-09-07 这次操作的实测报告
- `/opt/weixin-ClawBot-API/.env` — `IMA_ILINK_CLIENT_ID` / `IMA_ILINK_API_KEY` 在这里

## 15. 持续输入操作手册（工作流标准）

**本节是真实的工作流，不是测试。** 当运营/客服/培训等部门送来新一批电商资料，按这个流程持续灌进 IMA KB。

### 15.1 三大原则

1. **限流优先**：宁可慢，不要冲。`workers=2, batch=80, pause=15` 是经过实测的安全节奏。
2. **断点续跑**：`qa_distill.py` 自带 cache（`/tmp/qa_distill_cache/`），中断后下次启动自动跳过已蒸馏的源。**不要加 `--no-resume`。**
3. **每批一个 KB**：不同主题/来源的语料用不同 KB 名（同时作为 `topic`），方便后续 `qa_distill --topic-filter` 选灌。

### 15.2 三种典型工作场景

#### 场景 A：一次性大批量（>500 条）

```bash
# 1) 规范化源 → docs/knowledge/
python3 utils/import_business_lang.py \
    --src /path/to/new-corpus \
    --kb-name "clawbot-新语料-2026Q4" \
    --only-local \
    --workers 4

# 2) 等 1 小时让 IMA 完全冷却（或者直接 `at` 排到明天）
echo "/usr/bin/python3 utils/qa_distill.py \
    --topic-filter 'clawbot-新语料-2026Q4' \
    --kb-name 'clawbot-新语料-2026Q4' \
    --workers 2 --batch-size 80 --batch-pause 15 \
    >> logs/qa_distill_new.log 2>&1" | at 23:00

# 3) 第二天看日志
tail -100 /opt/weixin-ClawBot-API/logs/qa_distill_new.log

# 4) 把 KB ID 追加到 .env
grep IMA_ILINK_DEFAULT_KB .env
# 如果是逗号分隔多个，直接 sed 追加
KB_ID=<从日志拿的 ID>
sed -i "s|^IMA_ILINK_DEFAULT_KB=.*|IMA_ILINK_DEFAULT_KB=\1,${KB_ID}|" .env
```

#### 场景 B：少量新内容（<100 条）

```bash
# 直接跑，不会触发限流
python3 utils/qa_distill.py \
    --topic-filter "clawbot-新语料" \
    --kb-name "clawbot-新语料" \
    --workers 2 --batch-size 50 --batch-pause 5
```

#### 场景 C：定时自动刷（已有 KB，需要重建）

```bash
# systemd timer 已经在跑（/etc/systemd/system/qa-distill.timer），每天 01:02
systemctl list-timers qa-distill.timer

# 或者手动立刻触发
systemctl start qa-distill.service

# 手工修改 KB 名字后，记得改 timer 里的 ExecStart
```

### 15.3 容量节奏参考

| 源数据规模 | LLM 蒸馏 | IMA 上传 | 建议时机 |
|---|---|---|---|
| <50 条 note | <2 min | <1 min | 立刻跑 |
| 50-200 条 | 2-8 min | 1-3 min | 业务低峰期 |
| 200-500 条 | 8-15 min | 3-10 min | `at` 到晚间 |
| 500-2000 条 | 15-60 min | 15-40 min | `at` + 1 小时后，给 IMA 充分冷却 |
| >2000 条 | >1h | >1h | 拆成多个 KB 分批跑 |

### 15.4 中断恢复（重点）

**假设** converted/ 任务跑到一半被杀（或机器重启、或 OOM）：

```bash
# 1) 看 cache 状态
ls /tmp/qa_distill_cache/ | wc -l
# 输出：332（已蒸馏完的源数）

# 2) 重新跑（cache 命中，跳过已蒸馏；Stage 3 自动跳过已上传的 note）
python3 utils/qa_distill.py \
    --topic-filter "clawbot-客服话术-补充" \
    --kb-name "clawbot-客服话术-补充" \
    --workers 2 --batch-size 80 --batch-pause 15
# Stage 1: 0 秒（全部 cache 命中）
# Stage 2: 0 个新写（文件已存在）
# Stage 3: 自动用 list_existing_titles 跳过已上传的 note，只传剩余
```

**不要** `rm -rf /tmp/qa_distill_cache/`——除非源文件全改了（cache key 包含 mtime，会自动失效）。

### 15.5 监控指标

跑完后看：

```bash
# 1) 实际创建/失败的 note 数
grep "全部完成" logs/qa_distill_*.log
# 期望: created=N  failed=0

# 2) KB 内 note 总数
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('/opt/weixin-ClawBot-API/.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
print(c.get_knowledge_list('<KB_ID>', folder_id='', limit=50))
"

# 3) search 抽样（关键词命中率）
# 见 test_search_quality.py
```

### 15.6 已知 KB ID 索引（持续维护）

| KB 名 | ID | 创建时间 | note 数 | 来源 |
|---|---|---|---|---|
| clawbot-客服话术 | `ImunprYoTmOGWNXtL7ywvUkeoHNgx_CsFg9wQlI5wfA=` | 2026-09-07 | 57（49 个 title 错位） | 第一次实验，**保留作历史** |
| clawbot-客服话术-v2 | `ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw=` | 2026-09-07 | 57 | 长段落（修复 title） |
| clawbot-客服话术-qa | `ekbTc5WaCPZTZMLAL5LzYOggze2qIibVAJlA-DxZ1MI=` | 2026-09-07 | 213 | 第一版蒸馏（无 Q 前缀 title） |
| clawbot-客服话术-qa-v2 | `p1UhPCkNwDqGFiXGVCJ0U8TauAqPGjHO-0gRlTz30Dg=` | 2026-09-07 | 214 | 第二版蒸馏 |
| clawbot-客服话术-qa-v3 | `20nAGFezP5XM_jp2_z73cvx5kGQkAlQg6z25lmnz3I4=` | 2026-09-07 | 218 | 第三版蒸馏（title 含 Q），**当前在用** |
| clawbot-客服话术-抖店 | `QAch3ZOeYaD0tUaA_yNapinqZVxjNeOaO0P4UxRyZxE=` | 2026-09-07 | 69 | 121 抖店客服话术 蒸馏 |
| clawbot-客服话术-补充 | `QYjVb9KBjDJSViPI-8ihaMcA-jopxUGD4O9-DrWowKI=` | 2026-09-07 | 1251 | converted/ 蒸馏（at job #3 跑完，2 worker / 80 batch / 15s pause / 25.5 min） |
| clawbot-客服话术-培训 | `CXD2uW4ldXTf7XON4NskhYSbPx9F9-bRw6sf5bx0UMw=` | 2026-09-08 | 39 | converted_lo/ 蒸馏（前台跑完，1m26s，量小未触发限流） |
| clawbot-客服话术-121培训 | `RVi5_wsCrIs8MYNG4e-NtynoKSCUGdTxHzIQnhIKyBI=` | 2026-09-08 | 753 | 121抖店客服话术/客服售前售中售后话术/ 子目录（165 .doc 蒸馏，2 worker / 80 batch / 10s pause / 29.5 min） |

> **IMA 没有 delete API**，这些 KB 永久留着。命名加版本号是无奈之举。后续如果 IMA 提供 rename 或删 API，再清理 v1/v2。

### 15.7 经验教训（流程级）

| 教训 | 应用 |
|---|---|
| 多 worker 冲限流要花 2x 时间 | 2 worker + batch + pause 比 4 worker 暴力灌快 |
| IMA `code=200001` 不是 bug 而是设计 | 尊重它，给 sleep 让它冷却 |
| 失败的中间状态会保留（部分 note 已上传，部分未上传） | `list_existing_titles` 检查是幂等的关键 |
| 每批一个 KB 比一个 mega KB 灵活 | 按主题拆分，未来按需 `topic-filter` |
| `topic` 字段既是 metadata 又是过滤器 | 不要硬编码，统一用 `--kb-name` 传 |
