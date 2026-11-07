# 客服话术目录导入 IMA 实测报告

> 时间：2026-09-07
> 工具：`utils/import_business_lang.py`（新增）
> 源目录：`/opt/business-lang/02.客服沟通话术汇总/`
> KB：腾讯 ima 个人知识库
> **通用 playbook**：见 [`ima-batch-import-playbook.md`](./ima-batch-import-playbook.md)（下次批量导入直接照搬）

## 一、结论速览

| 项 | 值 |
|---|---|
| **可用 KB ID** | `ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw=` |
| **KB 名称** | `clawbot-客服话术-v2` |
| **类型** | `KBT_MINE_KB`（个人 KB） |
| **note 总数** | 57（与脚本切块数完全一致，无丢失） |
| **title 正确率** | **57/57**（v1 是 8/57，详见 §三） |
| **总耗时** | 1m35s（含 IMA 限流重试） |
| **遗留坏 KB** | `clawbot-客服话术` = `ImunprYoTmOGWNXtL7ywvUkeoHNgx_CsFg9wQlI5wfA=`（49 个 note title 被覆写，留作历史记录） |

把上面的 KB ID 写到 `.env` 即可让 clawbot 自动检索：

```bash
echo 'IMA_ILINK_DEFAULT_KB=ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw=' >> /opt/weixin-ClawBot-API/.env
```

> 当前 `.env` 的 `IMA_ILINK_DEFAULT_KB` 仍是 `clawbot-pipe-verify`（机器人生产 KB）。**没有动它**，要不要切换是另一次决策。

## 二、切块结果（按源文件）

| 源文件 | 格式 | note 数 |
|---|---|---|
| 各阶段客服回复话术表 | .xlsx | 10 |
| 客服催付话术与技巧 | .csv | 4 |
| 客服回复快捷语 | .csv | 4 |
| 客服回复话术全集 | .doc（catdoc） | 4 |
| 客服快捷回复语（干货分享） | .doc（catdoc） | 3 |
| 客服快捷短语 | .doc（catdoc） | 2 |
| 客服术语大全【精华版】 | .xlsx | 30 |
| **合计** | | **57** |

切块大小：单条 1450-1530 字符（H1 标题占 20-50 字符）。来源：

- `.doc` → `catdoc -d utf-8` 抽文本，正则折叠视觉换行，按空行切段
- `.xls` → `libreoffice --convert-to xlsx` → pandas 读
- `.xlsx` → pandas 读
- `.csv` → chardet 探测编码（GB2312/GBK/HZ 升级到 GB18030） → pandas 读
- 所有表格 → Markdown 表，每 20 行一块
- 所有 note → content 前面加 `# <title>\n\n` 显式 H1（见 §三）

## 三、踩到的两个坑（**重要**）

### 坑 1：IM A 把表格首格当 title（49/57 错位）✅ 已修

**症状**：v1 灌完后查 `get_knowledge_list`，发现 49/57 note 的 `title` 字段是表格首行第一格的内容，例如：

```
客服回复快捷语.csv → title 全变成 "快捷短语编码"
各阶段客服回复话术表.xlsx → title 全变成 "短信场合" / "Unnamed: 0" 等列名
```

8 个 title 正确的是 `.doc` 转的纯文本（content 开头是中文散文，不是表格），所以 IMA 没有触发首格解析。

**根因**：IMA 的 `import_doc` 服务端在解析 content 时，如果第一行不是 Markdown H1（`# ...`），会把表格首行的第一格当 title 覆写 API 参数。

**修复**（`iter_chunks` 里加 `_wrap_with_h1`）：每条 note 的 content 前面强制加 `# <title>\n\n`，让 IMA 走 H1 解析而不是表格首格。

```python
def _wrap_with_h1(title: str, content: str) -> str:
    if content.lstrip().startswith("# "):
        return content
    return f"# {title}\n\n{content}"
```

**验证**：v2 KB 灌完后查 `get_knowledge_list`，**57/57 title 完全匹配 API 参数**（`客服术语大全【精华版】 · 客服常用的话术 · 片段001` 这种）。

### 坑 2：IMA 触发 rate limit（`code=200001`），但自带重试全接住 ⚠️ 上传慢

**症状**：灌 57 条 note 时大约 1/3 调用遇到 403：

```json
{"code":200001,"msg":"请求频率超限，请稍后重试"}
```

**原因**：4 个 worker 并行触发 IMA 限流。

**为什么没失败**：`ImaClient` 自带 5 次重试、backoff = `[2, 4, 8, 16, 32]` 秒。最坏情况一条 note 会被重试 5 次（32s × 5 + 实际调用），但**最终全部 57 条都成功创建**。

**耗时**：90 秒（vs 3.6 秒 dry-run），其中大部分时间是 retry 等待。

**调优方向**（未实施）：
- `workers=1` 串行会显著慢（57 × ~1s = 1 分钟线性），但减少 403
- `workers=2` 是甜点位（实测合理）
- 真要快，可走分段：每批 10 条 → sleep 5s → 下一批（避 403 但用时长）

## 四、检索命中率（已知 IMA 限制）

灌完 v2 后等 90 秒让 IMA 全文索引建好，跑了 9 条 query：

| Query | 命中 | 备注 |
|---|---|---|
| `客服` | 2 | 高频字面 |
| `亲` | 0 | v1 命中过；v2 这次 0，索引可能还在建 |
| `催付款` | 0 | 长词，IMA 中文关键词匹配率低 |
| `运费险` | 0 | 同上 |
| `退货地址` | 0 | 同上 |
| `签收` | 0 | |
| `5分好评` | 0 | |
| `退款` | 0 | |
| `双12` | 0 | |

**这是 IMA 的已知限制**，详见 `docs/IMA_KB.md §4`：腾讯 ima OpenAPI 的 `search_knowledge` 走**精确词匹配**，不是 embedding 检索。短字面（如 "客服"）命中好；自然语言问句或长词几乎必 0。

**生产环境的缓解**（`.env` 已配）：
- `IMA_ILINK_KEYWORD_EXTRACT=1` — 搜 IMA 前用 LLM 抽 1-3 个关键词，让"运费险怎么赔？"这种问句能命中含"运费险"的 note
- `IMA_ILINK_RERANK=0`（默认） — 可改 `1` 让 LLM 二次打分
- `IMA_ILINK_LOCAL_FALLBACK` — 0 命中时 fallback 到 `utils/local_kb.py` 离线 KB（注意：本次灌的内容**没**写到 `docs/knowledge/`，需要的话跑 `--only-local`）

## 五、复现命令

```bash
cd /opt/weixin-ClawBot-API

# 1) Dry-run（不调 IMA，纯本地切块）
python3 utils/import_business_lang.py --dry-run

# 2) 只落本地 docs/knowledge/（不调 IMA）
python3 utils/import_business_lang.py --only-local

# 3) 真灌到指定 KB（自动建 KB，跳过已存在的 title）
python3 utils/import_business_lang.py --workers 2
#     默认 --kb-name "clawbot-客服话术"
#     默认 --src /opt/business-lang/02.客服沟通话术汇总
#     默认 --max-chars 1500

# 4) 自定义参数
python3 utils/import_business_lang.py \
    --src /some/other/dir \
    --kb-name "clawbot-其它话术" \
    --max-chars 1500 \
    --workers 2

# 5) 验证 KB 状态
python3 utils/list_ima_kb.py | grep 客服
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('/opt/weixin-ClawBot-API/.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
print(c.search_knowledge('客服', knowledge_base_id='ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw=', limit=3))
"
```

## 六、清理（可选）

v1 KB (`clawbot-客服话术` / `ImunprYoTmOGWNXtL7ywvUkeoHNgx_CsFg9wQlI5wfA=`) 是失败版本，49 个 note title 被覆写但内容仍可搜。**当前没找到 IMA delete_knowledge_base API 端点**（curl 测试 `POST /openapi/wiki/v1/delete_knowledge_base` → HTTP 404）。

要清理可去 IMA Web 控制台：<https://ima.qq.com/> → 我的知识库 → 找到 `clawbot-客服话术` → 删除。

或者留着当反面教材。

## 七、脚本设计要点（给后来人）

`utils/import_business_lang.py` 关键设计：

1. **Stage 1（并行）**：4 worker 跑 `convert_one_file`：
   - `.doc` → `catdoc -d utf-8`（LO 没装 MD 导出过滤器；catdoc 是为 CFB 二进制设计的）
   - `.xls` → `libreoffice --convert-to xlsx`（xlrd 2.0+ 不支持 .xls），每个 worker 独立 UserInstallation profile 防 LO 单实例互锁
   - `.xlsx / .csv / .md` → 直接复制到 staging

2. **Stage 2（顺序）**：staging → 解析 → 切块：
   - catdoc 输出按 40 字中文换行，**单 `\n` 不是段落边界**；`_normalize_wrapped_text` 折叠单换行为空格、保留空行；同时识别 `1、` `•` `一、` 等列表项前补空行
   - 表格按 20 行一块（`TABLE_ROWS_PER_CHUNK`）
   - 单段落超 1500 字符：先按句末标点切，再硬切兜底
   - **每条 note 内容前加 `# <title>\n\n`**（修复坑 1）

3. **Stage 3（并行）**：4 worker 跑 `import_doc` + `add_knowledge`：
   - 复用 `seed_ima_kb.find_kb_by_name` / `list_existing_titles` 做幂等
   - IMA 限流让总耗时 ~90s 而不是 ~3.6s（详见坑 2）

4. **编码兼容**：chardet 探测 CSV；GB2312/HZ/GBK 一律升级到 GB18030 避免严格子集解码失败

## 八、相关文件

| 路径 | 说明 |
|---|---|
| `utils/import_business_lang.py` | 主脚本 |
| `docs/IMA_KB.md` | IMA 接入坑与运维（既有） |
| `docs/business-lang-import-report.md` | 本文档 |
| `docs/knowledge/` | `--only-local` 模式下落的 .md（与 v2 KB 同步；本次只跑了 `--only-local` 一次，后来 dry-run 重写会按 frontmatter 去重） |
| `/opt/business-lang/02.客服沟通话术汇总/` | 源数据（原始 11 个文件） |
