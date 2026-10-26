"""ClawBot 侧的 xiaolvshu 桥客户端。

多用户共享的 ClawBot：每个发消息的人（from_user_id）在桥那边有自己的 vault、
输出目录、公众号凭据。这边只负责：

  1. 认指令（斜杠命令 + 自然语言/语音意图）
  2. 调桥（子进程，stdout 一行 JSON）
  3. 把结果变成微信里能看懂的回复文本

桥的位置在环境变量里：
  XIAOLVSHU_BRIDGE    默认 /opt/obsidian-xiaolvshu/scripts/xiaolvshu_bridge.py
  XIAOLVSHU_PYTHON   默认 /usr/bin/python3（要带 Pillow + pyyaml）
  XIAOLVSHU_TIMEOUT  默认 240 秒（出图 + 上传 + 调接口）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

BRIDGE = os.environ.get(
    "XIAOLVSHU_BRIDGE", "/opt/obsidian-xiaolvshu/scripts/xiaolvshu_bridge.py"
)
PYTHON = os.environ.get("XIAOLVSHU_PYTHON", "/usr/bin/python3")
TIMEOUT = float(os.environ.get("XIAOLVSHU_TIMEOUT", "240"))
# 钉住桥的版本（git commit sha 或 tag）。桥在另一个仓库、另一台机器上也可能
# 被改 —— 这个 pin 让「A 用的是哪版桥」变成可查的事实，而不是靠记忆。
# 生产上装的桥的 HEAD 跟 pin 对不上时，_check_bridge_version() 会记一条告警。
BRIDGE_REF = os.environ.get("XIAOLVSHU_BRIDGE_REF", "").strip()

_version_checked = False


def _check_bridge_version() -> None:
    """生产上跑的桥，是不是 .env 里钉的那一版？（只查一次）"""
    global _version_checked
    if _version_checked:
        return
    _version_checked = True
    if not BRIDGE_REF:
        return
    repo = Path(BRIDGE).resolve().parent.parent
    if not (repo / ".git").exists():
        return  # 不是 git 部署的（拷贝过去的），没法比
    try:
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if not head:
        return
    # 工作区有未提交改动 → 装的就不是 HEAD 那版（HEAD 比不出来）
    try:
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        dirty = ""
    if dirty:
        from utils.logging_setup import get_logger
        get_logger("message").warning(
            "xiaolvshu bridge has uncommitted changes repo=%s "
            "（装到生产的不是 pin 那版 —— 先提交再部署）",
            str(repo),
        )
    if head.startswith(BRIDGE_REF) or BRIDGE_REF.startswith(head[:7]):
        return
    from utils.logging_setup import get_logger
    get_logger("message").warning(
        "xiaolvshu bridge version drift: pinned=%s installed=%s repo=%s "
        "（.env 的 XIAOLVSHU_BRIDGE_REF 与生产上的桥不一致）",
        BRIDGE_REF[:12], head[:12], str(repo),
    )

# ── 斜杠命令 ────────────────────────────────────────────────────────────────
# /发贴图号 沉香   /发公众号 沉香   /发列表   /发状态   /选 2   /重发 沉香
_CMD_RE = re.compile(
    r"^/(发贴图号|发公众号|发列表|发状态|选|重发|发)\s*(.*)$", re.S
)

# ── 自然语言 / 语音意图 ─────────────────────────────────────────────────────
# 只在「动词 + 目标词」都出现在开头 14 字里才接管，避免把普通聊天误吃掉。
_VERBS = ("发", "推送", "推", "投稿", "发布", "排版")
_NEWSPIC_TARGETS = ("贴图号", "小绿书", "图片消息", "贴图")
_ARTICLE_TARGETS = ("公众号文章", "公众号", "图文", "文章", "推文")
_ANY_TARGET = ("草稿箱", "草稿")
_ALL_TARGETS = _NEWSPIC_TARGETS + _ARTICLE_TARGETS + _ANY_TARGET
_HEAD = 14

# 关键词清洗：去掉指令词和客套话，剩下的才是笔记标题关键词
_STRIP_WORDS = (
    "请", "帮我", "麻烦", "给我", "把", "将", "一下", "那篇", "这篇", "那个",
    "这个", "已经", "现在", "马上", "立刻", "谢谢", "吧", "呢", "啊", "的话",
    "内容", "稿子", "笔记", "到", "去", "个", "一篇", "来", "成", "为", "做",
)


def _clean_keyword(text: str, kind: str | None = None) -> str:
    """把一句话削成笔记标题关键词。

    动词和目标词都要削掉 —— 「帮我把沉香那篇发到草稿箱」里
    「发」和「草稿箱」是指令，不是标题的一部分。
    """
    kw = text
    for w in _ALL_TARGETS:
        kw = kw.replace(w, " ")
    for w in _VERBS:
        kw = kw.replace(w, " ")
    for w in _STRIP_WORDS:
        kw = kw.replace(w, " ")
    kw = re.sub(r"[\s，,。.！!？?、：:；;\"'“”‘’（）()\[\]【】…]+", " ", kw)
    return re.sub(r"\s+", " ", kw).strip()


def parse_command(text: str):
    """→ (动作, 参数) 或 None。动作：publish / list / status / select / republish。"""
    m = _CMD_RE.match((text or "").strip())
    if not m:
        return None
    cmd, arg = m.group(1), m.group(2).strip()
    if cmd == "发贴图号":
        return ("publish", ("newspic", arg))
    if cmd == "发公众号":
        return ("publish", ("article", arg))
    if cmd == "发列表":
        return ("list", arg)
    if cmd == "发状态":
        return ("status", "")
    if cmd == "选":
        try:
            return ("select", int(arg))
        except ValueError:
            return ("status", "")
    if cmd == "重发":
        return ("republish", arg)
    if cmd == "发":
        # /发 <关键词> —— 目标词都没说，让桥按笔记自己判断
        return ("publish", ("auto", arg))
    return None


def detect_intent(text: str):
    """自然语言 / 语音转写里的发布意图 → (kind|None, 关键词) 或 None。"""
    text = (text or "").strip()
    if not text:
        return None
    head = text[:_HEAD]
    if not any(v in head for v in _VERBS):
        return None
    kind = None
    if any(t in head for t in _NEWSPIC_TARGETS):
        kind = "newspic"
    elif any(t in head for t in _ARTICLE_TARGETS):
        kind = "article"
    elif any(t in head for t in _ANY_TARGET):
        kind = "auto"
    if kind is None:
        return None
    kw = _clean_keyword(text, kind)
    return (kind, kw)


# ── 调桥 ────────────────────────────────────────────────────────────────────

def _run_bridge(args: list[str], uid: str) -> dict:
    _check_bridge_version()
    cmd = [PYTHON, BRIDGE, "--user", uid, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd="/tmp"
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout",
                "error": f"超过 {int(TIMEOUT)} 秒还没跑完"}
    except OSError as e:
        return {"ok": False, "code": "spawn_failed", "error": str(e)}
    # stdout 末尾一行是 JSON（前面可能有渲染日志，但桥把它们都赶到 stderr 了）
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    tail = (proc.stderr or proc.stdout or "").strip()[-600:]
    return {"ok": False, "code": "bridge_failed", "error": tail or "桥没有输出"}


async def call_bridge(args: list[str], uid: str) -> dict:
    return await asyncio.to_thread(_run_bridge, args, uid)


# ── 回复文本 ────────────────────────────────────────────────────────────────

def _kind_label(kind: str) -> str:
    return {"newspic": "贴图号", "article": "图文"}.get(kind, "文章")


def format_result(action: str, res: dict) -> str:
    if res.get("ok"):
        if action == "list":
            rows = res.get("results") or []
            if not rows:
                return "你的知识库里还没有能发的稿子。"
            lines = ["你的知识库里这些能发："]
            for i, r in enumerate(rows, 1):
                lines.append(f"{i}. {r['title']}（{r.get('mtime_str', '')}）")
            lines.append("发哪篇：/发贴图号 关键词 或 /发公众号 关键词")
            return "\n".join(lines)
        if action == "status":
            entries = res.get("entries") or []
            if not entries:
                return "还没发过任何东西。"
            lines = ["最近发过："]
            for e in entries:
                lines.append(
                    f"· {e.get('ts')} {_kind_label(e.get('kind', ''))}"
                    f"《{e.get('title', '')}》"
                )
            return "\n".join(lines)
        # publish
        kind = res.get("kind", "")
        extra = ""
        if res.get("cards"):
            extra = f"\n{res['cards']} 张图"
        mid = res.get("media_id") or "（没拿到 media_id）"
        return (
            f"✅ {_kind_label(kind)}已进草稿箱\n"
            f"《{res.get('title', '')}》{extra}\n"
            f"media_id: {mid}\n"
            f"去公众号后台草稿箱核对后发表。贴图标题上限 20 字。"
        )

    code = res.get("code", "")
    err = res.get("error", "")
    if code == "need_select":
        lines = [f"命中多篇，选一个（回复 /选 序号）："]
        for c in res.get("candidates", []):
            lines.append(f"{c['i']}. {c['title']}")
        return "\n".join(lines)
    if code == "duplicate":
        e = res.get("entry") or {}
        return (
            f"⚠️ 这篇 {e.get('ts', '')} 已经推过了"
            f"（{_kind_label(e.get('kind', ''))}，media_id {e.get('media_id', '') or '无'}）。\n"
            f"要重推就说：/重发 关键词"
        )
    if code == "not_found":
        return (
            f"你的知识库里没找到「{res.get('keyword', '')}」。\n"
            f"你的 vault：{res.get('vault', '')}\n"
            f"把 .md 放进去，或者先 /发列表 看看能发什么。"
        )
    if code == "no_creds":
        return (
            "还没配公众号凭据，发不出去。\n"
            f"{res.get('hint', '')}"
        )
    if code == "cross_vault":
        return "这篇不在你的知识库里，发不了。"
    if code == "timeout":
        return f"⏱ {err}\n出图 + 上传比较慢，可以再发一次同样的指令。"
    if code == "no_pending":
        return "没有待选的文章了。先 /发贴图号 关键词。"
    if code == "bad_index":
        return err
    return f"❌ 发布失败：{err or code}"


# ── 入口 ────────────────────────────────────────────────────────────────────

async def _publish(uid: str, kind: str, keyword: str, force: bool = False) -> str:
    """把一篇推到草稿箱。kind 可以是 newspic / article / auto（auto = 桥去猜）。

    多命中时桥会返回 need_select 并把候选存进 pending，之后 /选 N 接着发。
    """
    if not keyword:
        return "发哪篇？给个标题关键词，比如「发贴图号 沉香」。"
    args = ["publish", "--type", kind, "--keyword", keyword]
    if force:
        args.append("--force")
    res = await call_bridge(args, uid)
    return format_result("publish", res)


async def handle(text: str, uid: str) -> str | None:
    """认领这条消息。返回 None = 不是发布类消息，交给 AI 照常回答。"""
    cmd = parse_command(text)
    if cmd is None:
        intent = detect_intent(text)
        if intent is None:
            return None
        kind, keyword = intent
        return await _publish(uid, kind, keyword)

    action, payload = cmd
    if action == "publish":
        kind, keyword = payload
        return await _publish(uid, kind, keyword)
    if action == "republish":
        return await _publish(uid, "auto", payload, force=True)
    if action == "list":
        res = await call_bridge(["search", "--keyword", payload], uid)
        return format_result("list", res)
    if action == "status":
        res = await call_bridge(["status"], uid)
        return format_result("status", res)
    if action == "select":
        res = await call_bridge(["select", "--index", str(payload)], uid)
        return format_result("publish", res)
    return None
