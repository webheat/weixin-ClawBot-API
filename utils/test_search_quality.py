"""v2 KB 搜索效果实测：直接 API vs 模拟 LLM 抽关键词 vs 本地 KB 兜底。

3 路径对照：
  A. IMA 直搜         — search_knowledge(query)                  （最差基线）
  B. IMA 多关键词合并   — 把问题拆成 1-3 关键词逐个搜，合并去重     （模拟 IMA_ILINK_KEYWORD_EXTRACT=1）
  C. 本地 KB 兜底      — IMA 0 命中时走 utils/local_kb.LocalKBIndex （模拟 CLAWBOT_LOCAL_FALLBACK=1）

对每条 query 评估：
  - 0 命中 = ❌
  - 命中但内容不相关 = ⚠️
  - 命中且内容相关 = ✅

输出每条 query 三路结果 + 总命中率，作为下个批次调 IMA_ILINK_KEYWORD_EXTRACT /
CLAWBOT_LOCAL_FALLBACK 是否值得保留的实证依据。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/weixin-ClawBot-API")
from dotenv import load_dotenv
load_dotenv("/opt/weixin-ClawBot-API/.env", override=False)

from ima import ImaClient, ImaConfig  # noqa: E402
from utils.local_kb import LocalKBIndex  # noqa: E402


KB = "ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw="
LOCAL_DIR = "/opt/weixin-ClawBot-API/docs/knowledge"


# ---------------------------------------------------------------------------
# 测试集：分 5 类
# ---------------------------------------------------------------------------

# (query, expected_keywords, judgement, note)
# judgement 是人评：'✅' 该命中、'⚠️' 命中宽松就行、'❌' 不该命中
QUERIES: list[tuple[str, list[str], str, str]] = [
    # ── 1. 高频字面（应该稳）──
    ("客服",          ["客服"],            "✅", "高频字"),
    ("亲",            ["亲"],              "⚠️", "高频字（IMA 索引不稳）"),

    # ── 2. 中等长度关键词（应命中）──
    ("退货地址",      ["退货地址"],        "✅", ""),
    ("运费险",        ["运费险"],          "✅", "客服话术全集多次出现"),
    ("催付款",        ["催付款"],          "✅", "客服回复话术全集章节"),
    ("签收",          ["签收"],            "✅", "客服快捷短语"),
    ("5分好评",       ["5分好评"],         "✅", "字面短语"),
    ("双12",          ["双12"],            "✅", "字面"),

    # ── 3. 自然语言问句（直搜常 0，分词后才命中）──
    ("运费险怎么赔？",     ["运费险", "赔"],     "✅", "拆词后 '运费险' 应命中"),
    ("买家要求退款怎么办？", ["退款"],            "✅", "拆词后 '退款' 应命中"),
    ("怎么催客户付款？",   ["催付款", "催付"],   "✅", "拆词后 '催付款' 应命中"),
    ("客户要换货怎么处理？", ["换货"],            "✅", "拆词后 '换货' 应命中"),
    ("发货延迟怎么回复？",  ["发货延迟"],         "✅", "客服话术表含此字段"),

    # ── 4. 弱相关 / 边界 ──
    ("DS 快速",       ["DS"],              "❌", "不该命中任何客服内容"),
    ("咖啡店营业时间", ["咖啡店"],          "❌", "零匹配测试"),

    # ── 5. 单字面 / 模糊 ──
    ("快递",          ["快递"],            "⚠️", "应出现于发货延迟章节"),
    ("亲，本店包邮",   ["包邮"],            "⚠️", "未在话术中"),
    ("旺旺",          ["旺旺"],            "✅", "客服快捷短语出现多次"),
]


# ---------------------------------------------------------------------------
# 模拟 LLM 关键词抽取
# ---------------------------------------------------------------------------

def naive_keywords(query: str) -> list[str]:
    """极简版"关键词抽取"：剥标点、按空格分；中文按 2/3 字滑动窗口。

    真版本是 LLM 调一次拿 1-3 词。这里只是模拟拆分意图，看拆分后命中数。
    """
    import re
    q = re.sub(r"[，。！？、；：,.!?;:\"'""''()（）【】\[\]]", " ", query)
    tokens: list[str] = []
    # 截掉无意义尾巴（"怎么"、"怎么办" 等）
    q = re.sub(r"(怎么|如何|怎么办|是什么|能不能|可以|吗|？|\?)+", " ", q)
    parts = [p.strip() for p in q.split() if p.strip()]
    for p in parts:
        tokens.append(p)
        # 中文 2/3 字滑动
        if re.fullmatch(r"[一-鿿]+", p) and len(p) >= 4:
            for w in (2, 3):
                for i in range(len(p) - w + 1):
                    sub = p[i:i + w]
                    if sub not in tokens:
                        tokens.append(sub)
    # 去空 + 去单字（"怎么"/"客户"被剥后留下的单字意义不大）+ 限 5
    out: list[str] = []
    for t in tokens:
        if not t or len(t) < 2:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= 5:
            break
    return out or [query]


# ---------------------------------------------------------------------------
# 三路搜索
# ---------------------------------------------------------------------------

def path_a_direct(c: ImaClient, q: str) -> list[str]:
    """IMA 直搜。返回命中 title 列表。"""
    hits = c.search_knowledge(q, knowledge_base_id=KB, limit=5)
    return [h.title for h in hits]


def path_b_keywords(c: ImaClient, q: str) -> list[str]:
    """模拟 IMA_ILINK_KEYWORD_EXTRACT=1：拆词逐个搜，合并去重。"""
    kws = naive_keywords(q)
    seen: set[str] = set()
    titles: list[str] = []
    for kw in kws:
        for h in c.search_knowledge(kw, knowledge_base_id=KB, limit=5):
            if h.title in seen:
                continue
            seen.add(h.title)
            titles.append(h.title)
        if len(titles) >= 8:
            break
    return titles[:8]


def path_c_local(q: str, idx: LocalKBIndex) -> list[str]:
    """CLAWBOT_LOCAL_FALLBACK=1：本 utils/local_kb.py 离线 KB。"""
    kws = naive_keywords(q)
    hits = idx.search(kws, limit=5)
    return [h.title for h in hits]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    c = ImaClient(ImaConfig.from_env(env_files=None))
    idx = LocalKBIndex(LOCAL_DIR)
    print(f"v2 KB   : {KB}")
    print(f"Local KB: {LOCAL_DIR}  ({idx.file_count()} files)")
    print(f"测试 query 数: {len(QUERIES)}\n")

    rows: list[tuple[str, str, list[str], list[str], list[str]]] = []

    for query, _, expect, note in QUERIES:
        print(f"--- q={query!r}  [{expect}]  {note}")
        a_titles: list[str] = []
        b_titles: list[str] = []
        c_titles: list[str] = []

        t0 = time.perf_counter()
        try:
            a_titles = path_a_direct(c, query)
        except Exception as exc:
            a_titles = [f"ERROR: {exc}"]
        ta = time.perf_counter() - t0

        t0 = time.perf_counter()
        try:
            b_titles = path_b_keywords(c, query)
        except Exception as exc:
            b_titles = [f"ERROR: {exc}"]
        tb = time.perf_counter() - t0

        t0 = time.perf_counter()
        c_titles = path_c_local(query, idx)
        tc = time.perf_counter() - t0

        kws = naive_keywords(query)
        print(f"   keywords={kws}")
        print(f"   A 直搜         ({ta*1000:4.0f}ms) → {len(a_titles)}  {[t[:35] for t in a_titles[:3]]}")
        print(f"   B 抽关键词合并  ({tb*1000:4.0f}ms) → {len(b_titles)}  {[t[:35] for t in b_titles[:3]]}")
        print(f"   C 本地兜底      ({tc*1000:4.0f}ms) → {len(c_titles)}  {[t[:35] for t in c_titles[:3]]}")
        print()

        rows.append((query, expect, a_titles, b_titles, c_titles))

    # ===== 总结 =====
    print("=" * 70)
    print("汇总：每路命中数 / 期望数")
    print("=" * 70)
    only_should = [r for r in rows if r[1] in ("✅", "⚠️")]
    expect_should_hit = len(only_should)
    expect_should_not = len([r for r in rows if r[1] == "❌"])

    a_hit_should = sum(1 for r in only_should if len(r[2]) > 0 and not r[2][0].startswith("ERROR"))
    b_hit_should = sum(1 for r in only_should if len(r[3]) > 0 and not r[3][0].startswith("ERROR"))
    c_hit_should = sum(1 for r in only_should if len(r[4]) > 0)

    a_wrong = sum(1 for r in rows if r[1] == "❌" and len(r[2]) > 0 and not r[2][0].startswith("ERROR"))
    b_wrong = sum(1 for r in rows if r[1] == "❌" and len(r[3]) > 0 and not r[3][0].startswith("ERROR"))
    c_wrong = sum(1 for r in rows if r[1] == "❌" and len(r[4]) > 0)

    print(f"应当命中（✅+⚠️, {expect_should_hit} 条）: "
          f"A={a_hit_should}/{expect_should_hit} ({a_hit_should/expect_should_hit*100:.0f}%) "
          f"B={b_hit_should}/{expect_should_hit} ({b_hit_should/expect_should_hit*100:.0f}%) "
          f"C={c_hit_should}/{expect_should_hit} ({c_hit_should/expect_should_hit*100:.0f}%)")
    print(f"不该命中（❌, {expect_should_not} 条）: "
          f"A={a_wrong}/{expect_should_not} 误命中  "
          f"B={b_wrong}/{expect_should_not} 误命中  "
          f"C={c_wrong}/{expect_should_not} 误命中")

    # 全路径：模拟 _AIWithIma 的实际行为（B 命中则用 B，否则 C 兜底）
    full_hit = 0
    for q, expect, a, b, c_titles in rows:
        if expect == "❌":
            continue
        chosen = b if len(b) > 0 and not b[0].startswith("ERROR") else c_titles
        if chosen:
            full_hit += 1
    print(f"\n全管线（B ∪ C 兜底）命中率：{full_hit}/{expect_should_hit} "
          f"({full_hit/expect_should_hit*100:.0f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
