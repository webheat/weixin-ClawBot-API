"""一次性脚本：用 IMA OpenAPI 灌一个测试知识库，验证 search→inject→LLM 链路。

设计目标：
  - 生成 N 条（默认 150）Q&A 对，主题可配
  - 每条以 Markdown 形式 import_doc 创建 note
  - 紧接着 add_knowledge 把 note 挂到一个**新建的 KBT_MINE_KB 个人 KB**
  - 输出 KB ID 供用户写入 .env

可重入：每个 note 的 title 用 ``<prefix>-NNNN`` 编号，跳过服务端已存在的；
KB 本身用固定名字（``clawbot-pipe-verify``），已存在则复用。

用法：
  ./venv/bin/python utils/seed_ima_kb.py --count 150
  ./venv/bin/python utils/seed_ima_kb.py --count 10 --topic "微信支付"
  ./venv/bin/python utils/seed_ima_kb.py --kb-name "clawbot-pipe-verify"
  ./venv/bin/python utils/seed_ima_kb.py --only-print     # 只打 Q&A，不调 API

主题默认 "clawbot 微信机器人" —— Q&A 覆盖微信长轮询 / IMA 知识库 /
DeepSeek DusAPI / 重连机制 / QR 登录 等本仓库真实概念，方便后续真人发问。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

# 允许从仓库根目录直接 python utils/seed_ima_kb.py 调用
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from ima import ImaClient, ImaConfig  # noqa: E402
from utils.logging_setup import setup_logging, get_logger  # noqa: E402


log = get_logger("seed")


# ---------------------------------------------------------------------------
# Q&A 生成
# ---------------------------------------------------------------------------

# 每条 Q&A 的骨架：(category, 问句模板, 答句模板, 关键词列表)
# 关键词会在 title 里强制出现，便于 search_knowledge 命中
QA_TEMPLATES: list[tuple[str, str, str, list[str]]] = [
    ("微信长轮询",
     "微信长轮询 getupdates 的 timeout 是多少？",
     "默认 LONG_POLL_TIMEOUT 通常 25s。配置在 bot.py 顶部常量；"
     "timeout 是 HTTP 连接最长等待时间，不是 getupdates 的入参。",
     ["长轮询", "getupdates", "timeout"]),
    ("微信长轮询",
     "getupdates 返回的消息去重逻辑在哪里？",
     "靠 sync_buf / get_updates_buf 这两个 opaque cursor，服务端保证单调推进。"
     "客户端只做透明转发，存储到 weixin_state.json。",
     ["cursor", "sync_buf"]),
    ("IMA 检索",
     "IMA 知识库 search_knowledge 是语义检索吗？",
     "不是。腾讯 ima OpenAPI 的 search_knowledge 走关键词匹配（含少量向量召回），"
     "严格按字面命中；自然语言问句常常 hits=0。详见 docs/IMA_KB.md §4。",
     ["关键词", "语义", "命中"]),
    ("IMA 检索",
     "search_knowledge 为什么没有 content / snippet 字段？",
     "IMA 服务端不返回正文，只返回 media_id / title / highlight_content / "
     "parent_folder_id / media_type 共 5 个字段。要拿全文需另调 get_doc_content。",
     ["media_id", "highlight_content"]),
    ("AI 路由",
     "clawbot 怎么处理 ima 命中 0 条的情况？",
     "_AIWithIma.chat 走 fallback：搜不到 hits 时不拼接参考资料，prompt 长度不变，"
     "日志记 mode=llm-only reason=ima-no-match。",
     ["no-match", "fallback"]),
    ("AI 路由",
     "clawbot 在 IMA 未配置时怎么表现？",
     "ImaClient.configured() 返回 False 时 _AIWithIma 直接透传到 base.chat，"
     "日志记 mode=llm-only reason=ima-not-configured。",
     ["not-configured", "透传"]),
    ("AI 路由",
     "clawbot 支持哪些 AI 提供方？",
     "DusAPI（Anthropic 格式）和 DeepSeekAPI（OpenAI 格式），"
     "通过 config.json 的 provider 字段切换。",
     ["DusAPI", "DeepSeek"]),
    ("重连",
     "clawbot 多久会强制重连？",
     "RECONNECT_CONFIG.session_duration，默认约 24 小时。"
     "warning_before 提前 X 分钟发提醒，force_before 强制触发。",
     ["重连", "session_duration"]),
    ("重连",
     "重连时怎么保留上下文？",
     "same-account 重连保留 get_updates_buf / contexts / last_contact；"
     "account 切换（ilink_bot_id 变化）全部清空。",
     ["apply_new_login", "contexts"]),
    ("QR 登录",
     "clawbot 支持网页扫码登录吗？",
     "支持。qr_web.py 起一个 127.0.0.1:18300 的 aiohttp，"
     "nginx ^~ /clawbot/ 反代出去；多用户由 qr_portal.py 在 :18300 单入口分发。",
     ["qr_web", "18300"]),
    ("QR 登录",
     "扫码后为什么还要输 verify_code？",
     "腾讯扫码分两段：第一次扫码后服务返回待确认，需要在手机上点同意。"
     "verify_code 是网页 UI 用来同步手机端操作结果的 token。",
     ["verify_code", "扫码"]),
    ("多用户",
     "多用户模式下每个用户怎么隔离？",
     "state/config/log 都按用户名分文件（weixin_state_<name>.json 等），"
     "env 走 /etc/clawbot/<name>.env + ima.env 共享兜底，端口从 env 取。",
     ["多用户", "/etc/clawbot"]),
    ("多用户",
     "clawbot 用的 systemd 单元名是什么？",
     "clawbot@<name>.service；nginx 上游走 portal 的 :18300。",
     ["systemd", "clawbot@"]),
    ("日志",
     "clawbot 日志存在哪里？",
     "logs/clawbot.log（单租户）或 logs/clawbot_<name>.log（多用户）；"
     "每天午夜切分，保留 7 天备份。",
     ["clawbot.log", "logs/"]),
    ("日志",
     "CLAWBOT_LOG_LEVEL=DEBUG 会打什么额外内容？",
     "所有 clawbot.* 子 logger 的 DEBUG 行都会打到终端；"
     "默认 INFO 级别下 DEBUG 只进文件不进终端。",
     ["DEBUG", "终端"]),
    ("IMA 写入",
     "怎么往 ima 知识库灌笔记？",
     "两步：先 POST /openapi/note/v1/import_doc 拿到 note_id，"
     "再 POST /openapi/wiki/v1/add_knowledge 带 note_info.content_id 挂到 KB。",
     ["import_doc", "add_knowledge"]),
    ("IMA 写入",
     "import_doc 一次性最多能写多大？",
     "官方实测约 1500 字符 / 单次。超长需多次 append_doc 续写。",
     ["1500", "字符"]),
    ("IMA 写入",
     "KB 类型有哪几种？",
     "KBT_MINE_KB（个人）/ KBT_SHARED_KB（共享）/ KBT_SUBSCRIBED_CREATE_KB（订阅）。"
     "API 同时接受整型 1001 / 1002 / 1004。订阅型需开通知识号。",
     ["KBT_MINE_KB", "订阅"]),
    ("AI 模型",
     "DeepSeekAPI 默认模型是什么？",
     "DeepSeek-V3 / DeepSeek-V4 均可；V4-flash 默认 thinking 关闭以减少延迟。",
     ["DeepSeek", "model"]),
    ("AI 模型",
     "DusAPI 怎么区分 claude 和 gpt？",
     "在 chat() 里按 model 名分派到 Anthropic /v1/messages 还是 GPT 兼容格式。"
     "DusConfig.model1 字段决定走哪条解析路径。",
     ["model1", "Anthropic"]),
    ("AI 模型",
     "clawbot 的 max_tokens 是多少？",
     "DeepSeekAPI / DusAPI 都写死 max_tokens=1024，"
     "因为微信消息 item 单条长度有限。",
     ["max_tokens", "1024"]),
    ("错误处理",
     "ret=-14 怎么处理？",
     "send_msg_safe 把它当 stale_token 重新抛出；"
     "message_loop 捕获后清 bot_token、sleep RETRY_DELAY、跑 login_with_qrcode。",
     ["ret=-14", "stale_token"]),
    ("错误处理",
     "网络抖动 vs 长轮询超时怎么区分？",
     "asyncio.TimeoutError + long_poll=True 视为正常心跳（_timeout: True）；"
     "其它 TimeoutError 进 retry ladder；网络异常走 network_type 标记。",
     ["TimeoutError", "long_poll"]),
    ("WeChat 协议",
     "X-WECHAT-UIN 头每次都要换吗？",
     "是。bot.py 的 make_headers 每次请求都新生成随机 uint32 → base64，"
     "不能缓存复用。",
     ["X-WECHAT-UIN", "随机"]),
    ("WeChat 协议",
     "channel_version 字段填什么？",
     "\"2.4.6\"。base_info 在每个 POST body 里都带，sanitize_bot_agent 会过滤 UA。",
     ["channel_version", "2.4.6"]),
    ("上下文",
     "context_token 怎么更新？",
     "从服务端 push 的入站消息里拿，覆盖 runtime_state[\"contexts\"][from_id]。"
     "重连后会话换了，token 也跟着换。",
     ["context_token", "覆盖"]),
    ("上下文",
     "welcomed_users 是干什么的？",
     "已发过 COMMANDS_MSG 的 from_id 集合。新用户首次触发欢迎语。",
     ["welcomed_users", "COMMANDS_MSG"]),
    ("文档",
     "clawbot 项目文档有哪些？",
     "README.md / docs/multi-user.md / docs/IMA_KB.md / docs/PORTABLE.md / "
     "TODO.md；协议参考 weixin-openclaw-api-py-docs.md。",
     ["docs/", "README"]),
    ("部署",
     "PyInstaller 怎么打包？",
     "weixin-clawbot.spec + ./venv/bin/pyinstaller 输出 dist/weixin-clawbot/，"
     "复制目录和 .env 到 U 盘根目录即可便携运行。",
     ["PyInstaller", "spec"]),
]


def generate_qa_pairs(topic: str, count: int) -> list[dict]:
    """生成 ``count`` 条 Q&A 记录（dict 含 ``title`` + ``content``）。

    模板池不足时按分类循环 + 末尾递增编号，避免重复。
    """
    rng = random.Random(0xC1A8)  # 固定种子，可复现
    pairs: list[dict] = []
    base = len(QA_TEMPLATES)
    for i in range(count):
        cat, q_tpl, a_tpl, kws = QA_TEMPLATES[i % base]
        # 模板重复时让"关键词"和"问答内容"略作变化，避免 N 条雷同
        seq = i // base + 1
        if seq > 1:
            q = q_tpl + f"（第 {seq} 个变体）"
            a = a_tpl + f" 变体说明：当 KB 中存在多条相似 Q&A 时，"
            f"search_knowledge 会按 score 排序后截断到 limit={5}。 "
        else:
            q = q_tpl
            a = a_tpl
        title = f"{topic}·{cat}·{i+1:03d}·{kws[0]}"
        content = (
            f"# {title}\n\n"
            f"## 问\n{q}\n\n"
            f"## 答\n{a}\n\n"
            f"## 关键词\n" + "、".join(kws) + "\n\n"
            f"## 主题\n{topic}\n"
        )
        pairs.append({"title": title, "content": content, "q": q, "a": a, "cat": cat, "kws": kws})
    return pairs


# ---------------------------------------------------------------------------
# 与 IMA 交互
# ---------------------------------------------------------------------------

KB_NAME = "clawbot-pipe-verify"   # 固定 KB 名字，便于复用 / 清理
KB_DESC = "clawbot 链路验证临时知识库（seed_ima_kb.py 自动创建）"
KB_TYPE = ImaClient.KB_TYPE_MINE   # 个人 KB：可写、可检索


def find_kb_by_name(client: ImaClient, name: str) -> str:
    """在当前凭据可见的 KB 里按精确名字找，返回 kb_id 或空串。"""
    cursor = ""
    while True:
        try:
            data = client._session.post(
                f"{client.cfg.base_url}/openapi/wiki/v1/search_knowledge_base",
                data=json.dumps(
                    {"query": name, "cursor": cursor, "limit": 20},
                    ensure_ascii=False,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=client.cfg.timeout,
            )
            j = data.json()
        except Exception as exc:
            log.warning("search_knowledge_base failed: %s", exc)
            return ""
        if j.get("code") != 0:
            log.warning("search_knowledge_base err: %s", j.get("msg"))
            return ""
        for item in (j.get("data") or {}).get("info_list") or []:
            if item.get("name") == name:
                return item.get("id") or item.get("kb_id") or ""
        if (j.get("data") or {}).get("is_end"):
            return ""
        cursor = (j.get("data") or {}).get("next_cursor") or ""


def list_existing_titles(client: ImaClient, kb_id: str, limit: int = 50) -> set[str]:
    """列出 KB 内已存在的 title 集合，用于幂等跳过。

    注意：ima 服务端 limit 必须 ≤ 50，否则报 ``value must be inside range (0, 50]``。
    """
    """列出 KB 内已存在的 title 集合，用于幂等跳过。"""
    titles: set[str] = set()
    try:
        data = client._session.post(
            f"{client.cfg.base_url}/openapi/wiki/v1/get_knowledge_list",
            data=json.dumps(
                {"knowledge_base_id": kb_id, "folder_id": "", "limit": limit},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=client.cfg.timeout,
        )
        j = data.json()
        if j.get("code") != 0:
            log.warning("get_knowledge_list err: %s", j.get("msg"))
            return titles
        items = (j.get("data") or {}).get("knowledge_list") or []
        for it in items:
            t = it.get("title") or it.get("name")
            if t:
                titles.add(t)
    except Exception as exc:
        log.warning("get_knowledge_list failed: %s", exc)
    return titles


# ---------------------------------------------------------------------------
# 本地 .md 落盘
# ---------------------------------------------------------------------------

def _slugify(s: str, max_len: int = 60) -> str:
    """把 title 变成安全的文件名。保留中文；把空格和特殊字符替成 ``-``。"""
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[\\/:*?\"<>|]+", "", s)
    return s[:max_len] or "untitled"


def write_one_local(pair: dict, out_dir: Path) -> bool:
    """把单条 Q&A 写成一个 .md 文件。已存在则跳过；返回 True 表示新增。"""
    if not pair or "title" not in pair or "content" not in pair:
        return False
    if not out_dir:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名：取标题里的编号段（``topic·cat·NNN·keyword``）；保留可读性
    m = re.match(r"^.+?·(\d{3})·", pair["title"])
    seq = m.group(1) if m else f"{abs(hash(pair['title'])) % 1000:03d}"
    fname = f"qa-{seq}-{_slugify(pair['title'])}.md"
    path = out_dir / fname
    if path.exists():
        return False
    # frontmatter 写 topic / cat / kws，方便 LocalKBIndex 之后用 frontmatter
    kws = pair.get("kws") or []
    cat = pair.get("cat") or ""
    fm = (
        "---\n"
        f"title: {pair['title']}\n"
        f"topic: {pair.get('topic', '')}\n"
        f"category: {cat}\n"
        f"keywords: [{', '.join(kws)}]\n"
        "---\n\n"
    )
    try:
        path.write_text(fm + pair["content"], encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[seed] 写本地文件 {path} 失败: {exc}")
        return False


def write_local_files(pairs: list[dict], out_dir: Path) -> int:
    """批量落盘，返回新增条数。"""
    if not out_dir:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in pairs:
        if write_one_local(p, out_dir):
            n += 1
    print(f"[seed] 写本地 {n} 条 → {out_dir}")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="灌 IMA 知识库做链路验证")
    parser.add_argument("--count", type=int, default=150, help="Q&A 条数（默认 150）")
    parser.add_argument("--topic", type=str, default="clawbot 微信机器人", help="Q&A 主题前缀")
    parser.add_argument("--kb-name", type=str, default=KB_NAME, help=f"KB 名字（默认 {KB_NAME}）")
    parser.add_argument(
        "--kb-desc", type=str, default=KB_DESC, help="KB 描述",
    )
    parser.add_argument(
        "--only-print", action="store_true",
        help="只生成 Q&A 并打印样本，不调 IMA API",
    )
    parser.add_argument(
        "--only-local", action="store_true",
        help="只写本地 .md 文件，不调 IMA API",
    )
    parser.add_argument(
        "--local-out", type=str, default="docs/knowledge",
        help="本地 .md 落盘目录（相对项目根，默认 docs/knowledge）",
    )
    parser.add_argument(
        "--skip-create", action="store_true",
        help="不复用现有 KB，也不新建；只往 --kb-id 指定的目标灌",
    )
    parser.add_argument(
        "--kb-id", type=str, default="",
        help="显式指定 KB ID（与 --skip-create 配合）",
    )
    parser.add_argument(
        "--index-wait", type=int, default=0,
        help="灌完后等多少秒让 IMA 解析（默认 0，跳过）",
    )
    args = parser.parse_args()

    pairs = generate_qa_pairs(args.topic, args.count)
    print(f"[seed] 生成 {len(pairs)} 条 Q&A（topic={args.topic!r}）")
    if args.only_print:
        print("--- 第 1 条样例 ---")
        print(pairs[0]["title"])
        print(pairs[0]["content"])
        if len(pairs) >= 2:
            mid = pairs[len(pairs) // 2]
            print(f"--- 第 {len(pairs)//2 + 1} 条样例 ---")
            print(mid["title"])
            print(mid["content"])
        return 0

    if args.only_local:
        write_local_files(pairs, Path(args.local_out))
        return 0

    setup_logging(level=os.getenv("CLAWBOT_LOG_LEVEL", "INFO"))
    client = ImaClient(ImaConfig.from_env(env_files=None))
    if not client.configured():
        print("[seed] 凭据缺失，请在 .env 里设 IMA_ILINK_CLIENT_ID / IMA_ILINK_API_KEY")
        return 1

    # 1) 找 / 建 KB
    kb_id = ""
    if args.kb_id:
        kb_id = args.kb_id
    elif not args.skip_create:
        kb_id = find_kb_by_name(client, args.kb_name)
        if kb_id:
            print(f"[seed] 复用现有 KB：name={args.kb_name!r}  id={kb_id}")
        else:
            print(f"[seed] 创建 KB：name={args.kb_name!r}  type=KBT_MINE_KB")
            try:
                kb_id, kb_name = client.create_knowledge_base(
                    args.kb_name, args.kb_desc, type_=KB_TYPE,
                )
            except Exception as exc:
                print(f"[seed] 创建 KB 失败：{exc}")
                return 1
            print(f"[seed] 创建成功  id={kb_id}  name={kb_name!r}")
    if not kb_id:
        print("[seed] 没有可用 KB（既未指定也未创建），退出")
        return 1

    # 2) 列出已存在的 title，幂等跳过
    print("[seed] 列已有 title...")
    existing = list_existing_titles(client, kb_id)
    print(f"[seed] 已存在 {len(existing)} 条")

    # 3) 灌笔记
    created, skipped, failed = 0, 0, 0
    written_local = 0
    t0 = time.perf_counter()
    for i, p in enumerate(pairs, 1):
        if p["title"] in existing:
            skipped += 1
            # 即使 IMA 端已存在，仍然把 .md 写到本地（如果本地还没写）
            if write_one_local(p, Path(args.local_out)):
                written_local += 1
            continue
        try:
            note_id = client.import_doc(p["title"], p["content"], content_format=1)
            if not note_id:
                print(f"[seed] {i:3d}/{len(pairs)} import_doc 无 note_id，跳过")
                failed += 1
                continue
            mid = client.add_knowledge(kb_id, p["title"], note_id=note_id, media_type=11)
            created += 1
            if write_one_local(p, Path(args.local_out)):
                written_local += 1
            if i % 20 == 0 or i == len(pairs):
                elapsed = time.perf_counter() - t0
                print(
                    f"[seed] {i:3d}/{len(pairs)}  created={created}  "
                    f"skipped={skipped}  failed={failed}  local_written={written_local}  "
                    f"elapsed={elapsed:.1f}s  last_note_id={note_id}  "
                    f"last_media_id={mid!r}"
                )
        except Exception as exc:
            failed += 1
            print(f"[seed] {i:3d}/{len(pairs)} FAILED {p['title'][:30]!r}: {exc}")

    print()
    print("=" * 60)
    print(f"KB ID   : {kb_id}")
    print(f"KB name : {args.kb_name}")
    print(f"created : {created}")
    print(f"skipped : {skipped}")
    print(f"failed  : {failed}")
    print()
    print("下一步：把上面 KB ID 写入 .env / /etc/clawbot/ima.env：")
    print(f"  IMA_ILINK_DEFAULT_KB={kb_id}")
    if args.index_wait > 0:
        print(f"\n等 {args.index_wait}s 让 IMA 解析...")
        time.sleep(args.index_wait)
        # 抽样验证
        print("\n[seed] 抽样 search_knowledge 验证：")
        for keyword in ["clawbot", "重连", "IMA", "DeepSeek"]:
            hits = client.search_knowledge(keyword, knowledge_base_id=kb_id, limit=2)
            print(f"  q={keyword!r}  hits={len(hits)}  titles={[h.title for h in hits]}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
