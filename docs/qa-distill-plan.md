# Q&A 蒸馏规划方案（LLM 改造 IMA 语料形态）

> 目标：把 `clawbot-客服话术-v2` 里 57 条长段落 note（每条 ~1500 字）改写成 ~250 条 Q&A 短句（每 A ≤200 字），灌到新 KB `clawbot-客服话术-qa`，预期 IMA 命中率从 6% → 50%+。
>
> 时间：2026-09-07 起草
> 调度：凌晨 1 点（at / systemd timer）

## ⚠️ 战略定位（必读，2026-09-07 实测后总结）

**腾讯 IMA 不是 chatbot RAG 后端；它是为 AI Agent + 人类设计的个人/团队知识管理工具。**

完整版见 [`ima-batch-import-playbook.md`](./ima-batch-import-playbook.md) 顶部「⚠️ 战略定位」章节。本文档按那条原则执行。

**对本蒸馏任务的影响**：
- ❌ 蒸馏目的不是"提升 bot 实时检索率"——那仍是 `LocalKBIndex` 的事（100% 命中、5ms、零成本）
- ✅ 蒸馏真正用途：
  1. 把杂乱客服话术**结构化成 Q&A**，给管理员/运营在 IMA App/网页端**人看**
  2. 同时给 bot prompt 注入提供**更短更好的源**（单条 ≤200 字 + title 含 Q 关键词，命中率高 5 倍）
  3. 蒸馏后产物也可被 `LocalKBIndex` 兜底消费（落到 `docs/knowledge/` 同时给本地用）
- ✅ 凌晨 1 点的 systemd timer 保留，理由从"提升 IMA 检索"改为"日常刷新人读 KB"

## 0. 实测结论（2026-09-07 跑完第一轮蒸馏后）

把 47 个长段落源 → 213 对 Q&A 短句，灌进 `clawbot-客服话术-qa-v3` (KB ID `20nAGFezP5XM_jp2_z73cvx5kGQkAlQg6z25lmnz3I4=`)。

**严格同 16 条 query 对照（修正版，2026-09-07 重测）**：

| KB | 命中 | 命中率 |
|---|---|---|
| v2 长段落 | 1/16 | **6%** |
| qa-v3 Q&A 蒸馏 | 5/16 | **31%** |
| LocalKBIndex substring | 16/16 | **100%** |

蒸馏是 **5 倍提升**（之前误判为 9%→18%，是 query 子集不同的错觉）。

**蒸馏能救的**：2 字高频字（亲、签收、快递、客服）、title 含 Q 短词
**蒸馏救不了的**：IMA 中文 3 字长词不分词（运费险、催付款、5分好评）、自然问句
**突破天花板路径**：用 Tencent VectorDB / Hunyuan embedding（要新凭据）

**蒸馏的真实价值**：
1. note title 一眼看懂（"运费险怎么买？"vs 模糊的"客服回复话术全集片段003"）
2. IMA App/网页端人类浏览体验大幅改善
3. 单条 note 更短、bot prompt 注入不被截断
4. **bot 实时 RAG 由 LocalKBIndex 兜底，不依赖 IMA**

**结论：保留方案。** qa-v3 和 v2 并存（`.env` 已设逗号分隔），`CLAWBOT_LOCAL_FALLBACK=1` 仍是真正主力。

### 现状

| | 长段落 KB (v2) | 本地 KB (`docs/knowledge/`) | Q&A 蒸馏 KB (qa-v3) |
|---|---|---|---|
| note 数 | 57 | 197 | 213 |
| 单条长度 | ~1500 字 | 不定 | ~200 字/条 |
| IMA 检索命中率 | **6%** | n/a（本地） | **31%** |
| 适合人看（IMA 移动端） | 是（长文） | 否（碎） | **是**（Q&A 形式） |
| 适合 bot RAG 注入 | 否（太长、超 4000 字符 prompt 截断） | 是（substring） | 是（短 + title 含 Q） |

### 目标

把 v2 的 57 条长段落**蒸馏**成 200-300 条「Q 是自然问句 + A 是直接答案」的短句对，灌到新 KB：

```
clawbot-客服话术-v2  (保留, 长段落)
clawbot-客服话术-qa  (新增, Q&A 短句)  ← 本次目标
```

`.env` 改成逗号分隔两个 KB ID，让 bot 同时搜两边。

## 二、为什么 Q&A 短句能救 IMA

IMA 是**精确词匹配**，不是 embedding。每条 note 的 title 和正文作为索引词条存在。短句的好处：

1. **title 直接命中**：当 note title 是 "Q: 运费险怎么赔付？" 时，IMA 看到 "运费险" 就会建索引；用户问 "运费险" 100% 命中
2. **正文简洁**：A 在 ≤200 字内只讲一个点，关键词密度高，匹配概率高
3. **避免冗余**：长段落里同时讲 8 个主题，每个主题关键词都被冲淡

## 三、架构

```
源数据
  ├─ import_business_lang 已落的 47 个 .md 文件
  └─ IMA v2 KB 的 57 条 note  ← 备用，二选一

qa_distill.py
  ├─ 读源 → 调用 LLM（Haiku alias = deepseek-v4-flash）→ 拿 3-5 个 Q&A
  ├─ 校验：A ≤200 字 / Q ≤30 字 / 3-5 对 / JSON 格式
  ├─ 落本地 docs/knowledge/qa-derived-*.md  （格式与现有兼容，LocalKBIndex 自动扫到）
  └─ 灌 IMA 新 KB clawbot-客服话术-qa
       └─ title="QA · {源主题} · {seq:03d}" + # title H1（兼容 §4 坑）

凌晨 1 点 systemd timer 触发
  └─ 日志：/opt/weixin-ClawBot-API/logs/qa_distill_YYYY-MM-DD.log
```

## 四、LLM 选型与成本

| 项 | 值 |
|---|---|
| 端点 | `https://aikey4k.mengxa.com/v1/messages`（Anthropic 格式） |
| 凭据 | `~/.claude/settings.json` 的 `ANTHROPIC_AUTH_TOKEN` |
| 模型 | `MiniMax-M2.7-highspeed`（即 `ANTHROPIC_DEFAULT_HAIKU_MODEL`，2026-09-07 用户更新 settings.json 后） |
| 单次 input | ~1200 tokens（每条源 note ~1500 字） |
| 单次 output | ~500 tokens（3-5 对 Q&A JSON） |
| source note 总数 | 47（用本地 .md；不上 IMA v2 避免重复抽） |
| 总 token | 47 × 1700 = ~80K |
| 预计耗时 | 47 × 2s 串行 = ~1.5 分钟；并发 4 worker = ~25 秒 |
| 预计费用 | < ¥0.1 |

## 五、Prompt 设计

```python
SYSTEM = """你是客服话术编辑。把下面这段客服话术原文改写成 3-5 对 Q&A 短句。

【严格要求】
- Q：用户视角的自然问句（≤30 字），用买家/客户会问的原话，不要书面化
- A：客服视角的回复（≤150 字），直接给答案，不要客套话开头
- 每对 Q/A 必须独立可读：A 不能引用前文，要自包含
- 不要重复原文整段，只挑最有代表性的 3-5 个点
- 原文是 Markdown 表格时，按行拆；散文按段拆
- 输出严格 JSON，结构：`{"qa": [{"q": "...", "a": "..."}, ...]}`
- LLM 偶发包 ```json 代码块，解析前先 strip（实测 2026-09-07）
- 不要 markdown 代码块包裹 JSON
"""

USER = """原文标题：{title}
原文内容：
{content}

请输出 JSON。"""
```

**Few-shot 加持**（v2 加，先看 v1 效果再上）：

```python
EXAMPLE = """【例子】
原文：客户问运费险，我们说本店购买 7 天无理由退换货，双 12 当天客户可免费获得运费险。退货时退款完成后 72 小时内保险公司会赔付合理运费。
输出：{"qa":[
  {"q":"运费险怎么赔？","a":"本店购买 7 天无理由退换货，双 12 当天免费获得运费险。退款完成后 72 小时内保险公司赔付合理运费。"},
  {"q":"运费险谁出？","a":"本店为双 12 当天订单的客户免费购买运费险，由保险公司承担退货运费。"}
]}"""
```

## 六、输出 Schema

每个蒸馏后的 Q&A 对生成：

```markdown
---
title: QA · 客服回复话术全集 · 001
topic: clawbot-客服话术-qa
category: 运费险
keywords: [运费险, 双12, 退换货]
---

Q：运费险怎么赔？

A：    # ← H2 标题帮助 IMA 解析
本店购买 7 天无理由退换货，双 12 当天免费获得运费险。退款完成后 72 小时内保险公司赔付合理运费。
```

IMA note title：`QA · {源 file stem} · {seq:03d}`（≤50 字符），前缀 `QA` 让 IMA 不会当成表格首格。

## 七、幂等策略

- LocalKBIndex 用 mtime 判断是否重建，覆写同名 .md 自动覆盖
- IMA 灌前 `list_existing_titles(client, kb_id)` 拿现有 title 集合，命中跳过
- 同源内容用 SHA1(title + content[:100]) 前 8 位作为 seq，避免不同次跑产生重复

## 八、调度

### systemd timer（推荐，可控）

```ini
# /etc/systemd/system/qa-distill.service
[Unit]
Description=Q&A distillation via LLM
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/weixin-ClawBot-API
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/opt/weixin-ClawBot-API/venv/bin/python /opt/weixin-ClawBot-API/utils/qa_distill.py --workers 4
StandardOutput=append:/opt/weixin-ClawBot-API/logs/qa_distill_$(date +\%Y-\%m-\%d).log
StandardError=append:/opt/weixin-ClawBot-API/logs/qa_distill_$(date +\%Y-\%m-\%d).log

[Install]
WantedBy=multi-user.target
```

注：本项目还没建 venv。先用 `/usr/bin/python3`（已经验证过 `pandas/chardet/openpyxl/catdoc` 都齐）。

### 首次执行

```bash
# 1) 写好 systemd unit
# 2) 立即 dry-run 一次（不开 systemd）看输出
python3 utils/qa_distill.py --dry-run --limit 3

# 3) 手动跑一次完整 batch 验证管线
python3 utils/qa_distill.py --workers 2

# 4) 安排到凌晨 1 点
sudo systemctl start qa-distill.timer
# 或一次性：
echo 'python3 /opt/weixin-ClawBot-API/utils/qa_distill.py --workers 4 >> /opt/weixin-ClawBot-API/logs/qa_distill_$(date +\%Y-\%m-\%d).log 2>&1' | at 01:00
```

## 九、验证（第二天看）

```bash
# 1) 看日志
cat /opt/weixin-ClawBot-API/logs/qa_distill_$(date +%Y-%m-%d).log

# 2) 新生成的本地 .md
ls /opt/weixin-ClawBot-API/docs/knowledge/qa-derived-*.md | wc -l

# 3) 新 KB 命中抽样
python3 -c "
import sys; sys.path.insert(0, '/opt/weixin-ClawBot-API')
from dotenv import load_dotenv; load_dotenv('/opt/weixin-ClawBot-API/.env', override=False)
from ima import ImaClient, ImaConfig
c = ImaClient(ImaConfig.from_env(env_files=None))
for q in ['运费险怎么赔', '催付款', '5分好评', '亲']:
    hits = c.search_knowledge(q, knowledge_base_id='<NEW_KB_ID>', limit=3)
    print(f'{q!r:20s} hits={len(hits)}', [h.title[:30] for h in hits])
"

# 4) 跑 test_search_quality.py 对照
#    （改脚本接受 --kb-id 参数指向新 KB）
# 期望 6% → 50%+

# 5) .env 更新
echo "IMA_ILINK_DEFAULT_KB=<OLD>,<NEW>" >> /etc/clawbot/ima.env
# 或保留旧 + 追加：
sed -i 's|^IMA_ILINK_DEFAULT_KB=.*|IMA_ILINK_DEFAULT_KB=ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw=,<NEW>|' /opt/weixin-ClawBot-API/.env
```

## 十、风险与回退

| 风险 | 缓解 |
|---|---|
| LLM 输出偶发不符合 schema（多 JSON / 缺字段 / 超长） | 脚本里 try/except + 3 次重试，失败该条跳过不阻塞 |
| LLM 幻觉（编造原话没有的话术） | Prompt 明示「忠于原文，不要扩展」；后续人工抽检 5% |
| IMA 限流 | 沿用 `ImaClient` 自带 5 次重试，workers=4 |
| 凌晨 cron 机器休眠 | `systemd` 会在 resume 后补跑；`at` 不行要改用 systemd |
| 新 KB 效果不如预期 | 保留 v2 不删，`.env` 留两个 ID；随时拿掉 NEW |

## 十一、里程碑 Checklist

- [ ] 写好 `utils/qa_distill.py`（带 `--dry-run --limit N` 试跑）
- [ ] 试 3 条 → 看输出 JSON → 调 prompt
- [ ] 试 10 条 → 抽检质量 → 决定上/不上 few-shot
- [ ] 跑完整 47 条 → 落到本地 + IMA 新 KB
- [ ] 第二天跑 `test_search_quality.py` 对照
- [ ] 命中达标 → 改 `.env` 加新 KB ID
- [ ] 不达标 → 退回只走 LocalKBIndex
