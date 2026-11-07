#!/usr/bin/env python3
"""Q&A 蒸馏：把客服话术长段落 → Q&A 短句对。

把 ``docs/knowledge/qa-NNN-<源>.md``（``import_business_lang.py --only-local`` 落地）里
的 47 个长段落读出来，逐条调 LLM 改成 3-5 对 Q&A 短句，同时：

  1) 落本地：``docs/knowledge/qa-NNN-<源>-<seq>.md``（frontmatter 兼容 LocalKBIndex）
  2) 灌 IMA：``clawbot-客服话术-qa``（个人 KB，KBT_MINE_KB，自动新建）

LLM 来源：``~/.claude/settings.json`` 的 ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` /
``ANTHROPIC_DEFAULT_HAIKU_MODEL``（实测 MiniMax-M2.7-highspeed 比 deepseek-v4-flash 语气更地道）。

**断点续跑**：每个源蒸馏完后原子写 cache（``<cache_dir>/<md5>.json``，tmp+rename），
下次启动自动跳过已缓存的源。cache key 含源文件 ``mtime + size``，源改了自动失效。
Cache 目录可通过 ``QA_DISTILL_CACHE`` 环境变量覆盖（默认 ``/tmp/qa_distill_cache``）。

用法：
    # 1) 试跑 3 条，只看 LLM 输出
    python3 utils/qa_distill.py --dry-run --limit 3

    # 2) 落本地 + 灌 IMA，4 worker 并发（默认会跳过已缓存源）
    python3 utils/qa_distill.py --workers 4

    # 3) 仅本地，不调 IMA
    python3 utils/qa_distill.py --only-local

    # 4) 强制重跑（忽略 cache）
    python3 utils/qa_distill.py --no-resume

    # 5) 自定义 cache 目录
    QA_DISTILL_CACHE=/var/cache/qa_distill python3 utils/qa_distill.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 优先从 settings.json 拿 LLM 凭据（CLAUDE_CODE 自身用的）；.env 是 IMA 凭据
SETTINGS = json.loads((Path.home() / ".claude" / "settings.json").read_text())
ENV = SETTINGS.get("env", {})
LLM_BASE = ENV["ANTHROPIC_BASE_URL"].rstrip("/")
LLM_TOKEN = ENV["ANTHROPIC_AUTH_TOKEN"]
LLM_MODEL = ENV.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from ima import ImaClient, ImaConfig  # noqa: E402
from utils.seed_ima_kb import (  # noqa: E402
    find_kb_by_name,
    list_existing_titles,
    write_one_local,
)
from utils.logging_setup import setup_logging, get_logger  # noqa: E402


log = get_logger("qa_distill")


KB_NAME_DEFAULT = "clawbot-客服话术-qa"
KB_DESC_DEFAULT = "客服话术蒸馏 Q&A 短句版（qa_distill.py 自动生成）"
SOURCE_DIR_DEFAULT = ROOT / "docs" / "knowledge"
LOCAL_OUT_DEFAULT = ROOT / "docs" / "knowledge"
LLM_TIMEOUT_S = 30
LLM_MAX_TOKENS = 1024
LLM_RETRIES = 3
LLM_RETRY_DELAYS = (2, 4, 8)
MAX_INPUT_CHARS = 2400  # 输入到 LLM 的内容上限（防 prompt 过长）

# Cache：每个源蒸馏完写一份，含 mtime/size 校验
CACHE_DIR = Path(os.environ.get("QA_DISTILL_CACHE", "/tmp/qa_distill_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(source_path: Path) -> str:
    return hashlib.md5(str(source_path.resolve()).encode("utf-8")).hexdigest()


def _cache_path(source_path: Path) -> Path:
    return CACHE_DIR / f"{_cache_key(source_path)}.json"


def _cache_load(source_path: Path) -> Optional[list[dict]]:
    """读 cache：mtime/size 匹配才返回 notes，否则当失效。"""
    cp = _cache_path(source_path)
    if not cp.is_file():
        return None
    try:
        stat = source_path.stat()
        data = json.loads(cp.read_text(encoding="utf-8"))
        if data.get("mtime") != stat.st_mtime or data.get("size") != stat.st_size:
            return None
        notes = data.get("notes")
        if not isinstance(notes, list):
            return None
        return notes
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _cache_save(source_path: Path, notes: list[dict]) -> None:
    """原子写 cache：写 .tmp 然后 rename，避免半截文件。"""
    stat = source_path.stat()
    payload = {"mtime": stat.st_mtime, "size": stat.st_size, "notes": notes}
    cp = _cache_path(source_path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{cp.name}.", suffix=".tmp", dir=str(CACHE_DIR),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, cp)  # 原子 rename
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


SYSTEM_PROMPT = """你是客服话术编辑。把下面这段客服话术原文改写成 3-5 对 Q&A 短句。

【严格要求】
- Q：用户视角的自然问句（≤30 字），用买家/客户会问的原话，不要书面化
- A：客服视角的回复（≤150 字），直接给答案，不要客套话开头
- 每对 Q/A 必须独立可读：A 不能引用前文，要自包含
- 不要重复原文整段，只挑最有代表性的 3-5 个点
- 原文是 Markdown 表格时，按行拆；散文按段拆
- 输出严格 JSON，结构：`{"qa": [{"q": "...", "a": "..."}, ...]}`
"""


# ---------------------------------------------------------------------------
# 1. LLM 调用
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """LLM 偶发包 ```json ... ``` 代码块，剥掉再 parse。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*\n", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def call_llm(content: str, title: str) -> list[dict[str, str]]:
    """调一次 LLM，返回 ``[{"q": ..., "a": ...}, ...]``。失败抛 RuntimeError。

    用 ``urllib.request`` 原生调用 Anthropic /v1/messages，复用 ~/.claude/settings.json
    里的凭据；不引入 anthropic SDK。
    """
    user_prompt = (
        f"原文标题：{title}\n"
        f"原文内容：\n{content}\n\n"
        f"请输出 JSON。"
    )

    req_body = json.dumps({
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{LLM_BASE}/v1/messages",
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": LLM_TOKEN,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    last_exc: Optional[Exception] = None
    for attempt in range(1, LLM_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                body = json.loads(resp.read())
            text = "".join(
                b.get("text", "") for b in body.get("content", [])
                if b.get("type") == "text"
            )
            parsed = json.loads(_strip_code_fence(text))
            qa_list = parsed.get("qa", [])
            if not isinstance(qa_list, list) or not qa_list:
                raise ValueError(f"empty qa list: {text[:200]}")
            cleaned = []
            for item in qa_list:
                q = str(item.get("q", "")).strip()
                a = str(item.get("a", "")).strip()
                if not q or not a:
                    continue
                cleaned.append({"q": q[:100], "a": a[:300]})
            if not cleaned:
                raise ValueError(f"all qa entries empty: {text[:200]}")
            return cleaned
        except Exception as exc:
            last_exc = exc
            log.warning("LLM 调用失败 attempt=%d: %s", attempt, exc)
            if attempt < LLM_RETRIES:
                time.sleep(LLM_RETRY_DELAYS[attempt - 1])
    raise RuntimeError(f"LLM 调用 {LLM_RETRIES} 次全失败: {last_exc}")


# ---------------------------------------------------------------------------
# 2. 源数据读取
# ---------------------------------------------------------------------------


def read_source_note(path: Path) -> tuple[str, str, str]:
    """读 ``docs/knowledge/qa-NNN-*.md``，返回 ``(title, plain_content, topic)``。
    topic 从 frontmatter `topic:` 取（默认空串）。
    """
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if m:
        fm, body = m.group(1), m.group(2)
        title_m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
        topic_m = re.search(r"^topic:\s*(.+)$", fm, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else path.stem
        topic = topic_m.group(1).strip() if topic_m else ""
        return title, body.strip(), topic
    return path.stem, text.strip(), ""


def list_source_notes(source_dir: Path, topic_filter: str = "") -> list[Path]:
    """列 qa-NNN-*.md 但跳过 qa-derived-*（避免递归蒸馏）。

    ``topic_filter``: 非空时只返回 frontmatter `topic:` 等于它的文件。
    空时返回所有。配合 ``--topic-filter clawbot-客服话术`` 可避开
    seed_ima_kb 的 clawbot 自身 Q&A。
    """
    out: list[Path] = []
    for p in sorted(source_dir.glob("qa-*.md")):
        if p.stem.startswith("qa-derived"):
            continue
        if topic_filter:
            _, _, topic = read_source_note(p)
            if topic != topic_filter:
                continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 3. 输出格式化
# ---------------------------------------------------------------------------


def _safe_title_for_ima(*parts: str) -> str:
    s = " · ".join(p for p in parts if p)
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] or "untitled"


def format_pair_md(seq: int, source_topic: str, category: str, q: str, a: str) -> tuple[str, str]:
    """生成单条 note 的 (title, content)。

    title 设计：``QA · {topic} · {seq:03d} · {q[:12]}``
    - 含 ``QA ·`` 让运维一眼看出是蒸馏产物
    - 含 seq 让多 chunk 同 topic 时不冲突（虽然 category 也编码过）
    - **含 Q 前缀**（关键）：让 IMA title 索引直接命中长词关键词（"运费险怎么赔"
      这种问题字面），否则 IMA 只看 title 不细看 content 找不到。
    """
    q_prefix = re.sub(r"[\\/:*?\"<>|\s·]+", "", q)[:12]  # 去掉标点空格
    title = _safe_title_for_ima(f"QA·{source_topic}", f"{seq:03d}", q_prefix)
    fm = (
        "---\n"
        f"title: {title}\n"
        f"topic: clawbot-客服话术-qa\n"
        f"category: {category}\n"
        f"keywords: []\n"
        "---\n\n"
    )
    body = f"# {title}\n\n## Q\n{q}\n\n## A\n{a}\n"
    return title, fm + body


def qa_pairs_to_notes(
    qa_list: list[dict[str, str]],
    source_path: Path,
    source_title: str,
) -> list[dict]:
    """LLM 返回的 Q&A 列表 → ``[{title, content, ...}, ...]``。"""
    stem = re.sub(r"^qa-\d+-", "", source_path.stem)
    topic_m = re.search(r"[一-鿿【】]+", source_title)
    topic = topic_m.group(0) if topic_m else stem

    notes = []
    for i, pair in enumerate(qa_list, 1):
        title, content = format_pair_md(i, topic, stem, pair["q"], pair["a"])
        notes.append({
            "title": title,
            "content": content,
            "topic": "clawbot-客服话术-qa",
            "category": stem,
            "kws": [],
        })
    return notes


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------


@dataclass
class DistillResult:
    source: str
    ok: bool
    error: str = ""
    qa_count: int = 0
    notes: list[dict] = None  # type: ignore
    elapsed_s: float = 0.0
    from_cache: bool = False  # True 表示命中 cache（无 LLM 调用）

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


def distill_one(path: Path, use_cache: bool = True) -> DistillResult:
    """处理一个源 note，返回 DistillResult。

    优先级：cache 命中 → 直接返回；否则调 LLM，完后原子写 cache。
    ``use_cache=False`` 强制重跑（忽略已有 cache）。
    """
    t0 = time.perf_counter()
    if use_cache:
        cached = _cache_load(path)
        if cached is not None:
            return DistillResult(
                source=path.name, ok=True,
                qa_count=len(cached), notes=cached,
                elapsed_s=time.perf_counter() - t0,
                from_cache=True,
            )
    source_title, source_content, source_topic = read_source_note(path)
    truncated = source_content[:MAX_INPUT_CHARS]
    if len(source_content) > MAX_INPUT_CHARS:
        log.warning("[%s] 内容超 %d 字符，截断到 %d",
                    path.name, len(source_content), MAX_INPUT_CHARS)
    try:
        qa_list = call_llm(truncated, source_title)
        notes = qa_pairs_to_notes(qa_list, path, source_title)
        # 原子写 cache（即便后续 IMA 上传失败，distill 结果也不会丢）
        try:
            _cache_save(path, notes)
        except OSError as exc:
            log.warning("[%s] cache 写入失败（不影响结果）：%s", path.name, exc)
        return DistillResult(
            source=path.name, ok=True,
            qa_count=len(notes), notes=notes,
            elapsed_s=time.perf_counter() - t0,
        )
    except Exception as exc:
        return DistillResult(
            source=path.name, ok=False, error=str(exc),
            elapsed_s=time.perf_counter() - t0,
        )


def upload_notes_to_ima(
    client: ImaClient, kb_id: str, notes: list[dict], workers: int,
    batch_size: int = 100, batch_pause: float = 10.0,
) -> tuple[int, int, int]:
    """分批并行 import_doc + add_knowledge，批间 sleep 让 IMA 冷却。

    默认 ``workers=3`` + ``batch_size=100`` + ``batch_pause=10s``：
    实测在 1540 条规模下稳过 IMA 限流（``code=200001``）不卡死。

    返回 ``(created, skipped, failed)``。
    """
    existing = list_existing_titles(client, kb_id)
    pending = [n for n in notes if n["title"] not in existing]
    skipped = len(notes) - len(pending)
    if not pending:
        log.info("[upload] 0 待灌（%d 已存在）", skipped)
        return 0, skipped, 0

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    log.info(
        "[upload] %d 条待灌（%d 已存在），分 %d 批（每批 ≤%d，间隔 %.1fs），workers=%d",
        len(pending), skipped, len(batches), batch_size, batch_pause, workers,
    )

    def _one(c, k, title, content):
        try:
            note_id = c.import_doc(title, content, content_format=1)
            if not note_id:
                return ("failed", title, "no note_id")
            c.add_knowledge(k, title, note_id=note_id, media_type=11)
            return ("created", title, note_id)
        except Exception as exc:
            return ("failed", title, str(exc))

    created = failed = 0
    t0 = time.perf_counter()
    for batch_idx, batch in enumerate(batches, 1):
        batch_t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_one, client, kb_id, n["title"], n["content"]): n["title"]
                for n in batch
            }
            done = 0
            for fut in as_completed(futures):
                done += 1
                status, title, info = fut.result()
                if status == "created":
                    created += 1
                else:
                    failed += 1
                    log.warning("[upload] %s FAILED: %s", title[:30], info)
        batch_elapsed = time.perf_counter() - batch_t0
        log.info(
            "[upload] 批次 %d/%d  本批 %d 条  created=%d  failed=%d  批用时 %.1fs",
            batch_idx, len(batches), len(batch), done, 0, batch_elapsed,
        )
        # 批间 sleep：让 IMA 限流冷却；最后一批不 sleep
        if batch_idx < len(batches):
            log.info("[upload] sleep %.1fs 让 IMA 冷却...", batch_pause)
            time.sleep(batch_pause)

    elapsed = time.perf_counter() - t0
    log.info("[upload] 全部完成  created=%d  failed=%d  total=%.1fs",
             created, failed, elapsed)
    return created, skipped, failed


def list_derived_notes(local_out: Path) -> list[dict]:
    """读 ``local_out`` 里所有 ``qa-NNN-*-QA·*.md``（蒸馏产物），返回 ``[{title, content, ...}, ...]``。"""
    out: list[dict] = []
    for p in sorted(local_out.glob("qa-*QA·*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
        if not m:
            continue
        fm, body = m.group(1), m.group(2)
        title_m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
        topic_m = re.search(r"^topic:\s*(.+)$", fm, re.MULTILINE)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        topic = topic_m.group(1).strip() if topic_m else ""
        out.append({
            "title": title,
            "content": body.strip(),
            "topic": topic,
            "category": "",
            "kws": [],
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="客服话术长段落 → Q&A 短句蒸馏")
    parser.add_argument("--src", default=str(SOURCE_DIR_DEFAULT),
                        help=f"源 .md 目录（默认 {SOURCE_DIR_DEFAULT}）")
    parser.add_argument("--local-out", default=str(LOCAL_OUT_DEFAULT),
                        help=f"本地落盘目录（默认 {LOCAL_OUT_DEFAULT}）")
    parser.add_argument("--kb-name", default=KB_NAME_DEFAULT,
                        help=f"目标 IMA KB 名（默认 {KB_NAME_DEFAULT}）")
    parser.add_argument("--kb-desc", default=KB_DESC_DEFAULT, help="KB 描述")
    parser.add_argument("--kb-id", default="", help="显式指定 KB ID（跳过查重/创建）")
    parser.add_argument("--topic-filter", default="clawbot-客服话术",
                        help="只蒸馏 frontmatter topic 等于这个值的源（空 = 全部；"
                             "默认 'clawbot-客服话术' 用于避开 seed_ima_kb 输出）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个源（0 = 全部）")
    parser.add_argument("--workers", type=int, default=3,
                        help="Stage 1 (LLM) + Stage 3 (IMA) 并发 worker 数（默认 3，"
                             "上限参考 IMA rate limit 规格；不要轻易加大）")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Stage 3 IMA 上传每批笔记数（默认 100；批间 sleep）")
    parser.add_argument("--batch-pause", type=float, default=10.0,
                        help="Stage 3 批间 sleep 秒数（默认 10s，让 IMA 冷却）")
    parser.add_argument("--dry-run", action="store_true", help="只调 LLM 不落库")
    parser.add_argument("--only-local", action="store_true",
                        help="只写本地 docs/knowledge/，不调 IMA")
    parser.add_argument("--from-local", action="store_true",
                        help="跳过 LLM 蒸馏，直接读本地已落地的 qa-*QA·*.md 上传 IMA")
    parser.add_argument("--no-resume", action="store_true",
                        help="忽略 cache 强制重跑（默认会用 cache 跳过已蒸馏的源）")
    args = parser.parse_args()

    src_dir = Path(args.src)
    local_out = Path(args.local_out)
    if args.from_local:
        print(f"[run] --from-local 模式：从 {local_out} 读 qa-*QA·*.md")
        sources = []  # 不需要 source
    else:
        sources = list_source_notes(src_dir, topic_filter=args.topic_filter)
        if args.limit > 0:
            sources = sources[: args.limit]

    print(f"[run] model={LLM_MODEL}")
    print(f"[run] endpoint={LLM_BASE}")
    print(f"[run] src={src_dir}  sources={len(sources)}  workers={args.workers}")
    print(f"[run] dry_run={args.dry_run}  only_local={args.only_local}  "
          f"kb_name={args.kb_name!r}")
    print(f"[run] cache={CACHE_DIR}  resume={not args.no_resume}")

    if not args.dry_run and not args.only_local:
        from utils.logging_setup import setup_logging
        setup_logging(level=os.getenv("CLAWBOT_LOG_LEVEL", "INFO"))

    # ───────── Stage 1: 并发调 LLM（带 cache 跳过）─────────
    print(f"\n[stage 1] LLM 蒸馏 ({len(sources)} 条源)  workers={args.workers}")
    results: list[DistillResult] = []
    failed = 0
    from_cache_count = 0
    t0 = time.perf_counter()
    use_cache = not args.no_resume

    def _distill(p):
        return distill_one(p, use_cache=use_cache)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_distill, p): p for p in sources}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            mark = "✓" if r.ok else "✗"
            cache_tag = " [cached]" if r.from_cache else ""
            extra = "" if r.ok else f"  ({r.error[:80]})"
            print(f"  {mark}{cache_tag} {r.source:55s}  qa={r.qa_count:2d}  "
                  f"elapsed={r.elapsed_s:.1f}s{extra}", flush=True)
            if not r.ok:
                failed += 1
            elif r.from_cache:
                from_cache_count += 1

    total_qa = sum(r.qa_count for r in results)
    fresh_count = len([r for r in results if r.ok and not r.from_cache])
    print(f"\n[stage 1] 完成：{len(results)} 源 → {total_qa} 对 Q&A，"
          f"{failed} 条源失败  "
          f"({fresh_count} 新蒸馏, {from_cache_count} 命中 cache)  "
          f"耗时 {time.perf_counter()-t0:.1f}s")

    # 收集所有 notes
    all_notes: list[dict] = []
    for r in results:
        all_notes.extend(r.notes)

    if args.dry_run:
        for r in results:
            if r.ok and r.notes:
                print("\n[dry-run] 样例（来自 %s）：" % r.source)
                print(f"  title: {r.notes[0]['title']}")
                print(f"  content ({len(r.notes[0]['content'])} 字符):")
                print("  " + r.notes[0]["content"].replace("\n", "\n  ")[:400])
                break
        return 0

    # ───────── Stage 2: 落本地（--from-local 跳过）─────────
    if args.from_local:
        # 直接从本地读蒸馏产物
        all_notes = list_derived_notes(local_out)
        print(f"\n[stage 2 skip] --from-local：读到 {len(all_notes)} 条已落地的 Q&A")
        if not all_notes:
            print("[err] 没有本地 qa-*QA·*.md 可用，先跑一次完整流水线")
            return 1
    else:
        # 直接用 title 算文件名，避免 write_one_local 的 hash 冲突。
        # 命名：qa-{seq:03d}-{source_stem}-{title_slug}.md
        print(f"\n[stage 2] 写本地 {local_out}")
        local_out.mkdir(parents=True, exist_ok=True)
        n_local = 0
        n_skip = 0
        for seq, n in enumerate(all_notes, 1):
            title_slug = re.sub(r"[\\/:*?\"<>|\s]+", "-", n["title"]).strip("-")[:60]
            stem_slug = re.sub(r"[\\/:*?\"<>|\s·]+", "-", n.get("category", "qa")).strip("-")[:40]
            fname = f"qa-{seq:03d}-{stem_slug}-{title_slug}.md"
            path = local_out / fname
            if path.exists():
                n_skip += 1
                continue
            path.write_text(n["content"], encoding="utf-8")
            n_local += 1
        print(f"[stage 2] 新写 {n_local} 个 .md（{n_skip} 已存在跳过）")

    if args.only_local:
        return 0

    n_local = 0  # 默认值，--from-local 跳过 Stage 2 不修改它

    # ───────── Stage 3: 灌 IMA ─────────
    client = ImaClient(ImaConfig.from_env(env_files=None))
    if not client.configured():
        print("[err] IMA 凭据缺失，无法灌库")
        return 1

    kb_id = args.kb_id
    if not kb_id:
        kb_id = find_kb_by_name(client, args.kb_name)
        if kb_id:
            print(f"\n[stage 3] 复用现有 KB：name={args.kb_name!r}  id={kb_id}")
        else:
            print(f"\n[stage 3] 创建 KB：name={args.kb_name!r}  type=KBT_MINE_KB")
            kb_id, kb_name = client.create_knowledge_base(
                args.kb_name, args.kb_desc, type_=ImaClient.KB_TYPE_MINE,
            )
            print(f"[stage 3] 创建成功  id={kb_id}  name={kb_name!r}")

    created, skipped, upload_failed = upload_notes_to_ima(
        client, kb_id, all_notes, args.workers,
        batch_size=args.batch_size, batch_pause=args.batch_pause,
    )

    print()
    print("=" * 60)
    print(f"KB ID   : {kb_id}")
    print(f"KB name : {args.kb_name}")
    print(f"LLM OK  : {len(results) - failed} / {len(results)} 源")
    print(f"created : {created}")
    print(f"skipped : {skipped}")
    print(f"failed  : {upload_failed}")
    print(f"local   : {n_local} 新写")
    print()
    print("下一步：把 KB ID 写入 .env，逗号分隔与 v2 共存：")
    v2_kb = "ni8v9SS002WYG8G6MInst9ViwZU9crjPxxCyH7Yjkzw="
    print(f"  IMA_ILINK_DEFAULT_KB={v2_kb},{kb_id}")
    return 0 if failed == 0 and upload_failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
