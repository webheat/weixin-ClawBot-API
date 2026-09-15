"""qr_portal.py —— 多用户 ClawBot 单入口 portal。

绑定 :18300，根据 cookie 派发到对应用户的 qr_web.py：

  - 无 cookie              → 单按钮登录页（OAuth 或 ephemeral，绝不列出用户）
  - 有 cookie（bot 在跑）  → 反代到对应 bot 的 qr_web.py
  - 有 cookie（bot 失踪）  → 清 cookie + 跳回登录页

登录方式（双轨，可同时启用）：

  1. **微信开放平台 OAuth2.0**（``WXOAPP_ENABLED=1``）：访客看到"微信扫码登录"
     按钮 → openid 绑定 bot → cookie 持久化。**隐私安全**：无 cookie 的访客
     看不到任何用户列表。

  2. **ephemeral bot**（``WXOAPP_ENABLED=0`` 且 ``WXOAPP_EPHEMERAL_ENABLED=1``）：
     每个访客点击"扫码登录" → portal 自动分配一个临时 bot
     （``eph_<hex>`` 短 ID，新 systemd unit、新端口）→ cookie 8h 有效 →
     自动 GC 回收。比 OAuth 少一步（不用先扫开放平台），但每个访客独占
     一个 bot 进程。无用户列表泄露。

  3. 两者都关 → 503 错误页（"请联系管理员配置登录方式"），同样无列表泄露。

切换账号（右上角"切换账号"按钮）：

  ``GET /switch`` → 触发当前 bot 的 ``/relink`` → bot 进程内 ``relogin_listener``
  清 token + 走 ``do_reconnect`` → **同一 bot 进程、同一端口、新 iLink QR**。
  **保留 cookie**（不重新走登录），浏览器原地看到新 QR。

  - OAuth 模式：visitor 重新微信扫码，bot 用同一 iLink 账号重新拿 token；
    或者扫另一个微信 → bot 账号切换。
  - ephemeral 模式：visitor 重新扫码，本 bot 进程复用，只换 iLink token。

后端 QR UI 由 qr_web.py 提供，每个用户一个进程、独立端口。portal 不持久化
任何状态；cookie 是唯一会话标识。后端鉴权：portal 反代时注入
``Authorization: Bearer <token>``（从用户 env 读），qr_web._check_bearer
校验通过；qr_web 的 ``?token=`` 路径仍然兼容（portal 不剥，传给后端即可）。

nginx 配：
    location ^~ /clawbot/ {
        proxy_pass http://127.0.0.1:18300/;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 65s;
    }

启动：
    python qr_portal.py                                # 直接跑
    systemctl enable --now clawbot-portal.service      # systemd
"""

import asyncio
import glob
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from utils.bot_launcher import BotLauncher, BotLauncherError, short_id_from_openid
from utils.logging_setup import get_logger, setup_logging
from wechat_oauth import WeChatOAuth, WeChatOAuthError, sign_state, verify_state

log = get_logger("portal")

USER_ENV_DIR = "/etc/clawbot"
DEFAULT_PORTAL_HOST = "127.0.0.1"
DEFAULT_PORTAL_PORT = 18300
DEFAULT_WXOAPP_SCOPE = "snsapi_login"
DEFAULT_WXOAPP_STATE_TTL = 600
DEFAULT_WXOAPP_BINDINGS_PATH = "./var/oauth_bindings.json"
DEFAULT_WXOAPP_PENDING_PATH = "./var/oauth_pending.json"
DEFAULT_WXOAPP_REFRESH_CACHE_DIR = "./var/wxoapp_refresh"
DEFAULT_WXOAPP_COOKIE_MAX_AGE = 8 * 3600  # 8 小时（OAuth 后 cookie 有效期）

PORTAL_HOST_ENV = "CLAWBOT_PORTAL_HOST"
PORTAL_PORT_ENV = "CLAWBOT_PORTAL_PORT"
COOKIE_NAME = "clawbot_user"

PROXY_TIMEOUT = aiohttp.ClientTimeout(total=30)
PING_TIMEOUT = aiohttp.ClientTimeout(total=2)


# ---- 微信开放平台 OAuth2.0 配置 ----

def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_wxoapp_config(env_path: str = ".env") -> dict:
    """从 .env 读 WXOAPP_* + WXOAPP_EPHEMERAL_ENABLED 配置。

    用 ``load_dotenv(override=False)`` 加载（不覆盖现有 os.environ），
    风格参照 :class:`ima.ImaConfig.from_env`。任一关键字段为空时
    ``enabled`` 仍按 ``WXOAPP_ENABLED`` 取值，但应用层会再校验并降级
    到 ephemeral / 错误页。
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        load_dotenv = None  # type: ignore

    if load_dotenv is not None and Path(env_path).exists():
        load_dotenv(env_path, override=False)

    enabled = _truthy(os.environ.get("WXOAPP_ENABLED", "0"))
    app_id = os.environ.get("WXOAPP_APP_ID", "").strip()
    app_secret = os.environ.get("WXOAPP_APP_SECRET", "").strip()
    redirect_uri = os.environ.get("WXOAPP_REDIRECT_URI", "").strip()
    state_secret = os.environ.get("WXOAPP_STATE_SECRET", "").strip()

    def _f_int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    return {
        "enabled": enabled,
        "app_id": app_id,
        "app_secret": app_secret,
        "redirect_uri": redirect_uri,
        "scope": os.environ.get("WXOAPP_SCOPE", DEFAULT_WXOAPP_SCOPE).strip() or DEFAULT_WXOAPP_SCOPE,
        "state_secret": state_secret,
        "state_ttl": _f_int("WXOAPP_STATE_TTL", DEFAULT_WXOAPP_STATE_TTL),
        "bindings_path": os.environ.get("WXOAPP_BINDINGS_PATH",
                                        DEFAULT_WXOAPP_BINDINGS_PATH).strip() or DEFAULT_WXOAPP_BINDINGS_PATH,
        "pending_path": os.environ.get("WXOAPP_PENDING_PATH",
                                       DEFAULT_WXOAPP_PENDING_PATH).strip() or DEFAULT_WXOAPP_PENDING_PATH,
        "refresh_cache_dir": os.environ.get("WXOAPP_REFRESH_CACHE_DIR",
                                            DEFAULT_WXOAPP_REFRESH_CACHE_DIR).strip() or DEFAULT_WXOAPP_REFRESH_CACHE_DIR,
        "cookie_max_age": _f_int("WXOAPP_COOKIE_MAX_AGE", DEFAULT_WXOAPP_COOKIE_MAX_AGE),
        "ephemeral_enabled": _truthy(os.environ.get("WXOAPP_EPHEMERAL_ENABLED",
                                                    "1" if not enabled else "0")),
        "first_bind_policy": os.environ.get("WXOAPP_FIRST_BIND_POLICY", "bind_from_cookie").strip()
            or "bind_from_cookie",
        "debug": _truthy(os.environ.get("WXOAPP_DEBUG", "0")),
    }


# 模块级：启动时由 main()/run() 一次性算清；handle_* 读它即可。
wx_cfg: dict = {}
WX_OAUTH: Optional[WeChatOAuth] = None
bot_launcher = BotLauncher()  # OAuth / ephemeral 启动的 bot 实例; var/bot_sessions.json


def _oauth_ready() -> bool:
    """开关 + 关键字段都齐了才算 ready，否则回退到 ephemeral。"""
    return bool(wx_cfg.get("enabled")
                and wx_cfg.get("app_id")
                and wx_cfg.get("app_secret"))


def _ephemeral_ready() -> bool:
    """OAuth 关 + ephemeral 开关 = 1 时启用 ephemeral 登录（无 OAuth 也能用）。

    默认：OAuth 关时 ephemeral 自动开（``WXOAPP_EPHEMERAL_ENABLED`` 隐式 1），
    让 portal 在没配开放平台时仍能工作（只是每个访客独占一个 bot 进程）。
    """
    return bool(wx_cfg.get("ephemeral_enabled"))


def _login_mode() -> str:
    """当前 portal 应使用的登录模式。

    - ``"oauth"``    ：OAuth 可用，访客看开放平台扫码登录按钮。
    - ``"ephemeral"``：OAuth 关 + ephemeral 开，访客点按钮 → 临时 bot。
    - ``"none"``     ：两者都关 → 503 错误页（admin 未配置）。

    三种模式**都不会**渲染任何用户列表。
    """
    if _oauth_ready():
        return "oauth"
    if _ephemeral_ready():
        return "ephemeral"
    return "none"


# ---- 用户枚举 ----

def _parse_env(path: Path) -> dict:
    """极简 KEY=VALUE 解析；够用，不引外部依赖。"""
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def enumerate_users() -> dict:
    """合并两个数据源：
    1. /etc/clawbot/*.env 老 picker 用户（手工配置）
    2. var/bot_sessions.json OAuth 自动启动的 bot 实例

    返回 {name: {port, token, env_path, source}}；proxy_to() 直接用 port + token。
    source 字段只是诊断用，不参与路由。
    """
    users = {}
    env_dir = USER_ENV_DIR
    # 1. 老路径：手工 /etc/clawbot/*.env（alice 等）
    for path_str in sorted(glob.glob(f"{env_dir}/*.env")):
        path = Path(path_str)
        name = path.stem
        if name == "ima" or name.endswith(".example") or name.startswith("."):
            continue
        env = _parse_env(path)
        port_raw = env.get("CLAWBOT_WEB_PORT", "").strip()
        if not port_raw.isdigit():
            continue
        users[name] = {
            "port": int(port_raw),
            "token": env.get("CLAWBOT_WEB_TOKEN", "").strip(),
            "env_path": str(path),
            "source": "env_file",
        }
    # 2. OAuth bot sessions（var/bot_sessions.json）
    try:
        sessions = bot_launcher._load_sessions()
    except Exception as exc:
        log.warning("bot_sessions 加载失败: %s", exc)
        sessions = {}
    for short_id, info in sessions.items():
        existing = users.get(short_id, {})
        # sessions 的 port 是权威(env 文件可能被外部改过端口不一致)
        port = int(info.get("port", existing.get("port", 0)))
        # token 走 env 文件(bot_launcher 创建 <short_id>.env 时写入的)
        token = existing.get("token", "")
        env_path = info.get("env_path") or existing.get("env_path") or f"{env_dir}/{short_id}.env"
        users[short_id] = {
            "port": port,
            "token": token,
            "env_path": env_path,
            "source": "oauth",
        }
    return users


# ---- 状态探测 ----

async def fetch_state(session: aiohttp.ClientSession, port: int) -> Optional[dict]:
    try:
        async with session.get(f"http://127.0.0.1:{port}/state",
                               timeout=PING_TIMEOUT) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


# ---- 登录页 HTML ----
#
# 三种模式对应的页面，**都不展示任何用户列表**（这是隐私约束）：
#   - OAUTH_LOGIN_HTML      ：OAuth 登录按钮 → openid 绑 bot
#   - EPHEMERAL_LOGIN_HTML  ：ephemeral 登录按钮 → portal 自动分配临时 bot
#   - NOT_CONFIGURED_HTML   ：两种登录方式都关 → 503 错误页
#
# 历史：早期版本还有 ``_PICKER_HTML`` / ``_render_picker``，会列出所有
# 用户 + 端口 + 实时状态，被 Agent A 审计为隐私泄露（qr_portal.py 旧 226-282），
# 已删除。

OAUTH_LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<title>ClawBot 登录</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:48px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center}
h1{margin:0 0 8px;font-size:24px}p{color:#666;margin:0 0 32px}
a.btn,button.btn{display:inline-block;padding:14px 32px;background:#07c160;color:#fff;border-radius:8px;
border:0;text-decoration:none;font-size:16px;font-weight:500;cursor:pointer;font-family:inherit}
a.btn:hover,button.btn:hover{background:#06a050}
.note{color:#999;font-size:12px;margin-top:24px;line-height:1.6}</style>
<div class=card><h1>ClawBot 控制台</h1><p>请使用微信扫码登录</p>
<a class=btn href="/oauth/login">微信扫码登录</a>
<p class=note>登录后会启动一个专属机器人进程，期间您的扫码记录<br>仅您本人可见，不会在页面显示其他用户。</p></div>"""

EPHEMERAL_LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<title>ClawBot 登录</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:48px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center;max-width:480px}
h1{margin:0 0 8px;font-size:24px}p{color:#666;margin:0 0 24px}
form{margin:0}
button.btn{display:inline-block;padding:14px 40px;background:#07c160;color:#fff;border-radius:8px;
border:0;font-size:16px;font-weight:500;cursor:pointer;font-family:inherit}
button.btn:hover{background:#06a050}
.note{color:#999;font-size:12px;margin-top:24px;line-height:1.6}</style>
<div class=card><h1>ClawBot 控制台</h1><p>点击下方按钮启动您的专属会话</p>
<form method="POST" action="/ephemeral/start">
<button class=btn type="submit">扫码登录</button>
</form>
<p class=note>每次点击都会启动一个独立的机器人进程（eph_ 前缀），<br>
8 小时内可继续使用，过期自动清理；<br>
会话期间您的扫码记录仅您本人可见。</p></div>"""

NOT_CONFIGURED_HTML = """<!doctype html><meta charset=utf-8>
<title>ClawBot 未配置登录</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:48px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center;max-width:480px}
h1{margin:0 0 12px;font-size:22px;color:#c62828}p{color:#666;margin:0 0 24px;line-height:1.7}
code{background:#eaeef2;padding:2px 8px;border-radius:3px;font-family:ui-monospace,monospace;font-size:13px}</style>
<div class=card>
<h1>ClawBot 暂未配置登录方式</h1>
<p>管理员尚未配置 <code>WXOAPP_ENABLED=1</code>（开放平台）或 <code>WXOAPP_EPHEMERAL_ENABLED=1</code>（临时会话）。<br>
请联系管理员启用其中之一。</p>
<p>当前状态：<code>WXOAPP_ENABLED=__WXOAPP_ENABLED__</code> · <code>WXOAPP_EPHEMERAL_ENABLED=__WXOAPP_EPHEMERAL_ENABLED__</code></p>
</div>"""


# ---- 路由 ----

def _public_prefix(request: web.Request) -> str:
    """从 X-Forwarded-Prefix 取 nginx 前缀（默认空）。用于构造 picker / select /
    switch 链接，使得无论 portal 部署在哪个子路径下，HTML 里的链接都对。

    nginx 配（多入口反代时必加）：
        location ^~ /clawbot/ {
            proxy_set_header X-Forwarded-Prefix /clawbot;
            proxy_pass http://127.0.0.1:18300/;
        }
    直接访问 portal (http://127.0.0.1:18300/) 时没有该头，链接就是根路径。
    """
    pfx = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    return pfx


def _render_login_page(request: web.Request) -> web.Response:
    """根据 ``_login_mode()`` 渲染对应登录页（OAuth / ephemeral / 错误页）。

    **绝不**渲染任何用户列表——这是隐私约束。
    """
    mode = _login_mode()
    if mode == "oauth":
        log.info("login page served mode=oauth")
        return web.Response(text=OAUTH_LOGIN_HTML, content_type="text/html")
    if mode == "ephemeral":
        log.info("login page served mode=ephemeral")
        return web.Response(text=EPHEMERAL_LOGIN_HTML, content_type="text/html")
    # 两边都没配 → 503，避免泄露任何用户/端口信息
    html = (NOT_CONFIGURED_HTML
            .replace("__WXOAPP_ENABLED__", str(int(bool(wx_cfg.get("enabled")))))
            .replace("__WXOAPP_EPHEMERAL_ENABLED__", str(int(bool(wx_cfg.get("ephemeral_enabled"))))))
    log.warning("login page served mode=none (WXOAPP_ENABLED + WXOAPP_EPHEMERAL_ENABLED both off)")
    return web.Response(text=html, content_type="text/html", status=503)


async def handle_index(request: web.Request) -> web.StreamResponse:
    """入口：cookie 路由 → 反代 bot；无 cookie → 登录页（无用户列表）。

    历史：早期版本当 cookie 缺失或对应 bot 已停时 fallback 到 ``_render_picker``
    （qr_portal.py 旧 343-353），会被 Agent A 审计为泄露用户列表，已删除。
    现在唯一 fallback 是登录页。
    """
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        users = enumerate_users()
        if cookie in users:
            return await proxy_to(request, users[cookie], cookie)
        # cookie 指向的 bot 已被回收 / systemd 停了：清 cookie 再渲染登录页
        log.info("stale cookie cleared short_id=%s", cookie[:8] + "…" if len(cookie) > 8 else cookie)
        resp = _render_login_page(request)
        resp.del_cookie(COOKIE_NAME)
        return resp
    return _render_login_page(request)


async def handle_ephemeral_start(request: web.Request) -> web.StreamResponse:
    """ephemeral 登录入口（POST）：分配临时 bot、set cookie、302 回 /。

    short_id 形如 ``eph_<16-hex>``，portal sweeper 据此按 TTL 回收。
    ``openid=""`` 表示未绑定微信开放平台身份，纯临时会话。
    """
    if _login_mode() != "ephemeral":
        log.warning("ephemeral start rejected: mode != ephemeral")
        raise web.HTTPNotFound()
    short_id = f"eph_{secrets.token_hex(8)}"
    try:
        info = bot_launcher.start_or_get(short_id, openid="")
    except BotLauncherError as exc:
        log.error("ephemeral start failed short_id=%s err=%s", short_id, exc)
        return web.Response(
            text=f"<!doctype html><meta charset=utf-8><h2>启动失败</h2>"
                 f"<p>无法分配临时机器人进程：<code>{exc}</code></p>"
                 f"<p>请稍后重试，或联系管理员。</p>",
            content_type="text/html",
            status=503,
        )
    pfx = _public_prefix(request)
    resp = web.HTTPFound(f"{pfx}/")
    resp.set_cookie(COOKIE_NAME, short_id,
                    max_age=wx_cfg.get("cookie_max_age", 8 * 3600),
                    httponly=True,
                    samesite="Lax")
    log.info("ephemeral start ok short_id=%s port=%d", short_id, info["port"])
    return resp


async def _trigger_bot_relink(user: str) -> bool:
    """对指定 bot 进程的 /relink 端点 POST，触发清 token + 重新扫码。

    失败时返回 False 但不抛异常——切换流程不应被 IPC 失败阻塞
    （即使触发失败，cookie 也还在，用户至少还能继续看当前 bot 页面）。
    """
    users = enumerate_users()
    info = users.get(user)
    if not info:
        log.warning("relink trigger skipped: user not found user=%s", user)
        return False
    port = info.get("port")
    token = info.get("token")
    if not port:
        log.warning("relink trigger skipped: missing port user=%s", user)
        return False
    url = f"http://127.0.0.1:{port}/relink"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers) as r:
                if r.status == 200:
                    log.info("relink trigger ok user=%s port=%d", user, port)
                    return True
                log.warning("relink trigger failed user=%s port=%d status=%d",
                            user, port, r.status)
                return False
    except Exception as exc:
        log.warning("relink trigger crashed user=%s port=%d err=%s",
                    user, port, exc)
        return False


async def handle_switch(request: web.Request) -> web.Response:
    """"切换账号"按钮：触发当前 bot 重置登录态（清 token + 重新出 QR）。

    **保留 cookie**：用户原地看到新 QR，无需重新走 OAuth / ephemeral 登录。
    如果 bot 不可达（systemd stopped / 端口 down），IPC 失败不阻塞 → 302 回 /
    让浏览器重新 proxy 到 bot（bot 可能已恢复 / 重连成功）。
    """
    selected = request.cookies.get(COOKIE_NAME)
    relink_ok = False
    if selected:
        relink_ok = await _trigger_bot_relink(selected)
        if relink_ok:
            # 刷新 last_used_at，sweeper 视角这是"最近活跃"
            bot_launcher.touch(selected)
    pfx = _public_prefix(request)
    resp = web.HTTPFound(f"{pfx}/")
    # 注意：故意不 del_cookie。切换语义是"换一个新的 iLink QR 给我这个 bot"，
    # 而不是"退出登录"。重新走 OAuth / ephemeral 才能拿到 cookie 反而打断流程。
    log.info("switch user (relink %s, cookie kept) from=%s",
             "ok" if relink_ok else "skipped/failed", selected or "-")
    return resp


async def handle_healthz(request: web.Request) -> web.Response:
    """存活探针。**不返回用户列表**（早期版本会 enumerate + 列出 short_id/port，
    被 Agent A 审计为隐私泄露，已删）。"""
    return web.json_response({
        "ok": True,
        "login_mode": _login_mode(),
    })


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """catch-all: 根据 cookie 找到用户并反代；找不到回 picker。"""
    users = enumerate_users()
    selected = request.cookies.get(COOKIE_NAME)
    if not selected or selected not in users:
        return web.HTTPFound(_absolute_portal_url(request))
    return await proxy_to(request, users[selected], selected)


# ---- 反代实现 ----

# Hop-by-hop 头：不能跨代理转发
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # 下面两个 aiohttp 会自动按 body 重算，必须剥掉，否则注入后长度不一致会被截断
    "content-length", "content-encoding",
})

# 请求头里 portal 要去掉、避免被后端误读为外层 nginx 的头
SKIP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host", "x-forwarded-for", "x-real-ip", "x-forwarded-proto",
    "x-forwarded-port", "x-forwarded-host",
}

# 注入"切换账号"按钮：只在反代 HTML 时使用
# 点击 → /switch → 触发当前 bot 的 /relink → 新 iLink QR → 保留 cookie 原地刷新
SWITCH_BAR_HTML_TEMPLATE = (
    '<div id="clawbot-switch-bar" style="position:fixed;top:0;right:0;'
    'padding:6px 14px;background:#07c160;color:#fff;font-size:12px;'
    'border-radius:0 0 0 6px;z-index:99999;'
    'box-shadow:0 2px 6px rgba(0,0,0,0.15);font-family:system-ui,sans-serif;">'
    '<a href="__SWITCH_URL__" style="color:#fff;text-decoration:none;">切换账号</a>'
    '</div>'
)


async def proxy_to(request: web.Request, info: dict, name: str) -> web.StreamResponse:
    port = info["port"]
    target = f"http://127.0.0.1:{port}{request.rel_url}"

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in SKIP_REQUEST_HEADERS}
    # 让后端知道外层客户端（沿用 nginx 已经设过的，没有就用 socket 远端）
    headers["X-Forwarded-For"] = (
        request.headers.get("X-Forwarded-For", request.remote or "")
    )
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", "https")
    headers["X-Forwarded-Host"] = request.headers.get("Host", "")

    # 注入 WEB_TOKEN 让 qr_web 的 verify_code 鉴权通过
    token = info.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = await request.read() if request.can_read_body else None

    session: aiohttp.ClientSession = request.app["client_session"]
    try:
        upstream = await session.request(
            request.method, target,
            headers=headers, data=body,
            allow_redirects=False,
            timeout=PROXY_TIMEOUT,
        )
    except aiohttp.ClientError as exc:
        log.warning("proxy connect failed user=%s port=%d err=%s", name, port, exc)
        # 注意：响应体**不包含** name / port / 异常详情，避免给前端泄露内部信息
        # （Agent A 审计 A4）。name+port 留 log line 即可，exc 单独记到 log.warning。
        return web.Response(
            status=502,
            text="<!doctype html><meta charset=utf-8><h2>502 上游无响应</h2>"
                 "<p>该用户的服务当前不可用，请稍后重试或"
                 "<a href='/switch'>&larr; 切换账号</a>。</p>",
            content_type="text/html",
        )

    # 构造响应头（剥 hop-by-hop）
    response_headers = [(k, v) for k, v in upstream.headers.items()
                        if k.lower() not in HOP_BY_HOP]

    # HTML 响应：缓冲 + 注入切换按钮
    is_html = upstream.headers.get("Content-Type", "").startswith("text/html")
    if is_html:
        body_bytes = await upstream.read()
        pfx = _public_prefix(request)
        # 切换按钮指向 /switch（触发同 bot 重新出 QR，保留 cookie）。
        # 早期版本指向 /oauth/logout，会清 cookie 把用户踢回登录页——切换 UX 错。
        switch_bar = SWITCH_BAR_HTML_TEMPLATE.replace("__SWITCH_URL__", f"{pfx}/switch")
        marker = b"</body>"
        lower = body_bytes.lower()
        if marker in lower:
            idx = lower.index(marker)
            body_bytes = body_bytes[:idx] + switch_bar.encode("utf-8") + body_bytes[idx:]
        else:
            body_bytes = body_bytes + switch_bar.encode("utf-8")
        # 命中成功反代 → 刷新 last_used_at，让 sweeper 别误杀活跃 bot
        bot_launcher.touch(name)
        return web.Response(
            body=body_bytes,
            status=upstream.status,
            headers=response_headers,
        )

    # 非 HTML：流式透传
    response = web.StreamResponse(status=upstream.status, headers=response_headers)
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
    finally:
        upstream.release()
        await response.write_eof()
    return response


# ---- 微信开放平台 OAuth2.0 路由 ----

def _absolute_portal_url(request: web.Request, path: str = "/clawbot/") -> str:
    """拼 portal 在 nginx 下的绝对 URL。

    nginx 已经设了 ``X-Forwarded-Proto`` / ``Host`` header。
    portal 实际挂在 ``/clawbot/`` 反代下，**跳转目标必须带 /clawbot/ 前缀**，
    否则浏览器会把 ``/`` 解析为 origin 根（``ocbot.aixifs.com/``），
    落到 nginx catch-all → :8081 bag-video 兜底 → 502。
    """
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    host = request.headers.get("Host", "ocbot.aixifs.com")
    return f"{scheme}://{host}{path}"


def _set_cookie_redirect(request: web.Request, bot_user: str) -> web.HTTPFound:
    """写 cookie + 302 到 /clawbot/（绝对 URL，避免裸域 502）。

    cookie 有效期从 .env 的 ``WXOAPP_COOKIE_MAX_AGE`` 读取（默认 8 小时）。
    到期后用户需重新走 OAuth 扫码；也可以点页面上"退出登录"主动清。
    """
    resp = web.HTTPFound(_absolute_portal_url(request))
    resp.set_cookie(COOKIE_NAME, bot_user,
                    max_age=wx_cfg.get("cookie_max_age", 8 * 3600),
                    httponly=True,
                    samesite="Lax")
    return resp


# ---- State TTL 校验 ----
#
# 设计：state raw 直接编码 timestamp（格式 "<nonce>|<monotonic_ts>"），
# verify_state 解签后从 raw 里拿 ts 跟当前时间比，不再依赖 _pending_states 内存字典。
# 好处：portal 重启不丢 state；用户 back/刷新只要签名合法 + 在 TTL 内就接受。

_STATE_EXPIRED_HTML = """<!doctype html><meta charset=utf-8>
<title>登录会话已过期</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:40px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.08);text-align:center;max-width:420px}
h2{margin:0 0 12px;font-size:20px}p{color:#666;margin:0 0 20px;font-size:14px}
a.btn{display:inline-block;padding:12px 24px;background:#07c160;color:#fff;border-radius:8px;
text-decoration:none;font-size:14px;font-weight:500}</style>
<div class=card>
<h2>登录会话已过期</h2>
<p>OAuth state 超过 {ttl} 秒有效期。<br>请重新扫码登录。</p>
<a class=btn href="/oauth/login">→ 重新发起扫码登录</a>
</div>"""


def _check_state_age(raw: str) -> Optional[web.StreamResponse]:
    """从 raw 中解析 timestamp，超 TTL 返友好提示；OK 返 None。

    raw 格式：``<nonce>|<monotonic_ts>``
    """
    try:
        _, ts_str = raw.rsplit("|", 1)
        ts = float(ts_str)
    except (ValueError, AttributeError):
        log.warning("oauth cb: state has no timestamp (raw=%s)", raw[:16])
        return web.Response(text="invalid state format", status=400)
    age = time.monotonic() - ts
    if age > wx_cfg["state_ttl"]:
        log.warning("oauth cb: state expired age=%.1fs ttl=%ds", age, wx_cfg["state_ttl"])
        return web.Response(
            text=_STATE_EXPIRED_HTML.format(ttl=wx_cfg["state_ttl"]),
            status=400,
            content_type="text/html",
        )
    if wx_cfg.get("debug"):
        log.info("oauth cb: state ok age=%.1fs", age)
    return None


async def handle_oauth_login(request: web.Request) -> web.HTTPFound:
    """生成 state（自带 timestamp），签名后 302 到开放平台 authorize。"""
    if not _oauth_ready():
        raise web.HTTPNotFound()
    # state raw 编码 timestamp（"<nonce>|<monotonic_ts>"），
    # 这样 TTL 校验不依赖内存字典，portal 重启/用户 back 都不会误报 expired
    nonce = secrets.token_urlsafe(16)
    raw = f"{nonce}|{time.monotonic()}"
    signed = sign_state(raw, wx_cfg["state_secret"])
    if wx_cfg.get("debug"):
        log.info("oauth login: raw=%s signed=%s…", nonce[:8], signed[:16])
    raise web.HTTPFound(WX_OAUTH.build_authorize_url(signed))


async def handle_oauth_cb(request: web.StreamResponse) -> web.StreamResponse:
    """OAuth 微信回调 → 拿到 openid → 启对应 bot 进程 → 反代到 iLink 二维码。

    简化流程（无 BindingsStore / 无 for_user / 无 first_bind_policy）：
    1. 验 state 签名 + TTL
    2. exchange_code 拿 openid
    3. short_id = openid[:12]
    4. bot_launcher.start_or_get(short_id, openid)  ← 启或复用 bot 进程
    5. 设 cookie clawbot_user=<short_id> + 302 回 picker
       （proxy_handler 反代到 :port，由 bot.py 自己 fetch_login_qrcode 渲染 iLink 二维码）
    """
    if not _oauth_ready():
        raise web.HTTPNotFound()
    code = request.query.get("code", "").strip()
    state_signed = request.query.get("state", "").strip()
    if not code or not state_signed:
        raise web.HTTPBadRequest(text="missing code or state")

    raw = verify_state(state_signed, wx_cfg["state_secret"])
    if not raw:
        log.warning("oauth cb: state signature invalid (signed=%s…)", state_signed[:16])
        raise web.HTTPBadRequest(text="invalid state signature")

    # TTL 校验从 raw 自带 timestamp，不再依赖 _pending_states
    expired_resp = _check_state_age(raw)
    if expired_resp is not None:
        return expired_resp

    try:
        tok = WX_OAUTH.exchange_code(code)
    except WeChatOAuthError as exc:
        log.warning("oauth cb: exchange_code failed: %s", exc)
        return web.Response(text=f"授权失败: {exc}", status=400)

    openid = tok.get("openid", "")
    if not openid:
        log.error("oauth cb: token has no openid: %s", {k: "…" for k in tok.keys()})
        return web.Response(text="授权响应缺少 openid", status=400)

    short_id = short_id_from_openid(openid)
    try:
        info = bot_launcher.start_or_get(short_id, openid)
    except BotLauncherError as exc:
        log.error("oauth cb: bot launcher failed short_id=%s err=%s", short_id, exc)
        return web.Response(text=f"启动 bot 失败: {exc}", status=503)

    log.info("oauth cb: openid=%s… → bot short_id=%s port=%d",
             openid[:12], short_id, info["port"])

    # 设 cookie (short_id 作为 user 名) + 302 回 picker 由 proxy_handler 反代
    return _set_cookie_redirect(request, short_id)


async def handle_oauth_poll(request: web.Request) -> web.Response:
    """前端长轮询占位：手机端确认后浏览器 reload 即可。
    这里简单返 200，避免前端 setInterval 一直报错。
    """
    return web.Response(text="ok")


async def handle_oauth_logout(request: web.Request) -> web.HTTPFound:
    resp = web.HTTPFound(_absolute_portal_url(request))
    resp.del_cookie(COOKIE_NAME)
    log.info("oauth logout (cookie cleared)")
    return resp


# ---- App 工厂 + main ----

async def make_app() -> web.Application:
    app = web.Application()
    app["client_session"] = aiohttp.ClientSession()
    # 显式路由优先于 catch-all
    app.router.add_get("/", handle_index)
    # /select 已删除（Agent A 审计 B1）：OAuth 模式下任何人可 GET /select?name=
    # 任意已知用户即拿到 cookie，等价于账号劫持。现在 cookie 只能由
    # handle_oauth_cb 或 handle_ephemeral_start 设置。
    app.router.add_post("/ephemeral/start", handle_ephemeral_start)
    app.router.add_get("/switch", handle_switch)
    app.router.add_get("/healthz", handle_healthz)
    # OAuth 路由（开关关闭时内部 _oauth_ready() 直接 404，行为不变）
    app.router.add_get("/oauth/login", handle_oauth_login)
    app.router.add_get("/oauth/cb", handle_oauth_cb)
    app.router.add_get("/oauth/poll", handle_oauth_poll)
    app.router.add_get("/oauth/logout", handle_oauth_logout)
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    return app


async def run() -> None:
    global wx_cfg, WX_OAUTH

    # 模块级 OAuth 初始化（按 ima.from_env 风格：一次算清）
    wx_cfg = load_wxoapp_config()
    if _oauth_ready():
        WX_OAUTH = WeChatOAuth(
            wx_cfg["app_id"],
            wx_cfg["app_secret"],
            wx_cfg["redirect_uri"],
            scope=wx_cfg["scope"],
        )
        log.info("oauth enabled (app_id=%s redirect=%s policy=%s ttl=%ds)",
                 wx_cfg["app_id"][:8] + "…",
                 wx_cfg["redirect_uri"], wx_cfg["first_bind_policy"], wx_cfg["state_ttl"])
    else:
        WX_OAUTH = None
        if wx_cfg.get("enabled"):
            log.warning("oauth enabled but app_id/app_secret missing — falling back to ephemeral")
        else:
            log.info("oauth disabled (WXOAPP_ENABLED=0) — using ephemeral=%s",
                     "on" if _ephemeral_ready() else "off")

    host = os.getenv(PORTAL_HOST_ENV, DEFAULT_PORTAL_HOST)
    port = int(os.getenv(PORTAL_PORT_ENV, str(DEFAULT_PORTAL_PORT)))
    app = await make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
    except (OSError, RuntimeError) as exc:
        log.error("portal bind failed host=%s port=%d err=%s", host, port, exc)
        print(f"[portal] 无法绑定 {host}:{port}: {exc}", file=sys.stderr)
        await runner.cleanup()
        await app["client_session"].close()
        return

    log.info("portal bound http://%s:%d/ login_mode=%s users=%d ephemeral_sweeper=%s",
             host, port, _login_mode(), len(enumerate_users()),
             "on" if _ephemeral_ready() else "off")
    print(f"[portal] listening http://{host}:{port}/  (nginx /clawbot/ 反代)  "
          f"login_mode={_login_mode()}")

    # ephemeral sweeper：仅当 ephemeral 模式下启动，每 5 分钟扫描 sessions，
    # 回收超过 ttl + grace (10 min) 未活动的 eph_* bot（释放端口 + 删 env 文件）。
    sweeper_task: Optional[asyncio.Task] = None
    if _ephemeral_ready():
        sweeper_task = asyncio.create_task(_ephemeral_sweeper_task())

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        if sweeper_task is not None:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
        await app["client_session"].close()
        await runner.cleanup()
        log.info("portal stopped")


async def _ephemeral_sweeper_task() -> None:
    """每 5 分钟扫描 bot_sessions，回收过期的 eph_* 临时 bot。

    永不回收 OAuth 绑定的 bot（short_id 不以 ``eph_`` 开头）。回收动作：
    ``systemctl stop`` → 删 env 文件 → 从 ``var/bot_sessions.json`` 移除。

    异常隔离：本 task 抛异常会被外层 cancel 吞掉，但内层 try 已逐项隔离，
    单个 bot 失败不影响其他 bot。
    """
    while True:
        await asyncio.sleep(300)  # 5 min
        try:
            ttl = float(wx_cfg.get("cookie_max_age", 8 * 3600))
            reaped = bot_launcher.reap_ephemeral(ttl_seconds=ttl, grace_seconds=600)
            if reaped:
                log.info("ephemeral sweeper finished n=%d", len(reaped))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ephemeral sweeper crashed err=%s", exc, exc_info=True)


def main() -> None:
    log_file = Path(os.getenv("CLAWBOT_LOG_DIR", "logs")) / "clawbot_portal.log"
    setup_logging(level=os.getenv("CLAWBOT_LOG_LEVEL", "INFO"), log_file=log_file)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[portal] stopped")


if __name__ == "__main__":
    main()
