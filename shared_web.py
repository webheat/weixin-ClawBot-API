"""Shared-process web routes for the ClawBot runtime.

This module never starts a process or subprocess; sessions are created on
demand via ``POST /start`` and live as in-process tasks under ``manager``.
``manager`` is a small duck-typed boundary supplied by the shared bot
process::

    await manager.get_or_create(session_id, config=None)
    manager.get(session_id) -> BotSession | None
    await manager.stop(session_id)
    await manager.touch(session_id)       # a sync implementation is also OK

Each ``BotSession`` must expose ``qr_state`` and either ``request_relogin`` or
``relogin_event``.  The browser only receives an opaque, random cookie; the
cookie is mapped to a session id on the server and is never treated as a
session id.
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
from typing import Any, Mapping, Optional

from aiohttp import web

log = logging.getLogger("clawbot.shared_web")

DEFAULT_PREFIX = "/clawbot"
DEFAULT_COOKIE = "clawbot_session"
DEFAULT_SESSION_TTL = 8 * 3600
DEFAULT_SESSION_LIMIT = 100
DEFAULT_RATE_LIMIT = 5
DEFAULT_RATE_WINDOW = 60.0
DEFAULT_RESUME_TTL = 30 * 24 * 3600


@dataclass
class BrowserBinding:
    session_id: str
    created_at: float
    last_used_at: float
    transient: bool = True
    csrf_token: str = ""
    last_switch_at: float = 0.0


class BrowserSessions:
    """In-memory opaque-cookie store.

    A process-local store is intentional: a browser session must not be
    portable between independent bot processes.  Deployments with multiple
    web workers should route all requests to the shared process or provide a
    store with the same interface backed by a protected server-side database.
    """

    def __init__(self, ttl: float = DEFAULT_SESSION_TTL,
                 resume_ttl: float = DEFAULT_RESUME_TTL):
        self.ttl = max(0.01, float(ttl))
        self.resume_ttl = max(self.ttl, float(resume_ttl))
        self._bindings: dict[str, BrowserBinding] = {}
        # A separate opaque capability lets a browser recover the control
        # binding after inactivity. It never contains the user id and is
        # valid only while the background session still exists.
        self._resume_tokens: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def bind(self, session_id: str, *, transient: bool = True) -> str:
        now = time.time()
        sid = secrets.token_urlsafe(32)
        async with self._lock:
            self._bindings[sid] = BrowserBinding(
                session_id, now, now, transient, csrf_token=secrets.token_urlsafe(32)
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

    async def issue_resume(self, session_id: str) -> str:
        token = secrets.token_urlsafe(32)
        async with self._lock:
            self._resume_tokens[token] = (str(session_id), time.time() + self.resume_ttl)
        return token

    async def resolve_resume(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        async with self._lock:
            record = self._resume_tokens.get(token)
            if record is None:
                return None
            session_id, expires_at = record
            if time.time() >= expires_at:
                self._resume_tokens.pop(token, None)
                return None
            return session_id

    async def revoke_resume(self, session_id: str) -> None:
        key = str(session_id)
        async with self._lock:
            for token, record in list(self._resume_tokens.items()):
                if record[0] == key:
                    self._resume_tokens.pop(token, None)

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


# Paths the browser polls continuously — keep these at DEBUG so we don't drown
# the shared log when many visitors are active at once.
_WEB_POLL_PATHS = frozenset({"/state", "/healthz", "/qrcode.png"})


def _web_user_label(request: web.Request) -> str:
    """Return the last 12 chars of the opaque session cookie, or ``"-"``.

    The opaque token is logged as a routing hint, not as authentication.
    Truncating to 12 chars matches the ``ilink_user_id[-12:]`` style used
    throughout the project and keeps the log line readable.
    """

    sid = request.cookies.get(DEFAULT_COOKIE) or ""
    return sid[-12:] if sid else "-"


@web.middleware
async def _web_request_log_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    """Log every aiohttp request through ``clawbot.web`` so that previously
    silent triggers (POST /switch, POST /start, etc.) leave a trail.

    ``GET /state`` / ``/healthz`` / ``/qrcode.png`` are browser polls — emit
    them at DEBUG.  Everything else is INFO.  4xx/5xx escalate to
    WARNING/ERROR.  Exceptions raised by the handler are caught so the log
    line still records the status code the framework would have produced.
    """

    start = time.perf_counter()
    method = request.method
    path = request.rel_url.path
    user_label = _web_user_label(request)
    is_poll = path in _WEB_POLL_PATHS and method == "GET"
    log_level = logging.DEBUG if is_poll else logging.INFO
    response: Optional[web.StreamResponse] = None
    try:
        response = await handler(request)
        return response
    except web.HTTPException as exc:
        log_level = max(log_level, logging.WARNING if exc.status < 500 else logging.ERROR)
        dur_ms = (time.perf_counter() - start) * 1000
        log.log(
            log_level,
            "web req method=%s path=%s user=%s status=%d dur_ms=%.1f err=%s",
            method, path, user_label, exc.status, dur_ms, exc.reason,
        )
        raise
    except Exception:
        dur_ms = (time.perf_counter() - start) * 1000
        log.exception(
            "web req crashed method=%s path=%s user=%s dur_ms=%.1f",
            method, path, user_label, dur_ms,
        )
        raise
    finally:
        if response is not None:
            status = response.status
            if status >= 500:
                log_level = logging.ERROR
            elif status >= 400 and log_level < logging.WARNING:
                log_level = logging.WARNING
            dur_ms = (time.perf_counter() - start) * 1000
            log.log(
                log_level,
                "web req method=%s path=%s user=%s status=%d dur_ms=%.1f",
                method, path, user_label, status, dur_ms,
            )


def _build_ima_client(ima_env: Mapping[str, str] | None) -> Optional[Any]:
    """从 per-session ``ima_env`` 字典构造 ImaClient（共享凭据，不污染 os.environ）。

    shared_runtime 已经把 ``/etc/clawbot/ima.env`` 解析成字典塞到
    ``session_config["ima_env"]`` 里 —— 这里拿这份字典构造，避免 web 端去读
    ``os.environ``（多账号场景下会拿错）。凭据缺失返回 ``None``。
    """
    if not ima_env:
        return None
    try:
        from ima import ImaClient, ImaConfig

        def _f(name: str, default: str) -> str:
            v = str(ima_env.get(name) or "")
            return v if v else default

        def _bool(name: str) -> bool:
            return str(ima_env.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}

        def _int(name: str, default: int) -> int:
            try:
                return int(str(ima_env.get(name) or default))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                v = float(str(ima_env.get(name) or ""))
                return v if v > 0 else default
            except (TypeError, ValueError):
                return default

        search_limit = max(1, _int("IMA_ILINK_SEARCH_LIMIT", 5))
        cfg = ImaConfig(
            base_url=_f("IMA_ILINK_BASE_URL", "https://ima.qq.com").rstrip("/"),
            client_id=_f("IMA_ILINK_CLIENT_ID", ""),
            api_key=_f("IMA_ILINK_API_KEY", ""),
            default_knowledge_base_id=_f("IMA_ILINK_DEFAULT_KB", ""),
            timeout=_float("IMA_ILINK_TIMEOUT", 15.0),
            search_limit=search_limit,
            rerank_enabled=_bool("IMA_ILINK_RERANK"),
            rerank_top_k=max(1, min(_int("IMA_ILINK_RERANK_TOP_K", 3), search_limit)),
            fetch_body=_bool("IMA_ILINK_FETCH_BODY"),
            keyword_extract=_bool("IMA_ILINK_KEYWORD_EXTRACT"),
        )
        if not cfg.configured():
            return None
        return ImaClient(cfg)
    except Exception as exc:  # 构造异常不能让 web UI 崩
        log.warning("build_ima_client failed err=%s", exc)
        return None


def _wants_json(request: web.Request) -> bool:
    """POST/PUT 后客户端期望 JSON 而非 HTML 时使用。"""
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


INDEX_HTML = """<!doctype html><html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>ClawBot 登录</title>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:24px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:12px;padding:20px;text-align:center;margin-bottom:14px}
.card.left{text-align:left}.qr{display:inline-block;padding:12px;min-width:220px;min-height:220px;line-height:220px}
.qr img{max-width:280px}button{font-size:16px;padding:9px 18px;border:0;border-radius:6px;background:#07c160;color:#fff}
button.gray{background:#888}input{font-size:18px;padding:8px;width:150px}.ok{color:#07c160;font-weight:600}.err{color:#c00}
.kb-card{font-size:14px;line-height:1.6}.kb-card .kb-line{word-break:break-all}
.kb-card .kb-name{font-weight:600;color:#222}.kb-actions{margin-top:10px}
.kb-actions form, .kb-actions a{display:inline-block;margin-right:6px}
.kb-actions button{font-size:13px;padding:6px 12px}
.kb-actions .gray{background:#888}
.kb-actions .red{background:#c0392b}
</style>
<body><h1>ClawBot 微信登录</h1><div class=card><div id=status>等待登录...</div>
<div id=qr class=qr>—</div><form id=verify style=display:none><input id=code inputmode=numeric
pattern=\\d{4,8} minlength=4 maxlength=8 placeholder=配对码 required><button>提交</button></form>
<p id=error class=err></p><button id=switch type=button style=display:none>切换用户</button></div>
<div id=kbcard class=card left kbcard style=display:none>
  <div class=kb-card>
    <div>当前知识库：</div>
    <div class=kb-line><span id=kblabel class=kb-name>—</span></div>
    <div class=kb-actions id=kbactions></div>
  </div>
</div>
<script>
const root=__ROOT__, csrf=__CSRF__;
async function poll(){try{let r=await fetch(root+'/state',{cache:'no-store'});if(r.status===401){location.reload();return}if(!r.ok)throw Error(r.status);render(await r.json())}
catch(e){document.querySelector('#error').textContent='拉取状态失败：'+e}setTimeout(poll,1000)}
function escapeHtml(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(s){let st=document.querySelector('#status'),q=document.querySelector('#qr'),f=document.querySelector('#verify'),sw=document.querySelector('#switch');
 document.querySelector('#error').textContent=s.last_error||'';sw.style.display='inline-block';
 if(s.status==='logged_in'){st.innerHTML='<span class=ok>登录成功 ✓</span>';q.innerHTML='✓';f.style.display='none';} else {
   st.textContent=s.is_regenerating?'正在生成新二维码...':(s.status==='qr_pending'?'请用微信扫描下方二维码':(s.status==='scanned'?'已扫码，请在手机上确认...':s.status));
   q.innerHTML=s.is_regenerating?'⟳':(s.has_qr_png?'<img alt=QR src="'+root+'/qrcode.png?v='+s.qr_seq+'">':'—');f.style.display=s.verify_prompt?'flex':'none';
 }
 renderKb(s);}
function renderKb(s){
 let card=document.querySelector('#kbcard'),label=document.querySelector('#kblabel'),actions=document.querySelector('#kbactions');
 if(!s.ilink_user_id){card.style.display='none';return;}
 card.style.display='block';
 if(s.kb_binding && s.kb_binding.kb_id){
   label.textContent=(s.kb_binding.kb_name||'(未命名)')+'（'+s.kb_binding.kb_id+'）';
   actions.innerHTML=
     '<a href="'+root+'/ima/bind"><button class=gray>更换</button></a>'+
     '<form method=post action="'+root+'/ima/unbind" style=display:inline onsubmit="return true">'+
     '<input type=hidden name=csrf value="'+escapeHtml(csrf)+'">'+
     '<button type=submit class=red>解绑</button></form>';
 } else {
   label.textContent='未绑定（回退默认 IMA_ILINK_DEFAULT_KB）';
   actions.innerHTML='<a href="'+root+'/ima/bind"><button>绑定</button></a>';
 }}
 document.querySelector('#verify').onsubmit=async e=>{e.preventDefault();let c=document.querySelector('#code').value.trim();
 if(!/^\\d{4,8}$/.test(c))return;let r=await fetch(root+'/verify_code',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({code:c})});
 if(!r.ok)document.querySelector('#error').textContent='提交失败：'+r.status;else document.querySelector('#code').value=''};
 document.querySelector('#switch').onclick=()=>fetch(root+'/switch',{method:'POST',headers:{'X-CSRF-Token':csrf}});poll();
</script></body></html>"""

LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">
<title>ClawBot 登录</title><style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f6f7f9}
.card{background:#fff;padding:48px;border-radius:12px;text-align:center;box-shadow:0 4px 20px #0001}button{padding:14px 32px;background:#07c160;color:#fff;border:0;border-radius:8px;font-size:16px}</style>
<div class=card><h1>ClawBot 控制台</h1><p>扫码创建一个微信会话</p><form method=post action=\"__START__\"><button>扫码登录</button></form></div>"""


BIND_HTML = """<!doctype html><html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>选择 IMA 知识库</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:24px auto;padding:0 16px;color:#222}
.card{border:1px solid #ddd;border-radius:12px;padding:20px;margin-bottom:16px}
h1{font-size:18px;margin:0 0 12px}.row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #f3f3f3}
.row:last-child{border-bottom:0}.row label{cursor:pointer;flex:1}
.kb-id{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#888}
.kb-type{font-size:11px;color:#fff;background:#07c160;padding:1px 6px;border-radius:4px;margin-left:6px}
.kb-type.sub{background:#5b6cf2}
.btns{margin-top:16px;display:flex;gap:8px}
button{font-size:14px;padding:8px 16px;border:0;border-radius:6px;background:#07c160;color:#fff;cursor:pointer}
button.gray{background:#999}
.err{color:#c00;font-size:13px;margin-top:8px}.ok{color:#07c160;font-weight:600;font-size:13px;margin-top:8px}
a{color:#07c160;text-decoration:none}
</style>
<body><h1>选择你的 IMA 知识库</h1>
<div class=card>
<form method=post action=\"__ACTION__\" id=bindform>
<input type=hidden name=csrf value=\"__CSRF__\">
__KBS__
<div class=btns><button type=submit>提交</button>
<a href=\"__ROOT__\" class=\"gray\" style=\"padding:8px 16px;background:#999;color:#fff;border-radius:6px\">取消</a></div>
</form>
<p class=err>__ERR__</p>
</div>
<div class=card style=\"font-size:13px;color:#666\">
当前账号：<code>__OWNER__</code><br>
当前绑定：__CUR__
</div>
</body></html>"""


def _render_kb_rows(kbs: list[dict], current_kb_id: str = "") -> str:
    """生成 KB 单选列表 HTML 行；空列表返提示文案。"""
    if not kbs:
        return "<p style='color:#c00'>暂无可绑定的知识库（账号下没有共享或订阅类 KB）。</p>"
    rows = []
    for kb in kbs:
        type_label = "共享" if kb.get("kb_type") == 1002 else "订阅"
        type_class = "sub" if kb.get("kb_type") != 1002 else ""
        checked = " checked" if kb.get("kb_id") == current_kb_id and current_kb_id else ""
        rows.append(
            f'<div class=row><label><input type=radio name=kb_id value="{escape(kb["kb_id"])}"{checked}> '
            f'{escape(kb.get("kb_name") or "(未命名)")}'
            f'<span class="kb-type {type_class}">{type_label}</span><br>'
            f'<span class=kb-id>{escape(kb["kb_id"])}</span></label></div>'
        )
    return "\n".join(rows)


def _render_current_label(binding: Optional[dict]) -> str:
    if not binding:
        return "未绑定（回退默认 KB）"
    return f"{escape(binding.get('kb_name') or '(未命名)')}（{escape(binding.get('kb_id') or '')}）"


def build_web_app(manager: Any, *, prefix: str = DEFAULT_PREFIX,
                  config: Optional[dict] = None,
                  sessions: Optional[BrowserSessions] = None) -> web.Application:
    """Build the shared-process aiohttp application.

    Config keys: ``session_ttl``, ``session_limit``, ``rate_limit``,
    ``rate_window``, ``cookie_name``, ``resume_ttl``, and optional
    ``session_config`` passed to ``manager.get_or_create``.
    """
    cfg = dict(config or {})
    if prefix and not re.fullmatch(r"/?[A-Za-z0-9._~/-]*", str(prefix)):
        raise ValueError("prefix contains unsafe characters")
    pfx = "/" + prefix.strip("/") if prefix and prefix.strip("/") else ""
    cookie_name = str(cfg.get("cookie_name", DEFAULT_COOKIE))
    resume_cookie_name = str(cfg.get("resume_cookie_name", cookie_name + "_resume"))
    store = sessions or BrowserSessions(
        cfg.get("session_ttl", DEFAULT_SESSION_TTL),
        cfg.get("resume_ttl", DEFAULT_RESUME_TTL),
    )
    limiter = SlidingRateLimiter(cfg.get("rate_limit", DEFAULT_RATE_LIMIT),
                                 cfg.get("rate_window", DEFAULT_RATE_WINDOW),
                                 cfg.get("rate_limit_max_keys", 10000))
    max_sessions = max(1, int(cfg.get("session_limit", DEFAULT_SESSION_LIMIT)))
    create_lock = asyncio.Lock()
    pending_creates = 0
    pending_sessions: dict[str, asyncio.Task] = {}
    app = web.Application(middlewares=[_web_request_log_middleware])
    app["manager"] = manager
    app["browser_sessions"] = store

    def root_for(request: web.Request) -> str:
        return _public_prefix(
            request, pfx, trust_proxy=_as_bool(cfg.get("trust_proxy"))
        )

    def secure_cookie_for(request: web.Request) -> bool:
        secure_setting = cfg.get("cookie_secure")
        return (
            request.secure
            or (_as_bool(cfg.get("trust_proxy"))
                and request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https")
        ) if secure_setting is None else _as_bool(secure_setting)

    def set_session_cookie(response: web.StreamResponse, request: web.Request,
                           sid: str) -> None:
        response.set_cookie(cookie_name, sid, max_age=int(store.ttl), httponly=True,
                            samesite="Lax", secure=secure_cookie_for(request), path="/")

    def set_resume_cookie(response: web.StreamResponse, request: web.Request,
                          token: str) -> None:
        resume_ttl = max(1, int(getattr(store, "resume_ttl", DEFAULT_RESUME_TTL)))
        response.set_cookie(resume_cookie_name, token, max_age=resume_ttl, httponly=True,
                            samesite="Lax", secure=secure_cookie_for(request), path="/")

    async def binding(request: web.Request) -> tuple[Optional[BrowserBinding], Optional[web.Response]]:
        b = await store.resolve(request.cookies.get(cookie_name))
        if b is None:
            return None, web.json_response({"error": "unauthorized"}, status=401)
        session = manager.get(b.session_id)
        if session is None and b.session_id not in pending_sessions:
            await store.remove(request.cookies.get(cookie_name))
            return None, web.json_response({"error": "session_not_found"}, status=401)
        await _maybe_await(getattr(manager, "touch", lambda _: None)(b.session_id))
        return b, None

    async def resume_existing(request: web.Request) -> Optional[str]:
        resolver = getattr(store, "resolve_resume", None)
        token = request.cookies.get(resume_cookie_name)
        if not callable(resolver) or not token:
            return None
        session_id = await _maybe_await(resolver(token))
        if not session_id:
            return None
        if manager.get(session_id) is None and session_id not in pending_sessions:
            revoke = getattr(store, "revoke_resume", None)
            if callable(revoke):
                await _maybe_await(revoke(session_id))
            return None
        sid = await store.bind(session_id)
        await _maybe_await(getattr(manager, "touch", lambda _: None)(session_id))
        return sid

    async def index(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            recovered_sid = await resume_existing(request)
            if recovered_sid:
                response = web.Response(status=302,
                                        headers={"Location": root_for(request) + "/"})
                set_session_cookie(response, request, recovered_sid)
                return response
            # unauthenticated browser sees the login page, never a user list
            start = root_for(request) + "/start"
            response = web.Response(text=LOGIN_HTML.replace("__START__", escape(start)),
                                    content_type="text/html")
            if request.cookies.get(cookie_name):
                response.del_cookie(cookie_name, path="/")
            return response
        root = root_for(request) or ""
        page = (INDEX_HTML.replace("__ROOT__", json.dumps(root))
                .replace("__CSRF__", json.dumps(b.csrf_token)))
        return web.Response(text=page, content_type="text/html")

    async def session_start(request: web.Request) -> web.StreamResponse:
        nonlocal pending_creates
        allowed, retry = await limiter.allow(
            _client_key(request, trust_proxy=_as_bool(cfg.get("trust_proxy"))))
        if not allowed:
            return web.json_response({"error": "rate_limited", "retry_after": retry},
                                     status=429, headers={"Retry-After": str(max(1, int(retry)))})
        recovered_sid = await resume_existing(request)
        if recovered_sid:
            response = web.Response(status=302,
                                    headers={"Location": root_for(request) + "/"})
            set_session_cookie(response, request, recovered_sid)
            return response
        async with create_lock:
            existing = {b.session_id for _, b in await store.snapshot()}
            active = len(existing) + pending_creates
            if active >= max_sessions:
                return web.json_response({"error": "session_limit"}, status=429,
                                         headers={"Retry-After": "60"})
            pending_creates += 1
        session_id = secrets.token_urlsafe(32)
        # Bind before starting: BotSession.start() may block while waiting for
        # QR confirmation.  The browser must receive its cookie immediately.
        sid = await store.bind(session_id)
        issue_resume = getattr(store, "issue_resume", None)
        resume_token = (
            await _maybe_await(issue_resume(session_id))
            if callable(issue_resume) else ""
        )

        async def create_when_ready() -> None:
            session_config = cfg.get("session_config")
            try:
                create_background = getattr(manager, "create_background", None)
                if callable(create_background):
                    result = create_background(session_id, session_config)
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
                        session_id, session_config, wait_ready=False))
                else:
                    # Legacy implementation can wait for a 480s QR flow;
                    # running it on this background task avoids request deadlock.
                    await _maybe_await(get_or_create(session_id, session_config))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("shared session create failed session=%s err=%s", session_id, exc)
                try:
                    await _maybe_await(manager.stop(session_id))
                except Exception:
                    pass
                revoke = getattr(store, "revoke_resume", None)
                if callable(revoke):
                    await _maybe_await(revoke(session_id))
                raise
            finally:
                pending_sessions.pop(session_id, None)

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
                task = asyncio.create_task(create_when_ready(), name=f"web-create-{session_id}")
                pending_sessions[session_id] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
        except Exception as exc:
            await store.remove(sid)
            revoke = getattr(store, "revoke_resume", None)
            if callable(revoke):
                await _maybe_await(revoke(session_id))
            log.warning("shared session create failed err=%s", exc)
            return web.json_response({"error": "session_create_failed"}, status=503)
        finally:
            async with create_lock:
                pending_creates -= 1
        response = web.Response(status=302,
                                headers={"Location": root_for(request) + "/"})
        set_session_cookie(response, request, sid)
        if resume_token:
            set_resume_cookie(response, request, resume_token)
        return response

    async def state(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        session = manager.get(b.session_id)  # type: ignore[union-attr]
        if session is None:
            return web.json_response({"status": "starting", "has_qr_png": False}, status=202)
        body = _state_dict(getattr(session, "qr_state", None))
        # Per-user IMA KB 绑定（docs/IMA_PER_USER_BINDING.md）：让 /state
        # 顺手带出当前 owner 和 binding，省一次独立接口。前端 polling
        # 1 秒一次，IMABindings.lookup 是同步内存命中，不构成负担。
        body["ilink_user_id"] = str(getattr(session, "ilink_user_id", "") or "")
        try:
            from utils.ima_bindings import get_default_bindings as _get_bindings
            bindings = await _get_bindings()
            current = (
                bindings.lookup(body["ilink_user_id"]) if body["ilink_user_id"] else None
            )
            body["kb_binding"] = (
                {"kb_id": current.get("kb_id", ""), "kb_name": current.get("kb_name", "")}
                if current else None
            )
        except Exception:
            body["kb_binding"] = None
        return web.json_response(body, headers={"Cache-Control": "no-store"})

    async def qrcode(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        session = manager.get(b.session_id)  # type: ignore[union-attr]
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
        session = manager.get(b.session_id)  # type: ignore[union-attr]
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
        session = manager.get(b.session_id)  # type: ignore[union-attr]
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
                    log.warning("web relogin failed session=%s err=%s", b.session_id, exc)

            task = asyncio.create_task(trigger_relogin(), name=f"web-switch-{b.session_id}")
            pending_sessions[f"switch:{b.session_id}"] = task
            task.add_done_callback(
                lambda done, key=f"switch:{b.session_id}": pending_sessions.pop(key, None)
            )
        else:
            event = getattr(session, "relogin_event", None)
            if event is None or not hasattr(event, "set"):
                return web.json_response({"error": "relogin unavailable"}, status=409)
            event.set()
        return web.json_response({"ok": True, "status": _state_dict(getattr(session, "qr_state", None)).get("status")})

    # ========== Per-user IMA KB 绑定路由（docs/IMA_WEB_UI.md） ==========
    async def _get_ima_env() -> dict[str, str]:
        """从 ``session_config.ima_env`` 读 per-session IMA 凭据字典。"""
        session_config = cfg.get("session_config") or {}
        return dict(session_config.get("ima_env") or {})

    async def _get_owner_ilink_id(b: BrowserBinding) -> str:
        """查 BotSession 的 ilink_user_id；不依赖 BotSession.ilink_user_id 存在性。"""
        session = manager.get(b.session_id)
        return str(getattr(session, "ilink_user_id", "") or "")

    async def ima_bind_get(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        owner_id = await _get_owner_ilink_id(b)
        if not owner_id:
            return web.json_response({"error": "not_logged_in"}, status=412)
        # 拉取可绑定 KB
        ima_env = await _get_ima_env()
        client = _build_ima_client(ima_env)
        kbs: list[dict] = []
        kb_error = ""
        if client is None:
            kb_error = "IMA 未配置（缺 IMA_ILINK_CLIENT_ID / API_KEY）。"
        else:
            try:
                kbs = await asyncio.get_running_loop().run_in_executor(
                    None, client.list_searchable_kbs
                )
            except Exception as exc:
                kb_error = f"拉取 KB 失败：{exc}"
                log.warning("ima_bind list_searchable_kbs failed err=%s", exc)
        # 当前 binding
        current_kb_id = ""
        current_label = "未绑定（回退默认 KB）"
        try:
            from utils.ima_bindings import get_default_bindings as _get_bindings
            bindings = await _get_bindings()
            existing = bindings.lookup(owner_id)
            if existing:
                current_kb_id = str(existing.get("kb_id") or "")
                current_label = _render_current_label(existing)
        except Exception:
            pass
        root = root_for(request) or ""
        action = root + "/ima/bind"
        page = (
            BIND_HTML
            .replace("__ACTION__", escape(action))
            .replace("__CSRF__", escape(b.csrf_token))
            .replace("__KBS__", _render_kb_rows(kbs, current_kb_id))
            .replace("__ROOT__", escape(root + "/"))
            .replace("__OWNER__", escape(owner_id))
            .replace("__CUR__", current_label)
            .replace("__ERR__", escape(kb_error))
        )
        return web.Response(text=page, content_type="text/html")

    async def ima_bind_post(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), b.csrf_token):
            return web.json_response({"error": "csrf"}, status=403)
        owner_id = await _get_owner_ilink_id(b)
        if not owner_id:
            return web.json_response({"error": "not_logged_in"}, status=412)
        # 接受 JSON 或 form
        kb_id = ""
        kb_name = ""
        kb_type = 0
        try:
            ctype = request.headers.get("Content-Type", "").lower()
            if "application/json" in ctype:
                payload = await request.json()
                kb_id = str((payload or {}).get("kb_id") or "").strip()
                kb_name = str((payload or {}).get("kb_name") or "").strip()
                try:
                    kb_type = int((payload or {}).get("kb_type") or 0)
                except (TypeError, ValueError):
                    kb_type = 0
            else:
                form = await request.post()
                kb_id = str(form.get("kb_id") or "").strip()
        except Exception:
            return web.json_response({"error": "invalid body"}, status=400)
        if not kb_id:
            return web.json_response({"error": "kb_id required"}, status=400)
        if kb_type not in (1002, 1004):
            return web.json_response(
                {"error": "kb_type must be 1002 (shared) or 1004 (subscribed)"},
                status=400,
            )
        # 用真实列表再校验 kb_id 存在；防前端伪造
        client = _build_ima_client(await _get_ima_env())
        if client is not None:
            try:
                kbs = await asyncio.get_running_loop().run_in_executor(
                    None, client.list_searchable_kbs
                )
                matched = next((kb for kb in kbs if kb.get("kb_id") == kb_id), None)
                if matched is None:
                    return web.json_response(
                        {"error": "kb_id not in searchable list"}, status=400,
                    )
                # 用 IMA 服务端的真实 name / type 覆盖客户端字段（防 spoofing）
                kb_id = str(matched.get("kb_id") or kb_id)
                kb_name = str(matched.get("kb_name") or kb_name)
                kb_type = int(matched.get("kb_type") or kb_type)
            except Exception as exc:
                # 校验失败时仍允许写入（高可用优先）；但记 warn
                log.warning("ima_bind_post re-validate failed err=%s", exc)
        try:
            from utils.ima_bindings import get_default_bindings as _get_bindings
            bindings = await _get_bindings()
            bot_id_at_bind = ""
            session = manager.get(b.session_id)
            if session is not None:
                bot_id_at_bind = str(getattr(session, "ilink_bot_id", "") or "")
            await bindings.bind(
                owner_id, kb_id, kb_name, kb_type,
                bound_by="web",
                bot_id_at_bind=bot_id_at_bind,
            )
        except Exception as exc:
            log.warning("ima_bind_post bind failed err=%s", exc)
            return web.json_response({"error": f"bind failed: {exc}"}, status=500)
        if _wants_json(request):
            return web.json_response({"ok": True, "kb_id": kb_id})
        return web.HTTPFound(root_for(request) + "/")

    async def ima_unbind_post(request: web.Request) -> web.Response:
        b, error = await binding(request)
        if error:
            return error
        if not secrets.compare_digest(
                request.headers.get("X-CSRF-Token", ""), b.csrf_token):
            return web.json_response({"error": "csrf"}, status=403)
        owner_id = await _get_owner_ilink_id(b)
        if not owner_id:
            return web.json_response({"error": "not_logged_in"}, status=412)
        try:
            from utils.ima_bindings import get_default_bindings as _get_bindings
            bindings = await _get_bindings()
            removed = await bindings.unbind(owner_id)
        except Exception as exc:
            log.warning("ima_unbind_post failed err=%s", exc)
            return web.json_response({"error": f"unbind failed: {exc}"}, status=500)
        if _wants_json(request):
            return web.json_response({"ok": True, "removed": removed})
        return web.HTTPFound(root_for(request) + "/")

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "shared_process": True})

    async def sweep_expired() -> None:
        while True:
            await asyncio.sleep(max(0.05, min(60.0, store.ttl / 4)))
            for _, expired_binding in await store.expire():
                if expired_binding.transient:
                    starter = pending_sessions.pop(expired_binding.session_id, None)
                    if starter is not None and not starter.done():
                        starter.cancel()
                        await asyncio.gather(starter, return_exceptions=True)
                    session = manager.get(expired_binding.session_id)
                    authenticated = getattr(session, "has_authenticated_connection", None)
                    if authenticated is None and session is not None:
                        token = getattr(session, "bot_token", "")
                        state = getattr(session, "runtime_state", {}) or {}
                        authenticated = bool(token or state.get("bot_token"))
                    if authenticated:
                        # Browser control-plane expiry must not stop a live
                        # WeChat data-plane connection.  getupdates remains
                        # active and -14 still drives controlled QR recovery.
                        log.info("expired browser binding retained authenticated session=%s",
                                 expired_binding.session_id)
                        continue
                    try:
                        await _maybe_await(manager.stop(expired_binding.session_id))
                    except Exception as exc:
                        log.warning("expired session stop failed session=%s err=%s",
                                    expired_binding.session_id, exc)

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
            ("/ima/bind", "GET", ima_bind_get), ("/ima/bind", "POST", ima_bind_post),
            ("/ima/unbind", "POST", ima_unbind_post),
            ("/start", "POST", session_start), ("/healthz", "GET", healthz)):
            path = (base.rstrip("/") + suffix) or "/"
            app.router.add_route(method, path, handler)
    if pfx:
        # Internal root aliases support nginx's prefix-stripping proxy_pass.
        for suffix, method, handler in (("/", "GET", index), ("/state", "GET", state),
            ("/qrcode.png", "GET", qrcode), ("/verify_code", "POST", verify_code),
            ("/switch", "POST", switch),
            ("/ima/bind", "GET", ima_bind_get), ("/ima/bind", "POST", ima_bind_post),
            ("/ima/unbind", "POST", ima_unbind_post),
            ("/start", "POST", session_start), ("/healthz", "GET", healthz)):
            app.router.add_route(method, suffix, handler)
    return app


__all__ = ["BrowserSessions", "build_web_app", "SlidingRateLimiter"]
