"""演示脚本：同一 query 下 BM25 (``local_kb.py``) vs 语义检索 (``semantic_kb.py``) 并排输出。

直接对比词面匹配 vs 向量相似度的命中差异。给"演示效果"用。

用法::

    python utils/demo_semantic_kb.py                          # 用内置示例 query
    python utils/demo_semantic_kb.py "扫码登录怎么配 verify_code"
    python utils/demo_semantic_kb.py --rebuild                # 强制全量重建 embedding
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from utils.local_kb import LocalKBIndex  # noqa: E402
from utils.semantic_kb import SemanticKBIndex  # noqa: E402


# 精心挑的 5 个示例 query，故意混了"关键词能命中"和"语义但词面不沾边"两种场景
DEFAULT_QUERIES = [
    "长轮询",                                          # 1) 词面直击
    "怎么知道微信有新消息",                            # 2) 语义版长轮询，BM25 难命中
    "扫码登录的 verify_code 怎么用",                   # 3) 词面 + 语义混合
    "AI 模型选 DeepSeek 还是 DusAPI",                  # 4) 业务决策类
    "我改了文档为什么 KB 没生效",                      # 5) 排查类，BM25 难命中
]


def truncate(s: str, n: int = 180) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def fmt_score(s: float, kind: str) -> str:
    if kind == "bm25":
        return f"{s:>7.2f}"
    return f"{s:.4f}"


def run_one(query: str, top_k: int, kb_dir: str) -> None:
    bm25 = LocalKBIndex(kb_dir)
    sem = SemanticKBIndex(kb_dir)

    print()
    print("=" * 72)
    print(f"Query : {query}")
    print("=" * 72)

    # ---- BM25 ----
    bm25_hits = bm25.search([query], limit=top_k)
    print(f"\n[BM25 / local_kb.py]  hits={len(bm25_hits)}")
    if not bm25_hits:
        print("  (no hit)")
    for i, h in enumerate(bm25_hits, 1):
        print(f"  {i:>2}. score={fmt_score(h.score, 'bm25')}  {h.title}")
        print(f"      {truncate(h.content)}")

    # ---- Semantic ----
    sem_hits = sem.search(query, limit=top_k)
    print(f"\n[Semantic / semantic_kb.py]  hits={len(sem_hits)}")
    if not sem_hits:
        print("  (no hit)")
    for i, h in enumerate(sem_hits, 1):
        print(f"  {i:>2}. score={fmt_score(h.score, 'sem')}  {h.title}")
        print(f"      {truncate(h.content)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BM25 vs 语义检索对比演示")
    parser.add_argument(
        "query", nargs="*",
        help="要查询的问题；省略则用内置示例",
    )
    parser.add_argument(
        "--kb-dir", default="docs/knowledge",
        help="KB 目录（默认 docs/knowledge）",
    )
    parser.add_argument(
        "--top-k", type=int, default=3,
        help="每种方式取 top 多少（默认 3）",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="强制全量重建语义索引",
    )
    args = parser.parse_args()

    if args.rebuild:
        print("[rebuild] 强制重建语义索引…")
        sem = SemanticKBIndex(args.kb_dir)
        sem.rebuild(force=True)
        print(f"[rebuild] done: files={sem.file_count()}  chunks={sem.chunk_count()}")

    queries = [" ".join(args.query)] if args.query else DEFAULT_QUERIES
    for q in queries:
        run_one(q, args.top_k, args.kb_dir)
    print()


if __name__ == "__main__":
    main()