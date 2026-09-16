"""Per-account runtime for the shared-process ClawBot.

This module deliberately keeps the protocol helpers in :mod:`bot`.  The old
CLI remains usable, while a manager can host many isolated sessions in one
event loop.  A session owns *all* mutable account state; only the aiohttp
connection pool is shared.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from qr_web import QrFlowState, make_web_on_qrcode


# Lazy compatibility hooks.  Keeping these at module scope also gives an
# embedding web app a narrow seam for metrics/tests without importing bot.py
# (which has a legacy CLI bootstrap at module scope).
async def send_msg_safe(*args: Any, **kwargs: Any) -> Any:
    from bot import send_msg_safe as impl
    return await impl(*args, **kwargs)


async def send_typing_safe(*args: Any, **kwargs: Any) -> Any:
    from bot import send_typing_safe as impl
    return await impl(*args, **kwargs)


async def get_typing_ticket_safe(*args: Any, **kwargs: Any) -> Any:
    from bot import get_typing_ticket_safe as impl
    return await impl(*args, **kwargs)


def _safe_name(value: str) -> str:
    raw = str(value or "")
    if raw and re.fullmatch(r"[A-Za-z0-9_.-]+", raw):
        return raw
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", raw).strip("._-") or "user"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{slug[:48]}_{digest}"


def _empty_state() -> dict[str, Any]:
    return {
        "bot_token": "",
        "baseurl": "",
        "ilink_bot_id": "",
        "ilink_user_id": "",
        "get_updates_buf": "",
        "contexts": {},
        "last_contact": {"from_id": "", "context_token": ""},
        # A returned cursor is not committed until the whole batch is handled.
        # Keeping the batch makes a process crash recoverable instead of lossy.
        "pending_messages": [],
        "processed_message_ids": [],
    }


class BotSession:
    """One WeChat account and its complete lifecycle.

    ``start`` is safe to run concurrently for different instances.  It never
    holds a manager lock while waiting for QR confirmation or network I/O.
    """

    def __init__(
        self,
        session_id: str,
        http: aiohttp.ClientSession,
        config: dict[str, Any] | None = None,
        *,
        state_file: str | os.PathLike[str] | None = None,
        ai_client: Any = None,
        on_event: Optional[Callable[[str, "BotSession", dict[str, Any]], Awaitable[None] | None]] = None,
        on_qrcode: Optional[Callable[[str], Awaitable[None] | None]] = None,
    ) -> None:
        self.session_id = str(session_id)
        self.http = http
        self.config = copy.deepcopy(config or {})
        state_root = Path(str(self.config.get("state_dir") or "."))
        # State files pre-2026-09-16 used the ``weixin_state_eph_<hex>`` pattern
        # with the opaque session id minted by shared_web.  After the rename
        # the new code path uses ``weixin_state_<token_urlsafe>`` without the
        # ``eph_`` prefix.  Existing on-disk files are intentionally left
        # untouched — they belong to sessions that no longer run and the
        # next GC sweep (or ``weixin_state_*.json`` TTL) reaps them.
        self.state_file = Path(state_file) if state_file is not None else (
            state_root / f"weixin_state_{_safe_name(self.session_id)}.json"
        )
        self._state_lock = threading.Lock()
        self._on_event = on_event

        self.runtime_state = self._load_state()
        self.bot_token = str(self.runtime_state.get("bot_token") or "").strip()
        self.baseurl = str(self.runtime_state.get("baseurl") or self._base_url).strip()
        self.ilink_bot_id = str(self.runtime_state.get("ilink_bot_id") or "")
        self.ilink_user_id = str(self.runtime_state.get("ilink_user_id") or "")
        self.contexts: dict[str, str] = dict(self.runtime_state.get("contexts") or {})
        contact = self.runtime_state.get("last_contact") or {}
        self.last_contact = {
            "from_id": contact.get("from_id") or None,
            "context_token": contact.get("context_token") or None,
        }
        self.welcomed_users = set(self.contexts)
        self.typing_ticket_cache: dict[str, str] = {}
        self.manual_reconnect_pending: dict[str, bool] = {}

        self.qr_state = QrFlowState()
        self.web_on_qrcode = on_qrcode or make_web_on_qrcode(self.qr_state, http)
        self.login_time = float(self.runtime_state.get("login_time") or time.time())
        self.last_used_at = time.time()

        self._token_ref = [self.bot_token]
        self._base_url_ref = [self.baseurl]
        self._tasks: dict[str, asyncio.Task] = {}
        self._relogin_task: asyncio.Task | None = None
        self._relogin_event = asyncio.Event()
        self._initial_cancel = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._relogin_lock = asyncio.Lock()
        self._pending_relogin: asyncio.Future | None = None
        self._reconnect_lock = asyncio.Lock()
        # Serializes _drain_batch vs _reconnect so the token-clear in
        # _reconnect() cannot race with an in-flight AI / send reply.
        # See docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md §5.2.
        self._drain_lock = asyncio.Lock()
        # Keep an authenticated session alive while its credential is being
        # replaced.  _reconnect() intentionally clears bot_token before QR
        # login, so the token alone cannot describe this transitional state.
        self._reauthenticating = False
        self._reauthentication_required = False
        self._initializing = False
        self._started = False
        self._stopped = False
        self._stop_lock = asyncio.Lock()
        self._fatal = asyncio.Event()

        self.ai = ai_client if ai_client is not None else self._make_ai(self.config)

    @property
    def _base_url(self) -> str:
        return str(self.config.get("ilink_base_url") or "https://ilinkai.weixin.qq.com")

    @property
    def has_authenticated_connection(self) -> bool:
        """Whether this session owns a token that can be kept alive.

        The browser login page is only a control plane.  Once iLink has
        returned a bot token, the connection is owned by the background
        session and must outlive an idle browser tab.
        """
        return bool(
            str(self._token_ref[0] or self.bot_token or "").strip()
            or self._reauthenticating
            or self._reauthentication_required
        )

    @staticmethod
    def _make_ai(config: dict[str, Any]) -> Any:
        """Build one private AI/IMA stack without reading global credentials."""
        try:
            from bot import _AIWithIma, create_ai_client
            cfg = dict(config)
            if "api_key" not in cfg:
                provider = cfg.get("provider", "dusapi")
                cfg.update(dict((cfg.get("providers") or {}).get(provider) or {}))
                cfg["provider"] = provider
            base = create_ai_client(cfg)
            ima_env = dict(config.get("ima_env") or {})
            if not ima_env:
                return base
            from ima import ImaClient, ImaConfig

            def integer(name: str, default: int) -> int:
                try:
                    return int(ima_env.get(name, default))
                except (TypeError, ValueError):
                    return default

            def decimal(name: str, default: float) -> float:
                try:
                    return float(ima_env.get(name, default))
                except (TypeError, ValueError):
                    return default

            enabled = lambda name: str(ima_env.get(name, "")).strip().lower() in {
                "1", "true", "yes", "on"
            }
            search_limit = max(1, integer("IMA_ILINK_SEARCH_LIMIT", 5))
            ima_config = ImaConfig(
                base_url=str(ima_env.get("IMA_ILINK_BASE_URL") or "https://ima.qq.com").rstrip("/"),
                client_id=str(ima_env.get("IMA_ILINK_CLIENT_ID") or ""),
                api_key=str(ima_env.get("IMA_ILINK_API_KEY") or ""),
                default_knowledge_base_id=str(ima_env.get("IMA_ILINK_DEFAULT_KB") or ""),
                timeout=max(0.1, decimal("IMA_ILINK_TIMEOUT", 15.0)),
                search_limit=search_limit,
                rerank_enabled=enabled("IMA_ILINK_RERANK"),
                rerank_top_k=max(1, min(integer("IMA_ILINK_RERANK_TOP_K", 3), search_limit)),
                fetch_body=enabled("IMA_ILINK_FETCH_BODY"),
                keyword_extract=enabled("IMA_ILINK_KEYWORD_EXTRACT"),
            )
            return _AIWithIma(
                base, ImaClient(ima_config), environ=dict(config.get("runtime_env") or {})
            )
        except Exception:
            # A session can still login and receive commands when AI is not
            # configured; handle_message returns a useful degraded response.
            return None

    def _load_state(self) -> dict[str, Any]:
        state = _empty_state()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state.update(raw)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        if not isinstance(state.get("contexts"), dict):
            state["contexts"] = {}
        if not isinstance(state.get("pending_messages"), list):
            state["pending_messages"] = []
        if not isinstance(state.get("processed_message_ids"), list):
            state["processed_message_ids"] = []
        return state

    def save_state(self) -> None:
        with self._state_lock:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = dict(self.runtime_state)
            state.update({
                "bot_token": self.bot_token,
                "baseurl": self.baseurl,
                "ilink_bot_id": self.ilink_bot_id,
                "ilink_user_id": self.ilink_user_id,
                "contexts": dict(self.contexts),
                "last_contact": dict(self.last_contact),
                "login_time": self.login_time,
            })
            temp = self.state_file.with_name(self.state_file.name + ".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temp.chmod(0o600)
            except OSError:
                pass
            os.replace(temp, self.state_file)
            self.runtime_state = state

    async def _emit(self, event: str, **payload: Any) -> None:
        if self._on_event is None:
            return
        result = self._on_event(event, self, payload)
        if asyncio.iscoroutine(result):
            await result

    async def _on_qr(self, content: str) -> None:
        result = self.web_on_qrcode(content)
        if asyncio.iscoroutine(result):
            await result
        await self._emit("qrcode", content=content)

    async def _apply_login(self, result: dict[str, Any], *, initial: bool = False) -> None:
        from bot import notify_lifecycle

        new_token = str(result.get("bot_token") or "").strip()
        if not new_token:
            raise RuntimeError("登录响应缺少 bot_token")
        old_token, old_base, old_id = self.bot_token, self.baseurl, self.ilink_bot_id
        new_base = str(result.get("baseurl") or old_base or self._base_url)
        new_id = str(result.get("ilink_bot_id") or old_id)
        account_changed = bool(new_id and old_id and new_id != old_id)
        if account_changed:
            self.contexts.clear()
            self.welcomed_users.clear()
            self.typing_ticket_cache.clear()
            self.manual_reconnect_pending.clear()
            self.last_contact = {"from_id": None, "context_token": None}
            self.runtime_state["get_updates_buf"] = ""
            self.runtime_state["pending_messages"] = []
            self.runtime_state["pending_cursor"] = ""
            self.runtime_state["processed_message_ids"] = []
        changed = account_changed or old_token != new_token or old_base != new_base
        self.bot_token, self.baseurl = new_token, new_base
        self.ilink_bot_id = new_id
        self.ilink_user_id = str(result.get("ilink_user_id") or self.ilink_user_id)
        self._token_ref[0], self._base_url_ref[0] = self.bot_token, self.baseurl
        self.login_time = time.time()
        self.qr_state.status = "logged_in"
        self.qr_state.logged_in_at = self.login_time
        self.save_state()
        if changed and old_token:
            await notify_lifecycle(self.http, "ilink/bot/msg/notifystop", old_token, old_base)
        if changed or initial:
            self.typing_ticket_cache.clear()
            await notify_lifecycle(self.http, "ilink/bot/msg/notifystart", self.bot_token, self.baseurl)
        # Per-user IMA KB 绑定（docs/IMA_PER_USER_BINDING.md §3）：运行时**不**
        # 写 ima_bindings.json —— 绑定只能由用户在 web UI 或 /bindkb 命令主动
        # 设置。这里只读一次、记一行日志，便于排查"这个 owner 当前 KB 是什么"。
        if self.ilink_user_id:
            try:
                from utils.ima_bindings import get_default_bindings as _get_bindings
                from utils.logging_setup import get_logger as _get_logger
                bindings = await _get_bindings()
                existing = bindings.lookup(self.ilink_user_id)
                _get_logger("ima_bindings").info(
                    "apply_login user=%s kb=%s",
                    self.ilink_user_id[-12:],
                    (existing.get("kb_id") if existing else None) or "-",
                )
            except Exception as exc:  # 持久化层异常不影响登录流程
                from utils.logging_setup import get_logger as _get_logger
                _get_logger("ima_bindings").debug(
                    "apply_login binding lookup skipped err=%s", exc
                )
        await self._emit("logged_in", account_changed=account_changed)

    async def _login(self, *, reconnect: bool = False) -> dict[str, Any]:
        from bot import login_with_qrcode
        local = [self.bot_token] if self.bot_token else []
        return await login_with_qrcode(
            self.http, local, existing_state=self.runtime_state,
            on_qrcode=self._on_qr, web_state=self.qr_state,
            cancel_event=self._initial_cancel if not reconnect else None,
            save_qr_artifact=False,
        )

    async def _reconnect(self) -> dict[str, Any]:
        from bot import notify_lifecycle
        # Hold _drain_lock for the entire reconnect: any in-flight _drain_batch
        # finishes before we clear bot_token; any new _drain_batch that arrives
        # during QR login waits for us. See
        # docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md §5.2.
        async with self._reconnect_lock, self._drain_lock:
            self.qr_state.is_regenerating = True
            self.qr_state.status = "qr_pending"
            # Never pass a stale token back through local_token_list.  When
            # iLink returns binded_redirect, login_with_qrcode may otherwise
            # treat that token as reusable and complete a "relogin" with the
            # same invalid credential forever.
            current = self.bot_token
            current_base = self.baseurl
            had_authenticated_connection = bool(
                current or self._reauthentication_required
            )
            self._reauthenticating = had_authenticated_connection
            self.bot_token = ""
            self._token_ref[0] = ""
            self.runtime_state["bot_token"] = ""
            self.save_state()
            try:
                result = await self._login(reconnect=True)
                if result.get("already_connected"):
                    # Without a local token, binded_redirect is not a valid
                    # login result.  login_with_qrcode normally regenerates
                    # the QR; keep this guard for custom/test login hooks.
                    raise RuntimeError("iLink 返回 binded_redirect，但没有可复用的本地 token")
                if current:
                    await notify_lifecycle(self.http, "ilink/bot/msg/notifystop",
                                           current, current_base)
                await self._apply_login(result)
                self._reauthenticating = False
                self._reauthentication_required = False
                return result
            except Exception as exc:
                # A failed QR attempt must remain recoverable from the web
                # control plane.  Do not let browser-binding expiry reap a
                # previously authenticated session while it has no token.
                if had_authenticated_connection:
                    self._reauthentication_required = True
                self.qr_state.status = "error"
                self.qr_state.last_error = str(exc)
                await self._emit("relogin_failed", error=str(exc))
                raise
            finally:
                self._reauthenticating = False
                self.qr_state.is_regenerating = False

    async def request_relogin(self, reason: str = "manual") -> dict[str, Any]:
        # Always trace *who* asked for a relogin and *which* iLink owner it
        # targets.  The opaque session token (``session_id``) is logged as-is;
        # ``ilink_user_id`` is the WeChat-account-stable owner identity and is
        # truncated to its last 12 chars to keep logs short without leaking
        # the full sensitive value.  ``bot_id`` rotates per QR so it is safe.
        from utils.logging_setup import get_logger
        log_reconnect = get_logger("reconnect")
        owner = (self.ilink_user_id or "")[-12:]
        log_reconnect.info(
            "relogin_requested session=%s reason=%s bot_id=%s owner=%s "
            "stopped=%s authenticating=%s",
            self.session_id, reason, self.ilink_bot_id or "-",
            owner or "-", self._stopped, self._reauthenticating,
        )
        if self._stopped:
            raise RuntimeError("session stopped")
        async with self._relogin_lock:
            future = self._pending_relogin
            if future is None or future.done():
                future = asyncio.get_running_loop().create_future()
                self._pending_relogin = future
            self._initial_cancel.set()
            self._relogin_event.set()
        await self._emit("relogin_requested", reason=reason)
        return await future

    async def _relogin_listener(self) -> None:
        while True:
            await self._relogin_event.wait()
            self._relogin_event.clear()
            if self._initializing:
                await self._ready_event.wait()
            async with self._relogin_lock:
                future = self._pending_relogin
            if future is None or future.done():
                continue
            try:
                result = await self._reconnect()
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                async with self._relogin_lock:
                    if self._pending_relogin is future:
                        self._pending_relogin = None
                self._initial_cancel.clear()

    async def start(self) -> "BotSession":
        if self._started:
            return self
        if self._stopped:
            raise RuntimeError("session stopped")
        # Listener is intentionally created before QR login so /relink can
        # cancel and take over an initial QR flow.
        self._relogin_task = asyncio.create_task(self._relogin_listener(), name=f"relogin-{self.session_id}")
        self._initializing = True
        already_applied = False
        try:
            if self.bot_token:
                result = {"bot_token": self.bot_token, "baseurl": self.baseurl,
                          "ilink_bot_id": self.ilink_bot_id, "ilink_user_id": self.ilink_user_id}
            else:
                try:
                    # Cancel the in-flight 35s QR status request immediately
                    # when /relink asks the listener to take over.
                    login_task = asyncio.create_task(self._login(), name=f"initial-login-{self.session_id}")
                    cancel_task = asyncio.create_task(self._initial_cancel.wait(),
                                                      name=f"cancel-login-{self.session_id}")
                    done, _ = await asyncio.wait((login_task, cancel_task),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if cancel_task in done and not login_task.done():
                        login_task.cancel()
                    result = await login_task
                except asyncio.CancelledError:
                    # Only the explicit relogin signal may interrupt initial QR.
                    if not self._initial_cancel.is_set():
                        raise
                    self._initializing = False
                    self._ready_event.set()
                    async with self._relogin_lock:
                        pending = self._pending_relogin
                    if pending is None:
                        raise RuntimeError("initial login cancelled without relogin request")
                    result = await asyncio.shield(pending)
                    already_applied = True
                finally:
                    if "cancel_task" in locals() and not cancel_task.done():
                        cancel_task.cancel()
                    if "cancel_task" in locals():
                        await asyncio.gather(cancel_task, return_exceptions=True)
                    if "login_task" in locals() and not login_task.done():
                        login_task.cancel()
                        await asyncio.gather(login_task, return_exceptions=True)
        finally:
            self._initializing = False
            self._ready_event.set()
        if not already_applied:
            await self._apply_login(result, initial=True)
        self._started = True
        self._tasks["message"] = self._supervised("message", self._message_loop())
        # getupdates is the protocol keepalive.  A local wall-clock timer
        # must not force a user to scan a new QR while the token is valid.
        from bot import RECONNECT_CONFIG
        if RECONNECT_CONFIG.get("proactive_relogin", False):
            self._tasks["timer"] = self._supervised("timer", self._timer_loop())
        await self._emit("started")
        return self

    def _supervised(self, name: str, awaitable: Awaitable[Any]) -> asyncio.Task:
        async def runner() -> None:
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fatal.set()
                await self._emit("task_failed", task=name, error=str(exc))
                raise
        task = asyncio.create_task(runner(), name=f"{name}-{self.session_id}")
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return task

    async def _send_reliable(self, msg: dict[str, Any], to_id: str,
                             context_token: str, text: str, kind: str) -> None:
        """Send a transactional reply with a replay-stable client id."""
        from bot import API_TIMEOUT, api_post, base_info, ensure_business_success

        identity = self.ilink_bot_id or self.session_id
        client_id = "openclaw-weixin:" + hashlib.sha256(
            f"{identity}:{self._message_id(msg)}:{kind}".encode("utf-8")
        ).hexdigest()[:32]
        result = await api_post(
            self.http,
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
            self._token_ref[0],
            self._base_url_ref[0] or None,
            timeout=API_TIMEOUT,
        )
        ensure_business_success(result, "sendmessage")

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        from bot import (
            COMMANDS_MSG,
            INTRO_DETAIL_MSG,
            VOICE_TRANSCRIPT_UNAVAILABLE_MSG,
            extract_message_text,
            extract_voice_transcript,
            is_voice_message,
        )
        if not isinstance(msg, dict) or msg.get("message_type") != 1:
            return
        from_id = str(msg.get("from_user_id") or "")
        context = str(msg.get("context_token") or "")
        if not from_id or not context:
            return
        # iLink's voice_item.text is its server-side ASR result.  Once it is
        # present, this deliberately follows the exact same command/LLM path
        # as ordinary text; no voice capability explanation is sent.
        text = extract_message_text(msg).strip()
        has_voice = is_voice_message(msg)
        voice_text = extract_voice_transcript(msg).strip() if has_voice else ""
        if has_voice:
            from bot import log_msg
            log_msg.info(
                "recv msg from=%s type=%s len=%d preview=%r",
                from_id[-8:] if from_id else "-",
                "voice_transcript" if voice_text else "voice_without_transcript",
                len(text),
                text[:30],
            )
            # A voice message gets either the LLM answer or the actionable
            # missing-transcript feedback.  It must not be prefixed with the
            # long first-contact welcome message.
            self.welcomed_users.add(from_id)
        self.last_contact = {"from_id": from_id, "context_token": context}
        self.contexts[from_id] = context
        self.runtime_state["last_contact"] = dict(self.last_contact)
        self.runtime_state["contexts"] = dict(self.contexts)
        self.save_state()
        normalized = text.upper()
        if self.manual_reconnect_pending.get(from_id) and normalized in ("Y", "N"):
            self.manual_reconnect_pending.pop(from_id, None)
            await self._send_reliable(
                msg, from_id, context,
                "好的，正在重新连接..." if normalized == "Y" else "已取消重新连接",
                "reconnect-confirm",
            )
            if normalized == "Y":
                await self.request_relogin("manual")
            return
        if has_voice and not voice_text:
            await self._send_reliable(
                msg, from_id, context,
                VOICE_TRANSCRIPT_UNAVAILABLE_MSG,
                "voice-transcript-missing",
            )
            return

        if not text:
            return

        # Welcome is additive: the first user message must still reach the AI.
        if from_id not in self.welcomed_users:
            self.welcomed_users.add(from_id)
            await send_msg_safe(self.http, from_id, context, INTRO_DETAIL_MSG,
                                self._token_ref, self._base_url_ref)
        if normalized == "/HELP" or text == "/指令":
            await self._send_reliable(msg, from_id, context, COMMANDS_MSG, "help")
            return
        if normalized == "/TIME":
            from bot import RECONNECT_CONFIG
            if RECONNECT_CONFIG.get("proactive_relogin", False):
                remaining = max(
                    0.0,
                    self.login_time
                    + float(RECONNECT_CONFIG.get("session_duration", 86400))
                    - time.time(),
                )
                hours = int(remaining // 3600)
                minutes = int((remaining % 3600) // 60)
                seconds = int((remaining % 60))
                display = (
                    f"{hours} 小时 {minutes} 分钟"
                    if hours else f"{minutes} 分钟 {seconds} 秒"
                )
                text_reply = f"当前连接剩余时间：{display}"
            else:
                text_reply = "当前连接由后台持续维护，服务端 token 失效时会自动恢复。"
            await self._send_reliable(
                msg, from_id, context, text_reply, "time"
            )
            return
        if text == "/重新连接":
            self.manual_reconnect_pending[from_id] = True
            await self._send_reliable(
                msg, from_id, context, "确认要立即重新连接吗？\n回复 Y 确认重连 / N 取消",
                "reconnect-prompt",
            )
            return
        ticket = await get_typing_ticket_safe(self.http, from_id, context,
                                              self.typing_ticket_cache,
                                              self._token_ref, self._base_url_ref)
        typing = False
        try:
            typing = await send_typing_safe(self.http, from_id, ticket, 1,
                                            self._token_ref, self._base_url_ref)
            if self.ai is None:
                reply = "AI 服务尚未配置，请联系管理员。"
            else:
                loop = asyncio.get_running_loop()
                reply = await loop.run_in_executor(None, self.ai.chat, text)
            reply = str(reply or "抱歉，我暂时没有生成有效回复。").strip()
            # AI replies are part of the reliable message transaction.  Unlike
            # optional welcome/typing messages, delivery failure must raise so
            # the inbound cursor is not committed and the batch can replay.
            await self._send_reliable(msg, from_id, context, reply, "ai-reply")
        finally:
            if typing:
                await send_typing_safe(self.http, from_id, ticket, 2,
                                       self._token_ref, self._base_url_ref)

    @staticmethod
    def _message_id(msg: dict[str, Any]) -> str:
        for key in ("msg_id", "message_id", "client_id"):
            if msg.get(key):
                return str(msg[key])
        return hashlib.sha256(json.dumps(msg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def _drain_batch(self, messages: list[dict[str, Any]], cursor: str | None) -> None:
        """Drain one batch of inbound iLink messages.

        Holds ``self._drain_lock`` across the whole body so that an in-flight
        ``_reconnect()`` cannot clear the bot token mid-batch. If a stale-token
        (``-14``) error escapes from ``handle_message`` (typically via
        ``_send_reliable``), the unprocessed messages are preserved in
        ``runtime_state["pending_messages"]`` and the exception is re-raised
        so the outer ``_message_loop`` triggers ``request_relogin``; the
        ``get_updates_buf`` cursor is *not* advanced, so the batch replays
        after the new token is in place.
        """
        async with self._drain_lock:
            from bot import ILinkAPIError
            processed = list(map(str, self.runtime_state.get("processed_message_ids") or []))[-1000:]
            seen = set(processed)
            for idx, msg in enumerate(messages):
                mid = self._message_id(msg)
                if mid in seen:
                    continue
                # Public hook intentionally used here: deployments/tests may wrap
                # ``handle_message`` for tracing or idempotency.
                try:
                    await self.handle_message(msg)
                except ILinkAPIError as exc:
                    if getattr(exc, "is_stale_token", False):
                        # Preserve unprocessed messages for replay after relogin.
                        remaining = [
                            m for i, m in enumerate(messages)
                            if i >= idx and self._message_id(m) not in seen
                        ]
                        self.runtime_state["pending_messages"] = remaining
                        if cursor:
                            self.runtime_state["pending_cursor"] = cursor
                        self.save_state()
                        # Re-raise so _message_loop's outer except triggers
                        # request_relogin("stale-token") — preserves the
                        # existing relogin control flow.
                        raise
                    raise
                seen.add(mid)
                processed.append(mid)
                processed = processed[-1000:]
                self.runtime_state["processed_message_ids"] = processed
                self.save_state()
            self.runtime_state["pending_messages"] = []
            if cursor:
                self.runtime_state["get_updates_buf"] = cursor
            self.save_state()

    async def handle_message(self, msg: dict[str, Any]) -> None:
        """Process one inbound message in this account's namespace."""
        await self._handle_message(msg)

    async def process_update_batch(self, payload: dict[str, Any]) -> None:
        """Persist and process one getupdates batch transactionally.

        ``get_updates_buf`` is deliberately untouched until every message has
        either completed or been recognized by the persisted idempotency set.
        """
        messages = [m for m in (payload.get("msgs") or []) if isinstance(m, dict)]
        cursor = payload.get("get_updates_buf")
        self.runtime_state["pending_messages"] = messages
        self.runtime_state["pending_cursor"] = cursor
        self.save_state()
        await self._drain_batch(messages, cursor)

    async def _message_loop(self) -> None:
        from bot import (LONG_POLL_TIMEOUT, MAX_CONSECUTIVE_FAILURES, RETRY_DELAY,
                         BACKOFF_DELAY, api_post, base_info, ensure_business_success,
                         ILinkAPIError, MAX_LONG_POLL_TIMEOUT)
        if self.runtime_state.get("pending_messages"):
            await self._drain_batch(self.runtime_state["pending_messages"],
                                    self.runtime_state.get("pending_cursor"))
        failures = 0
        while True:
            token, base = self._token_ref[0], self._base_url_ref[0]
            if not token:
                await asyncio.sleep(1)
                continue
            cursor = str(self.runtime_state.get("get_updates_buf") or "")
            try:
                result = await api_post(self.http, "ilink/bot/getupdates",
                                        {"get_updates_buf": cursor, "base_info": base_info()},
                                        token, base, timeout=LONG_POLL_TIMEOUT,
                                        long_poll=True, fallback_cursor=cursor)
                if result.get("_timeout"):
                    continue
                if token != self._token_ref[0] or base != self._base_url_ref[0]:
                    continue
                ensure_business_success(result, "getupdates")
                failures = 0
                msgs = [m for m in (result.get("msgs") or []) if isinstance(m, dict)]
                new_cursor = result.get("get_updates_buf")
                if msgs:
                    self.runtime_state["pending_messages"] = msgs
                    self.runtime_state["pending_cursor"] = new_cursor or cursor
                    self.save_state()
                    await self._drain_batch(msgs, new_cursor or cursor)
                elif isinstance(new_cursor, str) and new_cursor:
                    self.runtime_state["get_updates_buf"] = new_cursor
                    self.save_state()
            except asyncio.CancelledError:
                raise
            except ILinkAPIError as exc:
                if getattr(exc, "is_stale_token", False):
                    try:
                        await self.request_relogin("stale-token")
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Keep the browser binding/session alive after a QR
                        # timeout so the user can press Switch and retry.
                        await asyncio.sleep(5)
                    continue
                failures += 1
                await asyncio.sleep(BACKOFF_DELAY if failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0

    async def _timer_loop(self) -> None:
        from bot import RECONNECT_CONFIG
        while True:
            target = (
                self.login_time
                + float(RECONNECT_CONFIG.get("session_duration", 86400))
                - float(RECONNECT_CONFIG.get("force_before", 1800))
            )
            remaining = target - time.time()
            if remaining > 0:
                await asyncio.sleep(min(300.0, max(1.0, remaining)))
                continue
            if self._stopped:
                return
            try:
                await self.request_relogin("session-expiry")
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values())
        else:
            await self._fatal.wait()

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            async with self._relogin_lock:
                pending = self._pending_relogin
                self._pending_relogin = None
                if pending is not None and not pending.done():
                    pending.cancel()
                self._relogin_event.set()
            tasks = list(self._tasks.values())
            if self._relogin_task is not None:
                tasks.append(self._relogin_task)
            current = asyncio.current_task()
            for task in tasks:
                if task is not current and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in tasks if t is not current), return_exceptions=True)
            # Trace every shutdown so the [user=-] notifystop line is no
            # longer orphaned.  ``caller`` is the asyncio task that asked for
            # shutdown — useful when multiple paths (manager.stop, sweeper,
            # _evict_failed, signal handler) converge here.
            from utils.logging_setup import get_logger
            log_reconnect = get_logger("reconnect")
            owner = (self.ilink_user_id or "")[-12:]
            caller_task = asyncio.current_task()
            caller_name = caller_task.get_name() if caller_task is not None else "-"
            log_reconnect.info(
                "session.stop session=%s owner=%s bot_id=%s "
                "had_token=%s authenticating=%s reauth_required=%s "
                "caller=%s",
                self.session_id, owner or "-", self.ilink_bot_id or "-",
                bool(self.bot_token), self._reauthenticating,
                self._reauthentication_required, caller_name,
            )
            if self.bot_token:
                try:
                    from bot import notify_lifecycle
                    await asyncio.wait_for(notify_lifecycle(self.http, "ilink/bot/msg/notifystop",
                                                            self.bot_token, self.baseurl), 11)
                except Exception:
                    pass
            self.save_state()
            self._started = False
