"""Shared-process web routes for the multi-user ClawBot runtime.

This module deliberately has no dependency on :mod:`utils.bot_launcher` and
never starts a process.  ``manager`` is a small duck-typed boundary supplied by
the shared bot process::

    await manager.get_or_create(user_id, config=None)
    manager.get(user_id) -> BotSession | None
    await manager.stop(user_id)
    await manager.touch(user_id)       # a sync implementation is also OK

Each ``BotSession`` must expose ``qr_state`` and either ``request_relogin`` or
``relogin_event``.  The browser only receives an opaque, random cookie; the
cookie is mapped to a user id on the server and is never treated as a user id.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from html import escape
from typing import Any, Optional

from aiohttp import web

log = logging.getLogger("clawbot.shared_web")

DEFAULT_PREFIX = "/clawbot"
DEFAULT_COOKIE = "clawbot_session"
DEFAULT_SESSION_TTL = 8 * 3600
DEFAULT_EPHEMERAL_LIMIT = 100
DEFAULT_RATE_LIMIT = 5
DEFAULT_RATE_WINDOW = 60.0


@dataclass
class BrowserBinding:
    user_id: str
    created_at: float
    last_used_at: float
    ephemeral: bool = True
    csrf_token: str = ""
    last_switch_at: float = 0.0


class BrowserSessions:
    """In-memory opaque-cookie store.

    A process-local store is intentional: a browser session must not be
    portable between independent bot processes.  Deployments with multiple
    web workers should route all requests to the shared process or provide a
    store with the same interface backed by a protected server-side database.
    """

    def __init__(self, ttl: float = DEFAULT_SESSION_TTL):
        self.ttl = max(0.01, float(ttl))
        self._bindings: dict[str, BrowserBinding] = {}
        self._lock = asyncio.Lock()

    async def bind(self, user_id: str, *, ephemeral: bool = True) -> str:
        now = time.time()
        sid = secrets.token_urlsafe(32)
        async with self._lock:
            self._bindings[sid] = BrowserBinding(
                user_id, now, now, ephemeral, csrf_token=secrets.token_urlsafe(32)
            )
        return sid

    async def resolve(self, sid: Optional[str]) -> Optional[BrowserBinding]:
        if not sid:
            return None
        now = time.time()
        async with self._lock:
            binding = self._bindings.get(sid)
            if binding is None:
                return None
            if now - binding.last_used_at > self.ttl:
                # Keep it until expire() can return the user id for cleanup.
                return None
            binding.last_used_at = now
            return binding

    async def remove(self, sid: Optional[str]) -> Optional[BrowserBinding]:
        if not sid:
            return None
        async with self._lock:
            return self._bindings.pop(sid, None)

    async def snapshot(self) -> list[tuple[str, BrowserBinding]]:
        async with self._lock:
            return list(self._bindings.items())

    async def expire(self) -> list[tuple[str, BrowserBinding]]:
        """Remove and return expired bindings for manager lifecycle cleanup."""
        now = time.time()
        expired: list[tuple[str, BrowserBinding]] = []
        async with self._lock:
            for sid, binding in list(self._bindings.items()):
                if now - binding.last_used_at > self.ttl:
                    expired.append((sid, self._bindings.pop(sid)))
        return expired

    async def allow_switch(self, sid: Optional[str], debounce: float = 1.5) -> tuple[bool, float]:
        """Atomically apply the switch debounce to one browser binding."""
        if not sid:
            return False, 0.0
        now = time.monotonic()
        async with self._lock:
            binding = self._bindings.get(sid)
            if binding is None:
                return False, 0.0
            elapsed = now - binding.last_switch_at
            if binding.last_switch_at and elapsed < debounce:
                return False, debounce - elapsed
            binding.last_switch_at = now
            return True, 0.0


class SlidingRateLimiter:
    def __init__(self, limit: int, window: float, max_keys: int = 10000):
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.max_keys = max(100, int(max_keys))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        async with self._lock:
            if key not in self._hits and len(self._hits) >= self.max_keys:
                for old_key, old_hits in list(self._hits.items()):
                    while old_hits and now - old_hits[0] >= self.window:
                        old_hits.popleft()
                    if not old_hits:
                        self._hits.pop(old_key, None)
                if len(self._hits) >= self.max_keys:
                    key = "__overflow__"
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(0.1, self.window - (now - hits[0]))
            hits.append(now)
            return True, 0.0


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _public_prefix(request: web.Request, configured: str, *, trust_proxy: bool) -> str:
    forwarded = request.headers.get("X-Forwarded-Prefix", "").strip() if trust_proxy else ""
    if forwarded and re.fullmatch(r"/[A-Za-z0-9._~/-]*", "/" + forwarded.strip("/")):
        return "/" + forwarded.strip("/") if forwarded.strip("/") else ""
    return configured


def _client_key(request: web.Request, *, trust_proxy: bool = False) -> str:
    """Use the edge proxy's canonical client address for throttling.

    ``X-Forwarded-For`` is only trustworthy when the app is reachable through
    a proxy that overwrites it.  Deployments exposing this app directly simply
    omit the header and fall back to ``request.remote``.
    """
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or (request.remote or "unknown")
        return request.headers.get("X-Real-IP", "").strip() or request.remote or "unknown"
    return request.remote or "unknown"


def _state_dict(state: Any) -> dict:
    if state is None:
        return {"status": "idle", "has_qr_png": False, "qr_seq": 0}
    fn = getattr(state, "to_state_dict", None)
    if callable(fn):
        value = fn()
        return dict(value) if isinstance(value, dict) else {}
    if isinstance(state, dict):
        return dict(state)
    result = {}
    for key in ("status", "qr_seq", "qr_url", "current_qr_url", "has_qr_png",
                "verify_prompt", "verify_retry", "last_error", "logged_in_at",
                "is_regenerating"):
        if hasattr(state, key):
            result[key] = getattr(state, key)
    if "qr_url" not in result and "current_qr_url" in result:
        result["qr_url"] = result.pop("current_qr_url")
    if "has_qr_png" not in result:
        result["has_qr_png"] = bool(getattr(state, "current_qr_png", None))
    return result


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


INDEX_HTML = """<!doctype html><html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>ClawBot 登录</title>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:24px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:12px;padding:20px;text-align:center}.qr{display:inline-block;
padding:12px;min-width:220px;min-height:220px;line-height:220px}.qr img{max-width:280px}
button{font-size:16px;padding:9px 18px;border:0;border-radius:6px;background:#07c160;color:#fff}
input{font-size:18px;padding:8px;width:150px}.ok{color:#07c160;font-weight:600}.err{color:#c00}</style>
<body><h1>ClawBot 微信登录</h1><div class=card><div id=status>等待登录...</div>
<div id=qr class=qr>—</div><form id=verify style=display:none><input id=code inputmode=numeric
pattern=\\d{4,8} minlength=4 maxlength=8 placeholder=配对码 required><button>提交</button></form>
<p id=error class=err></p><button id=switch type=button style=display:none>切换用户</button></div>
<script>
const root=__ROOT__, csrf=__CSRF__;
async function poll(){try{let r=await fetch(root+'/state',{cache:'no-store'});if(!r.ok)throw Error(r.status);render(await r.json())}
catch(e){document.querySelector('#error').textContent='拉取状态失败：'+e}setTimeout(poll,1000)}
function render(s){let st=document.querySelector('#status'),q=document.querySelector('#qr'),f=document.querySelector('#verify'),sw=document.querySelector('#switch');
 document.querySelector('#error').textContent=s.last_error||'';sw.style.display='inline-block';
 if(s.status==='logged_in'){st.innerHTML='<span class=ok>登录成功 ✓</span>';q.innerHTML='✓';f.style.display='none';return}
 st.textContent=s.is_regenerating?'正在生成新二维码...':(s.status==='qr_pending'?'请用微信扫描下方二维码':(s.status==='scanned'?'已扫码，请在手机上确认...':s.status));
 q.innerHTML=s.is_regenerating?'⟳':(s.has_qr_png?'<img alt=QR src="'+root+'/qrcode.png?v='+s.qr_seq+'">':'—');f.style.display=s.verify_prompt?'flex':'none'}
 document.querySelector('#verify').onsubmit=async e=>{e.preventDefault();let c=document.querySelector('#code').value.trim();
 if(!/^\\d{4,8}$/.test(c))return;let r=await fetch(root+'/verify_code',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({code:c})});
 if(!r.ok)document.querySelector('#error').textContent='提交失败：'+r.status;else document.querySelector('#code').value=''};
 document.querySelector('#switch').onclick=()=>fetch(root+'/switch',{method:'POST',headers:{'X-CSRF-Token':csrf}});poll();
</script></body></html>"""

LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">
<title>ClawBot 登录</title><style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:48px;border-radius:12px;text-align:center;box-shadow:0 4px 20px #0001}button{padding:14px 32px;background:#07c160;color:#fff;border:0;border-radius:8px;font-size:16px}</style>
<div class=card><h1>ClawBot 控制台</h1><p>扫码创建一个微信会话</p><form method=post action=\"__START__\"><button>扫码登录</button></form></div>"""


def build_web_app(manager: Any, *, prefix: str = DEFAULT_PREFIX,
                  config: Optional[dict] = None,
                  sessions: Optional[BrowserSessions] = None) -> web.Application:
    """Build the shared-process aiohttp application.

    Config keys: ``session_ttl``, ``ephemeral_limit``, ``rate_limit``,
    ``rate_window``, ``cookie_name``, and optional
    ``session_config`` passed to ``manager.get_or_create``.
    """
    cfg = dict(config or {})
    if prefix and not re.fullmatch(r"/?[A-Za-z0-9._~/-]*", str(prefix)):
        raise ValueError("prefix contains unsafe characters")
    pfx = "/" + prefix.strip("/") if prefix and prefix.strip("/") else ""
    cookie_name = str(cfg.get("cookie_name", DEFAULT_COOKIE))
    store = sessions or BrowserSessions(cfg.get("session_ttl", DEFAULT_SESSION_TTL))
    limiter = SlidingRateLimiter(cfg.get("rate_limit", DEFAULT_RATE_LIMIT),
                                 cfg.get("rate_window", DEFAULT_RATE_WINDOW),
                                 cfg.get("rate_limit_max_keys", 10000))
    max_ephemeral = max(1, int(cfg.get("ephemeral_limit", DEFAULT_EPHEMERAL_LIMIT)))
    create_lock = asyncio.Lock()
    pending_creates = 0
    pending_sessions: dict[str, asyncio.Task] = {}
    app = web.Application()
    app["manager"] = manager
    app["browser_sessions"] = store

    def root_for(request: web.Request) -> str:
        return _public_prefix(
            request, pfx, trust_proxy=_as_bool(cfg.get("trust_proxy"))
        )

    async def binding(request: web.Request) -> tuple[Optional[BrowserBinding], Optional[web.Response]]:
        b = await store.resolve(request.cookies.get(cookie_name))
        if b is None:
            return None, web.json_response({"error": "unauthorized"}, status=401)
        session = manager.get(b.user_id)
        if session is None and b.user_id not in pending_sessions:
            await store.remove(request.cookies.get(cookie_name))
            return None, web.json_response({"error": "session_not_found"}, status=401)
        await _maybe_await(getattr(manager, "touch", lambda _: None)(b.user_id))
        return b, None

    async def index(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            # unauthenticated browser sees the login page, never a user list
            start = root_for(request) + "/ephemeral/start"
            response = web.Response(text=LOGIN_HTML.replace("__START__", escape(start)),
                                    content_type="text/html")
            if request.cookies.get(cookie_name):
                response.del_cookie(cookie_name, path="/")
            return response
        root = root_for(request) or ""
        page = (INDEX_HTML.replace("__ROOT__", json.dumps(root))
                .replace("__CSRF__", json.dumps(b.csrf_token)))
        return web.Response(text=page, content_type="text/html")

    async def ephemeral_start(request: web.Request) -> web.StreamResponse:
        nonlocal pending_creates
        allowed, retry = await limiter.allow(
            _client_key(request, trust_proxy=_as_bool(cfg.get("trust_proxy"))))
        if not allowed:
            return web.json_response({"error": "rate_limited", "retry_after": retry},
                                     status=429, headers={"Retry-After": str(max(1, int(retry)))})
        async with create_lock:
            existing = {b.user_id for _, b in await store.snapshot()}
            active = len(existing) + pending_creates
            if active >= max_ephemeral:
                return web.json_response({"error": "session_limit"}, status=429,
                                         headers={"Retry-After": "60"})
            pending_creates += 1
        user_id = "eph_" + secrets.token_hex(16)
        # Bind before starting: BotSession.start() may block while waiting for
        # QR confirmation.  The browser must receive its cookie immediately.
        sid = await store.bind(user_id)

        async def create_when_ready() -> None:
            session_config = cfg.get("session_config")
            try:
                create_background = getattr(manager, "create_background", None)
                if callable(create_background):
                    result = create_background(user_id, session_config)
                    await _maybe_await(result)
                    return
                get_or_create = manager.get_or_create
                try:
                    supports_wait = "wait_ready" in inspect.signature(
                        get_or_create).parameters
                except (TypeError, ValueError):
                    supports_wait = False
                if supports_wait:
                    # Explicit manager capability: returns before QR polling.
                    await _maybe_await(get_or_create(
                        user_id, session_config, wait_ready=False))
                else:
                    # Legacy implementation can wait for a 480s QR flow;
                    # running it on this background task avoids request deadlock.
                    await _maybe_await(get_or_create(user_id, session_config))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("shared session create failed user=%s err=%s", user_id, exc)
                try:
                    await _maybe_await(manager.stop(user_id))
                except Exception:
                    pass
                raise
            finally:
                pending_sessions.pop(user_id, None)

        try:
            # Explicit non-blocking manager capabilities can be awaited in the
            # request: they only register the session and return its QR state.
            # Only the legacy two-argument API is moved to a background task,
            # because it may await the complete QR confirmation.
            create_background = getattr(manager, "create_background", None)
            supports_wait = False
            if not callable(create_background):
                try:
                    supports_wait = "wait_ready" in inspect.signature(
                        manager.get_or_create).parameters
                except (TypeError, ValueError):
                    pass
            if callable(create_background) or supports_wait:
                await create_when_ready()
            else:
                task = asyncio.create_task(create_when_ready(), name=f"web-create-{user_id}")
                pending_sessions[user_id] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
        except Exception as exc:
            await store.remove(sid)
            log.warning("shared session create failed err=%s", exc)
            return web.json_response({"error": "session_create_failed"}, status=503)
        finally:
            async with create_lock:
                pending_creates -= 1
        response = web.Response(status=302,
                                headers={"Location": root_for(request) + "/"})
        secure_setting = cfg.get("cookie_secure")
        secure_cookie = (
            request.secure
            or (_as_bool(cfg.get("trust_proxy"))
                and request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https")
        ) if secure_setting is None else _as_bool(secure_setting)
        response.set_cookie(cookie_name, sid, max_age=int(store.ttl), httponly=True,
                            samesite="Lax", secure=secure_cookie,
                            path="/")
        return response

    async def state(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        session = manager.get(b.user_id)  # type: ignore[union-attr]
        if session is None:
            return web.json_response({"status": "starting", "has_qr_png": False}, status=202)
        return web.json_response(_state_dict(getattr(session, "qr_state", None)),
                                 headers={"Cache-Control": "no-store"})

    async def qrcode(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        session = manager.get(b.user_id)  # type: ignore[union-attr]
        if session is None:
            return web.Response(status=204)
        png = getattr(getattr(session, "qr_state", None), "current_qr_png", None)
        if not png:
            return web.Response(status=204)
        return web.Response(body=png, content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    async def verify_code(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), b.csrf_token):
            return web.json_response({"error": "csrf"}, status=403)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        code = str((payload or {}).get("code") or "").strip()
        if not code.isdigit() or not 4 <= len(code) <= 8:
            return web.json_response({"error": "code must be 4-8 digits"}, status=400)
        session = manager.get(b.user_id)  # type: ignore[union-attr]
        if session is None:
            return web.json_response({"error": "session_starting"}, status=409)
        state_obj = getattr(session, "qr_state", None)
        submit = getattr(state_obj, "submit_verify_code", None)
        if not callable(submit):
            return web.json_response({"error": "verify unavailable"}, status=409)
        submit(code)
        return web.json_response({"ok": True})

    async def switch(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), b.csrf_token):
            return web.json_response({"error": "csrf"}, status=403)
        allowed, retry = await store.allow_switch(request.cookies.get(cookie_name))
        if not allowed:
            return web.json_response({"ok": True, "rate_limited": True,
                                      "retry_after": retry})
        session = manager.get(b.user_id)  # type: ignore[union-attr]
        if session is None:
            return web.json_response({"error": "session_starting"}, status=409)
        request_relogin = getattr(session, "request_relogin", None)
        if callable(request_relogin):
            async def trigger_relogin() -> None:
                try:
                    await _maybe_await(request_relogin("web switch"))
                except TypeError:
                    await _maybe_await(request_relogin(reason="web switch"))
                except Exception as exc:
                    log.warning("web relogin failed user=%s err=%s", b.user_id, exc)

            task = asyncio.create_task(trigger_relogin(), name=f"web-switch-{b.user_id}")
            pending_sessions[f"switch:{b.user_id}"] = task
            task.add_done_callback(
                lambda done, key=f"switch:{b.user_id}": pending_sessions.pop(key, None)
            )
        else:
            event = getattr(session, "relogin_event", None)
            if event is None or not hasattr(event, "set"):
                return web.json_response({"error": "relogin unavailable"}, status=409)
            event.set()
        return web.json_response({"ok": True, "status": _state_dict(getattr(session, "qr_state", None)).get("status")})

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "shared_process": True})

    async def sweep_expired() -> None:
        while True:
            await asyncio.sleep(max(0.05, min(60.0, store.ttl / 4)))
            for _, expired_binding in await store.expire():
                if expired_binding.ephemeral:
                    starter = pending_sessions.pop(expired_binding.user_id, None)
                    if starter is not None and not starter.done():
                        starter.cancel()
                        await asyncio.gather(starter, return_exceptions=True)
                    session = manager.get(expired_binding.user_id)
                    authenticated = getattr(session, "has_authenticated_connection", None)
                    if authenticated is None and session is not None:
                        token = getattr(session, "bot_token", "")
                        state = getattr(session, "runtime_state", {}) or {}
                        authenticated = bool(token or state.get("bot_token"))
                    if authenticated:
                        # Browser control-plane expiry must not stop a live
                        # WeChat data-plane connection.  getupdates remains
                        # active and -14 still drives controlled QR recovery.
                        log.info("expired browser binding retained authenticated session user=%s",
                                 expired_binding.user_id)
                        continue
                    try:
                        await _maybe_await(manager.stop(expired_binding.user_id))
                    except Exception as exc:
                        log.warning("expired session stop failed user=%s err=%s",
                                    expired_binding.user_id, exc)

    async def lifecycle_cleanup(app_: web.Application):
        sweeper = asyncio.create_task(sweep_expired(), name="shared-web-sweeper")
        try:
            yield
        finally:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
            tasks = list(pending_sessions.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app.cleanup_ctx.append(lifecycle_cleanup)

    # Register both the configured prefix and root.  nginx proxy_pass with a
    # trailing slash strips /clawbot before forwarding, while direct/testing
    # clients commonly retain it; both forms must have identical semantics.
    paths = {pfx or ""}
    for base in list(paths):
        for suffix, method, handler in (("/", "GET", index), ("/state", "GET", state),
            ("/qrcode.png", "GET", qrcode), ("/verify_code", "POST", verify_code),
            ("/switch", "POST", switch),
            ("/ephemeral/start", "POST", ephemeral_start), ("/healthz", "GET", healthz)):
            path = (base.rstrip("/") + suffix) or "/"
            app.router.add_route(method, path, handler)
    if pfx:
        # Internal root aliases support nginx's prefix-stripping proxy_pass.
        for suffix, method, handler in (("/", "GET", index), ("/state", "GET", state),
            ("/qrcode.png", "GET", qrcode), ("/verify_code", "POST", verify_code),
            ("/switch", "POST", switch),
            ("/ephemeral/start", "POST", ephemeral_start), ("/healthz", "GET", healthz)):
            app.router.add_route(method, suffix, handler)
    return app


__all__ = ["BrowserSessions", "build_web_app", "SlidingRateLimiter"]
