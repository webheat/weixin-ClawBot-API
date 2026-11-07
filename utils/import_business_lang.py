#!/usr/bin/env python3
"""把 ``/opt/business-lang/02.客服沟通话术汇总/`` 这类目录里的客服话术文档
（``.doc`` / ``.xls`` / ``.xlsx`` / ``.csv``）灌进腾讯 ima 个人知识库。

为什么不直接用现成 ``seed_ima_kb.py``：
    seed_ima_kb 只生成内置 Q&A 模板，**不读外部文件**。本脚本负责把外部
    文件转 Markdown 文本，再按自然段切到 ``import_doc`` 的单次 ≤1500 字符
    上限（ima 服务端硬限制），最后挂到指定 KB。

转换策略：
    - ``.doc``   → ``libreoffice --headless --convert-to md`` → 按空行切段
    - ``.xls``   → ``libreoffice --headless --convert-to xlsx`` → 每 sheet
                   渲染成 Markdown 表，按行数切片
    - ``.xlsx``  → 直接 pandas 读 → 每 sheet 一张表
    - ``.csv``   → chardet 探测编码 → pandas 读 → 一张表
    - ``.md``    → 原样按空行切段（方便已经手工准备好的素材复用）

切块策略：
    - **按自然段切**：空行 (``\\n\\s*\\n``) 是一段；连续段落累积直到
      ``--max-chars``（默认 1500）封顶；若单个段落超长再按句末标点
      （。！？!?\\n）继续切；最后兜底按字符硬切避免超限。
    - **表格**：每个 sheet 是一段；超长则按 ``TABLE_ROWS_PER_CHUNK`` 行切。

幂等：
    - KB 用名字查重，命中则复用（见 ``seed_ima_kb.find_kb_by_name``）
    - 每个 note 的 ``title`` 用 ``<file_stem>--<section>--<seq:03d>``，
      ``list_existing_titles`` 命中就跳过

用法：
    # 1) 先 dry-run 看会切出多少块、什么标题
    ./venv/bin/python utils/import_business_lang.py --dry-run

    # 2) 落本地 Markdown（不调 IMA），方便人工检查
    ./venv/bin/python utils/import_business_lang.py --only-local

    # 3) 真灌，自动建 KB（name=clawbot-客服话术, type=KBT_MINE_KB）
    ./venv/bin/python utils/import_business_lang.py

    # 4) 自定义源目录 / KB 名
    ./venv/bin/python utils/import_business_lang.py \\
        --src /some/other/dir --kb-name "clawbot-其它话术"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

# 让脚本能从仓库根导入 ima / utils.seed_ima_kb
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from ima import ImaClient, ImaConfig  # noqa: E402

# 复用 seed_ima_kb 已验证的幂等工具
from utils.seed_ima_kb import (  # noqa: E402
    find_kb_by_name,
    list_existing_titles,
    write_one_local,
)
from utils.logging_setup import get_logger  # noqa: E402


log = get_logger("business_lang")

KB_NAME_DEFAULT = "clawbot-客服话术"
KB_DESC_DEFAULT = "客服话术本地目录自动灌入（import_business_lang.py）"
DEFAULT_SRC = "/opt/business-lang/02.客服沟通话术汇总"
MAX_CHARS_DEFAULT = 1500  # 实测 import_doc 服务端单次硬上限附近
TABLE_ROWS_PER_CHUNK = 20  # xlsx/xls/csv 每张表超长时按多少行切一块
SENTENCE_TERMINATORS = "。！？!?\n"


# ---------------------------------------------------------------------------
# 1. 转换：把 .doc / .xls 用 libreoffice 转成中间格式
# ---------------------------------------------------------------------------


def _lo_user_profile(staging: Path, key: str = "shared") -> Path:
    """每个 run 用独立的 LO UserInstallation，避免和服务端其它 LO 进程互锁。
    ``key`` 用来在同一 run 内给并行 worker 各自一份 profile，防止 LO 单实例
    限制导致 rc=1。
    """
    p = staging / f"lo_profile_{key}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _run_libreoffice(src: Path, out_dir: Path, fmt: str, staging: Path, key: str = "shared") -> Path:
    """调用 LO 把 ``src`` 转成 ``fmt``（xlsx），输出写到 ``out_dir``。
    返回输出文件路径（单文件场景）；LO 失败抛 RuntimeError。

    注意：本服务器 LO 没装 MD 导出过滤器，所以 ``.doc`` 不走 LO，
    走下面的 ``_run_catdoc``。
    """
    profile = _lo_user_profile(staging, key=key)
    cmd = [
        "libreoffice",
        "--headless",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", fmt,
        "--outdir", str(out_dir),
        str(src),
    ]
    log.info("[lo:%s] %s -> %s", key, src.name, fmt)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"libreoffice 转换失败 ({src.name}): rc={proc.returncode}\n"
            f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-500:]}"
        )
    out = out_dir / (src.stem + "." + fmt.split(":")[0])
    if not out.is_file():
        candidates = list(out_dir.glob(f"{src.stem}.*"))
        if not candidates:
            raise RuntimeError(f"libreoffice 没产出文件 in {out_dir}")
        out = candidates[0]
    return out


def _run_catdoc(src: Path, out_path: Path) -> Path:
    """``catdoc`` 把 ``.doc`` (CFB 二进制) 抽成纯文本，UTF-8 输出。
    catdoc 不会丢格式到 Markdown，只是按视觉行输出；中文按 ~40 字换行。
    下游 ``_normalize_wrapped_text`` 会把视觉单换行折叠为空格、保留空行
    作为段落边界。
    """
    log.info("[catdoc] %s", src.name)
    proc = subprocess.run(
        ["catdoc", "-d", "utf-8", str(src)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"catdoc 失败 ({src.name}): rc={proc.returncode}\n"
            f"stderr: {proc.stderr[-500:]}"
        )
    out_path.write_text(proc.stdout, encoding="utf-8")
    return out_path


def convert_one_file(
    entry: Path, staging: Path, worker_key: str,
) -> Path | None:
    """单文件转换：返回 staging 中的输出路径，无法处理返回 None。
    ``worker_key`` 用于给每个并行 worker 分配独立 LO profile。
    """
    suffix = entry.suffix.lower()
    try:
        if suffix == ".doc":
            out = staging / (entry.stem + ".txt")
            return _run_catdoc(entry, out)
        if suffix == ".xls":
            return _run_libreoffice(
                entry, staging, "xlsx", staging, key=worker_key,
            )
        if suffix in (".xlsx", ".csv", ".md", ".txt"):
            # .txt 当作已经处理好的纯文本直接复制（不再 catdoc / LO）
            dest = staging / entry.name
            shutil.copy2(entry, dest)
            return dest
        log.info("[stage] 跳过不支持的格式: %s", entry.name)
        return None
    except Exception as exc:
        log.error("[stage] %s 转换失败: %s", entry.name, exc)
        return None


def stage_inputs(
    src_dir: Path, staging: Path, workers: int = 4,
) -> list[Path]:
    """并行把 ``src_dir`` 里所有可识别文件转成 ``staging`` 下统一的
    ``.txt`` / ``.xlsx`` / ``.csv`` / ``.md``。

    每个文件独立转换（catdoc / LO 都是子进程，互不阻塞）；LO 的
    UserInstallation 已经按 worker 隔离，多 worker 不会互锁。
    """
    staging.mkdir(parents=True, exist_ok=True)
    entries = sorted(
        e for e in src_dir.iterdir()
        if e.is_file() and e.suffix.lower() in {".doc", ".xls", ".xlsx", ".csv", ".md", ".txt"}
    )
    if not entries:
        return []
    log.info("[stage] %d 个文件，并行 workers=%d", len(entries), workers)
    staged: list[Path] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for i, e in enumerate(entries):
            key = f"w{i % workers}"
            futures[pool.submit(convert_one_file, e, staging, key)] = e
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                out = fut.result()
                if out is not None:
                    staged.append(out)
                    log.info("[stage] ✓ %s → %s", entry.name, out.name)
            except Exception as exc:
                log.error("[stage] ✗ %s: %s", entry.name, exc)
    return sorted(staged)


# ---------------------------------------------------------------------------
# 2. 解析：把每种格式变成 Markdown 字符串列表（每个元素是一段）
# ---------------------------------------------------------------------------


def _normalize_wrapped_text(text: str) -> str:
    """把 catdoc 输出的 word-wrapped 单换行折叠成空格，保留空行作为段落边界。

    catdoc 处理中文 ``.doc`` 时会按 ~40 字视觉换行：单 ``\\n`` 不是段落
    边界，只是排版折行；连续 ``\\n\\n`` 才是真正的段落分隔。
    同时把 ``1、xxx`` / ``• xxx`` / ``· xxx`` 列表项前的换行恢复成空行，
    避免列表被错误合并成一个超长段落。
    """
    # 单换行（视觉折行）→ 空格
    text = re.sub(r"(?<=\S)\n(?=\S)", " ", text)
    # 列表项前补空行（编号 / bullet 开头）
    text = re.sub(r"\n(?=\d+[、.]\s)", "\n\n", text)
    text = re.sub(r"\n(?=[•·]\s)", "\n\n", text)
    text = re.sub(r"\n(?=[一二三四五六七八九十]+、\s)", "\n\n", text)
    # 多空行合一
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _read_text(path: Path) -> str:
    """容错读文本文件。"""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_text(path: Path) -> list[str]:
    """``{file}.md`` 或 catdoc 输出的 ``{file}.txt``：
    先 normalize word-wrapped 单换行，再按空行切段。
    """
    text = _read_text(path).strip()
    if not text:
        return []
    text = _normalize_wrapped_text(text)
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


# 别名：保留历史 import 名
parse_markdown = parse_text


def _detect_csv_encoding(path: Path) -> str:
    """chardet 探测 CSV 编码；confidence < 0.7 时按 utf-8 → gbk 兜底。

    已知坑：chardet 会把 GBK / GB18030 误判成 ``gb2312``。gb2312 是
    严格子集，遇到 0x80-0xFE 区段的字会 ``UnicodeDecodeError``；一律
    升级成 ``gb18030``（GBK 的超集，最稳）。
    """
    try:
        import chardet  # 延迟导入，没装也能跑（其它格式仍可用）
    except ImportError:
        return "utf-8"
    raw = path.read_bytes()
    if not raw:
        return "utf-8"
    guess = chardet.detect(raw[:200_000])
    enc = (guess.get("encoding") or "").lower()
    conf = float(guess.get("confidence") or 0.0)
    # 把 gb2312 / hzgb / iso-2022-cn 等严格子集都升级到 gb18030
    if enc in ("gb2312", "gbk", "gb18030", "hz-gb-2312", "hzgb", "iso-2022-cn"):
        enc = "gb18030"
    if conf >= 0.7 and enc:
        log.info("[csv] %s  编码=%s conf=%.2f", path.name, enc, conf)
        return enc
    # 兜底：utf-8 优先，失败再 gbk / gb18030（国内客服资料 GBK 很常见）
    for cand in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            raw.decode(cand)
            log.warning("[csv] %s  chardet conf=%.2f 不可信，兜底用 %s",
                        path.name, conf, cand)
            return cand
        except UnicodeDecodeError:
            continue
    return "utf-8"  # 最后保底（errors="replace" 在 pandas 里会兜住）


def _df_to_md_table(df) -> str:
    """pandas DataFrame → Markdown 表。空值显式渲染成空串，不丢列。"""
    headers = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for _, row in df.iterrows():
        cells = []
        for v in row.tolist():
            if v is None:
                cells.append("")
            else:
                s = str(v).replace("\n", " ").replace("|", "\\|").strip()
                cells.append(s)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def parse_xlsx(path: Path) -> list[tuple[str, str]]:
    """``.xlsx``：返回 ``[(sheet_title, md_table_text), ...]``。
    每个 sheet 一张表（chunking 在下一阶段按行处理）。
    """
    import pandas as pd
    out: list[tuple[str, str]] = []
    xl = pd.ExcelFile(path, engine="openpyxl")
    for sheet in xl.sheet_names:
        df = xl.parse(sheet, dtype=str, keep_default_na=False)
        if df.empty:
            continue
        out.append((sheet, _df_to_md_table(df)))
    return out


def parse_csv(path: Path) -> list[tuple[str, str]]:
    """``.csv``：chardet 探测编码后读，单文件当一个 sheet。"""
    import pandas as pd
    enc = _detect_csv_encoding(path)
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding=enc)
    if df.empty:
        return []
    return [(path.stem, _df_to_md_table(df))]


# ---------------------------------------------------------------------------
# 3. 切块：把过长的 Markdown 切成 ≤ max_chars 的若干段
# ---------------------------------------------------------------------------


def _hard_split(text: str, max_chars: int) -> list[str]:
    """兜底：按字符硬切。"""
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] if text else []


def _split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """单个段落超长时：先按句末标点切，再按字符硬切。"""
    if len(text) <= max_chars:
        return [text]
    # 按句末标点切，保留分隔符
    pieces: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENTENCE_TERMINATORS and len(buf) >= max_chars // 2:
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    # 任一片仍超长则硬切
    out: list[str] = []
    for p in pieces:
        if len(p) <= max_chars:
            out.append(p)
        else:
            out.extend(_hard_split(p, max_chars))
    return out


def chunk_paragraphs(paragraphs: Iterable[str], max_chars: int) -> list[str]:
    """按自然段合并累积，直到 ``max_chars`` 封顶开始新块。

    单段超长会内部再切。返回的列表元素都 ``<= max_chars``。
    """
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        # 单段先内部切碎
        sub = _split_long_paragraph(p, max_chars)
        for s in sub:
            if not buf:
                buf = s
                continue
            # 试探：加入后会超限吗？
            if len(buf) + 2 + len(s) <= max_chars:
                buf = buf + "\n\n" + s
            else:
                chunks.append(buf)
                buf = s
    if buf:
        chunks.append(buf)
    return chunks


def chunk_table_rows(
    sheet_name: str, table_md: str, rows_per_chunk: int, max_chars: int,
) -> list[str]:
    """一张 Markdown 表按行切片：保留表头，每 ``rows_per_chunk`` 行一块。"""
    lines = table_md.splitlines()
    if len(lines) <= 3:  # 只有表头+一行数据 → 一块
        if len(table_md) <= max_chars:
            return [table_md]
        # 否则按 _hard_split 切（极端：单行巨长）
        return _hard_split(table_md, max_chars)
    header = lines[:2]
    body = lines[2:]
    chunks: list[str] = []
    for i in range(0, len(body), rows_per_chunk):
        block_body = body[i:i + rows_per_chunk]
        block = "\n".join(header + block_body)
        if len(block) <= max_chars:
            chunks.append(block)
        else:
            # 单块也超长（行极宽）：硬切 body
            tmp_header = "\n".join(header) + "\n"
            # 直接按字符硬切整段
            chunks.extend(_hard_split(tmp_header + "\n".join(block_body), max_chars))
    return chunks


# ---------------------------------------------------------------------------
# 4. 标题：去重友好 + 文件系统安全
# ---------------------------------------------------------------------------


def _safe_title(*parts: str, max_len: int = 100) -> str:
    """拼一个稳定的、可作文件名的标题。"""
    s = " · ".join(p for p in parts if p)
    # 去掉文件系统危险字符；保留中日韩
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # NFKC 折叠全/半角
    s = unicodedata.normalize("NFKC", s)
    return s[:max_len] or "untitled"


# ---------------------------------------------------------------------------
# 5. 主流程：编排转换 → 切块 → 上传
# ---------------------------------------------------------------------------


def iter_chunks(
    src_dir: Path, max_chars: int, workers: int = 4,
) -> list[tuple[str, str, str]]:
    """返回 ``[(file_stem, title, content_md), ...]``，每个元素是一条 note。
    同一文件内 title 序号递增；不同文件之间通过 file_stem 区分。

    **重要**：每条 content 前面会强制加 ``# <title>\\n\\n`` 显式 H1。
    实测 IMA 对表格开头的 note 会把首行第一格当 title 覆盖 API 参数
    （49/57 都被覆写），H1 能强制让 IMA 走内容解析、与 API title 对齐。
    对 LocalKBIndex 也兼容（``_extract_title`` 也认 H1）。

    末尾追加一个标记项 ``("__staging__", "__staging_path__", str(path))``，
    调用方决定要不要清理 staging 目录。
    """
    with tempfile.TemporaryDirectory(prefix="business_lang_") as tmp:
        staging = Path(tmp)
        # Stage 1：并行文件转换
        staged = stage_inputs(src_dir, staging, workers=workers)

        # Stage 2：顺序解析（CPU 不是瓶颈，文件级解析 < 1s）
        #   如果未来文件很多可再加一层 parse 线程池；现在保持简单
        out: list[tuple[str, str, str]] = []
        for path in staged:
            suffix = path.suffix.lower()
            stem = path.stem
            if suffix in (".md", ".txt"):
                paragraphs = parse_text(path)
                chunks = chunk_paragraphs(paragraphs, max_chars)
                for i, c in enumerate(chunks, 1):
                    title = _safe_title(stem, f"片段{i:03d}")
                    out.append((stem, title, _wrap_with_h1(title, c)))
            elif suffix == ".xlsx":
                sheets = parse_xlsx(path)
                for sheet_name, table_md in sheets:
                    chunks = chunk_table_rows(
                        sheet_name, table_md, TABLE_ROWS_PER_CHUNK, max_chars,
                    )
                    for i, c in enumerate(chunks, 1):
                        title = _safe_title(stem, sheet_name, f"片段{i:03d}")
                        out.append((stem, title, _wrap_with_h1(title, c)))
            elif suffix == ".csv":
                sheets = parse_csv(path)
                for sheet_name, table_md in sheets:
                    chunks = chunk_table_rows(
                        sheet_name, table_md, TABLE_ROWS_PER_CHUNK, max_chars,
                    )
                    for i, c in enumerate(chunks, 1):
                        title = _safe_title(stem, sheet_name, f"片段{i:03d}")
                        out.append((stem, title, _wrap_with_h1(title, c)))

        out.append(("__staging__", "__staging_path__", str(staging)))
        return out


def _wrap_with_h1(title: str, content: str) -> str:
    """在 content 前面加 ``# <title>\\n\\n``，避免 IMA 把表格首格当 title。
    已含 H1 时不重复添加（按行首判断）。
    """
    if content.lstrip().startswith("# "):
        return content
    return f"# {title}\n\n{content}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把客服话术目录灌进腾讯 ima 个人知识库",
    )
    parser.add_argument("--src", default=DEFAULT_SRC, help=f"源目录（默认 {DEFAULT_SRC}）")
    parser.add_argument("--kb-name", default=KB_NAME_DEFAULT, help=f"KB 名（默认 {KB_NAME_DEFAULT}）")
    parser.add_argument("--kb-desc", default=KB_DESC_DEFAULT, help="KB 描述")
    parser.add_argument(
        "--max-chars", type=int, default=MAX_CHARS_DEFAULT,
        help=f"每条 note 字符上限（默认 {MAX_CHARS_DEFAULT}，约 import_doc 服务端硬限）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只解析+切块，不调 IMA，也不写本地",
    )
    parser.add_argument(
        "--only-local", action="store_true",
        help="只把切好的 .md 写到本地 docs/knowledge/，不调 IMA",
    )
    parser.add_argument(
        "--local-out", default="docs/knowledge",
        help="本地落盘目录（相对项目根，默认 docs/knowledge）",
    )
    parser.add_argument(
        "--keep-staging", action="store_true",
        help="保留 libreoffice 临时输出目录（默认清理）",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Stage 1 (文件转换) + Stage 3 (IMA 上传) 并行 worker 数（默认 4）",
    )
    args = parser.parse_args()

    src_dir = Path(args.src)
    if not src_dir.is_dir():
        print(f"[err] 源目录不存在: {src_dir}", file=sys.stderr)
        return 1

    print(f"[run] src={src_dir}  kb={args.kb_name!r}  max_chars={args.max_chars}  workers={args.workers}")
    raw = iter_chunks(src_dir, args.max_chars, workers=args.workers)
    # 抽出 staging path（标记项）
    staging_path: Path | None = None
    chunks: list[tuple[str, str, str]] = []
    for stem, title, content in raw:
        if stem == "__staging__":
            staging_path = Path(content)
            continue
        chunks.append((stem, title, content))

    print(f"[run] 共切出 {len(chunks)} 条 note")
    by_stem: dict[str, int] = {}
    for stem, _, _ in chunks:
        by_stem[stem] = by_stem.get(stem, 0) + 1
    for stem, n in sorted(by_stem.items()):
        print(f"  - {stem}: {n} 条")

    if args.dry_run:
        if chunks:
            print("\n[dry-run] 第 1 条样例：")
            print(f"  title: {chunks[0][1]}")
            print(f"  content ({len(chunks[0][2])} 字符):")
            print("  " + chunks[0][2].replace("\n", "\n  ")[:400])
        return 0

    if args.only_local:
        local_out = Path(args.local_out)
        n = 0
        for stem, title, content in chunks:
            pair = {
                "title": title,
                "content": content,
                "topic": args.kb_name,
                "category": stem,
                "kws": [],
            }
            if write_one_local(pair, local_out):
                n += 1
        print(f"[local] 新写 {n} 个 .md 到 {local_out}")
        return 0

    # ---- 真灌 IMA ----
    from utils.logging_setup import setup_logging

    setup_logging(level=os.getenv("CLAWBOT_LOG_LEVEL", "INFO"))
    client = ImaClient(ImaConfig.from_env(env_files=None))
    if not client.configured():
        print("[err] IMA 凭据缺失，请在 .env 里设 IMA_ILINK_CLIENT_ID / IMA_ILINK_API_KEY")
        return 1

    # 1) 找 / 建 KB
    kb_id = find_kb_by_name(client, args.kb_name)
    if kb_id:
        print(f"[kb] 复用现有 KB：name={args.kb_name!r}  id={kb_id}")
    else:
        print(f"[kb] 创建 KB：name={args.kb_name!r}  type=KBT_MINE_KB")
        kb_id, kb_name = client.create_knowledge_base(
            args.kb_name, args.kb_desc, type_=ImaClient.KB_TYPE_MINE,
        )
        print(f"[kb] 创建成功  id={kb_id}  name={kb_name!r}")

    # 2) 列已有 title，幂等跳过
    print("[kb] 列已有 title...")
    existing = list_existing_titles(client, kb_id)
    print(f"[kb] 已存在 {len(existing)} 条")

    # 3) 灌：先用线程池预筛掉已存在的，剩下的并行灌
    pending: list[tuple[str, str, str]] = []
    skipped = 0
    for stem, title, content in chunks:
        if title in existing:
            skipped += 1
        else:
            pending.append((stem, title, content))

    created = 0
    failed = 0
    failed_examples: list[str] = []
    t0 = time.perf_counter()
    if pending:
        log.info(
            "[upload] %d 条待灌，workers=%d", len(pending), args.workers,
        )

        def _upload_one(
            client: ImaClient, kb_id: str, title: str, content: str,
        ) -> tuple[str, str, str]:
            """返回 ``(status, title, note_id_or_err)``。
            ``status`` ∈ ``{"created", "failed"}``。
            """
            try:
                note_id = client.import_doc(title, content, content_format=1)
                if not note_id:
                    return ("failed", title, "no note_id")
                client.add_knowledge(kb_id, title, note_id=note_id, media_type=11)
                return ("created", title, note_id)
            except Exception as exc:
                return ("failed", title, str(exc))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_upload_one, client, kb_id, title, content): (i, title)
                for i, (stem, title, content) in enumerate(pending, 1)
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                status, title, info = fut.result()
                if status == "created":
                    created += 1
                else:
                    failed += 1
                    if len(failed_examples) < 5:
                        failed_examples.append(f"  {title[:40]!r}: {info}")
                if done % 10 == 0 or done == len(pending):
                    elapsed = time.perf_counter() - t0
                    log.info(
                        "[upload] %d/%d  created=%d  failed=%d  elapsed=%.1fs",
                        done, len(pending), created, failed, elapsed,
                    )

    print()
    print("=" * 60)
    print(f"KB ID   : {kb_id}")
    print(f"KB name : {args.kb_name}")
    print(f"created : {created}")
    print(f"skipped : {skipped}")
    print(f"failed  : {failed}")
    print()
    print("下一步：把 KB ID 写入 .env 或 /etc/clawbot/ima.env：")
    print(f"  IMA_ILINK_DEFAULT_KB={kb_id}")

    # 清理 staging（除非 --keep-staging）
    if staging_path and staging_path.exists() and not args.keep_staging:
        try:
            shutil.rmtree(staging_path)
        except OSError:
            pass

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
