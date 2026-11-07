"""本地 Markdown 知识库索引：IMA 兜底，零依赖。

用 ``LocalKBIndex`` 扫描一个目录下的所有 ``.md``，建立内存索引；搜索时对
每个文件做简单的"关键词出现次数"打分，返回 top N。结果是 ``SearchHit`` 列表，
可以无缝接到 ``_AIWithIma`` 的 ``_ima_build_context`` 流程里。

设计原则：
  - 零依赖（不引入 whoosh / jieba / faiss）；底层 ``Path.read_text()`` + 字符串计数
  - 中文友好（不强分词，按字面 substring 匹配，与 IMA 关键词检索策略对齐）
  - 文件 mtime 变化时自动重建索引（无需显式 reload）
  - 不解析 frontmatter 也不算分值；只把 frontmatter 块剥掉不参与计数（避免误匹配 tag）

默认目录：``docs/knowledge/``（项目根的相对路径）。
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ima import SearchHit


# 前置 YAML frontmatter（``---\n...\n---\n``）匹配；只剥，不解析
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    """剥掉 ``---\\n...\\n---\\n`` frontmatter，返回剩余正文。"""
    if not text:
        return ""
    m = _FRONT_MATTER_RE.match(text)
    if m:
        return text[m.end():]
    return text


@dataclass
class _Entry:
    path: Path
    title: str  # 取 H1 行；无 H1 用文件名
    content: str  # 全文（已剥 frontmatter）
    mtime: float
    key: str = ""  # 相对索引根的键（overlay 目录带 "user/" 前缀）

    def matches(self, terms: list[str]) -> tuple[int, set[str]]:
        """返回 ``(总命中次数, 命中的 terms 集合)``。大小写不敏感（英文部分）。"""
        if not self.content:
            return 0, set()
        content_lower = self.content.lower()
        title_lower = self.title.lower()
        total = 0
        hit_terms: set[str] = set()
        for term in terms:
            if not term:
                continue
            term_lower = term.lower()
            # 正文中出现次数 + 标题权重 ×2
            body_count = content_lower.count(term_lower)
            title_count = title_lower.count(term_lower) * 2
            n = body_count + title_count
            if n > 0:
                total += n
                hit_terms.add(term)
        return total, hit_terms


class LocalKBIndex:
    """扫描目录下所有 ``.md``，按关键词计数排序返回 ``SearchHit`` 列表。

    命中后 ``SearchHit.media_id`` 设为 ``local:<相对路径>``（前缀 ``local:``
    是给上层区分 IMA / 本地的标记）。``score`` 字段填命中次数。

    **两层目录**（多账号隔离用）：

    - ``directory`` —— 共享基座（``docs/knowledge``），所有人一起读；
    - ``extra_dirs`` —— 该账号私有的目录（每账号一个），只有这个账号看得到。

    overlay 里的文件在 media_id 里带 ``user/`` 前缀，避免和基座的相对路径撞名。
    """

    def __init__(self, directory: str | os.PathLike[str],
                 extra_dirs: Optional[Iterable[str | os.PathLike[str]]] = None):
        self.dir = self._resolve(directory)
        self.extra_dirs: list[Path] = []
        for item in (extra_dirs or []):
            resolved = self._resolve(item)
            if resolved.is_dir() and resolved != self.dir:
                self.extra_dirs.append(resolved)
        self._entries: list[_Entry] = []
        self._last_mtime: float = 0.0
        self.rebuild()

    @staticmethod
    def _resolve(directory: str | os.PathLike[str]) -> Path:
        """相对路径 → 项目根 / cwd 下的绝对路径。"""
        path = Path(directory)
        if path.is_absolute():
            return path
        # 解析成项目根的相对路径（脚本/bot 启动时 cwd 可能不同）
        # bot.py 跑在 /opt/weixin-ClawBot-API/；utils/*.py 也是
        here = Path(__file__).resolve().parent.parent  # 项目根
        candidate = here / path
        # 目录本身可能还没建（每账号私有库就是这样：先算路径，后创建），
        # 所以父目录在就算它有效。
        if candidate.is_dir() or candidate.parent.is_dir():
            return candidate
        # 退到 cwd
        return Path.cwd() / path

    @property
    def exists(self) -> bool:
        return self.dir.is_dir() or bool(self.extra_dirs)

    def _scan_dirs(self) -> list[tuple[Path, str]]:
        """(目录, media_id 前缀) 列表 —— 基座在前，overlay 在后。

        overlay 的前缀带序号（``user/0/``），这样多个 overlay 的相对路径
        不会互相撞名，也能从 media_id 反推出文件在哪。
        """
        out: list[tuple[Path, str]] = []
        if self.dir.is_dir():
            out.append((self.dir, ""))
        for index, extra in enumerate(self.extra_dirs):
            out.append((extra, f"user/{index}/"))
        return out

    def rebuild(self) -> None:
        """重建内存索引。静默吞掉读文件异常（单文件坏了不影响其它）。"""
        if not self.exists:
            self._entries = []
            self._last_mtime = 0.0
            return
        new_entries: list[_Entry] = []
        max_mtime = 0.0
        for root, prefix in self._scan_dirs():
            for path in sorted(root.rglob("*.md")):
                try:
                    stat = path.stat()
                    mtime = stat.st_mtime
                    if mtime > max_mtime:
                        max_mtime = mtime
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                body = _strip_front_matter(text)
                title = self._extract_title(body, path)
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                new_entries.append(_Entry(
                    path=path, title=title, content=body, mtime=mtime,
                    key=f"{prefix}{rel}",
                ))
        self._entries = new_entries
        self._last_mtime = max_mtime

    def _maybe_rebuild(self) -> None:
        """如目录里文件 mtime 推进了，触发重建。"""
        if not self.exists:
            return
        try:
            current_max = 0.0
            for root, _prefix in self._scan_dirs():
                for p in root.rglob("*.md"):
                    try:
                        mt = p.stat().st_mtime
                    except OSError:
                        continue
                    if mt > current_max:
                        current_max = mt
            if current_max > self._last_mtime:
                self.rebuild()
        except OSError:
            pass

    @staticmethod
    def _extract_title(body: str, path: Path) -> str:
        """从正文里抓第一个 H1（``# xxx``）作为 title；无 H1 用文件名去扩展名。"""
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()[:200]
        return path.stem

    def search(self, terms: Iterable[str], limit: int = 5) -> list[SearchHit]:
        """按关键词计数排序，返回 top ``limit`` 个 ``SearchHit``。

        terms 为空时返回空列表。空目录返回空列表。"""
        terms_list = [t for t in terms if t]
        if not terms_list:
            return []
        self._maybe_rebuild()
        if not self._entries:
            return []
        scored: list[tuple[int, _Entry]] = []
        for entry in self._entries:
            score, hit_terms = entry.matches(terms_list)
            if score <= 0:
                continue
            # 微调：命中的 term 越多越相关（避免单 term 重复计数占满）
            score = score * 10 + len(hit_terms)
            scored.append((score, entry))
        scored.sort(key=lambda x: -x[0])
        out: list[SearchHit] = []
        for score, entry in scored[:limit]:
            hit = SearchHit(
                media_id=f"local:{entry.key}",
                title=entry.title,
                content=entry.content,
                snippet="",  # SearchHit.display_snippet 会优先用 content
                highlight_content="",
                url=str(entry.path),
                knowledge_base_id="local",
                score=float(score),
            )
            out.append(hit)
        return out

    def file_count(self) -> int:
        self._maybe_rebuild()
        return len(self._entries)


if __name__ == "__main__":
    # 简易自测
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    target = sys.argv[1] if len(sys.argv) > 1 else "docs/knowledge"
    idx = LocalKBIndex(target)
    print(f"索引目录: {idx.dir}  文件数: {idx.file_count()}")
    if len(sys.argv) > 2:
        q = sys.argv[2]
        hits = idx.search([q], limit=5)
        print(f"搜 {q!r}: {len(hits)} hits")
        for h in hits:
            print(f"  - {h.title!r}  score={h.score}  media_id={h.media_id}")
