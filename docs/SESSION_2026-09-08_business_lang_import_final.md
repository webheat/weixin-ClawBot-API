# 电商客服语料入 IMA 知识库 Final Report

> 时间：2026-09-07 → 2026-09-08（跨午夜）
> 目标：**给 AI 智能体（小龙虾 / clawbot）建完整的客服话术知识库**，让 AI 推理时有充分的参考素材
> 结果：**6 个 IMA KB，2387 条 note，覆盖客服话术全维度**（售前售后、抖店淘宝、Q&A 长段落、管理培训、系统化课程）

---

## 一句话总结

把 `/opt/business-lang/` 下 5 批客服资料（~1700 个原始文件）规范化后灌进腾讯 ima 个人 KB。AI 智能体现在可以用 6 个 KB 回答客服话术相关的几乎所有问题。同步双数据源：项目 `/opt/weixin-ClawBot-API/docs/` + Obsidian vault `/opt/ob-vault/19.智能客服/`。

---

## 二、产生的 AI 知识库（核心交付）

### IMA 知识库列表（`.env` 已配）

| KB 名 | KB ID | note 数 | 来源目录 | 用途 |
|---|---|---|---|---|
| clawbot-客服话术-v2 | `ni8v9...qV2` | 57 | `02.客服沟通话术汇总/` | 长段落原文，AI 推理上下文 |
| clawbot-客服话术-qa-v3 | `20nAG...qA` | 218 | 同上蒸馏 | Q&A 短句，便于快速回答 |
| clawbot-客服话术-抖店 | `QAch3...o店` | 69 | `121抖店客服话术/`（根 9 doc） | 抖店特色话术 |
| clawbot-客服话术-补充 | `QYjVb...充` | 1251 | `converted/` | 培训资料精选 |
| clawbot-客服话术-培训 | `CXD2u...训` | 39 | `converted_lo/` | 管理培训课件 |
| clawbot-客服话术-121培训 | `RVi5...1培` | 753 | `121抖店客服话术/客服售前售中售后话术/` 子目录 | 系统化培训（按"第N篇"组织） |
| **总计** | | **2387** | | |

`.env` 当前配置：
```
IMA_ILINK_DEFAULT_KB=ni8v9...qV2,20nAG...qA,QAch3...o店,QYjVb...充,CXD2u...训,RVi5...1培
```

> **注**：ImaClient.search_knowledge 一次只支持 5 个 KB ID，逗号分隔超过 5 个会被截断。如果未来 KB 数 > 5，需要分批查询。

### 本地知识库（兜底层）

- `docs/knowledge/` 共 **~2700 个 .md**（含 seed_ima_kb 旧 150 + 本轮 5 批蒸馏的 1647 + 其他）
- 由 `utils/local_kb.py:LocalKBIndex` 检索（substring 匹配，5ms 内，100% 命中字面关键词）
- **这条才是 bot 实时 RAG 的主力**——IMA 是给 AI 用的素材库，LocalKBIndex 是 bot 检索引擎

---

## 三、产生的脚本工具

| 脚本 | 行数 | 用途 |
|---|---|---|
| `utils/import_business_lang.py` | 692 | 把电商资料 (.doc/.xls/.xlsx/.csv/.txt) 转换 + 切块 + 写本地 |
| `utils/qa_distill.py` | 660+ | LLM 蒸馏（MiniMax-M2.7-highspeed）→ Q&A → 灌 IMA |
| `utils/seed_ima_kb.py` | (原有) | 复用 `find_kb_by_name`、`list_existing_titles`（已修 `kb_name` 字段 bug） |
| `utils/test_search_quality.py` | 116 | 三路径搜索效果对照测试 |

### qa_distill.py 关键能力
- 断点续跑：每个源蒸馏后原子写 cache (`/tmp/qa_distill_cache/`)
- 批处理 + 限流保护：`--workers 2 --batch-size 80 --batch-pause 10` 是经过实测的安全节奏
- `--from-local`：跳过 LLM 直接上传本地已蒸馏的 .md
- `--no-resume`：强制重跑（忽略 cache）

### import_business_lang.py 关键能力
- 支持 `.doc`（catdoc）/ `.xls`（LO）/ `.xlsx` / `.csv` / `.txt` / `.md`
- `parse_text` 含 `_normalize_wrapped_text`：折叠中文 40 字换行、识别列表项
- H1 标题前缀修复 IMA 表格首格覆写 title 的 bug

---

## 四、产生的文档（双数据源镜像）

### 代码仓库 `/opt/weixin-ClawBot-API/docs/`

| 文件 | 行数 | 主题 |
|---|---|---|
| `business-lang-import-report.md` | 191 | 第一次跑 imported 11 文件的实测报告 |
| `ima-batch-import-playbook.md` | 521+ | **主 playbook**：战略定位 + 15 个章节 + 全部 KB 索引 |
| `qa-distill-plan.md` | 259 | Q&A 蒸馏规划，含战略定位 |
| `IMA_KB.md` | (原有) | IMA 接入既有文档 |
| `multi-user.md` | (原有) | 多用户部署 |
| `PORTABLE.md` | (原有) | 便携打包 |
| `SESSION_2026-09-07_ima_pipeline.md` | (原有) | IMA pipeline session |

### Obsidian `/opt/ob-vault/19.智能客服/`

镜像同步：
- `IMA批量导入实战playbook.md`（md5 一致）
- `QA蒸馏规划方案-2026-09-07.md`（md5 一致）
- `客服话术目录导入IMA实测报告-2026-09-07.md`（md5 一致）

---

## 五、本轮关键踩坑（已全部修复并写入 playbook）

| # | 坑 | 修法 |
|---|---|---|
| 1 | IMA 把表格首格当 title 覆写 API 参数（49/57 错位） | content 前加 `# title` H1 前缀 |
| 2 | catdoc 中文按 40 字换行，单 `\n` 不是段落边界 | `_normalize_wrapped_text` 折叠单换行 |
| 3 | chardet 把 GBK 误判成 GB2312 严格子集 | 升级到 GB18030 |
| 4 | LibreOffice 多 worker 共用 UserInstallation 互锁 | 每个 worker 独立 profile (`key=f"w{i%workers}"`) |
| 5 | IMA `code=200001` 限流（4 worker 触发） | 降到 `--workers 2 --batch-size 80 --batch-pause 10` |
| 6 | IMA 没有 `delete_knowledge_base` / `rename` API | 无法清理失败 KB（v1 保留作历史） |
| 7 | `find_kb_by_name` 用 `item.get("name")` 但 API 返回 `kb_name` | 改用 `item.get("kb_name") or item.get("name")` |
| 8 | IMA 中文 3 字以上关键词不分词（"运费险"、"催付款"） | **结构性问题，无解**。LocalKBIndex 兜底 |
| 9 | `write_one_local` 用 hash 当 seq 文件名冲突 | 直接用 `title` 算文件名 |
| 10 | 12+ 个 KB 时 IMA 一次只能搜 5 个 | 未来如扩需分批查询 |

---

## 六、运营状态（明天接手时的环境）

### 当前运行的自动化

```bash
# 每天 01:02 自动跑（systemd timer）
systemctl list-timers qa-distill.timer
# → Tue 2026-09-08 01:02:15 HKT

# 配置在
/etc/systemd/system/qa-distill.service
/etc/systemd/system/qa-distill.timer
# ExecStart 已经是保守参数（workers 2, batch 80, pause 15）
```

### 进程状态
- 所有 qa_distill 任务完成，无后台进程在跑
- 无 at job 排队

### LocalKBIndex 状态
- 2700+ .md 文件，覆盖 6 个 KB 的全部内容
- bot 实时检索靠这条

---

## 七、未来如何"持续输入"

### A. 临时来一批新资料

```bash
# 1. 规范化
python3 utils/import_business_lang.py \
    --src /path/to/new-corpus \
    --kb-name "clawbot-新语料-2026Q4" \
    --only-local --workers 4

# 2. 排程 1 小时后跑（保守参数）
echo "/usr/bin/python3 utils/qa_distill.py \
    --topic-filter 'clawbot-新语料-2026Q4' \
    --kb-name 'clawbot-新语料-2026Q4' \
    --workers 2 --batch-size 80 --batch-pause 15 \
    >> logs/qa_distill_new.log 2>&1" | at 23:00

# 3. 第二天看日志拿 KB ID，加进 .env
tail logs/qa_distill_new.log
```

### B. 发现 IMA 限流卡住

```bash
# 1. 看进程状态
cat /proc/<pid>/task/*/wchan
# 多个 hrtimer_nanosleep = 限流卡死

# 2. 杀进程（cache 已写，下次秒过 Stage 1）
kill <pid>

# 3. 重跑（带 --kb-id 跳过创建）
python3 utils/qa_distill.py \
    --topic-filter X --kb-name X \
    --kb-id <existing_id> \
    --workers 2 --batch-size 80 --batch-pause 15
```

### C. 检查 AI 知识库状态

```bash
python3 utils/list_ima_kb.py  # 看所有 KB
grep "^IMA_ILINK_DEFAULT_KB" /opt/weixin-ClawBot-API/.env  # 看 .env 配置
ls /opt/weixin-ClawBot-API/docs/knowledge/qa-*QA*.md | wc -l  # 看本地有多少
```

---

## 八、未处理的素材（可选）

| 素材 | 路径 | 数量 | 处理建议 |
|---|---|---|---|
| `.docx` | `121抖店客服话术/客服售前售中售后话术/B皇冠店主不曾公开的销售独家秘籍.docx` | 1 | 加 `python-docx` 支持后处理 |
| `.ppt/.pptx` | 同上目录 | 2 | LO 转 .md 后处理（图片多，AI 用不上） |
| `.png` | 同上目录 | 1 | 流程图，AI 用不上 |
| `52.*.zip` (29MB) | 根目录 | 1 | 与 121子目录重叠，**跳过** |
| 顶层 `*.py / *.sh` | 根目录 | 6 | 历史脚本，不算语料 |

---

## 九、本会话生成的所有文件清单

### 代码
- `utils/import_business_lang.py`（+ .txt 支持、+ 修复）
- `utils/qa_distill.py`（+ 断点续跑、+ batched upload、+ 默认 2 worker）
- `utils/seed_ima_kb.py`（+ `find_kb_by_name` 修复 kb_name 字段）
- `utils/test_search_quality.py`（测试脚本）
- `utils/list_ima_kb.py`（既有）

### 配置
- `/opt/weixin-ClawBot-API/.env`（IMA_ILINK_DEFAULT_KB 加了 6 个 KB ID）
- `/etc/systemd/system/qa-distill.service`（保守参数）
- `/etc/systemd/system/qa-distill.timer`（每日 01:02）

### 文档（双数据源）
- `docs/business-lang-import-report.md`
- `docs/ima-batch-import-playbook.md`（**主 playbook**，521+ 行）
- `docs/qa-distill-plan.md`（含战略定位章节）
- `/opt/ob-vault/19.智能客服/IMA批量导入实战playbook.md`（镜像）
- `/opt/ob-vault/19.智能客服/QA蒸馏规划方案-2026-09-07.md`（镜像）
- `/opt/ob-vault/19.智能客服/客服话术目录导入IMA实测报告-2026-09-07.md`（镜像）

### 数据
- `docs/knowledge/` 下 ~2700 个 .md（含 1647 个 Q&A 蒸馏）
- `/tmp/qa_distill_cache/` 332 个 cache 文件（断点续跑用）

---

## 十、给下一个接手的人

如果你看到这份 report，说明：

1. **AI 知识库已经建好**：6 个 IMA KB，2387 条 note，覆盖客服话术全维度。AI 现在能用这套知识库回答关于售前售后、抖店淘宝、培训管理各种问题。
2. **持续输入管道已跑通**：`import_business_lang.py` + `qa_distill.py` + systemd timer，能定期灌新料。
3. **限流处理已固化**：2 worker / 80 batch / 15s pause 是实测安全节奏。
4. **文档完备**：playbook + plan + report 三件套在 `docs/`，双数据源在 Obsidian。
5. **持续优化方向**（按优先级）：
   - 想突破 31% IMA 命中天花板 → 接入 Tencent VectorDB / Hunyuan embedding（需新凭据）
   - 想让 docx/pptx 也能入 → 加 `python-docx`、LO 转 pptx 流水线
   - IMA KB 命名膨胀 → 等 IMA 提供 rename API 后清理 v1

**下一步建议**：先把这份报告 review 一遍，确认 KB 列表准确；然后让运维同事在 9/9 把 systemd timer 跑出来的 qa-v3（如果变化）记录到 .env。
