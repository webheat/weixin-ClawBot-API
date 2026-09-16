import argparse
import asyncio
import base64
import io
import inspect
import json
import os
import re
import secrets
import time
import urllib.request
from functools import partial
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import aiohttp

from dusapi import DusAPI, DusConfig
from deepseek import DeepSeekAPI, DeepSeekConfig
from ima import ImaClient, ImaConfig, build_context_prompt as _ima_build_context, rerank_hits as _ima_rerank_hits
from qr_web import QrFlowState, make_web_on_qrcode, wait_for_verify_code, web_enabled, start as qr_web_start
from utils.local_kb import LocalKBIndex
from utils.logging_setup import get_logger
from utils.ima_bindings import get_default_bindings as _get_ima_bindings

try:
    from utils.semantic_kb import SemanticKBIndex  # 4 档 mode: llm+semantic
except Exception:  # fastembed 缺失不致命；只在 SEMANTIC_KB_ENABLED=1 时才用
    SemanticKBIndex = None  # type: ignore[assignment]

# 关键词抽取 prompt：用于 _AIWithIma._extract_keywords，从自然语言问句里
# 拆出 1-3 个最适合 IMA 搜索的关键词。
#
# 关键约束：腾讯 ima 的 search_knowledge 是字面关键词严格匹配，phrase 几乎
# 不命中；每个词必须独立，词与词之间用一个空格分开，绝不要连写。
# 例如"ima知识库"是错的，正确是"ima 知识库"。
_KEYWORD_EXTRACT_PROMPT = """你是检索关键词提取助手。用户用自然语言提问，下游是腾讯 ima 知识库（按字面关键词严格匹配，不是语义检索）。
请把用户问题拆成 1-3 个独立的字或单词，每个之间用一个空格分开。

硬性规则：
- 每个词/字必须独立，词与词之间用单个空格分隔
- 绝对不要把多个词连写（"ima知识库"是错的，"ima 知识库"才是对的）
- 中文短语拆成独立的单字或常用二字词
- 如果是问"什么是 X"，通常 X 本身就是关键词
- 数字、英文术语、专有名词整体保留

只输出一行关键词，不要任何解释、序号、标点或换行。

示例：
用户问：什么是 ima 知识库？
输出：ima 知识库

用户问：clawbot 多久会强制重连？重连流程是怎样的？
输出：重连 强制重连

用户问：请问下，扫码登录那段代码里的 verify_code 是干嘛用的？
输出：verify_code 扫码登录

用户问：腾讯的 ima 知识库 OpenAPI 的 search_knowledge 怎么用？
输出：ima 知识库 search_knowledge OpenAPI

用户问：{message}
输出："""

# 当 ``mode=llm-only`` 时拼到 system prompt 末尾，让 LLM 自我降自信并加标记。
# 设计动机：ctx_chars=0 时 LLM 是在"裸答"，可能瞎编；让它主动告诉用户
# "这是基于我自己的理解、不一定准"，比默默编一个好得多。
# 通过 env ``CLAWBOT_LLM_CAVEAT=0`` 可关闭（默认 1）。
_LLM_ONLY_CAVEAT_PROMPT = """【重要系统提示】本次回答未检索到任何知识库参考资料（既无 ima 云端命中，也无本地 .md 兜底命中），你的回答完全基于自身预训练知识。

请严格遵守以下原则：
1. 在回答开头加上明确的标记 "（未参考知识库）"，让用户一眼分辨这是 LLM 自主回答。
2. 如果你对自己的答案没有把握（例如涉及具体数字、日期、引文、项目内部细节、最新事件），直接告诉用户 "我不确定" 或 "建议查证"，不要硬猜。
3. 不要编造具体的版本号、配置项、函数名、API 端点等"看起来很具体"的内容；如果记不清，模糊处理或建议用户查文档。
4. 如果用户问的是关于 clawbot 项目本身的具体实现细节，你应该建议用户去看仓库里的源码 / docs/ 目录 / weixin-openclaw-api-py-docs.md。"""

# ========== 子系统 logger（诊断日志；用户态输出仍走 print） ==========
log_qr        = get_logger("qr")         # 二维码登录全链路
log_msg       = get_logger("message")    # 长轮询 / 消息收发
log_reconnect = get_logger("reconnect")  # 重连流程
log_ai        = get_logger("ai")         # AI 调用包装层
log_api       = get_logger("api")        # 每次 HTTP 自动 trace（DEBUG）
log_state     = get_logger("state")      # weixin_state.json 读写
log_ima       = get_logger("ima")        # ima 检索 / 写入
# =============================================

executor = ThreadPoolExecutor(max_workers=4)
ai = None  # 启动时从配置文件加载后初始化

# ========== 自动重连配置（可调参数） ==========
# 测试时将数值改小，例如：
#   "session_duration": 300, "warning_before": 60, "reminder_interval": 30,
#   "force_before": 60, "qrcode_scan_timeout": 120
#
# 策略：长轮询负责连接保活，服务端明确返回 stale token（-14）时才扫码恢复。
# 旧版本按本地 24 小时计时器主动发起二维码登录，这会把仍然有效的连接误判为
# 已退出，且用户不在网页旁边时无法完成扫码。保留 proactive_relogin 仅作紧急
# 兼容开关，生产默认关闭。
RECONNECT_CONFIG = {
    "session_duration":    24 * 3600,  # 会话总时长（秒）
    "warning_before":       2 * 3600,  # 提前多久发出警告（秒）
    "reminder_interval":      30 * 60, # 用户回 N 后多久再问（秒）
    "force_before":           30 * 60, # 最后多久强制重连（秒）
    "qrcode_scan_timeout":       480,  # 官方客户端默认整体等待时长（秒）
    "proactive_relogin":       False,  # 仅兼容旧行为；默认由 getupdates 保活
}
# 当 do_reconnect 还在 QR 扫描/登录流程里、或 elapsed 已越过 warning_before 但仍未成功
# 重连时，timer 任务的下一次 recheck 至少等这么秒；避免 sendmessage 失败 + do_reconnect 挂
# 起时退化成 ~4 Hz 日志风暴（logs/clawbot_alice.log 2026-09-13 09:18:56+）。
_RECONNECT_RECHECK_BACKOFF_SECS = 60.0
# =============================================

# ========== 配置文件 ==========
CONFIG_FILE = "config.json"
STATE_FILE = "weixin_state.json"
_DEFAULT_PROMPT = "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = "weixin-ClawBot-API/1.2.0 (python)"
DEFAULT_BOT_AGENT = "OpenClaw"
BOT_AGENT_MAX_LEN = 256

# iLink 2.4.6 官方客户端默认超时。长轮询超时属于正常控制流，不能当作业务失败。
QR_STATUS_TIMEOUT = 35
LONG_POLL_TIMEOUT = 35
API_TIMEOUT = 15
CONFIG_TIMEOUT = 10
CONFIG_CACHE_TTL = 24 * 60 * 60
CONFIG_CACHE_INITIAL_RETRY = 2
CONFIG_CACHE_MAX_RETRY = 60 * 60
MAX_QR_REFRESH_COUNT = 3
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY = 2
BACKOFF_DELAY = 30
MAX_LONG_POLL_TIMEOUT = 120

PROVIDERS = {
    "dusapi": {
        "label": "DusAPI",
        "base_url": "https://api.dusapi.com",
        "model": "gpt-5",
        "prompt": _DEFAULT_PROMPT,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "prompt": _DEFAULT_PROMPT,
    },
}


def mask_key(key: str) -> str:
    """保留前5位和后5位，中间用星号替换。"""
    if len(key) <= 10:
        return key
    return key[:5] + "*" * (len(key) - 10) + key[-5:]


def load_config_file() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"provider": "dusapi", "providers": {}}

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 兼容旧版扁平配置：{api_key, base_url, model, prompt}
    if "providers" not in cfg:
        old_provider_cfg = {
            "api_key": cfg.get("api_key", ""),
            "base_url": cfg.get("base_url", PROVIDERS["dusapi"]["base_url"]),
            "model": cfg.get("model", PROVIDERS["dusapi"]["model"]),
            "prompt": cfg.get("prompt", _DEFAULT_PROMPT),
        }
        cfg = {
            "provider": "dusapi",
            "providers": {"dusapi": old_provider_cfg},
        }
    cfg.setdefault("provider", "dusapi")
    cfg.setdefault("providers", {})
    return cfg


def save_config_file(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _empty_runtime_state() -> dict:
    return {
        "bot_token": "",
        "baseurl": "",
        "ilink_bot_id": "",
        "ilink_user_id": "",
        "get_updates_buf": "",
        "contexts": {},
        "last_contact": {"from_id": "", "context_token": ""},
    }


def load_runtime_state() -> dict:
    """加载 iLink 运行状态；token、游标和上下文按本地账号隔离保存。"""
    state = _empty_runtime_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            state.update({k: raw.get(k, v) for k, v in state.items()})
            if not isinstance(state.get("contexts"), dict):
                state["contexts"] = {}
            if not isinstance(state.get("last_contact"), dict):
                state["last_contact"] = {"from_id": "", "context_token": ""}
    except (OSError, ValueError, TypeError) as exc:
        log_state.warning("state load failed path=%s err=%s; starting empty",
                          STATE_FILE, exc)
        print(f"[状态] 无法读取 {STATE_FILE}，将从空状态开始: {exc}")
    return state


def save_runtime_state(state: dict):
    """原子保存 token、baseurl、get_updates_buf 和 context_token。"""
    temp_file = f"{STATE_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, STATE_FILE)
    except (OSError, TypeError, ValueError) as exc:
        log_state.error("state save failed err=%s", exc, exc_info=True)
        print(f"[状态] 保存 {STATE_FILE} 失败: {exc}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass


def sanitize_bot_agent(raw: str | None) -> str:
    """按 2.4.6 的 UA 风格规则清洗 bot_agent，避免非法元数据污染请求。"""
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_BOT_AGENT

    product_re = re.compile(r"^[A-Za-z0-9_.-]{1,32}/[A-Za-z0-9_.+\-]{1,32}$")
    comment_re = re.compile(r"^[\x20-\x27\x2A-\x7E]{1,64}$")
    raw_tokens = raw.strip().split()
    tokens = []
    i = 0
    while i < len(raw_tokens):
        token = raw_tokens[i]
        if token.startswith("(") and not token.endswith(")"):
            while i + 1 < len(raw_tokens) and not token.endswith(")"):
                i += 1
                token += " " + raw_tokens[i]
        tokens.append(token)
        i += 1

    accepted = []
    pending = None
    for token in tokens:
        if token.startswith("(") and token.endswith(")"):
            comment = token[1:-1]
            if pending and comment_re.fullmatch(comment):
                accepted.append(f"{pending} ({comment})")
                pending = None
            elif pending:
                accepted.append(pending)
                pending = None
            continue
        if pending:
            accepted.append(pending)
        pending = token if product_re.fullmatch(token) else None
    if pending:
        accepted.append(pending)

    if not accepted:
        return DEFAULT_BOT_AGENT

    result = " ".join(accepted)
    if len(result.encode("utf-8")) <= BOT_AGENT_MAX_LEN:
        return result
    kept = []
    size = 0
    for token in accepted:
        extra = len(token.encode("utf-8")) + (1 if kept else 0)
        if size + extra > BOT_AGENT_MAX_LEN:
            break
        kept.append(token)
        size += extra
    return " ".join(kept) if kept else DEFAULT_BOT_AGENT


def _safe_input(prompt: str, default: str = "") -> str:
    """stdin 没有 TTY（daemon / systemd / EOF）时返回 default，避免阻塞。"""
    try:
        return input(prompt)
    except EOFError:
        return default


def choose_provider(default_provider: str) -> str:
    print("\n请选择 AI 提供商：")
    keys = list(PROVIDERS.keys())
    for index, key in enumerate(keys, 1):
        default_mark = "（默认）" if key == default_provider else ""
        print(f"  {index}. {PROVIDERS[key]['label']} {default_mark}")

    while True:
        choice = _safe_input("输入序号或名称后回车: ").strip().lower()
        if not choice:
            return default_provider if default_provider in PROVIDERS else "dusapi"
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        if choice in PROVIDERS:
            return choice
        print("输入无效，请重新选择。")


def prompt_provider_config(provider: str, old_cfg: dict | None = None) -> dict:
    defaults = PROVIDERS[provider]
    old_cfg = old_cfg or {}
    print(f"\n配置 {defaults['label']}：")

    old_key = old_cfg.get("api_key", "")
    key_prompt = f"请输入 API Key（当前 {mask_key(old_key)}，留空沿用）: " if old_key else "请输入 API Key: "
    api_key = _safe_input(key_prompt).strip() or old_key

    old_base_url = old_cfg.get("base_url", defaults["base_url"])
    base_url = _safe_input(f"请输入 API 地址（留空默认/沿用 {old_base_url}）: ").strip() or old_base_url

    old_model = old_cfg.get("model", defaults["model"])
    model = _safe_input(f"请输入模型名称（留空默认/沿用 {old_model}）: ").strip() or old_model

    old_prompt = old_cfg.get("prompt", defaults["prompt"])
    prompt = _safe_input("请输入系统提示词（留空默认/沿用当前值）: ").strip() or old_prompt

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "prompt": prompt,
    }


def load_or_create_config() -> dict:
    """先选择 AI 提供商，再确认或创建对应配置。"""
    sep = "=" * 60
    dash = "-" * 60
    cfg = load_config_file()

    while True:
        provider = choose_provider(cfg.get("provider", "dusapi"))
        cfg["provider"] = provider
        provider_cfg = cfg["providers"].get(provider)
        label = PROVIDERS[provider]["label"]

        if not provider_cfg:
            print(f"\n未找到 {label} 配置，需要创建。")
            provider_cfg = prompt_provider_config(provider)
            cfg["providers"][provider] = provider_cfg
            save_config_file(cfg)
            print(f"\n配置已保存到 {CONFIG_FILE}\n")
            return {"provider": provider, **provider_cfg}

        print(f"\n{sep}")
        print(f"  当前选择：{label}")
        print("  当前配置如下：")
        print(sep)
        print(f"  API Key  : {mask_key(provider_cfg.get('api_key', ''))}")
        print(f"  API 地址 : {provider_cfg.get('base_url', '')}")
        print(f"  模型     : {provider_cfg.get('model', '')}")
        prompt_preview = provider_cfg.get("prompt", "")[:50]
        print(f"  提示词   : {prompt_preview}{'...' if len(provider_cfg.get('prompt','')) > 50 else ''}")
        print(dash)

        choice = _safe_input("\n使用此配置继续？(直接回车或输入 Y 继续 / 输入 N 重新配置 / 输入 S 切换提供商): ").strip().upper()
        if choice == "N":
            provider_cfg = prompt_provider_config(provider, provider_cfg)
            cfg["providers"][provider] = provider_cfg
            save_config_file(cfg)
            print(f"\n配置已保存到 {CONFIG_FILE}\n")
            return {"provider": provider, **provider_cfg}
        if choice == "S":
            continue
        else:
            save_config_file(cfg)
            return {"provider": provider, **provider_cfg}
# ==============================

BASE_URL = "https://ilinkai.weixin.qq.com"
COMMANDS_MSG = (
    "你好，我是 🪶 翼claw，您的个人微信专属智能助理。\n"
    "📚 接入 ima 知识库，支持文字 / 语音 / 公众号卡片。\n"
    "\n"
    "直接发消息即可对话，常用指令：\n"
    "/help    查看全部指令\n"
    "/time    查看当前连接状态\n"
    "/重新连接 立即刷新连接"
)

VOICE_TRANSCRIPT_UNAVAILABLE_MSG = (
    "这条语音的文字内容没有成功传到我这里，暂时无法理解。"
    "请重新发送一次语音；如果仍未识别，请在微信里点击“转文字”后发送，或直接发文字。"
)

INTRO_DETAIL_MSG = (
    "🪶 翼claw · 您的个人微信专属智能助理\n"
    "\n"
    "我是一个由大语言模型驱动的微信机器人（DeepSeek / Claude / GPT 可选），\n"
    "跑在您自己的服务器上。发消息给我，我会用 AI 帮您解答问题、\n"
    "整理资料、撰写内容。\n"
    "\n"
    "📚 我能做什么\n"
    "\n"
    "• AI 自由对话：闲聊、写作、翻译、分析、头脑风暴——直接发消息就行。\n"
    "• 知识库问答：接入了腾讯 ima，预装 150 条您专属 Q&A。\n"
    "  \"xxx 怎么用 / xxx 是什么\" 这类问题优先从知识库找答案。\n"
    "• 三层兜底检索：云端 KB 没命中时，自动回退本地 KB 或语义检索。\n"
    "  断网也能用本地知识库回答。\n"
    "• 多模态理解：文字、语音（自动转文字）、公众号 / 小程序卡片\n"
    "  （自动读标题和摘要）都能识别处理。\n"
    "• 持续在线：后台持续维护连接，服务端 token 失效时自动进入恢复流程。\n"
    "\n"
    "⚙️ 常用指令\n"
    "\n"
    "• /help    · 查看全部指令\n"
    "• /time    · 查看当前连接状态\n"
    "• /重新连接 · 立即刷新连接\n"
    "\n"
    "💡 小提示\n"
    "问具体问题比\"你好\"更能发挥我的能力；\n"
    "知识库内容会随您上传的资料持续更新。\n"
    "\n"
    "━━━━━━━━━━━\n"
    "直接发消息即可开始对话 👋"
)


class ILinkAPIError(RuntimeError):
    """iLink HTTP、JSON 或业务层错误。"""

    def __init__(self, message, *, path="", status=None, ret=None, errcode=None,
                 response=None, network_type=None):
        super().__init__(message)
        self.path = path
        self.status = status
        self.ret = ret
        self.errcode = errcode
        self.response = response or {}
        self.network_type = network_type

    @property
    def code(self):
        return self.ret if self.ret not in (None, 0) else self.errcode

    @property
    def is_stale_token(self):
        return self.code == -14


def make_common_headers():
    """2.4.6 GET/POST 共用的最小应用头。"""
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def make_headers(token=None):
    """2.4.6 业务 POST 请求头；不手动设置 Content-Length。"""
    uin = str(secrets.randbits(32))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        **make_common_headers(),
    }
    if token and str(token).strip():
        headers["Authorization"] = f"Bearer {str(token).strip()}"
    return headers


def base_info():
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": sanitize_bot_agent(BOT_AGENT),
    }


def generate_client_id() -> str:
    """生成与 2.4.6 客户端格式一致、可用于消息幂等的唯一 ID。"""
    return f"openclaw-weixin:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def _redact_text(value):
    """避免把 token、二维码和上下文凭据写入终端日志。"""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value or "")
    else:
        text = str(value or "")
    patterns = (
        r"(?i)(bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param|appsecret|wxoapp_app_secret|access_token|refresh_token|oauth_code)=([^&\s,}\"]+)",
        r'(?i)("(?:bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param|appsecret|wxoapp_app_secret|access_token|refresh_token|oauth_code)"\s*:\s*")([^"]+)(")',
        r"(?i)('(?:bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param|appsecret|wxoapp_app_secret|access_token|refresh_token|oauth_code)'\s*:\s*')([^']+)(')",
        r'(?i)("local_token_list"\s*:\s*)\[[^\]]*\]',
    )
    for index, pattern in enumerate(patterns):
        if index == len(patterns) - 1:
            text = re.sub(pattern, r"\1[***]", text)
        else:
            text = re.sub(pattern, lambda m: f"{m.group(1)}***{m.group(3) if m.lastindex and m.lastindex >= 3 else ''}", text)
    return text[:500]


def _redact_path(path):
    # 查询串通常包含二维码、配对码或签名；保留参数名便于排障，不记录值。
    path = re.sub(r"(?i)([?&](?:qrcode|verify_code)=)[^&]*", r"\1***", str(path))
    return _redact_text(path)


def _network_type(exc):
    text = str(exc).upper()
    if any(code in text for code in ("ENOTFOUND", "EAI_AGAIN", "GETADDRINFO")):
        return "dns"
    if any(code in text for code in ("ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "CONNECT_TIMEOUT")):
        return "tcp"
    if any(code in text for code in ("SSL", "TLS", "CERT", "UNABLE_TO_VERIFY")):
        return "tls"
    if isinstance(exc, asyncio.TimeoutError) or "TIMEOUT" in text:
        return "timeout"
    return "unknown"


def _parse_json_response(text, path, status):
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ILinkAPIError(
            f"{path} 返回内容不是有效 JSON: {exc}",
            path=path,
            status=status,
        ) from exc
    if not isinstance(data, dict):
        raise ILinkAPIError(
            f"{path} 返回 JSON 类型错误: {type(data).__name__}",
            path=path,
            status=status,
        )
    return data


def _log_response(method, path, status, text):
    """每次 iLink HTTP 自动 trace。DEBUG 级别：默认 INFO 不污染，需要时
    ``CLAWBOT_LOG_LEVEL=DEBUG`` 一开即可复盘协议细节。"""
    log_api.debug("%s %s → %d %s", method, _redact_path(path), status, _redact_text(text))


def ensure_business_success(data, path):
    """检查 ret/errcode；字段缺省按官方客户端兼容规则视为成功。"""
    ret = data.get("ret")
    errcode = data.get("errcode")
    if ret not in (None, 0) or errcode not in (None, 0):
        message = data.get("errmsg") or "iLink 业务请求失败"
        raise ILinkAPIError(
            f"{path} ret={ret!r} errcode={errcode!r}: {message}",
            path=path,
            ret=ret,
            errcode=errcode,
            response=data,
        )
    return data


async def api_get(session, path, token=None, base_url=None, *, timeout=QR_STATUS_TIMEOUT,
                  long_poll=False):
    """执行 GET；二维码状态长轮询超时返回 wait，其余错误抛出。"""
    del token  # 2.4.6 官方 GET 二维码状态只发送公共应用头，不发送 Bearer。
    url = f"{(base_url or BASE_URL).rstrip('/')}/{path.lstrip('/')}"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        async with session.get(url, headers=make_common_headers(), timeout=client_timeout) as res:
            text = await res.text()
            _log_response("GET", path, res.status, text)
            if not 200 <= res.status < 300:
                error_data = {}
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        error_data = parsed
                except (TypeError, ValueError):
                    pass
                raise ILinkAPIError(
                    f"GET {path} HTTP {res.status}: {error_data.get('errmsg', '')}".rstrip(),
                    path=path,
                    status=res.status,
                    ret=error_data.get("ret"),
                    errcode=error_data.get("errcode"),
                    response=error_data,
                )
            return _parse_json_response(text, path, res.status)
    except asyncio.TimeoutError as exc:
        if long_poll:
            return {"status": "wait", "_timeout": True}
        raise ILinkAPIError(
            f"GET {path} 超时", path=path, network_type="timeout",
        ) from exc
    except asyncio.CancelledError:
        raise
    except ILinkAPIError:
        raise
    except (aiohttp.ClientError, OSError) as exc:
        kind = _network_type(exc)
        raise ILinkAPIError(
            f"GET {path} 网络错误({kind}): {exc}",
            path=path,
            network_type=kind,
        ) from exc


async def api_post(session, path, body, token=None, base_url=None, *, timeout=API_TIMEOUT,
                   long_poll=False, fallback_cursor=""):
    """执行 JSON POST；不手动设置 Content-Length。"""
    url = f"{(base_url or BASE_URL).rstrip('/')}/{path.lstrip('/')}"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        async with session.post(
            url,
            json=body,
            headers=make_headers(token),
            timeout=client_timeout,
        ) as res:
            text = await res.text()
            _log_response("POST", path, res.status, text)
            if not 200 <= res.status < 300:
                error_data = {}
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        error_data = parsed
                except (TypeError, ValueError):
                    pass
                raise ILinkAPIError(
                    f"POST {path} HTTP {res.status}: {error_data.get('errmsg', '')}".rstrip(),
                    path=path,
                    status=res.status,
                    ret=error_data.get("ret"),
                    errcode=error_data.get("errcode"),
                    response=error_data,
                )
            return _parse_json_response(text, path, res.status)
    except asyncio.TimeoutError as exc:
        if long_poll:
            return {
                "ret": 0,
                "msgs": [],
                "get_updates_buf": fallback_cursor,
                "_timeout": True,
            }
        raise ILinkAPIError(
            f"POST {path} 超时", path=path, network_type="timeout",
        ) from exc
    except asyncio.CancelledError:
        raise
    except ILinkAPIError:
        raise
    except (aiohttp.ClientError, OSError) as exc:
        kind = _network_type(exc)
        raise ILinkAPIError(
            f"POST {path} 网络错误({kind}): {exc}",
            path=path,
            network_type=kind,
        ) from exc


async def send_msg_safe(session, to_id, context_token, text, bot_token_ref, bot_base_url_ref):
    """发送微信消息，失败时降级为控制台打印，不抛异常。"""
    if not to_id or not context_token:
        log_msg.warning("sendmsg skipped reason=no_contact_or_token text_preview=%r",
                        (text or "")[:30])
        print(f"[重连通知] {_redact_text(text)}")
        return False
    try:
        client_id = generate_client_id()
        result = await api_post(
            session,
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": base_info(),
            },
            bot_token_ref[0],
            bot_base_url_ref[0] or None,
            timeout=API_TIMEOUT,
        )
        ensure_business_success(result, "sendmessage")
        safe_text = _redact_text(text)
        log_msg.info("sendmsg ok to=%s chars=%d preview=%r",
                     str(to_id)[-4:] if to_id else "-", len(text or ""),
                     (safe_text or "")[:30])
        print(f"[消息] 已发送: {safe_text[:50]}{'...' if len(safe_text) > 50 else ''}")
        return True
    except ILinkAPIError as exc:
        if exc.is_stale_token:
            raise
        log_msg.warning("sendmsg failed err=%s; degraded to console", _redact_text(exc))
        print(f"[重连通知] 发送失败({_redact_text(exc)})，降级打印: {_redact_text(text)}")
        return False
    except Exception as e:
        log_msg.error("sendmsg crashed err=%s; degraded to console", _redact_text(e), exc_info=True)
        print(f"[重连通知] 发送失败({_redact_text(e)})，降级打印: {_redact_text(text)}")
        return False


async def send_typing_safe(session, user_id, typing_ticket, status,
                           bot_token_ref, bot_base_url_ref):
    """尽力发送输入状态；失效 token 继续向上抛出以触发受控重登录。"""
    if not user_id or not typing_ticket:
        return False
    try:
        result = await api_post(
            session,
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": base_info(),
            },
            bot_token_ref[0],
            bot_base_url_ref[0] or None,
            timeout=CONFIG_TIMEOUT,
        )
        ensure_business_success(result, "sendtyping")
        return True
    except ILinkAPIError as exc:
        if exc.is_stale_token:
            raise
        log_msg.debug("sendtyping failed status=%d err=%s", status, _redact_text(exc))
        print(f"[输入状态] status={status} 发送失败: {_redact_text(exc)}")
        return False


async def get_typing_ticket_safe(session, user_id, context_token, cache,
                                 bot_token_ref, bot_base_url_ref):
    """按用户缓存 getconfig；失败时指数退避，但不阻断正常文字回复。"""
    now = time.time()
    entry = cache.get(user_id)
    if not isinstance(entry, dict):
        entry = None

    if entry is None or now >= float(entry.get("next_fetch_at", 0)):
        fetch_ok = False
        try:
            result = await api_post(
                session,
                "ilink/bot/getconfig",
                {
                    "ilink_user_id": user_id,
                    "context_token": context_token,
                    "base_info": base_info(),
                },
                bot_token_ref[0],
                bot_base_url_ref[0] or None,
                timeout=CONFIG_TIMEOUT,
            )
            ensure_business_success(result, "getconfig")
            cache[user_id] = {
                "typing_ticket": str(result.get("typing_ticket") or ""),
                "next_fetch_at": now + secrets.randbelow(CONFIG_CACHE_TTL + 1),
                "retry_delay": CONFIG_CACHE_INITIAL_RETRY,
            }
            fetch_ok = True
        except ILinkAPIError as exc:
            if exc.is_stale_token:
                raise
            log_msg.warning("getconfig failed err=%s; skip typing", _redact_text(exc))
            print(f"[配置] getconfig 失败，忽略输入状态: {_redact_text(exc)}")
        except Exception as exc:
            log_msg.warning("getconfig crashed err=%s; skip typing", _redact_text(exc))
            print(f"[配置] getconfig 异常，忽略输入状态: {_redact_text(exc)}")

        if not fetch_ok:
            if entry is None:
                cache[user_id] = {
                    "typing_ticket": "",
                    "next_fetch_at": now + CONFIG_CACHE_INITIAL_RETRY,
                    "retry_delay": CONFIG_CACHE_INITIAL_RETRY,
                }
            else:
                previous_delay = max(
                    CONFIG_CACHE_INITIAL_RETRY,
                    float(entry.get("retry_delay", CONFIG_CACHE_INITIAL_RETRY)),
                )
                next_delay = min(previous_delay * 2, CONFIG_CACHE_MAX_RETRY)
                entry["next_fetch_at"] = now + next_delay
                entry["retry_delay"] = next_delay

    cached = cache.get(user_id) or {}
    return str(cached.get("typing_ticket") or "")


def _voice_transcript(voice_item) -> str:
    """Return the transcript emitted by iLink for one voice item.

    The documented wire shape is ``voice_item.text``.  We intentionally do
    not treat the encrypted media payload as text: the configured LLM must
    receive the server ASR result, not a placeholder such as "[语音]".
    """
    if not isinstance(voice_item, dict):
        return ""
    value = voice_item.get("text")
    return value.strip() if isinstance(value, str) else ""


def is_voice_message(msg: dict) -> bool:
    """Whether an inbound iLink message contains a voice item.

    ``type`` is numeric in the official schema, but JSON bridges occasionally
    serialize it as a string or omit it and rely on the payload key.
    """
    if not isinstance(msg, dict):
        return False
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        if (
            item.get("type") in (3, "3")
            or "voice_item" in item
        ) and isinstance(item.get("voice_item"), dict):
            return True
    return False


def extract_voice_transcript(msg: dict) -> str:
    """Extract iLink's server-side ASR text without inventing voice content."""
    if not isinstance(msg, dict):
        return ""
    items = msg.get("item_list") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        transcript = _voice_transcript(item.get("voice_item"))
        if transcript:
            return transcript
    return ""


def extract_message_text(msg: dict) -> str:
    """遍历完整 item_list，提取文本/语音转写 / 转发卡片元数据。

    协议字段来源：``weixin-openclaw-api-py-docs.md:1021``（``ref_msg`` 由
    ``message_item`` + 摘要 ``title`` 组成）。覆盖的入站形态：

    * 普通文字（``text_item.text``）
    * 语音转写（``voice_item.text``）
    * 转发/引用的聊天记录（多个 ``text_item`` 遍历 + ``ref_msg.message_item``）
    * 链接 / 公众号文章 / 小程序卡片（``title`` / ``description`` / ``url`` /
      ``app_msg.title`` / ``app_msg.des``）

    不做的事：不解 CDN、不抓 URL 网页内容、不解析图片/文件/视频二进制。
    """
    parts: list[str] = []

    def _add(text):
        """去重 append：阻止文本在整个 parts 里重复出现。

        为什么用 ``text not in parts`` 而不是 ``parts[-1] != text``：
        引用回复场景下 ``text_item.text`` 与 ``ref_msg.message_item.text_item.text``
        是同一字符串但被 ``ref_msg.title`` 等中间字段隔开，``parts[-1]`` 检查
        会漏判；O(n²) 在单条消息最多 20 个 item 的场景下完全可接受。
        """
        text = (text or "").strip()
        if not text:
            return
        if text not in parts:
            parts.append(text)

    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue

        # 1. 直接挂在 item 上的文本 / 语音转写
        text_item = item.get("text_item") or {}
        if text_item.get("text"):
            _add(str(text_item["text"]))
        voice_text = _voice_transcript(item.get("voice_item"))
        if voice_text:
            _add(voice_text)

        # 2. 转发 / 引用：被引用 item 里也可能再嵌一份完整副本
        ref_msg = item.get("ref_msg") or {}
        if isinstance(ref_msg, dict):
            ref_title = ref_msg.get("title")
            if ref_title:
                _add(str(ref_title))
            ref_inner = ref_msg.get("message_item") or {}
            if isinstance(ref_inner, dict):
                ref_text = (ref_inner.get("text_item") or {}).get("text")
                if ref_text:
                    _add(str(ref_text))

        # 3. 链接 / 公众号 / 小程序卡片：标题、描述、URL
        #    不同协议版本字段名不一致，列几个常见 key 容错读取。
        for key in ("title", "description", "des"):
            val = item.get(key)
            if val:
                _add(str(val))
        app_msg = item.get("app_msg") or {}
        if isinstance(app_msg, dict):
            for key in ("title", "des", "description"):
                val = app_msg.get(key)
                if val:
                    _add(str(val))
        url = item.get("url")
        if url:
            _add(f"[链接] {url}")

        # 4. 识别到任何转发/卡片元数据时打一条 debug，便于运维排查
        if ref_msg or app_msg or item.get("url") or item.get("title"):
            log_msg.debug("forwarded item extracted title=%r has_text=%s url=%s",
                          (str(item.get("title"))[:40]) if item.get("title") else None,
                          bool(text_item.get("text") or voice_text),
                          bool(item.get("url")))

    return "\n".join(parts).strip()


async def do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                       typing_ticket_cache, reconnect_asked, warning_active,
                       reconnect_in_progress, login_time_ref, cfg, runtime_state=None,
                       web_on_qrcode=None, web_state=None):
    """执行重连流程，并同步保存 2.4.6 token、账号 ID 与游标状态。"""
    runtime_state = runtime_state if isinstance(runtime_state, dict) else _empty_runtime_state()
    if reconnect_in_progress[0]:
        log_reconnect.debug("reconnect skipped reason=in_progress")
        return
    reconnect_in_progress[0] = True
    warning_active[0] = False
    reconnect_asked.clear()

    # Bug fix: current_token / from_id / ctx 必须在 log 调用之前赋值——
    # 之前把读和写分置在第 850 行（读）和第 854/856 行（写），Python 编译时
    # 看到函数体内有 `current_token = ...` 就把整函数内 current_token 当 local，
    # 导致首次调用 do_reconnect 必抛 UnboundLocalError，让 reconnect_in_progress[0]
    # 卡在 True 不掉，timer 退化成 4 Hz 死循环（详见 logs/clawbot_alice.log
    # 2026-09-12 23:59:58 那条 remaining_s=-19036 的风暴）。
    current_token = bot_token_ref[0]
    from_id = last_contact.get("from_id")
    ctx = last_contact.get("context_token")

    try:
        log_reconnect.info("reconnect start current_token=%s contact=%s",
                           (current_token[:8] + "…") if current_token else "-",
                           from_id[-8:] if from_id else None)
        print("[重连] 开始重连流程...")

        async def deliver_qrcode(content):
            """把当前二维码同步输出到网页 + 终端。

            后台静默策略：QR 不再发到 last_contact 微信会话，仅走 web_on_qrcode +
            终端（print / render_terminal_qr）。用户收件箱保持安静。
            """
            if web_on_qrcode is not None:
                try:
                    await web_on_qrcode(content)
                except Exception as exc:
                    log_reconnect.warning("reconnect web_on_qrcode failed err=%s",
                                          _redact_text(exc))
                    print(f"[重连] web 二维码回调失败: {_redact_text(exc)}")
            print(f"[重连] 请扫码完成新连接：{_redact_text(content)}")
            # HTTP 图片链接已由 save_qrcode_content 渲染，其他格式在这里补渲染。
            if not content.startswith("http"):
                render_terminal_qr(content)
            log_reconnect.info("reconnect qr delivered via web+console (silent, no user msg)")

        try:
            login_result = await login_with_qrcode(
                session,
                [current_token] if current_token else [],
                existing_state=runtime_state,
                on_qrcode=deliver_qrcode,
                web_state=web_state,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_reconnect.error("reconnect qr login crashed err=%s; notify user",
                                _redact_text(exc), exc_info=True)
            print(f"[重连] 二维码登录失败: {_redact_text(exc)}")
            await send_msg_safe(
                session,
                from_id,
                ctx,
                "[失败] 二维码登录未完成，下次到期前会再次提醒",
                bot_token_ref,
                bot_base_url_ref,
            )
            login_time_ref[0] = time.time()
            return
        if login_result.get("already_connected") and current_token:
            log_reconnect.info("reconnect already_connected reusing token")
            print("[重连] 服务端提示已连接过此 OpenClaw，继续沿用当前连接")
            new_token = current_token
            new_base_url = bot_base_url_ref[0] or BASE_URL
            new_bot_id = runtime_state.get("ilink_bot_id", "")
            new_user_id = runtime_state.get("ilink_user_id", "")
        else:
            new_token = login_result.get("bot_token")
            new_base_url = login_result.get("baseurl") or bot_base_url_ref[0] or BASE_URL
            new_bot_id = login_result.get("ilink_bot_id", "")
            new_user_id = login_result.get("ilink_user_id") or runtime_state.get("ilink_user_id", "")

        if not new_token:
            reason = "登录状态异常" if login_result.get("login_error") else "扫码超时"
            log_reconnect.error("reconnect aborted reason=%s", reason)
            print(f"[重连] {reason}，重连未完成")
            await send_msg_safe(
                session,
                from_id,
                ctx,
                f"[失败] {reason}，重连未完成，下次到期前会再次提醒",
                bot_token_ref,
                bot_base_url_ref,
            )
            login_time_ref[0] = time.time()
            return

        old_bot_id = str(runtime_state.get("ilink_bot_id") or "")
        account_changed = bool(new_bot_id and old_bot_id != new_bot_id)
        old_base_url = bot_base_url_ref[0] or BASE_URL
        credentials_changed = bool(
            account_changed or new_token != current_token or new_base_url != old_base_url
        )
        if account_changed:
            log_reconnect.info("reconnect account changed old=%s new=%s; contexts cleared",
                               old_bot_id[:8] or "-", new_bot_id[:8] or "-")
            runtime_state["contexts"] = {}
            last_contact["from_id"] = None
            last_contact["context_token"] = None

        if credentials_changed and current_token:
            log_reconnect.info("reconnect credentials changed token_changed=%s baseurl_changed=%s",
                               new_token != current_token, new_base_url != old_base_url)
            await notify_lifecycle(
                session,
                "ilink/bot/msg/notifystop",
                current_token,
                old_base_url,
            )

        # 成功：原子替换 token、base_url；同账号保留游标，换账号时隔离状态。
        bot_token_ref[0] = new_token
        bot_base_url_ref[0] = new_base_url
        runtime_state.update({
            "bot_token": new_token,
            "baseurl": new_base_url,
            "ilink_bot_id": new_bot_id,
            "ilink_user_id": new_user_id,
            "get_updates_buf": "" if account_changed else runtime_state.get("get_updates_buf", ""),
            "last_contact": dict(last_contact),
        })
        save_runtime_state(runtime_state)
        log_reconnect.info("reconnect state saved baseurl=%s bot_id=%s",
                           new_base_url, new_bot_id[:8] or "-")
        if credentials_changed:
            typing_ticket_cache.clear()
            await notify_lifecycle(session, "ilink/bot/msg/notifystart", new_token, new_base_url)
            log_reconnect.info("reconnect credentials switched, typing_cache cleared")
            print("[重连] 新连接已建立，凭据已切换")
        else:
            log_reconnect.info("reconnect credentials unchanged, no switch")
            print("[重连] 当前连接仍然有效，无需切换凭据")
        if not account_changed:
            completion_text = (
                "[完成] 已自动重新连接，继续使用"
                if credentials_changed
                else "[完成] 当前连接仍然有效，继续使用"
            )
            await send_msg_safe(
                session,
                from_id,
                ctx,
                completion_text,
                bot_token_ref,
                bot_base_url_ref,
            )
        login_time_ref[0] = time.time()
        # 关键：前端 render() 只有看到 status==="logged_in" 才会显示"登录成功 ✓"。
        # do_reconnect 成功后必须同步刷这个状态，否则页面一直停留在 qr_pending
        # 显示 QR，哪怕 iLink 实际已登录成功（用户看到老 QR 继续扫 → 实际是 dead QR
        # 因为 do_reconnect 已在等 scan 结果并会进入新的 QR 周期）。
        # 旧路径（message_loop 的 -14 handler）单独写过一行，这里补齐 do_reconnect 主路径。
        if web_state is not None:
            web_state.status = "logged_in"
            web_state.logged_in_at = time.time()
            log_reconnect.info("reconnect web state set logged_in (frontend should show ✓)")
    finally:
        reconnect_in_progress[0] = False


async def reconnect_timer_task(session, bot_token_ref, bot_base_url_ref, last_contact,
                                typing_ticket_cache, reconnect_asked, warning_active,
                                reconnect_in_progress, login_time_ref, cfg, runtime_state=None,
                                web_on_qrcode=None, web_state=None):
    """独立定时器任务，与主消息循环并发运行。"""
    runtime_state = runtime_state if isinstance(runtime_state, dict) else _empty_runtime_state()
    session_dur_h = cfg["session_duration"] / 3600
    warn_before_h = cfg["warning_before"] / 3600
    force_before_m = cfg["force_before"] / 60
    log_reconnect.info("reconnect timer armed session_dur=%.1fh warn_before=%.1fh force_before=%.0fm",
                       session_dur_h, warn_before_h, force_before_m)
    while True:
        try:
            # Bug fix: 若上一次 force-fire / do_reconnect 仍在飞行中（典型场景是 QR 没人扫
            # 导致 login_with_qrcode 卡在扫码超时之前），就退避而非立刻重试。否则下面
            # first_wait=0 会让 timer 跟着 send_msg_safe 的超时节奏（~280ms）刷成 ~4 Hz
            # 日志风暴（logs/clawbot_alice.log 2026-09-13 09:18:56+）。
            if reconnect_in_progress[0]:
                log_reconnect.debug("reconnect timer backing off reason=in_progress")
                await asyncio.sleep(_RECONNECT_RECHECK_BACKOFF_SECS)
                continue

            elapsed = time.time() - login_time_ref[0]
            # Bug fix: 当 elapsed 已经越过 warning_before 阈值时，原始公式
            # `max(0, session_duration - warning_before - elapsed)` 退化成 0，外层 timer
            # 会以事件循环最快速度空转。给一个 ≥ reminder_interval 的下限，保证哪怕 do_reconnect
            # 短暂退场（极少见），我们也不会比正常提醒节奏更频繁地再 fire。
            raw_wait = cfg["session_duration"] - cfg["warning_before"] - elapsed
            first_wait = max(0, raw_wait)
            if first_wait == 0:
                first_wait = float(cfg.get("reminder_interval", 30 * 60))
            await asyncio.sleep(first_wait)

            # 后台静默策略：会话进入"接近到期"窗口后，仅在日志里记一行 armed，
            # 不向用户发任何"提醒"消息；外层 warning_active 也保持 False，
            # 让 message_loop 里 if warning_active[0] ... 那条分支不再被命中。
            remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
            log_reconnect.info("reconnect armed silently remaining_s=%.0f; awaiting force_before", remaining)

            # 静默等到 force_before：每 5 分钟重算一次 remaining，
            # 既不发任何用户消息，也不会让 timer 在 force_before 之前空转耗 CPU。
            while True:
                if remaining <= cfg["force_before"]:
                    break
                await asyncio.sleep(min(300.0, remaining - cfg["force_before"]))
                remaining = login_time_ref[0] + cfg["session_duration"] - time.time()

            # force_before 触发：直接走 do_reconnect（QR 走 web/terminal，不打扰用户）。
            log_reconnect.warning("reconnect force firing remaining_s=%.0f", remaining)
            print("[自动] 连接即将到期，开始强制重新连接...")
            if not last_contact.get("from_id") or not last_contact.get("context_token"):
                log_reconnect.info("reconnect force skipped reason=no_contact")
                print("[自动] 尚无最近联系人，跳过本轮自动重连（仅 web/terminal 出 QR）")
                login_time_ref[0] = time.time()
                continue
            # 走单一入口：若 listener 已在跑（被 /relink 抢先或 -14 触发），
            # 这里会 await 同一 Future，不会再开第二个 login_with_qrcode。
            try:
                await request_relogin("force-before")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_reconnect.warning("force reconnect failed err=%s", _redact_text(exc))
        except asyncio.CancelledError:
            raise
        except ILinkAPIError as exc:
            log_reconnect.warning("reconnect timer iLink err=%s; sleep %ds",
                                  _redact_text(exc), RETRY_DELAY)
            print(f"[自动重连] iLink 请求失败: {_redact_text(exc)}，稍后重新评估")
            await asyncio.sleep(RETRY_DELAY)
        except Exception as exc:
            log_reconnect.error("reconnect timer unhandled err=%s; sleep %ds",
                                _redact_text(exc), RETRY_DELAY, exc_info=True)
            print(f"[自动重连] 任务异常: {_redact_text(exc)}，稍后重新评估")
            await asyncio.sleep(RETRY_DELAY)


def render_terminal_qr(content: str):
    if not content:
        return
    print("\n扫码地址:", content)
    if content.startswith("http") and render_terminal_image_from_url(content):
        return
    render_generated_qr(content)


def render_terminal_image_from_url(url: str) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        image = Image.open(io.BytesIO(data)).convert("L")
        max_width = 72
        scale = max(1, int(image.width / max_width))
        width = max(1, int(image.width / scale))
        height = max(1, int(image.height / scale))
        image = image.resize((width, height))
        print()
        for y in range(height):
            print("".join("██" if image.getpixel((x, y)) < 128 else "  " for x in range(width)))
        print()
        return True
    except Exception as e:
        print(f"二维码图片渲染失败，改用本地二维码生成方式: {e}")
        return False


def render_generated_qr(content: str):
    try:
        import qrcode
    except ImportError:
        print("未安装 qrcode/Pillow，无法在终端渲染二维码；安装 `pip install qrcode pillow` 后会自动显示。")
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    print()
    for row in matrix:
        print("".join("██" if cell else "  " for cell in row))
    print()


def save_qrcode_content(content: str):
    if not content:
        return
    if content.startswith("data:image/"):
        header, b64 = content.split(",", 1)
        m = re.search(r"data:image/(\w+)", header)
        ext = m.group(1) if m else "png"
        with open(f"qrcode.{ext}", "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"二维码已保存到 qrcode.{ext}")
        log_qr.info("qr saved path=qrcode.%s fmt=%s", ext, ext)
    elif content.startswith("<svg"):
        with open("qrcode.svg", "w", encoding="utf-8") as f:
            f.write(content)
        print("二维码已保存到 qrcode.svg，用浏览器打开")
        log_qr.info("qr saved path=qrcode.svg")
    elif content.startswith("http"):
        render_terminal_qr(content)
        log_qr.info("qr URL (terminal render) url=%s", _redact_path(content))
    else:
        try:
            with open("qrcode.png", "wb") as f:
                f.write(base64.b64decode(content))
            print("二维码已保存到 qrcode.png")
            log_qr.info("qr saved path=qrcode.png from_raw_b64")
        except Exception:
            render_terminal_qr(content)


async def fetch_login_qrcode(session, local_token_list=None, base_url=None):
    """按 2.4.6 协议申请二维码；local_token_list 最多传 10 个。"""
    # 兼容旧版调用 fetch_login_qrcode(session, base_url, local_token_list)。
    # 最新协议始终使用固定二维码入口，旧 base_url 参数不会改变入口。
    if isinstance(local_token_list, str):
        legacy_tokens = base_url if isinstance(base_url, (list, tuple)) else []
        local_token_list = legacy_tokens
    tokens = []
    for token in local_token_list or []:
        token = str(token or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    body = {"local_token_list": tokens[:10]}
    log_qr.debug("qr fetch start local_tokens=%d", len(tokens))
    data = await api_post(
        session,
        "ilink/bot/get_bot_qrcode?bot_type=3",
        body,
        None,
        BASE_URL,
        # 官方 2.1.4 起不再为申请二维码设置固定客户端超时。
        timeout=None,
    )
    ensure_business_success(data, "get_bot_qrcode")
    if data.get("qrcode"):
        log_qr.info("qr fetched via POST has_qrcode=%s qr_len=%d",
                    bool(data.get("qrcode")), len(str(data.get("qrcode", ""))))
        return data

    # 只保留旧服务端的 GET 兼容兜底；2.4.6 官方流程为 POST。
    log_qr.warning("qr POST missing qrcode, falling back to GET (legacy)")
    data = await api_get(
        session,
        "ilink/bot/get_bot_qrcode?bot_type=3",
        None,
        BASE_URL,
        timeout=None,
    )
    ensure_business_success(data, "get_bot_qrcode")
    log_qr.info("qr fetched via GET fallback")
    return data


async def poll_login_status(session, qrcode, base_url=BASE_URL, verify_code=None):
    """轮询二维码状态。GET 请求不携带 Bearer token，超时视为 wait。"""
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    if verify_code:
        endpoint += f"&verify_code={quote(verify_code, safe='')}"
    status = await api_get(
        session,
        endpoint,
        None,
        base_url or BASE_URL,
        timeout=QR_STATUS_TIMEOUT,
        long_poll=True,
    )
    if status.get("_timeout"):
        return {"status": "wait"}
    ensure_business_success(status, "get_qrcode_status")
    state = status.get("status", "")

    if state == "confirmed" or status.get("bot_token"):
        bot_token = status.get("bot_token")
        ilink_bot_id = status.get("ilink_bot_id")
        if not bot_token or not ilink_bot_id:
            return {"login_error": "confirmed 响应缺少 bot_token 或 ilink_bot_id"}
        log_qr.info("poll status=confirmed bot_id=%s baseurl=%s",
                    str(ilink_bot_id)[:8] or "-",
                    (status.get("baseurl") or status.get("base_url") or base_url or BASE_URL))
        return {
            "bot_token": bot_token,
            "baseurl": status.get("baseurl") or status.get("base_url") or base_url or BASE_URL,
            "ilink_bot_id": ilink_bot_id,
            "ilink_user_id": status.get("ilink_user_id", ""),
        }
    if state == "binded_redirect" or status.get("binded_redirect"):
        log_qr.info("poll status=already_connected (binded_redirect)")
        return {"already_connected": True}
    if state == "expired":
        log_qr.info("poll status=expired")
        return {"expired": True}
    if state == "scaned_but_redirect":
        redirect_host = status.get("redirect_host")
        if redirect_host:
            redirect_base = str(redirect_host)
            if not redirect_base.startswith("http"):
                redirect_base = f"https://{redirect_base}"
            log_qr.info("poll status=redirect_host=%s", redirect_base)
            return {"redirect_base": redirect_base.rstrip("/")}
        log_qr.warning("poll status=redirect but no redirect_host, keep current")
        print("服务端要求切换扫码轮询节点，但未返回 redirect_host，继续使用当前节点。")
        return {}
    if state == "scaned":
        return {"scanned": True, "verify_code_accepted": bool(verify_code)}
    if state in ("need_verifycode", "verify_code_blocked") or status.get("need_verifycode"):
        if state == "verify_code_blocked":
            log_qr.warning("poll status=verify_code_blocked")
            return {"verify_code_blocked": True}
        return {"need_verifycode": True, "retry_verifycode": bool(verify_code)}
    if state and state != "wait":
        log_qr.warning("poll status=unknown state=%s body=%s", state, _redact_text(status))
    return {}


async def wait_login_confirmation(session, qrcode, base_url=BASE_URL, timeout_seconds=None,
                                  allow_already_connected=False, web_state=None,
                                  cancel_event=None):
    timeout_seconds = timeout_seconds or RECONNECT_CONFIG["qrcode_scan_timeout"]
    deadline = time.time() + timeout_seconds
    current_base_url = base_url or BASE_URL
    pending_verify_code = None
    scanned_printed = False

    while True:
        if time.time() >= deadline:
            log_qr.warning("login timed out after %.1fs", timeout_seconds)
            return {"timeout": True}
        # 协作式取消：portal /relink 在初始登录等待扫码阶段触发的 cancel_event
        # 必须在 1s 内命中，否则 listener 一直等不到 reconnect_in_progress 变 False
        if cancel_event is not None and cancel_event.is_set():
            log_qr.info("wait_login_confirmation cancelled by external event")
            return {"cancelled": True}

        try:
            result = await poll_login_status(session, qrcode, current_base_url, pending_verify_code)
        except ILinkAPIError as exc:
            log_qr.warning("poll network error type=%s err=%s, retry in 1s",
                           exc.network_type or "business", _redact_text(exc))
            await asyncio.sleep(1)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_qr.warning("poll error err=%s, retry in 1s", _redact_text(exc))
            await asyncio.sleep(1)
            continue

        if result.get("bot_token"):
            return result
        if result.get("login_error"):
            return result
        if result.get("already_connected"):
            return result if allow_already_connected else {"already_connected": True}
        if result.get("expired") or result.get("verify_code_blocked"):
            return result
        if result.get("redirect_base"):
            current_base_url = result["redirect_base"]
            log_qr.info("poll node switched to=%s", current_base_url)
            print(f"扫码轮询切换到新节点: {current_base_url}")
            continue
        if result.get("scanned"):
            if pending_verify_code and result.get("verify_code_accepted"):
                pending_verify_code = None
            if web_state is not None:
                web_state.status = "scanned"
            if not scanned_printed:
                log_qr.info("qr scanned, awaiting phone confirmation")
                print("已扫码，等待手机端确认...")
                scanned_printed = True
        if result.get("need_verifycode"):
            prompt = (
                "你输入的数字不匹配，请重新输入: "
                if result.get("retry_verifycode")
                else "请输入手机微信显示的数字配对码: "
            )
            if web_state is not None:
                remaining = max(1.0, deadline - time.time())
                # 单次等待不超过 120s；如果用户在页面不操作，触发超时让外层重发二维码
                code = await wait_for_verify_code(
                    web_state, prompt, bool(result.get("retry_verifycode")),
                    timeout=min(remaining, 120.0),
                )
                if code is None:
                    log_qr.warning("verify_code prompt timeout, restart qr flow")
                    print("[登录] 网页未提交配对码超时，回到二维码等待")
                    return {"timeout": True}
                pending_verify_code = code.strip()
            else:
                pending_verify_code = (await asyncio.to_thread(input, prompt)).strip()
            continue

        await asyncio.sleep(1)


async def login_with_qrcode(session, local_token_list=None, existing_state=None,
                            on_qrcode=None, web_state=None, cancel_event=None,
                            save_qr_artifact=True):
    """执行官方二维码登录，最多展示 MAX_QR_REFRESH_COUNT 个二维码。

    Args:
        cancel_event: 可选的 ``asyncio.Event``——若在循环中被 set，函数立刻
            抛 ``asyncio.CancelledError``。用于让 portal ``/switch`` 在初始
            登录等待扫码阶段也能触发接管（避免 listener 还没起来就被忽略）。
    """
    # 与官方实现一致：初始二维码计为第 1 次，最多共展示 3 个二维码。
    refresh_count = 1
    deadline = time.time() + RECONNECT_CONFIG["qrcode_scan_timeout"]
    existing_state = existing_state or {}
    log_qr.info("login start refresh_count=%d/%d deadline_in_s=%.0f",
                refresh_count, MAX_QR_REFRESH_COUNT,
                max(0.0, deadline - time.time()))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            log_qr.info("login cancelled by external event (cancel_event set)")
            raise asyncio.CancelledError("login_with_qrcode cancelled by event")
        remaining = deadline - time.time()
        if remaining <= 0:
            log_qr.error("login aborted refresh_count=%d reason=deadline_exceeded",
                         refresh_count)
            raise RuntimeError("登录等待超时，请重新运行后再试。")
        data = await fetch_login_qrcode(session, local_token_list)
        qrcode = data.get("qrcode")
        if not qrcode:
            log_qr.error("login failed reason=missing_qrcode")
            raise RuntimeError("二维码响应缺少 qrcode")
        qrcode_img_content = data.get("qrcode_img_content", "")

        print("qrcode:", _redact_text(qrcode))
        log_qr.info("qr cycle refresh=%d qr_len=%d img_content_present=%s",
                    refresh_count, len(str(qrcode)), bool(qrcode_img_content))
        qr_content = str(qrcode_img_content or qrcode)
        if save_qr_artifact:
            try:
                save_qrcode_content(qr_content)
            except Exception as exc:
                log_qr.warning("qr save failed err=%s, falling back to link", _redact_text(exc))
                print(f"二维码保存失败，将继续使用链接: {_redact_text(exc)}")
        if on_qrcode:
            callback_result = on_qrcode(qr_content)
            if inspect.isawaitable(callback_result):
                await callback_result
        print("等待扫码...")

        remaining = deadline - time.time()
        if remaining <= 0:
            log_qr.error("login aborted refresh_count=%d reason=deadline_after_qr",
                         refresh_count)
            raise RuntimeError("登录等待超时，请重新运行后再试。")
        login_result = await wait_login_confirmation(
            session,
            qrcode,
            BASE_URL,
            timeout_seconds=remaining,
            allow_already_connected=True,
            web_state=web_state,
            cancel_event=cancel_event,
        )
        if login_result.get("bot_token"):
            return login_result
        if login_result.get("cancelled"):
            # cancel_event 命中：把 cancelled 信号透传上去，让 main() 抛 CancelledError
            raise asyncio.CancelledError("login_with_qrcode cancelled by event")
        if login_result.get("already_connected"):
            old_token = str(existing_state.get("bot_token") or "").strip()
            if old_token:
                log_qr.info("login already_connected: reusing local token (len=%d)", len(old_token))
                print("服务端提示已连接过，复用本地保存的 token。")
                return {
                    "bot_token": old_token,
                    "baseurl": existing_state.get("baseurl") or BASE_URL,
                    "ilink_bot_id": existing_state.get("ilink_bot_id", ""),
                    "ilink_user_id": existing_state.get("ilink_user_id", ""),
                    "already_connected": True,
                }
            print("服务端提示此端已连接过，但本地没有可复用 token，将重新生成二维码。")
            log_qr.info("login already_connected but no local token, regenerating qr")
        elif login_result.get("expired"):
            log_qr.info("qr expired, regenerating (#%d)", refresh_count + 1)
            print("二维码已过期，正在重新生成...")
        elif login_result.get("verify_code_blocked"):
            log_qr.warning("verify_code blocked, regenerating (#%d)", refresh_count + 1)
            print("多次输入配对码错误，正在刷新二维码...")
        elif login_result.get("login_error"):
            log_qr.error("login failed reason=%s", login_result["login_error"])
            raise RuntimeError(login_result["login_error"])
        elif login_result.get("timeout"):
            log_qr.error("login failed reason=confirmation_timeout")
            raise RuntimeError("登录等待超时，请重新运行后再试。")

        refresh_count += 1
        if refresh_count > MAX_QR_REFRESH_COUNT:
            log_qr.error("login aborted refresh_count=%d reason=max_refresh_exceeded",
                         refresh_count - 1)
            raise RuntimeError("二维码多次失效或登录失败，请稍后重试。")


async def notify_lifecycle(session, endpoint, token, base_url):
    """发送 msg/notifystart 或 msg/notifystop 生命周期通知。"""
    try:
        result = await api_post(
            session,
            endpoint,
            {"base_info": base_info()},
            token,
            base_url or BASE_URL,
            timeout=CONFIG_TIMEOUT,
        )
        ensure_business_success(result, endpoint)
        log_reconnect.info("notify_lifecycle ok endpoint=%s", endpoint)
        print(f"[生命周期] {endpoint} 已通知")
        return True
    except ILinkAPIError as exc:
        log_reconnect.warning("notify_lifecycle failed endpoint=%s err=%s",
                              endpoint, _redact_text(exc))
        print(f"[生命周期] {endpoint} 通知失败: {_redact_text(exc)}")
        return False
    except Exception as exc:
        log_reconnect.error("notify_lifecycle crashed endpoint=%s err=%s",
                            endpoint, _redact_text(exc), exc_info=True)
        print(f"[生命周期] {endpoint} 通知异常: {_redact_text(exc)}")
        return False


async def main():
    """运行 Python Bot，并实现 2.4.6 的登录、长轮询和优雅停止流程。"""
    runtime_state = load_runtime_state()
    saved_token = str(runtime_state.get("bot_token") or "").strip()

    async with aiohttp.ClientSession() as session:
        # ---- 启动 web 登录页面（CLAWBOT_WEB_ENABLED=1 默认）----
        qr_state = QrFlowState()
        relogin_event = asyncio.Event()
        web_task: asyncio.Task | None = None
        web_on_qrcode = None
        if web_enabled():
            web_task = asyncio.create_task(qr_web_start(qr_state, relogin_event=relogin_event))
            web_on_qrcode = make_web_on_qrcode(qr_state, session)

        # ---- 初始化可变状态容器 + relogin_listener（必须在初始登录之前）----
        #
        # 历史 bug：relogin_listener 之前在 main() 末尾才创建，初始登录等待扫码期间
        # /relink 设的 event 没人消费，新 QR 永远不出。现把容器 / listener 都前置，
        # 初始登录期间用 reconnect_in_progress[0]=True 排斥 listener，
        # 登录完成后再让 listener 接棒（处理登录期间累积的 /relink 事件）。
        bot_token_ref = [saved_token]
        bot_base_url_ref = [runtime_state.get("baseurl", BASE_URL)]
        saved_contact = runtime_state.get("last_contact") or {}
        last_contact = {
            "from_id": saved_contact.get("from_id") or None,
            "context_token": saved_contact.get("context_token") or None,
        }
        typing_ticket_cache = {}
        welcomed_users = set(runtime_state.get("contexts", {}).keys())
        reconnect_asked = asyncio.Event()
        warning_active = [False]
        reconnect_in_progress = [False]
        login_time_ref = [time.time()]
        manual_reconnect_pending = {}
        # ---- 重新登录：单一 owner 模式 ----
        #
        # 历史 bug：relogin_listener（来自 /relink）、message_loop 的 -14 handler、
        # reconnect_timer_task 的 force_before、handle_message 的 "Y" 手动重连
        # 各自独立调 login_with_qrcode / do_reconnect。在 /relink 场景下 listener
        # 刚清空 token 触发自己的 do_reconnect 时，message_loop 的下一轮 getupdates
        # 用空 token 拿到 -14，又调一次 login_with_qrcode，两条链路并发抢
        # web_on_qrcode，前端看到的 QR 被两条链路互踩（"正在生成新二维码..." 卡很久
        # 然后突然换一张码 = 用户扫的码已失效）。修法：所有 4 条路径都通过
        # request_relogin() 入口；唯一真正干活的是 relogin_listener，并发调用者
        # 共享同一个 Future，避免双发登录。
        _relogin_lock = asyncio.Lock()
        _pending_relogin: list[Optional[asyncio.Future]] = [None]

        async def request_relogin(reason: str) -> dict:
            """所有重新登录路径的唯一入口。

            - 若已有 listener 在跑：直接 await 它的 Future，调用方零额外登录。
            - 若空闲：登记新 Future、set relogin_event 唤醒 listener，await 它的结果。

            Returns: do_reconnect 产出的 login_result dict（与 listen 自身用同一个）。
            Raises: do_reconnect 抛出的任何异常。
            """
            async with _relogin_lock:
                if _pending_relogin[0] is not None and not _pending_relogin[0].done():
                    log_reconnect.info("relogin dedup: joining in-flight reason=%s", reason)
                    existing = _pending_relogin[0]
                else:
                    existing = None
                    future: asyncio.Future = asyncio.get_event_loop().create_future()
                    _pending_relogin[0] = future
                    relogin_event.set()
                    log_reconnect.warning("relogin requested reason=%s", reason)
            if existing is not None:
                return await existing
            return await future

        async def relogin_listener():
            """重新登录的真正执行者（唯一 owner）。被 request_relogin 或 /relink 唤醒。

            触发条件 1：portal 用户在右上角点"切换账号" → /relink → qr_web 把
            relogin_event.set()。
            触发条件 2：request_relogin 被 message_loop / timer / handle_message
            "Y" 调用，登记 Future + set event。

            行为：清空 token 引用 → 调 do_reconnect → 把结果 set 给 _pending_relogin
            （如果有等待方）。Future 异常路径同样 set_exception，让 await 抛出来。
            """
            log_reconnect.warning("relogin_listener task started")
            while True:
                await relogin_event.wait()
                # reconnect_in_progress=True 时**不**清 event，让初始 login 的
                # cancel_event 检查能在 ~1s 内命中；但必须 sleep 避免 busy-loop
                # （不清 event + 不 sleep 会让 wait() 立刻返回陷入死循环）。
                if reconnect_in_progress[0]:
                    log_reconnect.info("relogin_listener skipped: reconnect in progress; event kept")
                    await asyncio.sleep(0.5)
                    continue
                relogin_event.clear()
                # 拿当前等待方（若有）。即使没有人 await 也要跑（/relink 是
                # fire-and-forget；没有等待方就直接 set 不上 future，do_reconnect
                # 仍然把新 QR 推到 qr_state）。
                async with _relogin_lock:
                    current_future = _pending_relogin[0]
                log_reconnect.warning("relogin_listener running; has_awaiter=%s",
                                     current_future is not None)
                try:
                    runtime_state["bot_token"] = ""
                    save_runtime_state(runtime_state)
                    bot_token_ref[0] = ""
                    qr_state.status = "qr_pending"  # 双保险（qr_web 已经设过）
                    login_result = await do_reconnect(
                        session, bot_token_ref, bot_base_url_ref, last_contact,
                        typing_ticket_cache, reconnect_asked, warning_active,
                        reconnect_in_progress, login_time_ref, RECONNECT_CONFIG,
                        runtime_state,
                        web_on_qrcode=web_on_qrcode,
                        web_state=qr_state,
                    )
                    if current_future is not None and not current_future.done():
                        current_future.set_result(login_result)
                except asyncio.CancelledError:
                    # main() 在被取消时也会取消本 task；如果有等待方，要把异常传出去
                    # 否则 caller 永久挂起。
                    if current_future is not None and not current_future.done():
                        current_future.set_exception(asyncio.CancelledError())
                    raise
                except Exception as exc:
                    log_reconnect.error("relogin_listener crashed err=%s",
                                        _redact_text(exc), exc_info=True)
                    if current_future is not None and not current_future.done():
                        current_future.set_exception(exc)
                finally:
                    # 兜底：do_reconnect 任何退出路径（成功 / 异常 / max_refresh_exceeded）
                    # 都确保 is_regenerating=False。如果 set_qr_png 没机会跑（fetch 失败
                    # / qrcode 缺失），前端就不会永远卡在"正在生成..."。
                    if qr_state.is_regenerating:
                        log_reconnect.warning("relogin_listener: clearing stale is_regenerating "
                                              "(do_reconnect exited without set_qr_png)")
                        qr_state.is_regenerating = False
                    # 释放 _pending_relogin 槽位，让下一轮 request_relogin 能起新流程。
                    async with _relogin_lock:
                        if _pending_relogin[0] is current_future:
                            _pending_relogin[0] = None

        relogin_task = asyncio.create_task(relogin_listener())

        # 初始登录期间占住 reconnect_in_progress，避免 relogin_listener 抢跑
        # 触发第二个 login_with_qrcode（两个并行会竞争 QR、互相覆盖 state）。
        # 包成 task 是为了让 cancel_event 命中时能强制 cancel_task.cancel() 中断
        # iLink 的 35s 长轮询 poll_login_status（不用 task 的话只能在 1-35s 后
        # 排队等 poll 返回再检测 cancel，listener 一直等不到 reconnect_in_progress
        # 变 False）。
        reconnect_in_progress[0] = True
        login_result: Optional[dict] = None
        login_task: Optional[asyncio.Task] = None
        try:
            if saved_token:
                # 官方客户端会按账号复用本地凭据；若服务端随后返回 -14，
                # 消息循环会停止紧密轮询并进入受控二维码重登录。
                print("[登录] 复用本地保存的微信连接；token 失效时会自动要求重新扫码。")
                log_qr.info("login reuse saved_token len=%d", len(saved_token))
                login_result = {
                    "bot_token": saved_token,
                    "baseurl": runtime_state.get("baseurl") or BASE_URL,
                    "ilink_bot_id": runtime_state.get("ilink_bot_id", ""),
                    "ilink_user_id": runtime_state.get("ilink_user_id", ""),
                }
            else:
                # 同时启动 cancel-watcher：relogin_event 一被 set，立刻取消 login_task
                # （task 模式：cancel_event 协作式检测在 iLink 35s 长轮询里走不到，
                # 必须强制 cancel task 才能让 login_with_qrcode 立刻抛 CancelledError）
                async def _cancel_initial_login():
                    await relogin_event.wait()
                    if login_task is not None and not login_task.done():
                        log_qr.info("cancel-watcher: cancelling initial login task")
                        login_task.cancel()
                cancel_watcher = asyncio.create_task(_cancel_initial_login())
                login_task = asyncio.create_task(login_with_qrcode(
                    session,
                    [],
                    existing_state=runtime_state,
                    on_qrcode=web_on_qrcode,
                    web_state=qr_state,
                ))
                try:
                    login_result = await login_task
                finally:
                    cancel_watcher.cancel()
                    try:
                        await cancel_watcher
                    except (asyncio.CancelledError, Exception):
                        pass
        except asyncio.CancelledError:
            # relogin_event 触发 cancel_task → login_with_qrcode 抛 CancelledError；
            # 此时不要退出 main()，让 relogin_listener 接手：它会清 token、跑 do_reconnect
            # 重新出 QR、扫到后再把 token 写回 bot_token_ref[0]。这里等 do_reconnect
            # 完成（bot_token_ref 被 listener 重新填好）后继续走 message_loop。
            log_qr.warning("initial login cancelled by /relink; "
                           "awaiting relogin_listener to produce new token")
            # **立刻**释放 reconnect_in_progress，让 listener 的下一轮 wait() 命中
            # 并启动 do_reconnect（不再 sleep + skip）。如果等 60s listener 还没填回
            # token（一般不会发生），则下面抛 RuntimeError 终止进程。
            reconnect_in_progress[0] = False
            for _ in range(600):  # 60s 上限
                await asyncio.sleep(0.1)
                if bot_token_ref[0]:
                    break
            else:
                raise RuntimeError("relogin_listener did not produce token within 60s")
            # 把 listener 刚拿到的凭据同步到 runtime_state 和 login_result，
            # 让下面的"登录成功"代码块正常 apply / 发 notifystart / 起 message_loop。
            login_result = {
                "bot_token": bot_token_ref[0],
                "baseurl": bot_base_url_ref[0] or runtime_state.get("baseurl") or BASE_URL,
                "ilink_bot_id": runtime_state.get("ilink_bot_id", ""),
                "ilink_user_id": runtime_state.get("ilink_user_id", ""),
            }
            log_qr.info("resuming main() after listener produced token "
                        "bot_id=%s", str(login_result["ilink_bot_id"])[:8] or "-")
        else:
            reconnect_in_progress[0] = False
        if login_result is None:
            # 理论不会到这里；defensive：未拿到 token 直接退出
            log_qr.error("login failed reason=no_login_result")
            return
        bot_token = str(login_result.get("bot_token") or "").strip()
        if not bot_token:
            log_qr.error("login failed reason=missing_bot_token_in_response")
            raise RuntimeError("登录响应缺少 bot_token")

        qr_state.status = "logged_in"
        qr_state.logged_in_at = time.time()

        bot_base_url = login_result.get("baseurl") or runtime_state.get("baseurl") or BASE_URL
        bot_base_url_ref[0] = bot_base_url
        bot_token_ref[0] = bot_token
        old_account_id = str(runtime_state.get("ilink_bot_id") or "")
        new_account_id = str(login_result.get("ilink_bot_id") or old_account_id)
        if new_account_id and old_account_id != new_account_id:
            # 账号切换时不能复用旧账号的游标和上下文。
            runtime_state["get_updates_buf"] = ""
            runtime_state["contexts"] = {}
            runtime_state["last_contact"] = {"from_id": "", "context_token": ""}

        runtime_state.update({
            "bot_token": bot_token,
            "baseurl": bot_base_url,
            "ilink_bot_id": new_account_id,
            "ilink_user_id": login_result.get("ilink_user_id") or runtime_state.get("ilink_user_id", ""),
        })
        save_runtime_state(runtime_state)

        print(f"登录成功！baseurl={bot_base_url}")
        print(f"{'=' * 40}\n{COMMANDS_MSG}\n{'=' * 40}")

        await notify_lifecycle(session, "ilink/bot/msg/notifystart", bot_token, bot_base_url)

        async def apply_new_login(result):
            """原子替换登录凭据，并在账号变化时隔离游标/上下文。"""
            new_token = str(result.get("bot_token") or "").strip()
            if not new_token:
                raise RuntimeError("重新登录响应缺少 bot_token")
            old_id = str(runtime_state.get("ilink_bot_id") or "")
            new_id = str(result.get("ilink_bot_id") or old_id)
            old_token = str(runtime_state.get("bot_token") or bot_token_ref[0] or "")
            old_base_url = str(runtime_state.get("baseurl") or bot_base_url_ref[0] or BASE_URL)
            new_base_url = result.get("baseurl") or old_base_url
            account_changed = bool(new_id and old_id != new_id)
            credentials_changed = bool(
                account_changed or new_token != old_token or new_base_url != old_base_url
            )
            log_reconnect.info("apply new_login old_id=%s new_id=%s account_changed=%s",
                               old_id[:8] or "-", new_id[:8] or "-", account_changed)
            if account_changed:
                log_reconnect.info("apply new_login account switched, contexts+cursor+last_contact cleared")
                runtime_state["get_updates_buf"] = ""
                runtime_state["contexts"] = {}
                runtime_state["last_contact"] = {"from_id": "", "context_token": ""}
                last_contact["from_id"] = None
                last_contact["context_token"] = None
                welcomed_users.clear()
            bot_token_ref[0] = new_token
            bot_base_url_ref[0] = new_base_url
            runtime_state.update({
                "bot_token": new_token,
                "baseurl": bot_base_url_ref[0],
                "ilink_bot_id": new_id,
                "ilink_user_id": result.get("ilink_user_id") or runtime_state.get("ilink_user_id", ""),
                "get_updates_buf": runtime_state.get("get_updates_buf") or "",
            })
            typing_ticket_cache.clear()
            save_runtime_state(runtime_state)
            log_reconnect.info("apply new_login saved new_baseurl=%s", new_base_url)
            login_time_ref[0] = time.time()
            if credentials_changed:
                await notify_lifecycle(
                    session,
                    "ilink/bot/msg/notifystart",
                    bot_token_ref[0],
                    bot_base_url_ref[0],
                )

        async def handle_message(msg):
            if not isinstance(msg, dict) or msg.get("message_type") != 1:
                return
            from_id = str(msg.get("from_user_id") or "")
            context_token = str(msg.get("context_token") or "")
            if not from_id or not context_token:
                log_msg.debug("handle msg skipped reason=missing_from_or_context")
                print("[消息] 缺少 from_user_id/context_token，跳过")
                return
            text = extract_message_text(msg)
            item_types = {
                item.get("type") for item in (msg.get("item_list") or [])
                if isinstance(item, dict)
            }
            has_voice = is_voice_message(msg)
            voice_text = extract_voice_transcript(msg).strip() if has_voice else ""
            input_kind = (
                "voice_transcript" if voice_text
                else "voice_without_transcript" if has_voice
                else "text"
            )
            log_msg.info("recv msg from=%s type=%s len=%d preview=%r",
                         from_id[-8:] if from_id else "-",
                         input_kind,
                         len(text),
                         (text or "")[:30])
            print(f"收到{('语音转写' if input_kind == 'voice_transcript' else '消息')}: "
                  f"{text or '[无可用转写]'}")

            last_contact.update({"from_id": from_id, "context_token": context_token})
            runtime_state.setdefault("contexts", {})[from_id] = context_token
            runtime_state["last_contact"] = dict(last_contact)
            save_runtime_state(runtime_state)

            # Voice messages already have their own conversational response:
            # either the LLM answer based on iLink's transcript or the
            # actionable missing-transcript feedback.  Do not prepend the
            # first-contact welcome message to either voice path.
            if has_voice:
                welcomed_users.add(from_id)

            normalized = text.strip().upper()
            if manual_reconnect_pending.get(from_id) and normalized in ("Y", "N"):
                log_msg.debug("handle manual reconnect reply from=%s choice=%s",
                              from_id[-8:], normalized)
                manual_reconnect_pending.pop(from_id, None)
                if normalized == "Y":
                    await send_msg_safe(session, from_id, context_token, "好的，正在重新连接...",
                                        bot_token_ref, bot_base_url_ref)
                    try:
                        await request_relogin("manual-reconnect")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log_msg.error("manual reconnect failed err=%s", _redact_text(exc), exc_info=True)
                        await send_msg_safe(session, from_id, context_token,
                                            f"[失败] 重连未完成: {_redact_text(exc)}",
                                            bot_token_ref, bot_base_url_ref)
                else:
                    await send_msg_safe(session, from_id, context_token, "已取消重新连接",
                                        bot_token_ref, bot_base_url_ref)
                return

            # 后台静默策略（2026-09-14 改）：reconnect_timer_task 不再 set
            # warning_active[0]，所以下面这条 if 永远不会为 True，保留仅为
            # 历史兼容 / 未来恢复交互式提醒时启用。
            if warning_active[0] and normalized in ("Y", "N"):
                log_msg.debug("handle reconnect warning reply from=%s choice=%s",
                              from_id[-8:], normalized)
                if normalized == "Y":
                    reconnect_asked.set()
                    await send_msg_safe(session, from_id, context_token, "好的，正在重新连接...",
                                        bot_token_ref, bot_base_url_ref)
                else:
                    await send_msg_safe(session, from_id, context_token, "好的，稍后再提醒您",
                                        bot_token_ref, bot_base_url_ref)
                return

            if has_voice and not voice_text:
                log_msg.info(
                    "voice transcript unavailable; sending actionable feedback from=%s",
                    from_id[-8:],
                )
                await send_msg_safe(
                    session, from_id, context_token,
                    VOICE_TRANSCRIPT_UNAVAILABLE_MSG,
                    bot_token_ref, bot_base_url_ref,
                )
                return

            if not text:
                log_msg.info("recv msg skipped reason=no_text_or_voice_transcript types=%s",
                             sorted(str(item_type) for item_type in item_types))
                return

            if from_id not in welcomed_users:
                log_msg.debug("handle welcome from=%s", from_id[-8:])
                welcomed_users.add(from_id)
                await send_msg_safe(session, from_id, context_token, INTRO_DETAIL_MSG,
                                    bot_token_ref, bot_base_url_ref)

            if normalized in ("/HELP", "/指令"):
                log_msg.debug("handle command name=/help from=%s", from_id[-8:])
                await send_msg_safe(session, from_id, context_token, COMMANDS_MSG,
                                    bot_token_ref, bot_base_url_ref)
                return
            if normalized == "/TIME":
                log_msg.debug("handle command name=/time from=%s", from_id[-8:])
                if RECONNECT_CONFIG.get("proactive_relogin", False):
                    remaining = max(0, login_time_ref[0] + RECONNECT_CONFIG["session_duration"] - time.time())
                    hours, minutes, seconds = int(remaining // 3600), int((remaining % 3600) // 60), int(remaining % 60)
                    display = f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟 {seconds} 秒"
                    text_reply = f"当前连接剩余时间：{display}"
                else:
                    text_reply = "当前连接由后台持续维护，服务端 token 失效时会自动恢复。"
                await send_msg_safe(session, from_id, context_token, text_reply,
                                    bot_token_ref, bot_base_url_ref)
                return
            if text == "/重新连接":
                log_msg.debug("handle command name=/重新连接 from=%s", from_id[-8:])
                if reconnect_in_progress[0]:
                    await send_msg_safe(session, from_id, context_token, "重连正在进行中，请稍候...",
                                        bot_token_ref, bot_base_url_ref)
                else:
                    manual_reconnect_pending[from_id] = True
                    await send_msg_safe(
                        session, from_id, context_token,
                        "确认要立即重新连接吗？\n回复 Y 确认重连 / N 取消",
                        bot_token_ref, bot_base_url_ref,
                    )
                return

            # ========== Per-user IMA KB 绑定命令（docs/IMA_WEB_UI.md §4.3） ==========
            # 仅 bot 主人（from_id == ilink_user_id）可操作；他人拒绝。
            owner_id_cmd = str(runtime_state.get("ilink_user_id") or "")
            if text.startswith("/bindkb") or text in ("/unbindkb", "/mykb"):
                if not owner_id_cmd:
                    await send_msg_safe(
                        session, from_id, context_token,
                        "尚未绑定到 iLink 账号，请先完成扫码登录后再管理知识库。",
                        bot_token_ref, bot_base_url_ref,
                    )
                    return
                if from_id != owner_id_cmd:
                    log_msg.warning(
                        "ima cmd denied cmd=%s from=%s owner=%s",
                        text.split()[0], from_id[-8:], owner_id_cmd[-8:],
                    )
                    await send_msg_safe(
                        session, from_id, context_token,
                        "❌ 权限不足：只有 bot 主人能管理知识库",
                        bot_token_ref, bot_base_url_ref,
                    )
                    return
                # 鉴权通过；进入分支
                cmd_parts = text.split(maxsplit=1)
                cmd = cmd_parts[0].upper()
                arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""
                log_msg.debug("handle command name=%s from=%s", cmd, from_id[-8:])
                if cmd == "/MYKB":
                    try:
                        bindings = await _get_ima_bindings()
                        b = bindings.lookup(owner_id_cmd)
                    except Exception as exc:
                        log_msg.warning("mykb lookup failed err=%s", exc)
                        b = None
                    if b:
                        kb_label = b.get("kb_name") or "(未命名)"
                        text_reply = (
                            f"当前知识库：{kb_label}（{b.get('kb_id', '')}）\n"
                            f"绑定时间：{b.get('bound_at', '')}"
                        )
                    else:
                        text_reply = "当前知识库：未绑定（回退默认 IMA_ILINK_DEFAULT_KB）"
                    await send_msg_safe(session, from_id, context_token, text_reply,
                                        bot_token_ref, bot_base_url_ref)
                    return
                if cmd == "/UNBINDKB":
                    try:
                        bindings = await _get_ima_bindings()
                        removed = await bindings.unbind(owner_id_cmd)
                    except Exception as exc:
                        log_msg.warning("unbindkb failed err=%s", exc)
                        removed = False
                    text_reply = (
                        "✅ 已解绑知识库，后续问答将回退到默认 KB。"
                        if removed else "当前没有绑定的知识库。"
                    )
                    await send_msg_safe(session, from_id, context_token, text_reply,
                                        bot_token_ref, bot_base_url_ref)
                    return
                if cmd == "/BINDKB":
                    # 构造 ImaClient 拿可绑定列表；与 bot_session._make_ai 同模式
                    ima_cfg = ImaConfig.from_env()
                    if not ima_cfg.configured():
                        await send_msg_safe(
                            session, from_id, context_token,
                            "❌ IMA 未配置（缺 IMA_ILINK_CLIENT_ID / API_KEY），无法绑定。",
                            bot_token_ref, bot_base_url_ref,
                        )
                        return
                    try:
                        client = ImaClient(ima_cfg)
                        kbs = client.list_searchable_kbs()
                    except Exception as exc:
                        log_msg.warning("bindkb list_searchable_kbs failed err=%s", exc)
                        await send_msg_safe(
                            session, from_id, context_token,
                            f"❌ 拉取 IMA 知识库列表失败：{_redact_text(exc)}",
                            bot_token_ref, bot_base_url_ref,
                        )
                        return
                    if not arg:
                        # 无参数：列出可绑定 KB
                        if not kbs:
                            text_reply = "暂无可绑定的知识库（账号下没有共享或订阅类 KB）。"
                        else:
                            lines = ["可绑定的 IMA 知识库："]
                            for idx, kb in enumerate(kbs, 1):
                                type_label = "共享" if kb["kb_type"] == 1002 else "订阅"
                                lines.append(
                                    f"  {idx}. {kb['kb_name']} [{type_label}] "
                                    f"({kb['kb_id'][:12]}…)"
                                )
                            lines.append("\n回复 /bindkb <kb_id> 完成绑定。")
                            text_reply = "\n".join(lines)
                        await send_msg_safe(session, from_id, context_token, text_reply,
                                            bot_token_ref, bot_base_url_ref)
                        return
                    # 有参数：按 kb_id 查找并绑定
                    matched = None
                    for kb in kbs:
                        if kb["kb_id"] == arg:
                            matched = kb
                            break
                    if matched is None:
                        # 接受前缀匹配（kb_id 可能被截断显示），但要求唯一命中
                        prefix_matches = [kb for kb in kbs if kb["kb_id"].startswith(arg)]
                        if len(prefix_matches) == 1:
                            matched = prefix_matches[0]
                        elif len(prefix_matches) > 1:
                            text_reply = (
                                f"❌ 前缀 {arg!r} 命中 {len(prefix_matches)} 个 KB，"
                                "请提供完整 kb_id。"
                            )
                            await send_msg_safe(session, from_id, context_token, text_reply,
                                                bot_token_ref, bot_base_url_ref)
                            return
                    if matched is None:
                        text_reply = (
                            f"❌ 未找到 kb_id={arg!r}。先发 /bindkb 查看可绑定列表。"
                        )
                        await send_msg_safe(session, from_id, context_token, text_reply,
                                            bot_token_ref, bot_base_url_ref)
                        return
                    try:
                        bindings = await _get_ima_bindings()
                        await bindings.bind(
                            owner_id_cmd,
                            matched["kb_id"],
                            matched["kb_name"],
                            int(matched["kb_type"]),
                            bound_by="wechat",
                            bot_id_at_bind=str(
                                runtime_state.get("ilink_bot_id") or ""
                            ),
                        )
                    except Exception as exc:
                        log_msg.warning("bindkb failed err=%s", exc)
                        await send_msg_safe(
                            session, from_id, context_token,
                            f"❌ 绑定失败：{_redact_text(exc)}",
                            bot_token_ref, bot_base_url_ref,
                        )
                        return
                    text_reply = (
                        f"✅ 已绑定 KB: {matched['kb_name']}（{matched['kb_id']}）\n"
                        "下一条消息开始生效。"
                    )
                    await send_msg_safe(session, from_id, context_token, text_reply,
                                        bot_token_ref, bot_base_url_ref)
                    return

            typing_ticket = await get_typing_ticket_safe(
                session,
                from_id,
                context_token,
                typing_ticket_cache,
                bot_token_ref,
                bot_base_url_ref,
            )

            typing_started = False
            t_ai_start = time.perf_counter()
            log_ai.info("ai call start from=%s prompt_chars=%d",
                        from_id[-8:] if from_id else "-", len(text))
            # Per-user IMA KB 绑定（docs/IMA_PER_USER_BINDING.md）：
            # 按 ilink_user_id 查 ima_bindings.json，把命中的 kb_id 透传给
            # _AIWithIma.chat；未命中走 IMA_ILINK_DEFAULT_KB 兜底。lookup 走
            # 单例 IMABindings（asyncio.Lock 序列化 bind/unbind）。
            owner_id = str(runtime_state.get("ilink_user_id") or "")
            kb_id: Optional[str] = None
            if owner_id:
                try:
                    bindings = await _get_ima_bindings()
                    kb_id = bindings.lookup_kb_id(owner_id)
                except Exception as exc:  # 持久化层异常不能让回复失败
                    log_ai.warning("ima_bindings lookup failed user=%s err=%s",
                                   owner_id[-8:], exc)
                    kb_id = None
            try:
                typing_started = await send_typing_safe(
                    session, from_id, typing_ticket, 1, bot_token_ref, bot_base_url_ref,
                )
                try:
                    loop = asyncio.get_running_loop()
                    reply = await loop.run_in_executor(
                        executor, partial(ai.chat, text, kb_id=kb_id),
                    )
                except Exception as exc:
                    dur_ms = (time.perf_counter() - t_ai_start) * 1000
                    log_ai.warning("ai call failed from=%s dur_ms=%.0f err=%s",
                                   from_id[-8:] if from_id else "-", dur_ms,
                                   _redact_text(exc))
                    print(f"[AI] 调用失败: {_redact_text(exc)}")
                    reply = "抱歉，AI 服务暂时不可用，请稍后再试。"
                reply = str(reply or "").strip() or "抱歉，我暂时没有生成有效回复。"

                client_id = generate_client_id()
                send_result = await api_post(
                    session,
                    "ilink/bot/sendmessage",
                    {
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": from_id,
                            "client_id": client_id,
                            "message_type": 2,
                            "message_state": 2,
                            "context_token": context_token,
                            "item_list": [{"type": 1, "text_item": {"text": reply}}],
                        },
                        "base_info": base_info(),
                    },
                    bot_token_ref[0],
                    bot_base_url_ref[0] or None,
                    timeout=API_TIMEOUT,
                )
                ensure_business_success(send_result, "sendmessage")
                safe_reply = _redact_text(reply)
                log_msg.info("reply sent to=%s chars=%d preview=%r",
                             from_id[-8:] if from_id else "-", len(reply),
                             (safe_reply or "")[:30])
                print(f"已回复: {safe_reply[:50]}{'...' if len(safe_reply) > 50 else ''}")
            finally:
                dur_ms_total = (time.perf_counter() - t_ai_start) * 1000
                log_ai.info("ai call done from=%s dur_ms=%.0f reply_chars=%d",
                            from_id[-8:] if from_id else "-", dur_ms_total,
                            len(reply) if 'reply' in locals() else 0)
                if typing_started:
                    await send_typing_safe(
                        session, from_id, typing_ticket, 2, bot_token_ref, bot_base_url_ref,
                    )

        async def message_loop():
            get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
            long_poll_timeout = LONG_POLL_TIMEOUT
            consecutive_failures = 0
            log_msg.info("message loop started cursor_len=%d poll_timeout=%.1fs",
                         len(get_updates_buf), long_poll_timeout)
            print("开始监听消息...")
            while True:
                try:
                    # 定时/手动重连可能由另一个协程清空持久化游标。
                    # 每轮先同步一次，避免新连接继续使用旧账号的游标。
                    state_cursor = str(runtime_state.get("get_updates_buf") or "")
                    if state_cursor != get_updates_buf:
                        get_updates_buf = state_cursor
                        long_poll_timeout = LONG_POLL_TIMEOUT

                    request_token = bot_token_ref[0]
                    request_base_url = bot_base_url_ref[0] or BASE_URL
                    log_msg.debug("getupdates start cursor_len=%d", len(get_updates_buf))
                    result = await api_post(
                        session,
                        "ilink/bot/getupdates",
                        {"get_updates_buf": get_updates_buf, "base_info": base_info()},
                        request_token,
                        request_base_url,
                        timeout=long_poll_timeout,
                        long_poll=True,
                        fallback_cursor=get_updates_buf,
                    )
                    if result.get("_timeout"):
                        log_msg.debug("getupdates long-poll timeout (normal)")
                        await asyncio.sleep(0)
                        continue
                    if (
                        request_token != bot_token_ref[0]
                        or request_base_url != (bot_base_url_ref[0] or BASE_URL)
                    ):
                        # 重连期间完成的旧长轮询响应不能覆盖新连接的状态。
                        log_msg.debug("getupdates stale response ignored (token/baseurl changed during reconnect)")
                        get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
                        long_poll_timeout = LONG_POLL_TIMEOUT
                        continue
                    ensure_business_success(result, "getupdates")
                    consecutive_failures = 0

                    new_cursor = result.get("get_updates_buf")
                    if isinstance(new_cursor, str) and new_cursor:
                        get_updates_buf = new_cursor
                        runtime_state["get_updates_buf"] = new_cursor
                        save_runtime_state(runtime_state)

                    suggested_ms = result.get("longpolling_timeout_ms")
                    try:
                        if suggested_ms is not None and float(suggested_ms) > 0:
                            long_poll_timeout = max(
                                1.0,
                                min(MAX_LONG_POLL_TIMEOUT, float(suggested_ms) / 1000.0),
                            )
                    except (TypeError, ValueError):
                        pass

                    msgs = result.get("msgs") or []
                    if msgs:
                        log_msg.info("getupdates received msgs=%d cursor_advanced=%s",
                                     len(msgs), bool(new_cursor))
                    for msg in msgs:
                        await handle_message(msg)
                except asyncio.CancelledError:
                    raise
                except ILinkAPIError as exc:
                    if exc.is_stale_token:
                        log_msg.warning("iLink -14 stale_token; requesting relogin")
                        print("[iLink] ret/errcode=-14，当前 token 已失效，进入受控重新登录。")
                        # 走单一入口：若 listener 已被 /relink 唤醒（典型场景：
                        # 用户点"切换账号"时 listener 刚清 token、开始 do_reconnect），
                        # 这里直接 await 同一 Future，绝不会并发调第二个
                        # login_with_qrcode 导致 web_on_qrcode 被两条链路互踩。
                        try:
                            await request_relogin("iLink -14")
                            # 重新读持久化游标：同账号 re-login 保留游标；换账号时
                            # listener 内部已清空 runtime_state。reset long-poll 节奏。
                            get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
                            long_poll_timeout = LONG_POLL_TIMEOUT
                            consecutive_failures = 0
                            continue
                        except asyncio.CancelledError:
                            raise
                        except Exception as relogin_exc:
                            log_msg.error("iLink controlled relogin failed err=%s",
                                          _redact_text(relogin_exc), exc_info=True)
                            print(f"[iLink] 重新登录失败: {_redact_text(relogin_exc)}")
                            consecutive_failures += 1
                            await asyncio.sleep(BACKOFF_DELAY)
                            continue
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        delay = BACKOFF_DELAY
                        consecutive_failures = 0
                    else:
                        delay = RETRY_DELAY
                    log_msg.warning("getupdates failed type=%s err=%s; retry in %ds (consec=%d)",
                                    exc.network_type or "business",
                                    _redact_text(exc), delay, consecutive_failures)
                    print(f"[iLink] getupdates 失败({exc.network_type or '业务'}): {_redact_text(exc)}；{delay}s 后重试")
                    await asyncio.sleep(delay)
                except Exception as exc:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        delay = BACKOFF_DELAY
                        consecutive_failures = 0
                    else:
                        delay = RETRY_DELAY
                    log_msg.error("message loop unhandled err=%s; retry in %ds",
                                  _redact_text(exc), delay, exc_info=True)
                    print(f"[消息循环] 未分类异常: {_redact_text(exc)}；{delay}s 后重试")
                    await asyncio.sleep(delay)

        timer_task = None
        if RECONNECT_CONFIG.get("proactive_relogin", False):
            timer_task = asyncio.create_task(reconnect_timer_task(
                session, bot_token_ref, bot_base_url_ref, last_contact,
                typing_ticket_cache, reconnect_asked, warning_active,
                reconnect_in_progress, login_time_ref, RECONNECT_CONFIG, runtime_state,
                web_on_qrcode=web_on_qrcode,
                web_state=qr_state,
            ))

        message_task = asyncio.create_task(message_loop())
        try:
            await message_task
        finally:
            all_tasks = [message_task, relogin_task]
            if timer_task is not None:
                all_tasks.append(timer_task)
            if web_task is not None:
                all_tasks.append(web_task)
            for task in all_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*all_tasks, return_exceptions=True)
            if bot_token_ref[0]:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(notify_lifecycle(
                            session,
                            "ilink/bot/msg/notifystop",
                            bot_token_ref[0],
                            bot_base_url_ref[0] or BASE_URL,
                        )),
                        timeout=CONFIG_TIMEOUT + 1,
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    log_reconnect.warning("notify_lifecycle stop timeout err=%s",
                                          _redact_text(exc))
                    print(f"[生命周期] 停止通知超时: {_redact_text(exc)}")
                except Exception as exc:
                    log_reconnect.error("notify_lifecycle stop crashed err=%s",
                                        _redact_text(exc), exc_info=True)
                    print(f"[生命周期] 停止通知未完成: {_redact_text(exc)}")


class _AIWithIma:
    """透明地把 ima 检索注入到 AI 调用的 system prompt。

    `handle_message` 仍然只调用 ``ai.chat(text)``；检索与提示词拼接
    在这里完成，不污染协议层的代码路径。如果 ``ima_client`` 未配置或检索
    为空，等价于直通到底层 ``_base``。
    """

    def __init__(self, base, ima_client: ImaClient, environ=None):
        self._base = base
        self._ima = ima_client
        self._env = os.environ if environ is None else dict(environ)
        self.config = base.config  # 保持 ai.config.prompt 等属性可访问
        # 本地 Markdown KB 兜底索引（lazy：只有命中 IMA 0 条时才会建索引）
        self._local_kb: Optional[LocalKBIndex] = None
        self._local_kb_attempted = False
        # 语义检索 KB 兜底索引（lazy：IMA + local 都 0 命中才触发）
        # 需 fastembed + 模型下载；首次 search 会触发重建（30~60s 阻塞），
        # 日志走 ``clawbot.semantic``，warn 级排查。
        self._semantic_kb = None
        self._semantic_kb_attempted = False

    def _maybe_local_kb(self) -> Optional[LocalKBIndex]:
        """lazy 构造本地 KB 索引，仅在开关开启时。"""
        if self._local_kb_attempted:
            return self._local_kb
        self._local_kb_attempted = True
        if self._env.get("CLAWBOT_LOCAL_FALLBACK", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return None
        kb_dir = self._env.get("CLAWBOT_LOCAL_KB_DIR", "docs/knowledge")
        try:
            self._local_kb = LocalKBIndex(kb_dir)
        except Exception as exc:  # 路径/权限异常不能让回复失败
            log_ai.warning("ai local_kb init failed dir=%s err=%s", kb_dir, exc)
            self._local_kb = None
        return self._local_kb

    def _maybe_semantic_kb(self):
        """lazy 构造语义检索索引。仅在 ``SEMANTIC_KB_ENABLED=1`` 时有效。

        失败（fastembed 未装 / 模型下不下来 / 路径错）一律静默降级返回 ``None``，
        让上层走纯 LLM。索引路径默认 ``docs/knowledge/``，落库到
        ``docs/.semantic_kb.sqlite3``（已 gitignore）。
        """
        if self._semantic_kb_attempted:
            return self._semantic_kb
        self._semantic_kb_attempted = True
        if SemanticKBIndex is None:
            log_ai.info(
                "ai semantic_kb unavailable: utils.semantic_kb.SemanticKBIndex "
                "import failed (fastembed 缺失？)。开 SEMANTIC_KB_ENABLED 前 "
                "先 pip install fastembed"
            )
            return None
        if self._env.get("SEMANTIC_KB_ENABLED", "").strip().lower() not in (
            "1", "true", "yes", "on",
        ):
            return None
        kb_dir = self._env.get("SEMANTIC_KB_DIR", "docs/knowledge")
        try:
            min_score_raw = self._env.get("SEMANTIC_KB_MIN_SCORE", "").strip()
            min_score = float(min_score_raw) if min_score_raw else 0.0
            self._semantic_kb = SemanticKBIndex(
                kb_dir, min_score=min_score,
            )
        except Exception as exc:  # 路径/权限/模型下载异常不能让回复失败
            log_ai.warning("ai semantic_kb init failed dir=%s err=%s", kb_dir, exc)
            self._semantic_kb = None
        return self._semantic_kb

    def _extract_keywords(self, message: str) -> list[str]:
        """用底层 LLM 从自然语言问句里抽 1-3 个最适合 IMA 搜索的关键词。

        返回 ``list[str]``，按相关性从高到低。每个元素是 1 个独立词/字。
        空列表表示抽取失败/无意义。
        异常一律向上抛，由调用方软降级到原始 message 整体搜。
        """
        if not message or not message.strip():
            return []
        # 短消息（≤2 字符）直接当关键词，避免一次 LLM 调用
        if len(message.strip()) <= 2:
            return [message.strip()]
        extract_prompt = _KEYWORD_EXTRACT_PROMPT.format(message=message)
        resp = self._base.chat(message, prompt=extract_prompt)
        terms: list[str] = []
        for line in (resp or "").splitlines():
            line = line.strip()
            if not line:
                continue
            # 去掉可能的前缀："关键词：" / "关键词:" / "答案：" / 序号"1."
            line = re.sub(
                r"^(关键词|keyword|keywords|答案|answer|提取|抽取|问题|query|输出)"
                r"[:：\s]+",
                "", line, flags=re.IGNORECASE,
            )
            line = re.sub(r"^\d+[\.\)、]\s*", "", line)
            line = line.strip().strip("\"'`").strip()
            if not line:
                continue
            # 一行可能有多个空格分隔的词；中文常见的中英标点也按分隔处理
            for term in re.split(r"[\s,，;；、]+", line):
                term = term.strip().strip("\"'`").strip()
                if not term:
                    continue
                if len(term) > 20:  # 太长就截掉（兜底 LLM 啰嗦输出整段解释）
                    term = term[:20]
                if term not in terms:  # 顺序保留、去重
                    terms.append(term)
                if len(terms) >= 3:
                    break
            if len(terms) >= 3:
                break
        return terms[:3]

    def _search_ima_merged(
        self,
        terms: list[str],
        limit: int,
        *,
        knowledge_base_id: Optional[str] = None,
    ) -> list:
        """对一组关键词分别搜 IMA，合并 hits 并按 media_id 去重（保留先出现顺序）。

        第一个 term 通常是 LLM 判定的最强信号，其结果排前面；后续 term 补充召回。
        每个 term 单独 search 时 limit 自动收紧，避免一次 IMA 调用返回太多。
        ``knowledge_base_id`` 透传到 :meth:`ImaClient.search_knowledge`；为 ``None``
        时回落到 ``ImaConfig.default_knowledge_base_id``（ima.py:507-512 已处理）。
        """
        seen: set[str] = set()
        merged: list = []
        for i, term in enumerate(terms):
            if not term:
                continue
            # 第一个 term 用完整 limit；后续 term 用更小的 limit 节省资源
            per_limit = limit if i == 0 else max(2, limit - i)
            try:
                hits = self._ima.search_knowledge(
                    term,
                    limit=per_limit,
                    knowledge_base_id=knowledge_base_id,
                )
            except Exception as exc:  # 单 term 失败不阻塞其它
                log_ima.warning("ai ima search term=%r failed err=%s", term, exc)
                continue
            for h in hits:
                if h.media_id and h.media_id in seen:
                    continue
                if h.media_id:
                    seen.add(h.media_id)
                merged.append(h)
        return merged[:limit]

    def chat(self, message, **kwargs):
        """透明地把 ima 检索注入到 AI 调用的 system prompt，并打印路由决策。

        ``handle_message`` 仍然只调用 ``ai.chat(text)``；检索与提示词拼接
        在这里完成，不污染协议层的代码路径。如果 ``ima_client`` 未配置或检索
        为空，等价于直通到底层 ``_base``。

        调试阶段新增路由日志（INFO 级别，终端 + ``logs/clawbot*.log`` 同时输出），
        四档 mode + reason 精确表达决策路径：
          ``mode=llm-only     reason=ima-not-configured``   IMA 未配置（缺凭据），纯 LLM
          ``mode=llm-only     reason=ima-search-failed``    IMA 检索异常（warn 已记录详情）
          ``mode=llm-only     reason=ima-no-match``         IMA 检索成功但 0 条 / ctx 为空
          ``mode=llm+ima      reason=hits-injected``        IMA 命中并注入 prompt
          ``mode=llm+local    reason=local-fallback``       IMA 0 命中，local KB BM25 兜底
                                                            （CLAWBOT_LOCAL_FALLBACK=1）
          ``mode=llm+semantic reason=semantic-fallback``    IMA 0 + local 0，语义检索兜底
                                                            （SEMANTIC_KB_ENABLED=1）
        一行 ``[AI 路由] mode=...`` 同步打到终端，方便肉眼确认本次回答走了哪条路径。

        新增 ``kb_id`` kwarg（per-user IMA 绑定，``docs/IMA_PER_USER_BINDING.md``）；
        非空时透传给 ``ImaClient.search_knowledge``，为空时继续走
        ``ImaConfig.default_knowledge_base_id`` 的 fallback。
        """
        prompt = kwargs.pop("prompt", None)
        if prompt is None:
            prompt = getattr(self.config, "prompt", "") or ""
        # Per-user KB 绑定键：调用方（handle_message）从 IMABindings 查
        # ilink_user_id 得到。None = 走 IMA_ILINK_DEFAULT_KB。
        kb_id = kwargs.pop("kb_id", None)

        # 默认路由：仅 LLM（IMA 未配置）
        mode = "llm-only"
        reason = "ima-not-configured"
        hits_count = 0
        ctx_chars = 0

        if self._ima.configured():
            reason = "ima-no-match"  # 进入检索分支，覆盖默认值；命中后会再覆盖
            # IMA_ILINK_KEYWORD_EXTRACT=1 时，先用 LLM 抽取 1-3 个关键词再搜。
            # 自然语言问句（"IMA是什么？"/"怎么重连？"）整体搜常 hits=0，拆成
            # 关键词后命中率显著提升。失败时软降级到原 message。
            extract_t0 = time.perf_counter()
            search_terms: list[str] = []  # 实际喂给 search_knowledge 的词列表
            if getattr(self._ima.cfg, "keyword_extract", False):
                try:
                    search_terms = self._extract_keywords(message)
                except Exception as exc:  # 抽取异常不能让回复失败
                    log_ima.warning("ai ima keyword_extract failed err=%s", exc)
                    search_terms = []
                if search_terms:
                    log_ima.info(
                        "ai ima keyword_extract ok q_in=%r terms=%r elapsed_ms=%.0f",
                        message[:60], search_terms,
                        (time.perf_counter() - extract_t0) * 1000,
                    )
                else:
                    log_ima.info(
                        "ai ima keyword_extract empty q_in=%r (fallback to original) elapsed_ms=%.0f",
                        message[:60], (time.perf_counter() - extract_t0) * 1000,
                    )
            if not search_terms:
                # 软降级：抽取失败/未开启 → 拿整句当 1 个搜索词
                search_terms = [message]

            try:
                # 多个 term 逐个搜，合并去重（_search_ima_merged）
                hits = self._search_ima_merged(
                    search_terms,
                    self._ima.cfg.search_limit,
                    knowledge_base_id=kb_id,
                )
                hits_count = len(hits)
                query_log = " | ".join(search_terms)
                print(f"[ima] query='{query_log[:60]}' hits={hits_count} "
                      f"kb_id={kb_id or '-'} "
                      f"titles={[getattr(h, 'title', '')[:30] for h in hits[:3]]}",
                      flush=True)
                log_ima.debug("ai ima inject prompt before=%d", len(prompt or ""))
            except Exception as exc:  # 检索异常绝不能让回复失败
                print(f"[ima] 检索异常: {exc}")
                log_ima.warning("ai ima search failed err=%s", exc)
                hits = []
                reason = "ima-search-failed"
                hits_count = 0

            # IMA 0 命中 → 本地 Markdown 兜底（CLAWBOT_LOCAL_FALLBACK=1）
            # 解决 IMA 关键词匹配对部分词不友好、本地却能 substring 命中的问题。
            if not hits:
                local_kb = self._maybe_local_kb()
                if local_kb and local_kb.exists:
                    local_t0 = time.perf_counter()
                    local_hits = local_kb.search(search_terms, limit=self._ima.cfg.search_limit)
                    if local_hits:
                        hits = local_hits
                        hits_count = len(hits)
                        reason = "local-fallback"
                        print(
                            f"[local] query='{query_log[:60]}' hits={hits_count} "
                            f"titles={[getattr(h, 'title', '')[:30] for h in hits[:3]]}",
                            flush=True,
                        )
                        log_ima.info(
                            "ai local_kb fallback hits=%d elapsed_ms=%.0f (ima was 0)",
                            hits_count, (time.perf_counter() - local_t0) * 1000,
                        )
                    else:
                        log_ima.info(
                            "ai local_kb fallback none (ima=0, local=0) elapsed_ms=%.0f",
                            (time.perf_counter() - local_t0) * 1000,
                        )

            # IMA 0 + local KB 0 → 语义检索兜底（SEMANTIC_KB_ENABLED=1）
            # 接在 local 后面，不抢 BM25 能命中的情形；直接吃整句 ``message``（语义
            # 检索本身能消化自然语言，无需 keyword_extract 后的 terms）。
            # 首次 search 会触发 embedding 重建（~30–60s 阻塞），日志里看得到。
            if not hits:
                semantic_kb = self._maybe_semantic_kb()
                if semantic_kb and semantic_kb.exists:
                    sem_t0 = time.perf_counter()
                    try:
                        sem_hits = semantic_kb.search(
                            message, limit=self._ima.cfg.search_limit
                        )
                    except Exception as exc:
                        log_ai.warning("ai semantic_kb search failed err=%s", exc)
                        sem_hits = []
                    sem_elapsed_ms = (time.perf_counter() - sem_t0) * 1000
                    if sem_hits:
                        hits = sem_hits
                        hits_count = len(hits)
                        reason = "semantic-fallback"
                        print(
                            f"[semantic] query='{(message or '')[:60]}' "
                            f"hits={hits_count} "
                            f"titles={[getattr(h, 'title', '')[:30] for h in hits[:3]]}",
                            flush=True,
                        )
                        log_ai.info(
                            "ai semantic_kb fallback hits=%d elapsed_ms=%.0f "
                            "(ima=0, local=0)",
                            hits_count, sem_elapsed_ms,
                        )
                    else:
                        log_ai.info(
                            "ai semantic_kb fallback none "
                            "(ima=0, local=0, semantic=0) elapsed_ms=%.0f",
                            sem_elapsed_ms,
                        )

            # 命中数 > 0 且 IMA_ILINK_FETCH_BODY=1：逐条 get_doc_content 拿正文。
            # 这是绕过 docs/IMA_KB.md §5 关键坑 2 的关键补丁——search_knowledge
            # 不返回 body，必须再调一次 note 服务端的端点。
            # 实测：只有"作者本人"创建的 note 才能拿正文，第三方 note 会 210005。
            # media_id 格式：``note_<32-hex>_<16-digit-note-id><16-digit-folder-id>``，
            # note_id 是紧跟 32-hex 之后的前 16 位数字（与 folder_id 无分隔符）。
            if (
                hits
                and getattr(self._ima.cfg, "fetch_body", False)
            ):
                fetch_t0 = time.perf_counter()
                enriched = 0
                for h in hits:
                    if h.display_snippet:  # 已有正文就跳过
                        enriched += 1
                        continue
                    m = re.search(r"^note_[0-9a-f]{32}_(\d{16})", h.media_id or "")
                    note_id = m.group(1) if m else ""
                    if not note_id:
                        log_ima.debug("ai ima fetch_body skip media_id=%r (no note_id)", h.media_id)
                        continue
                    try:
                        body = self._ima.get_doc_content(note_id)
                    except Exception as exc:  # 单条失败不阻塞其它
                        log_ima.warning("ai ima get_doc_content[%s] failed err=%s",
                                        note_id, exc)
                        body = ""
                    if body:
                        h.content = body  # SearchHit.content；display_snippet 会优先用
                        enriched += 1
                log_ima.info(
                    "ai ima fetch_body total=%d enriched=%d elapsed_ms=%.0f",
                    len(hits), enriched, (time.perf_counter() - fetch_t0) * 1000,
                )
            # 可选 LLM rerank：默认关闭（IMA_ILINK_RERANK=0），失败回退原序
            if (
                hits
                and getattr(self._ima.cfg, "rerank_enabled", False)
                and len(hits) > 1
            ):
                try:
                    rerank_t0 = time.perf_counter()
                    reranked = _ima_rerank_hits(
                        message,
                        hits,
                        self._base.chat,
                        top_k=getattr(self._ima.cfg, "rerank_top_k", 3),
                    )
                    log_ima.info(
                        "ai ima rerank before=%d after=%d elapsed_ms=%.0f",
                        len(hits), len(reranked),
                        (time.perf_counter() - rerank_t0) * 1000,
                    )
                    hits = reranked
                    hits_count = len(hits)
                except Exception as exc:  # rerank 异常绝不能让回复失败
                    log_ima.warning(
                        "ai ima rerank failed err=%s (continue with original)", exc
                    )
            if hits:
                ctx = _ima_build_context(hits)
                if ctx:
                    prompt = (prompt + "\n\n" + ctx) if prompt else ctx
                    # fallback 来源决定 mode；reason 字段负责更细的语义。
                    if reason == "local-fallback":
                        mode = "llm+local"
                    elif reason == "semantic-fallback":
                        mode = "llm+semantic"
                    else:
                        mode = "llm+ima"
                    if reason not in ("local-fallback", "semantic-fallback"):
                        reason = "hits-injected"
                    ctx_chars = len(ctx)
                    log_ima.debug("ai ima inject prompt after=%d (added %d, mode=%s)",
                                  len(prompt), ctx_chars, mode)
                else:
                    # hits 非空但 ctx 构建为空：理论上是 _ima_build_context 的 bug，
                    # 降级为 no-match 便于排查
                    log_ima.debug("ai ima inject prompt after=%d (ctx empty)", len(prompt))
            else:
                log_ima.debug("ai ima inject prompt after=%d (no hits)", len(prompt))

        # 路由决策统一日志：INFO 级别，文件 + 终端同时出现；便于 grep
        log_ai.info(
            "ai route mode=%s reason=%s msg_chars=%d hits=%d ctx_chars=%d kb_id=%s",
            mode, reason, len(message or ""), hits_count, ctx_chars,
            (kb_id or "-"),
        )
        print(
            f"[AI 路由] mode={mode} reason={reason} "
            f"hits={hits_count} ctx_chars={ctx_chars} kb_id={kb_id or '-'}",
            flush=True,
        )

        # mode=llm-only（无任何 KB 资料）时，给 LLM 拼 caveat 让它自我降自信 + 加标记
        if (
            mode == "llm-only"
            and ctx_chars == 0
            and self._env.get("CLAWBOT_LLM_CAVEAT", "1").strip().lower()
            in ("1", "true", "yes", "on")
        ):
            prompt = (prompt + "\n\n" + _LLM_ONLY_CAVEAT_PROMPT) if prompt else _LLM_ONLY_CAVEAT_PROMPT
            log_ai.info("ai route caveat injected (llm-only mode)")
        kwargs["prompt"] = prompt
        return self._base.chat(message, **kwargs)


def create_ai_client(raw_cfg: dict):
    """根据配置创建 AI 客户端，保持启动入口与协议代码解耦。"""
    if raw_cfg["provider"] == "deepseek":
        return DeepSeekAPI(DeepSeekConfig(
            api_key=raw_cfg["api_key"],
            base_url=raw_cfg["base_url"],
            model=raw_cfg["model"],
            prompt=raw_cfg["prompt"],
        ))
    return DusAPI(DusConfig(
        api_key=raw_cfg["api_key"],
        base_url=raw_cfg["base_url"],
        model1=raw_cfg["model"],
        prompt=raw_cfg["prompt"],
    ))


if __name__ == "__main__":
    # Default entry: dispatch to shared_runtime. The shared process serves
    # only session-token sessions (``secrets.token_urlsafe(32)`` minted in
    # ``shared_web.py``) — no named users, no per-port subprocess, no OAuth.
    import sys
    from shared_runtime import main as shared_main
    shared_main(sys.argv[1:])
