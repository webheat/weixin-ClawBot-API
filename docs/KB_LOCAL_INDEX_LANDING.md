# KB 本地索引层落地要点(2026-09-18)

回答两个问题:
1. **`local_kb.py` 的 substring 兜底要不要换成 FTS5?**
2. **如果有别的备选方案,推荐哪个?**

结论先行: **不直接换成裸 FTS5;推荐把 `local_kb.py` 升级为 "FTS5 + jieba tokenizer" 或保留 substring 并提高前置语义层的命中率**。semantic 层命中率 7/7,是当前 4 档 fallback 里最强的。

---

## 1. 现状与动机

`_AIWithIma.chat` 的 4 档 fallback(`bot.py`):

```
ima → local(substring) → semantic(bge) → llm_only
```

- `local_kb.py:1-14` 自述 "零依赖,中文友好,按字面 substring"。150 条 Q&A 实测命中 5/7。
- `semantic_kb.py` 已用 `bge-small-zh-v1.5` + fastembed,在生产环境跑 `SEMANTIC_KB_ENABLED=1` 才启用。150 条 Q&A 实测 7/7 全中。
- 用户问"能不能装 sqlite fts5" —— 验证过本机 sqlite3.45.1 已经 `ENABLE_FTS5`,**不用装**,Python stdlib 直接可用。

CLAUDE.md "Hard limit" 那段明确写了:**IMA 和 `LocalKBIndex` 都不做 embedding/semantic**。所以"加 FTS5" 主要是改进 substring 这一档。

## 2. Benchmark 数据(2026-09-18,150 条 `docs/knowledge/qa-*.md`)

| 层 | cold/warm 查询延迟 | 7 中文查询命中 | 索引构建 | 依赖 |
|---|---|---|---|---|
| substring(`local_kb.py`) | 2.0 / 2.0 ms | **5/7** | 30 ms (in-mem) | 无 |
| FTS5 + unicode61(裸) | 0.02 / 0.02 ms | **1/7** ❌ | 30 ms | stdlib |
| FTS5 + bigram | 0.04 / 0.04 ms | 1/7 (脚本 bug,见 §3) | 70 ms | stdlib |
| semantic (bge-small-zh-v1.5) | 12–22 / 11–16 ms | **7/7** | 8.9 s (一次性) | fastembed + onnxruntime |

7 个 query:`"IMA 是什么" / "扫码登录失败怎么办" / "context_token 过期" / "QR 码刷新次数上限" / "ret=-14" / "欢迎语怎么发" / "X-WECHAT-UIN 怎么生成"`

**关键发现:**
- **裸 FTS5 (unicode61) 在中文上比 substring 还差** —— unicode61 按 codepoint 切,中文每个字成一个 term,BM25 把它当字符级别匹配。`"IMA 是什么"` 在表里查 `I M A 是 什 么` 全部 AND 命中,只能匹配这些字面全出现的文档,反而漏了自然语言改写。
- **semantic 最强** —— 7/7 全中,延迟 11-22 ms 完全可接受。
- substring 5/7 不算差,因为 150 条文档里大都是"标题即关键词"的 Q&A 卡片。
- 一次性 rebuild 成本:semantic 8.9 s / FTS5 < 100 ms / substring 30 ms。

## 3. FTS5 失败的细节(避免重踩)

- **不能直接 `tokenize='unicode61'`** 给中文用。FTS5 内置 tokenizer 只有 ascii / porter / unicode61 / trigram 等几种,**没有中文分词**。
- **bigram 预切可行,但 query 必须不带引号**(phrase match 要求相邻 term,会强制字面顺序)。`utils/local_kb.py` 里 `terms` 是 list of strings,FTS5 的 `MATCH` 字符串里空格分隔 = 隐式 AND,BM25 打分,这是预期行为。
- **真正的中文方案 = 自定义 tokenizer**(Python 端 jieba 切好后调 FTS5 的 `fts5_tokenizer()` 注册一个 Python 回调),这样 query 和 doc 都按词切。代码量 ~30 行,但引入 `jieba` 依赖(CLAUDE.md "零依赖"原则被打破)。

## 4. 决策表

| 想要什么 | 推荐 |
|---|---|
| **保留零依赖、保持当前行为** | **什么都不动**,substring 已经 5/7 |
| **最小代价提命中,不动 AI 层** | 在 `_AIWithIma.chat` 里把 `semantic` 提到 `local` 之前(见 §5 方案 A) |
| **彻底重做 local 层,要更强的中文召回** | `local_kb.py` 换 FTS5 + jieba tokenizer(见 §6 方案 B) |
| **想保留 substring 又想要 semantic 的命中率** | 方案 A(语义前置)即可,不动 `local_kb.py` |

**我个人推荐方案 A**(一行 routing 顺序调整,语义层命中率 7/7 直接覆盖 substring 的 5/7),除非 `SEMANTIC_KB_ENABLED` 长期关着。

## 5. 方案 A(推荐):语义前置,零依赖

### 改动

`_AIWithIma.chat` 的 4-state machine 顺序:

```python
# 现状 (bot.py:1804-1886):
if ima_hits:
    mode = "llm+ima"
elif local_hits:
    mode = "llm+local"
elif sem_hits:
    mode = "llm+semantic"
else:
    mode = "llm-only"
```

改成:

```python
if ima_hits:
    mode = "llm+ima"
elif sem_hits:                    # ← 提到第二位
    mode = "llm+semantic"
elif local_hits:                  # ← 退到第三位
    mode = "llm+local"
else:
    mode = "llm-only"
```

### 风险

- semantic 慢 10-20 ms(无伤大雅,用户无感)
- 依赖 `SEMANTIC_KB_ENABLED=1` 开着。如果关着,退回原顺序(下面给条件分支)
- semantic 的 `min_score` 默认 `0.0`,可能命中噪声;CLAUDE.md `DEFAULT_MIN_SCORE` 注释里建议 `0.4`。**写代码时顺手在 search 调用前判断 `SEMANTIC_KB_ENABLED`** —— 不开就走旧路径,语义前移是渐进式生效

### 落地 diff(预计 5 行)

```python
# bot.py: _AIWithIma.chat
sem_enabled = os.getenv("SEMANTIC_KB_ENABLED", "0") == "1"
if ima_hits:
    mode = "llm+ima"; reason = "hits-injected"
elif sem_enabled and sem_hits:
    mode = "llm+semantic"; reason = "semantic-fallback"
elif local_hits:
    mode = "llm+local"; reason = "local-fallback"
elif sem_enabled and sem_hits:    # 双保险,sem_enabled=True 但上面漏了
    mode = "llm+semantic"; reason = "semantic-fallback"
else:
    mode = "llm-only"; reason = "no-hits"
```

实际只需要把 `sem_enabled and sem_hits` 那条分支从第三个 elif 提到第二个 elif,其它保持。**全文件改动 ≤ 8 行**。

### 验证

`logs/clawbot.log` 搜 `mode=` 即可看到分布变化:

```bash
grep "mode=llm+semantic" logs/clawbot.log | tail -20
```

跑一周观察 `mode=llm+semantic` 占比是否显著上升、`mode=llm+local` 是否相应下降;同时跑一组对照(故意把 `SEMANTIC_KB_ENABLED=0`)看 substring 的命中率对比。

## 6. 方案 B(可选,代价高):`local_kb.py` 换 FTS5 + jieba

仅在 `SEMANTIC_KB_ENABLED` 长期 `0` 且确实想增强 substring 时做。

### 文件改动

新增 `utils/fts_kb.py`(参考 `utils/local_kb.py` 接口,对外提供 `FtsKBIndex` 类,`search()` 返回同样的 `SearchHit` 列表)。

```python
# utils/fts_kb.py 骨架
import re, sqlite3, threading, time
from pathlib import Path
from typing import Iterable
from ima import SearchHit

CN_RE = re.compile(r"[一-鿿]")
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)

# 方案 B-1: jieba 词级(优)
try:
    import jieba
    _tokenize = lambda t: " ".join(jieba.cut_for_search(t))
except ImportError:
    # 方案 B-2: 退回 bigram(无 jieba 依赖)
    def _tokenize(t: str) -> str:
        out, buf = [], []
        for ch in t:
            if CN_RE.match(ch):
                if buf: out.append("".join(buf)); buf = []
                out.append(ch)
            else:
                buf.append(ch)
        if buf: out.append("".join(buf))
        cn = "".join(c for c in t if CN_RE.match(c))
        for i in range(len(cn) - 1):
            out.append(cn[i:i+2])
        return " ".join(out)


class FtsKBIndex:
    DB_NAME = ".fts_kb.sqlite3"
    SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS kb USING fts5(
        path UNINDEXED, title, body,
        tokenize='unicode61 remove_diacritics 2'
    )
    """

    def __init__(self, directory, db_path=None):
        self.dir = Path(directory).resolve()
        self.db_path = Path(db_path) if db_path else self.dir.parent / self.DB_NAME
        self._lock = threading.Lock()
        self._init_db()
        self._last_mtime = 0.0

    def _init_db(self):
        with self._lock:
            con = sqlite3.connect(str(self.db_path))
            con.execute(self.SCHEMA)
            con.commit(); con.close()

    def _maybe_rebuild(self):
        # 跟 local_kb.py 一样:扫一遍 mtime,有变化就 rebuild
        ...

    def rebuild(self):
        with self._lock:
            con = sqlite3.connect(str(self.db_path))
            con.execute("DELETE FROM kb")
            for p in sorted(self.dir.rglob("*.md")):
                text = p.read_text(encoding="utf-8", errors="ignore")
                m = _FRONT_MATTER_RE.match(text)
                body = text[m.end():] if m else text
                title = next(
                    (l[2:].strip()[:200] for l in body.splitlines()
                     if l.strip().startswith("# ")), p.stem
                )
                # 关键:title + body 都过 tokenizer 预切词,然后用 unicode61 写入
                # (unicode61 这里只负责把空格分好的 term 收成 term,不会再按字切)
                indexed = _tokenize(title + "\n" + body)
                con.execute("INSERT INTO kb(path, title, body) VALUES (?,?,?)",
                            (str(p), title, indexed))
            con.commit(); con.close()

    def search(self, terms: Iterable[str], limit: int = 5) -> list[SearchHit]:
        # terms 已经是 list[str](跟 local_kb.py 一样),拼成 FTS5 query
        # 注意:不要用 phrase match("..."),会强制相邻
        tokens = [_tokenize(t) for t in terms if t]
        if not tokens:
            return []
        query = " ".join(tokens)
        with self._lock:
            con = sqlite3.connect(str(self.db_path))
            try:
                rows = con.execute(
                    "SELECT path, title, snippet(kb, 2, '[', ']', '…', 12), bm25(kb) "
                    "FROM kb WHERE kb MATCH ? ORDER BY bm25(kb) LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # query 包含 FTS5 保留字符时的兜底:term by term
                ...
            finally:
                con.close()
        # 转 SearchHit ...
```

`_AIWithIma.chat` 里的 `local_hits = local_idx.search(...)` 换成 `fts_hits = fts_idx.search(...)`,`reason="fts-fallback"`,`mode="llm+fts"`。日志字段同步加。

### 依赖

新增 `jieba` (≈10 MB,纯 Python,无 C 扩展) 到 `requirements.txt`(可选,因为代码里有 ImportError 兜底走 bigram)。

### 验证

跟方案 A 一样,看 `mode=llm+fts` 在 `logs/clawbot.log` 的占比。同时跑 benchmark:

```bash
./venv/bin/python utils/fts_kb.py  # 自测入口(参考 local_kb.py:194-206)
```

## 7. 验收清单(任何方案落地后跑一遍)

| 检查 | 命令 / 文件 | 预期 |
|---|---|---|
| 模式分布变化 | `grep "mode=" logs/clawbot.log \| tail -50` | `llm+semantic` 占比上升,`llm+local` 持平或下降 |
| 命中率人工核对 | 拿 20 条历史问答回放 | 新模式命中数 ≥ 旧模式命中数 |
| 延迟 | `clawbot.ai` logger 里的 `elapsed=` 字段 | P95 < 200 ms(语义前置后) |
| 资源 | `ps aux \| grep bot.py` RSS | 启动后 +10-30 MB(semantic 已经在;FTS5 不增) |
| 回归 | `python -c "from bot import _AIWithIma; ..."` | 无 import 错误 |

## 8. 不做的事(明确范围)

- 不动 `ima.py`(IMA 在线服务,命中路径不变)
- 不动 `local_kb.py` 接口(方案 A 完全不碰,方案 B 加新文件不删旧的)
- 不动 `_AIWithIma.chat` 的对外签名(只调内部顺序)
- 不引入 `pysqlite3-binary`(Python 3.12 的 stdlib sqlite3 已经 FTS5 enabled)
- 不动 semantic 索引本身的构建/检索逻辑

## 9. 上下游引用

- 现状描述:`CLAUDE.md` "Product context: IMA vs Obsidian" 决策表 + "Hard limits both share" 段
- IMA 检索:`docs/IMA_KB.md` §4-5(关键词匹配 vs 语义)
- Semantic 层设计:`utils/semantic_kb.py:91-119`(DEFAULT_MODEL / DEFAULT_MIN_SCORE / lazy embedder)
- 当前 local 层:`utils/local_kb.py`(全文,194 行)
- AI 4 档 routing:`bot.py:1804-1886`(`_AIWithIma.chat`)
- IMA 命中/落库:`docs/SESSION_2026-09-07_ima_pipeline.md`

## 10. 已知坑(落地时复核)

- [ ] FTS5 unicode61 + 预切词的组合,**写入和查询都要走同一个 `_tokenize` 函数**,否则 query 和 index 的 term 空间不一致,BM25 全 0 分。
- [ ] jieba 首次加载 ~200ms(词典初始化),如果用 `import jieba` 在模块顶层,bot 启动会慢 200ms。建议 lazy:`_tokenize` 里首次调用时再 `import jieba`。
- [ ] `docs/knowledge/` 已有 150 条 Q&A,jieba 的默认词典对"机器人 / 长轮询 / 二维码"等术语有覆盖,但 "OpenClaw / iLink / ilink_user_id / context_token" 这种项目专有名词不在默认词典里,会被切成单字。要么 `jieba.add_word()` 提前注册,要么接受单字切分(在 150 条文档量级影响不大)。
- [ ] FTS5 的 `MATCH` 查询对带 `-` `+` `*` `(` 等保留字符会报错,`search()` 里要捕获 `sqlite3.OperationalError` 并降级(代码骨架里已留)。
- [ ] CLAUDE.md "Logger namespace" 表里没有 `clawbot.fts`,新模块加 logger 时按 `get_logger("fts")` 命名。

---

**TL;DR:** 装 FTS5 不用装(本机 Python sqlite3 已带)。要不要用取决于要不要放弃 substring 的"零依赖"承诺。**7 行代码把 semantic 提到 local 前面,命中率 5/7 → 7/7,延迟 +10-20 ms,这是 ROI 最高的方案**;真要本地索引升级再走方案 B。
