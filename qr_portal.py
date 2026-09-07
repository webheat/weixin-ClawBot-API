"""qr_portal.py —— 多用户 ClawBot 单入口 portal。

绑定 :18300，读取 /etc/clawbot/*.env 枚举用户：
  - 无 cookie              → 渲染 picker（用户列表 + 实时状态）
  - 有 cookie（user 存在）→ 反代到对应用户的 qr_web.py
  - 有 cookie（user 失踪）→ 清 cookie + 重定向 picker

HTML 响应里 portal 会自动注入一个右上角"切换用户"按钮（不会出现在
picker 自身、只会出现在反代的后端页面里），用户随时可切回 picker。

与 qr_web.py 的关系：
  - 后端 QR UI 由 qr_web.py 提供，每个用户一个进程、独立端口
  - portal 不持久化任何状态；cookie 是唯一会话标识（30 天）
  - 后端鉴权：portal 在反代时主动注入 `Authorization: Bearer <token>`
    （从用户 env 读），qr_web._check_bearer 校验通过；HTML 页面的
    `?token=...` 参数仍然兼容（portal 不剥，传给后端即可）

nginx 配（恢复最初形态）：
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
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from utils.logging_setup import get_logger, setup_logging

log = get_logger("portal")

USER_ENV_DIR = "/etc/clawbot"
DEFAULT_PORTAL_HOST = "127.0.0.1"
DEFAULT_PORTAL_PORT = 18300

PORTAL_HOST_ENV = "CLAWBOT_PORTAL_HOST"
PORTAL_PORT_ENV = "CLAWBOT_PORTAL_PORT"
COOKIE_NAME = "clawbot_user"
COOKIE_MAX_AGE = 30 * 86400  # 30 天

PROXY_TIMEOUT = aiohttp.ClientTimeout(total=30)
PING_TIMEOUT = aiohttp.ClientTimeout(total=2)

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
    """返回 {name: {port, token, env_path}}；按用户名排序。

    跳过 ima.env、*.example、点文件、CLAWBOT_WEB_PORT 不是数字的。
    """
    users = {}
    for path_str in sorted(glob.glob(f"{USER_ENV_DIR}/*.env")):
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


# ---- Picker HTML ----

_PICKER_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawBot 多用户登录</title>
<style>
  body { font-family: -apple-system, system-ui, "PingFang SC", sans-serif;
         max-width: 720px; margin: 32px auto; padding: 0 16px; color: #222; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.sub { color: #666; margin: 0 0 24px; font-size: 13px; }
  .user { display: flex; align-items: center; gap: 12px; padding: 14px 16px;
          margin-bottom: 8px; border: 1px solid #e0e0e0; border-radius: 8px;
          background: #fff; cursor: pointer; text-decoration: none; color: inherit;
          transition: background .15s, border-color .15s; }
  .user:hover { background: #f6f8fa; border-color: #b0b0b0; }
  .name { font-weight: 600; font-size: 16px; min-width: 80px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 12px; font-weight: 500; white-space: nowrap; }
  .b-ok { background: #e8f5e9; color: #2e7d32; }
  .b-qr { background: #fff3e0; color: #e65100; }
  .b-scan { background: #e3f2fd; color: #1565c0; }
  .b-idle { background: #f5f5f5; color: #616161; }
  .b-err { background: #ffebee; color: #c62828; }
  .b-down { background: #fafafa; color: #999; }
  .port { margin-left: auto; color: #999; font-size: 12px; font-family: ui-monospace, monospace; }
  .help { margin-top: 32px; padding: 16px; background: #f6f8fa; border-radius: 8px;
          font-size: 13px; color: #555; line-height: 1.7; }
  .help code { background: #eaeef2; padding: 1px 6px; border-radius: 3px;
               font-size: 12px; font-family: ui-monospace, monospace; }
  .footer { text-align: center; color: #999; font-size: 12px; margin-top: 24px; }
  .empty { color: #999; padding: 16px 0; text-align: center; }
</style></head>
<body>
<h1>ClawBot 多用户登录</h1>
<p class="sub">点击下方用户卡片进入该用户的扫码登录界面。可随时<a href="__SWITCH_URL__">切换用户</a>。</p>
<div id="list">__USER_ROWS__</div>
<div class="help">
  <strong>添加新用户：</strong><br>
  1. <code>sudo cp /etc/clawbot/user.env.example /etc/clawbot/&lt;name&gt;.env</code><br>
  2. 编辑该文件，设置 <code>CLAWBOT_WEB_PORT</code> 和 <code>CLAWBOT_WEB_TOKEN</code><br>
  3. <code>sudo systemctl enable --now clawbot@&lt;name&gt;</code><br>
  4. 刷新本页
</div>
<div class="footer">__FOOTER__</div>
<script>setTimeout(() => location.reload(), 5000);</script>
</body></html>
"""

_STATUS_LABEL = {
    "logged_in": ("登录成功", "b-ok"),
    "qr_pending": ("等待扫码", "b-qr"),
    "scanned": ("已扫码，待确认", "b-scan"),
    "idle": ("空闲", "b-idle"),
    "error": ("错误", "b-err"),
}


def _render_picker(users: dict, statuses: dict, pfx: str = "") -> web.Response:
    rows = []
    for name, info in users.items():
        st_label = statuses.get(name)
        if st_label is None:
            label, cls = "服务未启动", "b-down"
        else:
            label, cls = _STATUS_LABEL.get(st_label, (st_label, "b-idle"))
        rows.append(
            f'<a class="user" href="{pfx}/select?name={name}">'
            f'<span class="name">{name}</span>'
            f'<span class="badge {cls}">{label}</span>'
            f'<span class="port">:{info["port"]}</span>'
            f'</a>'
        )
    html = (_PICKER_HTML
            .replace("__USER_ROWS__", "\n".join(rows) if rows else
                     '<p class="empty">未发现任何用户。请参考下方"添加新用户"步骤。</p>')
            .replace("__SWITCH_URL__", f"{pfx}/switch")
            .replace("__FOOTER__",
                     f'portal 监听 127.0.0.1:18300 · 当前 {len(users)} 个用户 · '
                     f'{time.strftime("%Y-%m-%d %H:%M:%S")}'))
    return web.Response(text=html, content_type="text/html")


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


async def handle_index(request: web.Request) -> web.StreamResponse:
    pfx = _public_prefix(request)
    users = enumerate_users()
    selected = request.cookies.get(COOKIE_NAME)
    if selected and selected in users:
        return await proxy_to(request, users[selected], selected)

    statuses: dict[str, Optional[str]] = {}
    async with aiohttp.ClientSession() as session:
        for name, info in users.items():
            st = await fetch_state(session, info["port"])
            statuses[name] = st.get("status") if st else None
    return _render_picker(users, statuses, pfx)


async def handle_select(request: web.Request) -> web.Response:
    pfx = _public_prefix(request)
    name = request.query.get("name", "").strip()
    users = enumerate_users()
    if not name or name not in users:
        return web.HTTPBadRequest(text=f"未知用户: {name!r}")
    resp = web.HTTPFound(f"{pfx}/")
    resp.set_cookie(COOKIE_NAME, name,
                    max_age=COOKIE_MAX_AGE,
                    httponly=True,
                    samesite="Lax")
    log.info("select user=%s port=%d", name, users[name]["port"])
    return resp


async def handle_switch(request: web.Request) -> web.Response:
    pfx = _public_prefix(request)
    resp = web.HTTPFound(f"{pfx}/")
    resp.del_cookie(COOKIE_NAME)
    log.info("switch user (cookie cleared) from=%s",
             request.cookies.get(COOKIE_NAME, "-"))
    return resp


async def handle_healthz(request: web.Request) -> web.Response:
    users = enumerate_users()
    return web.json_response({
        "ok": True,
        "users": [{"name": n, "port": u["port"]} for n, u in users.items()],
    })


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """catch-all: 根据 cookie 找到用户并反代；找不到回 picker。"""
    users = enumerate_users()
    selected = request.cookies.get(COOKIE_NAME)
    if not selected or selected not in users:
        return web.HTTPFound("/")
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

# 注入"切换用户"按钮：只在反代 HTML 时使用
# href 用 placeholder，proxy_to 里按 X-Forwarded-Prefix 替换
SWITCH_BAR_HTML_TEMPLATE = (
    '<div id="clawbot-switch-bar" style="position:fixed;top:0;right:0;'
    'padding:6px 14px;background:#07c160;color:#fff;font-size:12px;'
    'border-radius:0 0 0 6px;z-index:99999;'
    'box-shadow:0 2px 6px rgba(0,0,0,0.15);font-family:system-ui,sans-serif;">'
    '<a href="__SWITCH_URL__" style="color:#fff;text-decoration:none;">切换用户</a>'
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
        return web.Response(
            status=502,
            text=f"<!doctype html><meta charset=utf-8><h2>502 上游无响应</h2>"
                 f"<p>用户 <code>{name}</code> 的服务 "
                 f"(127.0.0.1:{port}) 当前不可用。</p>"
                 f"<p>错误：<code>{exc}</code></p>"
                 f"<p><a href='/switch'>&larr; 切换用户</a></p>",
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
        switch_bar = SWITCH_BAR_HTML_TEMPLATE.replace("__SWITCH_URL__", f"{pfx}/switch")
        marker = b"</body>"
        lower = body_bytes.lower()
        if marker in lower:
            idx = lower.index(marker)
            body_bytes = body_bytes[:idx] + switch_bar.encode("utf-8") + body_bytes[idx:]
        else:
            body_bytes = body_bytes + switch_bar.encode("utf-8")
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


# ---- App 工厂 + main ----

async def make_app() -> web.Application:
    app = web.Application()
    app["client_session"] = aiohttp.ClientSession()
    # 显式路由优先于 catch-all
    app.router.add_get("/", handle_index)
    app.router.add_get("/select", handle_select)
    app.router.add_get("/switch", handle_switch)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    return app


async def run() -> None:
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

    log.info("portal bound http://%s:%d/ users=%d",
             host, port, len(enumerate_users()))
    print(f"[portal] listening http://{host}:{port}/  (nginx /clawbot/ 反代)")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app["client_session"].close()
        await runner.cleanup()
        log.info("portal stopped")


def main() -> None:
    log_file = Path(os.getenv("CLAWBOT_LOG_DIR", "logs")) / "clawbot_portal.log"
    setup_logging(level=os.getenv("CLAWBOT_LOG_LEVEL", "INFO"), log_file=log_file)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[portal] stopped")


if __name__ == "__main__":
    main()
