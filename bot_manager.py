"""Concurrency-safe container for shared-process :class:`BotSession` objects."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Optional

import aiohttp

from bot_session import BotSession
from utils.logging_setup import USER_LOG_CONTEXT

log = logging.getLogger("clawbot.bot_manager")


class BotManager:
    """Own N independent sessions without holding locks over network awaits."""

    def __init__(self, http: aiohttp.ClientSession, *, session_factory: Callable[..., BotSession] | None = None) -> None:
        self.http = http
        self.sessions: dict[str, BotSession] = {}
        self._starts: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._stopping = False
        self._maintenance: set[asyncio.Task] = set()
        # Resolve the module symbol at construction time; this is useful for
        # embedders that provide a specialized session implementation.
        self._factory = session_factory or BotSession

    async def get_or_create(self, session_id: str, config: dict[str, Any] | None = None,
                            *, wait_ready: bool = True, **kwargs: Any) -> BotSession:
        """Return a running session; concurrent callers share one start task.

        The lock protects only dictionary operations.  QR polling, AI setup and
        lifecycle HTTP requests all happen outside it, so one session cannot
        block another session's login for several minutes.
        """
        key = str(session_id)
        async with self._lock:
            if self._stopping:
                raise RuntimeError("manager stopping")
            existing = self.sessions.get(key)
            if existing is not None:
                if config is not None and dict(config) != getattr(existing, "config", {}):
                    raise ValueError(f"conflicting config for existing session {key!r}")
                session = existing
                starter = self._starts.get(key)
            else:
                external_on_event = kwargs.pop("on_event", None)

                async def managed_event(event: str, emitted_session: BotSession,
                                        payload: dict[str, Any]) -> None:
                    if event == "task_failed":
                        # 必须记日志：否则后台任务一死，会话被 evict，
                        # 症状只是「bot 不理人了」，看不出真正原因。
                        log.warning("session task failed sid=%s task=%s err=%s",
                                    key, payload.get("task"), payload.get("error"))
                        task = asyncio.create_task(
                            self._evict_failed(key, emitted_session),
                            name=f"evict-failed-{key}",
                        )
                        self._maintenance.add(task)
                        task.add_done_callback(self._maintenance_done)
                    if event == "logged_in":
                        # 同一微信账号只留一个会话。扫码会拿到新 token，旧会话
                        # 若还在轮询，两个 bot 抢同一个账号的消息流 —— 用户看到
                        # 的就是「有时候没人回」。
                        await self._evict_same_account(key, emitted_session)
                    if external_on_event is not None:
                        result = external_on_event(event, emitted_session, payload)
                        if inspect.isawaitable(result):
                            await result

                session = self._factory(
                    key, self.http, config or {}, on_event=managed_event, **kwargs
                )
                self.sessions[key] = session
                starter = asyncio.create_task(self._bootstrap(key, session), name=f"start-{key}")
                starter.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                self._starts[key] = starter
        if starter is None:
            return session
        if not wait_ready:
            # The HTTP portal needs a session object/QR state immediately; QR
            # confirmation must continue in the background.
            return session
        await asyncio.shield(starter)
        return session

    def _maintenance_done(self, task: asyncio.Task) -> None:
        self._maintenance.discard(task)
        if not task.cancelled():
            task.exception()

    async def _bootstrap(self, key: str, session: BotSession) -> None:
        """Start in the background and remove failed sessions atomically."""
        starter = asyncio.current_task()
        context_token = USER_LOG_CONTEXT.set(key)
        try:
            await session.start()
        except BaseException:
            async with self._lock:
                if self.sessions.get(key) is session:
                    self.sessions.pop(key, None)
                if self._starts.get(key) is starter:
                    self._starts.pop(key, None)
            await session.stop()
            raise
        finally:
            async with self._lock:
                if self._starts.get(key) is starter:
                    self._starts.pop(key, None)
            USER_LOG_CONTEXT.reset(context_token)

    async def _evict_failed(self, key: str, session: BotSession) -> None:
        """Remove a session whose permanent worker task has died."""
        context_token = USER_LOG_CONTEXT.set(key)
        async with self._lock:
            if self.sessions.get(key) is not session:
                USER_LOG_CONTEXT.reset(context_token)
                return
            self.sessions.pop(key, None)
            starter = self._starts.pop(key, None)
        current = asyncio.current_task()
        if starter is not None and starter is not current and not starter.done():
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)
        try:
            await session.stop()
        finally:
            USER_LOG_CONTEXT.reset(context_token)

    async def create_background(self, session_id: str, config: dict[str, Any] | None = None,
                                **kwargs: Any) -> BotSession:
        """Create/register immediately; QR login proceeds in a supervised task."""
        return await self.get_or_create(session_id, config, wait_ready=False, **kwargs)

    def get(self, session_id: str) -> Optional[BotSession]:
        return self.sessions.get(str(session_id))

    async def stop(self, session_id: str) -> None:
        key = str(session_id)
        context_token = USER_LOG_CONTEXT.set(key)
        try:
            async with self._lock:
                session = self.sessions.pop(key, None)
                starter = self._starts.pop(key, None)
            # Never hold _lock while cancelling a QR/login await.
            if starter is not None and not starter.done():
                starter.cancel()
                await asyncio.gather(starter, return_exceptions=True)
            if session is not None:
                await session.stop()
        finally:
            USER_LOG_CONTEXT.reset(context_token)

    async def stop_all(self) -> None:
        async with self._lock:
            self._stopping = True
            sessions = list(self.sessions.values())
            starts = list(self._starts.values())
            self.sessions.clear()
            self._starts.clear()
        for starter in starts:
            if not starter.done():
                starter.cancel()
        if starts:
            await asyncio.gather(*starts, return_exceptions=True)
        if sessions:
            await asyncio.gather(*(s.stop() for s in sessions), return_exceptions=True)
        maintenance = [task for task in self._maintenance if not task.done()]
        if maintenance:
            await asyncio.gather(*maintenance, return_exceptions=True)

    async def touch(self, session_id: str) -> None:
        session = self.get(session_id)
        if session is not None:
            session.last_used_at = time.time()

    async def _evict_same_account(self, keep_key: str, keep_session: Any) -> None:
        """踢掉与 ``keep_session`` 同一个微信账号的其它会话。

        判据是 ``ilink_user_id``（微信账号稳定 ID），不是 session_id ——
        重新扫码会得到新的 session_id，但账号还是同一个。
        """
        uid = str(getattr(keep_session, "ilink_user_id", "") or "")
        if not uid:
            return
        for other_key, other in list(self.sessions.items()):
            if other_key == keep_key:
                continue
            if str(getattr(other, "ilink_user_id", "") or "") != uid:
                continue
            await self.stop(other_key)
