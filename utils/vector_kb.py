"""向量 KB 索引：基于 Ollama nomic-embed-text 的本地语义检索。

与 ``utils/local_kb.py`` (``LocalKBIndex``) 同形态（同样的 SearchHit 输出），
可以单独跑，也可以叠在一起做 hybrid 检索。

设计：
  - 后端：本地 Ollama + nomic-embed-text（137MB，CPU 跑，单条 50-200ms）
  - 维度：768
  - 缓存：``<dir>/.vector_cache/<md5>.npy`` 按文件路径 + mtime 失效
  - 搜索：cosine similarity（向量化，sub-millisecond）

用法：
    idx = VectorKBIndex("/opt/weixin-ClawBot-API/docs/knowledge")
    hits = idx.search(["运费险怎么赔"], limit=5)
    for h in hits:
        print(h.title, h.score)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

# 复用 ima 模块的 SearchHit（与 LocalKBIndex 一致输出，便于上层 `_AIWithIma` 接入）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ima import SearchHit


_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
OLLAMA_EMBED_MODEL = "nomic-embed-text"
EMBED_BATCH = 16  # 单次 API 调用的最大文本数（Ollama 内部会批量处理）


def _strip_front_matter(text: str) -> str:
    if not text:
        return ""
    m = _FRONT_MATTER_RE.match(text)
    return text[m.end():] if m else text


def _extract_title(body: str, path: Path) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return path.stem


def _cache_key(path: Path) -> str:
    return hashlib.md5(str(path.resolve()).encode("utf-8")).hexdigest()


class VectorKBIndex:
    """基于 Ollama embedding 的本地语义检索。

    与 ``LocalKBIndex`` 同样输出 ``SearchHit``，可以无差别替换或 hybrid 合并。
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        model: str = OLLAMA_EMBED_MODEL,
        ollama_url: str = OLLAMA_DEFAULT_URL,
        cache_dir: str | os.PathLike[str] | None = None,
    ):
        self.dir = Path(directory)
        if not self.dir.is_absolute():
            here = Path(__file__).resolve().parent.parent
            candidate = here / self.dir
            self.dir = candidate if candidate.is_dir() else (Path.cwd() / self.dir)
        self.model = model
        self.ollama_url = ollama_url.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else self.dir / ".vector_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._paths: list[Path] = []
        self._titles: list[str] = []
        self._embeddings: np.ndarray = np.zeros((0, 768), dtype=np.float32)
        self._last_mtime: float = 0.0

    @property
    def exists(self) -> bool:
        return self.dir.is_dir()

    def file_count(self) -> int:
        return len(self._paths)

    # ───────────── embedding API ─────────────

    def _ollama_embed_batch(self, texts: list[str]) -> np.ndarray:
        """调 Ollama /api/embed 批量嵌入。失败抛 RuntimeError。"""
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.ollama_url}/api/embed",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        vectors = data.get("embeddings") or []
        if not vectors or len(vectors) != len(texts):
            raise RuntimeError(
                f"Ollama 返回 embedding 数量不匹配：期望 {len(texts)} 实际 {len(vectors)}"
            )
        return np.array(vectors, dtype=np.float32)

    def _embed_one(self, text: str) -> np.ndarray:
        return self._ollama_embed_batch([text])[0]

    # ───────────── 缓存 ─────────────

    def _cache_path(self, source: Path) -> Path:
        return self.cache_dir / f"{_cache_key(source)}.npy"

    def _load_cache(self, source: Path, mtime: float) -> np.ndarray | None:
        cp = self._cache_path(source)
        if not cp.is_file():
            return None
        # 缓存文件旁存一份 meta：source_path + mtime，避免误用
        meta_path = cp.with_suffix(".json")
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("mtime") != mtime or meta.get("model") != self.model:
                    return None
            except (json.JSONDecodeError, OSError):
                return None
        try:
            return np.load(cp)
        except (OSError, ValueError):
            return None

    def _save_cache(self, source: Path, mtime: float, vec: np.ndarray) -> None:
        cp = self._cache_path(source)
        np.save(cp, vec)
        meta_path = cp.with_suffix(".json")
        meta_path.write_text(
            json.dumps({"mtime": mtime, "model": self.model, "dim": int(vec.shape[0])},
                      ensure_ascii=False),
            encoding="utf-8",
        )

    # ───────────── 重建 ─────────────

    def rebuild(self) -> None:
        """重建索引：扫所有 .md，命中缓存复用，新文件调 Ollama 嵌入。"""
        if not self.exists:
            self._paths = []
            self._titles = []
            self._embeddings = np.zeros((0, 768), dtype=np.float32)
            return

        paths = sorted(self.dir.rglob("*.md"))
        if not paths:
            self._paths = []
            self._titles = []
            self._embeddings = np.zeros((0, 768), dtype=np.float32)
            return

        # 统计需要嵌入的文件
        embeddings: list[np.ndarray] = []
        self._paths = paths
        self._titles = []

        # 估算 embedding 维度（首次嵌入一个 sample）
        dim = None
        need_embed: list[tuple[Path, str, float]] = []
        for p in paths:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                body = _strip_front_matter(text).strip()
                title = _extract_title(body, p)
                self._titles.append(title)
                mtime = p.stat().st_mtime
                cached = self._load_cache(p, mtime)
                if cached is not None:
                    embeddings.append(cached)
                    if dim is None:
                        dim = cached.shape[0]
                    continue
                # 文件可能很大：截断到 4000 字符（embedding 模型有 token 上限）
                need_embed.append((p, body[:4000], mtime))
            except OSError:
                self._titles.append(p.stem)
                embeddings.append(None)  # type: ignore

        # 嵌入新/变更的文件（分批）
        t0 = time.perf_counter()
        for i in range(0, len(need_embed), EMBED_BATCH):
            batch = need_embed[i: i + EMBED_BATCH]
            texts = [b[1] for b in batch]
            try:
                vecs = self._ollama_embed_batch(texts)
            except Exception as exc:
                # 单批失败：降级到逐条
                print(f"[vector_kb] 批量嵌入失败，降级到逐条：{exc}", file=sys.stderr)
                vecs = np.zeros((len(batch), dim or 768), dtype=np.float32)
                for j, (p, t, m) in enumerate(batch):
                    try:
                        single = self._ollama_embed_batch([t])[0]
                        vecs[j] = single
                        self._save_cache(p, m, single)
                        if dim is None:
                            dim = single.shape[0]
                    except Exception as exc2:
                        print(f"[vector_kb] 跳过 {p.name}: {exc2}", file=sys.stderr)
            else:
                for (p, t, m), v in zip(batch, vecs):
                    self._save_cache(p, m, v)
                    if dim is None:
                        dim = v.shape[0]

        # 合并：嵌入新文件的 vec + 缓存的 vec 按 paths 顺序对齐
        if dim is None:
            dim = 768
        merged: list[np.ndarray] = []
        cached_iter = iter(embeddings)
        need_iter = iter(need_embed)
        for p in paths:
            mtime = p.stat().st_mtime
            cached = self._load_cache(p, mtime)
            if cached is not None:
                merged.append(cached)
            else:
                # 应该已经在上面嵌入并缓存了
                np_loaded = self._load_cache(p, mtime)
                if np_loaded is None:
                    # 极少见：嵌入失败的位置
                    merged.append(np.zeros(dim, dtype=np.float32))
                else:
                    merged.append(np_loaded)

        if merged:
            self._embeddings = np.stack(merged).astype(np.float32)
            self._last_mtime = max(p.stat().st_mtime for p in paths)
        else:
            self._embeddings = np.zeros((0, dim), dtype=np.float32)

        elapsed = time.perf_counter() - t0
        print(f"[vector_kb] rebuild: {len(paths)} 文件，"
              f"{len(need_embed)} 新嵌入，{elapsed:.1f}s",
              file=sys.stderr)

    def rebuild_if_needed(self) -> None:
        """如 mtime 推进则重建。"""
        if not self.exists:
            return
        try:
            current_max = 0.0
            for p in self.dir.rglob("*.md"):
                try:
                    mt = p.stat().st_mtime
                    if mt > current_max:
                        current_max = mt
                except OSError:
                    continue
            if current_max > self._last_mtime or self._embeddings.shape[0] == 0:
                self.rebuild()
        except OSError:
            pass

    # ───────────── 搜索 ─────────────

    def search(self, queries: Iterable[str], limit: int = 5) -> list[SearchHit]:
        """queries 是多个查询词；每个 query 嵌入后算 cosine similarity，跨 query 取 max。"""
        q_list = [q for q in queries if q]
        if not q_list or self._embeddings.shape[0] == 0:
            return []

        # 嵌入 query（多条）
        try:
            q_vecs = self._ollama_embed_batch(q_list)
        except Exception as exc:
            print(f"[vector_kb] 嵌入 query 失败：{exc}", file=sys.stderr)
            return []

        # cosine similarity: A @ B.T / (|A| * |B|)
        doc_norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        doc_norms[doc_norms == 0] = 1.0
        q_norms = np.linalg.norm(q_vecs, axis=1, keepdims=True)
        q_norms[q_norms == 0] = 1.0
        sims = (self._embeddings @ q_vecs.T) / (doc_norms * q_norms.T)
        # 每个 doc 对所有 query 取 max similarity（任一 query 命中即算命中）
        max_per_doc = sims.max(axis=1) if sims.ndim == 2 else sims

        top_idx = np.argsort(-max_per_doc)[:limit]
        out: list[SearchHit] = []
        for i in top_idx:
            try:
                rel = self._paths[i].relative_to(self.dir)
            except ValueError:
                rel = self._paths[i]
            path = self._paths[i]
            try:
                body = _strip_front_matter(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                body = ""
            # 截断到 400 字符做 snippet
            snippet = body[:400].replace("\n", " ").strip()
            hit = SearchHit(
                media_id=f"vector:{rel}",
                title=self._titles[i] or path.stem,
                content=body[:2000],  # 留够上下文
                snippet=snippet,
                highlight_content="",
                url=str(path),
                knowledge_base_id="vector",
                score=float(max_per_doc[i]),
            )
            out.append(hit)
        return out


# ---------------------------------------------------------------------------
# Hybrid 检索：vector + substring 联合打分
# ---------------------------------------------------------------------------


class HybridKBIndex:
    """``VectorKBIndex`` + ``LocalKBIndex`` 加权融合。

    权重：``vector_weight`` (默认 0.6) 给向量相似度，``substring_weight`` (0.4)
    给 LocalKBIndex 字符命中数。两者都归一化到 [0, 1] 后相加。
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        vector_weight: float = 0.6,
        substring_weight: float = 0.4,
    ):
        self.dir = Path(directory)
        self.vector_weight = vector_weight
        self.substring_weight = substring_weight
        self.vector_idx = VectorKBIndex(self.dir)
        try:
            from utils.local_kb import LocalKBIndex
            self.substring_idx = LocalKBIndex(self.dir)
        except ImportError:
            self.substring_idx = None

    def rebuild(self) -> None:
        self.vector_idx.rebuild()
        if self.substring_idx:
            self.substring_idx.rebuild()

    def search(self, queries: Iterable[str], limit: int = 5) -> list[SearchHit]:
        q_list = [q for q in queries if q]
        if not q_list:
            return []

        # Vector 检索
        v_hits = self.vector_idx.search(q_list, limit=limit * 3)
        v_score = {h.media_id: h.score for h in v_hits}

        # Substring 检索
        s_hits: list[SearchHit] = []
        if self.substring_idx:
            s_hits = self.substring_idx.search(q_list, limit=limit * 3)
            s_score = {h.media_id: h.score for h in s_hits}
        else:
            s_score = {}

        # 合并：把所有 doc 收齐
        all_ids = set(v_score) | set(s_score)
        results: list[SearchHit] = []
        for mid in all_ids:
            v = v_score.get(mid, 0.0)
            s = s_score.get(mid, 0.0)
            combined = v * self.vector_weight + s * self.substring_weight
            # 从任一 hit 取 title / content
            hit = next((h for h in v_hits + s_hits if h.media_id == mid), None)
            if hit is None:
                continue
            hit.score = combined
            results.append(hit)

        results.sort(key=lambda x: -x.score)
        return results[:limit]


# ---------------------------------------------------------------------------
# 简易自测
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VectorKBIndex 自测")
    parser.add_argument("--dir", default="/opt/weixin-ClawBot-API/docs/knowledge",
                        help="文档目录")
    parser.add_argument("--limit-docs", type=int, default=30,
                        help="只用前 N 个 .md 测（默认 30）")
    parser.add_argument("--query", default="运费险怎么赔付",
                        help="测试 query")
    args = parser.parse_args()

    idx = VectorKBIndex(args.dir)

    # 限制为前 N 个文件
    paths = sorted(Path(args.dir).rglob("*.md"))[: args.limit_docs]
    print(f"[vector_kb] 限制为前 {len(paths)} 个文档")

    # 重建只针对这些路径
    idx._paths = paths
    idx._titles = [p.stem for p in paths]
    need = []
    for p in paths:
        try:
            mtime = p.stat().st_mtime
            cached = idx._load_cache(p, mtime)
            if cached is not None:
                if not need and not idx._embeddings.size:
                    idx._embeddings = np.stack([cached])
                continue
            text = _strip_front_matter(p.read_text(encoding="utf-8", errors="replace"))[:4000]
            need.append((p, text, mtime))
        except OSError:
            pass

    print(f"[vector_kb] 需要嵌入 {len(need)} 个文件")
    t0 = time.perf_counter()
    for i in range(0, len(need), EMBED_BATCH):
        batch = need[i: i + EMBED_BATCH]
        texts = [b[1] for b in batch]
        vecs = idx._ollama_embed_batch(texts)
        for (p, t, m), v in zip(batch, vecs):
            idx._save_cache(p, m, v)
        print(f"  embedded {i + len(batch)}/{len(need)}")
    print(f"[vector_kb] 嵌入耗时 {time.perf_counter() - t0:.1f}s")

    # 重建 embeddings 矩阵
    all_vecs = []
    for p in paths:
        mtime = p.stat().st_mtime
        v = idx._load_cache(p, mtime)
        all_vecs.append(v if v is not None else np.zeros(768, dtype=np.float32))
    idx._embeddings = np.stack(all_vecs).astype(np.float32)
    idx._last_mtime = max(p.stat().st_mtime for p in paths)

    hits = idx.search([args.query], limit=5)
    print(f"\n[vector_kb] query={args.query!r}")
    for h in hits:
        print(f"  • score={h.score:.3f}  title={h.title[:50]!r}  url={h.url}")
