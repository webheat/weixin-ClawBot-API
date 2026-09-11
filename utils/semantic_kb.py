"""本地语义检索 KB：numpy + sqlite + fastembed（无 torch / faiss 依赖）。

设计原则（对齐 ``utils/local_kb.py``）：
- 零硬依赖（不引入 torch / faiss / chromadb）；底层 ``sqlite3`` + ``numpy``
- 中文友好：fastembed 默认 BAAI/bge-small-zh-v1.5（中文 SOTA 小模型，~93MB）
- 文件 mtime 变化时**只重嵌变化的 chunk**（增量重建）
- frontmatter 剥掉不参与 chunk
- 返回 :class:`ima.SearchHit`，接口与 ``LocalKBIndex`` 一致，便于上层替换

性能（150 docs / ~620KB → ~750 chunks）：
  - 首次重建 ≈ 30–60 s（包含模型下载 + 嵌入）
  - 增量重建（文档未变）≈ 0.1 s
  - 查询余弦相似度 top-K ≈ 1–5 ms（750×768 矩阵一次 matmul）

默认目录：``docs/knowledge/``；索引文件：``docs/.semantic_kb.sqlite3``。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ima import SearchHit


log_sem = logging.getLogger("clawbot.semantic")


# 前置 YAML frontmatter（``---\n...\n---\n``）匹配；只剥，不解析
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    """剥掉 ``---\\n...\\n---\\n`` frontmatter，返回剩余正文。"""
    if not text:
        return ""
    m = _FRONT_MATTER_RE.match(text)
    return text[m.end():] if m else text


def _chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    """按段落优先 + 字符窗口兜底切块。

    段落（``\\n\\n`` 分隔）单段 ≤ ``size`` 时累积拼接；超长段落按字符窗口切。
    返回的每个 chunk 已 strip。
    """
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        candidate = (current + "\n\n" + p) if current else p
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(p) > size:
            # 段落本身超长，按字符窗口切
            for i in range(0, len(p), size - overlap):
                chunks.append(p[i:i + size])
            current = ""
        else:
            current = p
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


class SemanticKBIndex:
    """扫描目录下所有 ``.md`` → 嵌入 → 持久化到 SQLite → 余弦 top-K 检索。

    ``media_id`` 设为 ``semantic:<相对路径>#<chunk_idx>``（前缀 ``semantic:``
    用于上层区分 IMA / BM25-local / 语义）。
    """

    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
    CHUNK_SIZE = 500
    CHUNK_OVERLAP = 50
    SCHEMA_VERSION = 1
    DEFAULT_MIN_SCORE = 0.0  # 0 = 不过滤（向后兼容）；建议 0.4 区分"真相关 vs 噪声"

    def __init__(
        self,
        directory: str | os.PathLike[str],
        db_path: Optional[str | os.PathLike[str]] = None,
        model_name: str = DEFAULT_MODEL,
        min_score: float = DEFAULT_MIN_SCORE,
    ):
        self.dir = Path(directory)
        if not self.dir.is_absolute():
            here = Path(__file__).resolve().parent.parent  # 项目根
            candidate = here / self.dir
            if candidate.is_dir():
                self.dir = candidate
            else:
                self.dir = Path.cwd() / directory
        if db_path is None:
            # 默认放 KB 父目录下（``docs/.semantic_kb.sqlite3``），便于 gitignore
            self.db_path = self.dir.parent / ".semantic_kb.sqlite3"
        else:
            self.db_path = Path(db_path)
        self.model_name = model_name
        self.min_score = float(min_score) if min_score else 0.0
        self._embedder = None  # lazy: fastembed.TextEmbedding
        self._lock = threading.Lock()
        self._init_db()
        # 构造时不主动 rebuild（避免 import 时就触发模型下载）；首次 search 时检查
        self._indexed = False

    @property
    def exists(self) -> bool:
        return self.dir.is_dir()

    # ---- model lazy load -------------------------------------------------

    @property
    def _model(self):
        if self._embedder is None:
            from fastembed import TextEmbedding  # 延迟导入
            log_sem.info(
                "loading fastembed model %s (首次会从 HuggingFace 拉 ~93MB)",
                self.model_name,
            )
            self._embedder = TextEmbedding(model_name=self.model_name)
        return self._embedder

    def _embed(self, texts: list[str]) -> list:
        """Batch embed。fastembed 是 generator，统一 list 化。"""
        if not texts:
            return []
        return list(self._model.embed(texts))

    # ---- sqlite ----------------------------------------------------------

    def _init_db(self) -> None:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    );
                    CREATE TABLE IF NOT EXISTS chunks (
                        file_path  TEXT    NOT NULL,
                        chunk_idx  INTEGER NOT NULL,
                        text       TEXT    NOT NULL,
                        mtime      REAL    NOT NULL,
                        embedding  BLOB    NOT NULL,
                        dim        INTEGER NOT NULL,
                        PRIMARY KEY (file_path, chunk_idx)
                    );
                    CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
                    """
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('model_name', ?)",
                    (self.model_name,),
                )
                conn.commit()
            finally:
                conn.close()

    # ---- rebuild ---------------------------------------------------------

    def rebuild(self, force: bool = False) -> None:
        """全量重建索引。文件 mtime 未变 → 跳过 embedding（除非 ``force=True``）。"""
        if not self.exists:
            return
        t0 = time.perf_counter()
        new_files: dict[str, list[tuple[int, str, float]]] = {}
        for path in sorted(self.dir.rglob("*.md")):
            try:
                stat = path.stat()
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            body = _strip_front_matter(text)
            chunks = _chunk_text(body, self.CHUNK_SIZE, self.CHUNK_OVERLAP)
            try:
                rel = str(path.relative_to(self.dir))
            except ValueError:
                rel = str(path)
            new_files[rel] = [
                (i, c, stat.st_mtime) for i, c in enumerate(chunks)
            ]

        if not new_files:
            log_sem.info("semantic rebuild: no .md files found in %s", self.dir)
            return

        # Diff against DB
        existing_mtimes = self._load_existing_mtimes()
        to_embed_texts: list[str] = []
        to_embed_keys: list[tuple[str, int, float]] = []

        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                # 删除：文件已删 / 文件 mtime 变了的 chunk
                for rel, items in new_files.items():
                    old_mtime = existing_mtimes.get(rel)
                    if old_mtime is None:
                        continue
                    if any(abs(it[2] - old_mtime) > 0.001 for it in items):
                        conn.execute(
                            "DELETE FROM chunks WHERE file_path = ?", (rel,)
                        )
                for rel in existing_mtimes:
                    if rel not in new_files:
                        conn.execute(
                            "DELETE FROM chunks WHERE file_path = ?", (rel,)
                        )
                conn.commit()

                # 收集需要嵌入的 chunk（DB 中不存在 / mtime 不一致 / force）
                for rel, items in new_files.items():
                    for chunk_idx, chunk_text_, mtime in items:
                        if force:
                            to_embed_texts.append(chunk_text_)
                            to_embed_keys.append((rel, chunk_idx, mtime))
                            continue
                        row = conn.execute(
                            "SELECT mtime FROM chunks WHERE file_path=? AND chunk_idx=?",
                            (rel, chunk_idx),
                        ).fetchone()
                        if row and abs(row[0] - mtime) < 0.001:
                            continue
                        to_embed_texts.append(chunk_text_)
                        to_embed_keys.append((rel, chunk_idx, mtime))
            finally:
                conn.close()

        if to_embed_texts:
            log_sem.info(
                "semantic rebuild: embedding %d new/changed chunks (model=%s)",
                len(to_embed_texts), self.model_name,
            )
            embed_t0 = time.perf_counter()
            embeddings = self._embed(to_embed_texts)
            embed_ms = (time.perf_counter() - embed_t0) * 1000
            log_sem.info("semantic embed batch done n=%d elapsed_ms=%.0f",
                         len(to_embed_texts), embed_ms)
            with self._lock:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    for (rel, chunk_idx, mtime), text_chunk, emb in zip(
                        to_embed_keys, to_embed_texts, embeddings
                    ):
                        emb_blob = np.asarray(emb, dtype="float32").tobytes()
                        conn.execute(
                            "INSERT OR REPLACE INTO chunks"
                            "(file_path, chunk_idx, text, mtime, embedding, dim) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (rel, chunk_idx, text_chunk, mtime,
                             emb_blob, int(emb.shape[0])),
                        )
                    conn.commit()
                finally:
                    conn.close()
        else:
            log_sem.info("semantic rebuild: nothing changed, skip embedding")

        elapsed_ms = (time.perf_counter() - t0) * 1000
        log_sem.info(
            "semantic rebuild done files=%d embedded_new=%d elapsed_ms=%.0f",
            len(new_files), len(to_embed_texts), elapsed_ms,
        )
        self._indexed = True

    def _load_existing_mtimes(self) -> dict[str, float]:
        if not self.db_path.exists():
            return {}
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(
                    "SELECT file_path, MAX(mtime) FROM chunks GROUP BY file_path"
                ).fetchall()
            finally:
                conn.close()
        return {r[0]: r[1] for r in rows}

    def _ensure_indexed(self) -> None:
        """首次 search 时确保索引就绪（mtime 检查 + 必要时重建）。"""
        if not self._indexed:
            self.rebuild()
            return
        if not self.exists:
            return
        try:
            current_max = 0.0
            for p in self.dir.rglob("*.md"):
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt > current_max:
                    current_max = mt
            existing = self._load_existing_mtimes()
            current_max_existing = max(existing.values()) if existing else 0.0
            if current_max > current_max_existing:
                self.rebuild()
        except OSError:
            pass

    # ---- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        """余弦相似度 top-K。query 为空 → ``[]``。"""
        if not query or not query.strip():
            return []
        self._ensure_indexed()
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                rows = conn.execute(
                    "SELECT file_path, chunk_idx, text, embedding, dim "
                    "FROM chunks"
                ).fetchall()
            finally:
                conn.close()
        if not rows:
            return []

        paths: list[str] = []
        idxs: list[int] = []
        texts: list[str] = []
        vecs = np.empty((len(rows), rows[0][4]), dtype="float32")
        for i, (p, idx, t, e, d) in enumerate(rows):
            paths.append(p)
            idxs.append(idx)
            texts.append(t)
            vecs[i] = np.frombuffer(e, dtype="float32").reshape(d)

        # 归一化 + 余弦
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        M_norm = vecs / norms
        q_emb = self._embed([query])[0].astype("float32")
        q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)
        scores = M_norm @ q_norm

        # top-K（结果按分数降序，所以一旦低于阈值就 break，后续都低于）
        k = min(max(limit, 1), len(scores))
        top_idx = np.argpartition(-scores, k - 1)[:k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]

        out: list[SearchHit] = []
        filtered = 0
        for idx in top_idx:
            score = float(scores[idx])
            if score <= 0:
                continue
            if self.min_score > 0 and score < self.min_score:
                filtered += 1
                continue
            hit = SearchHit(
                media_id=f"semantic:{paths[idx]}#{idxs[idx]}",
                title=Path(paths[idx]).stem,
                content=texts[idx],
                snippet=texts[idx][:200],
                highlight_content="",
                url=str(self.dir / paths[idx]),
                knowledge_base_id="semantic",
                score=score,
            )
            out.append(hit)
        if filtered:
            log_sem.info(
                "semantic search min_score=%.3f filtered=%d kept=%d",
                self.min_score, filtered, len(out),
            )
        return out

    def file_count(self) -> int:
        self._ensure_indexed()
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT file_path) FROM chunks"
                ).fetchone()
            finally:
                conn.close()
        return int(row[0]) if row else 0

    def chunk_count(self) -> int:
        self._ensure_indexed()
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            finally:
                conn.close()
        return int(row[0]) if row else 0


if __name__ == "__main__":
    # 简易自测
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    target = sys.argv[1] if len(sys.argv) > 1 else "docs/knowledge"
    idx = SemanticKBIndex(target)
    print(f"索引目录: {idx.dir}  files={idx.file_count()}  chunks={idx.chunk_count()}")
    if len(sys.argv) > 2:
        q = " ".join(sys.argv[2:])
        hits = idx.search(q, limit=5)
        print(f"搜 {q!r}: {len(hits)} hits")
        for h in hits:
            print(f"  - {h.title!r}  score={h.score:.4f}  media_id={h.media_id}")
            print(f"    {h.content[:160]!r}")