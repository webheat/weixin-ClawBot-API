"""Concurrency and lifecycle contracts for the shared BotManager."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from .conftest import FakeHTTP


manager_module = pytest.importorskip("bot_manager")
BotManager = getattr(manager_module, "BotManager", None)
if BotManager is None:
    pytest.skip("bot_manager.BotManager is not implemented yet", allow_module_level=True)


class ControlledSession:
    """Session double with controllable startup and observable shutdown."""

    starts: defaultdict[str, int] = defaultdict(int)
    stops: defaultdict[str, int] = defaultdict(int)
    start_started: dict[str, asyncio.Event] = {}
    start_gate: dict[str, asyncio.Event] = {}
    fail_first: set[str] = set()

    def __init__(self, user_id: str, session: Any, config: dict[str, Any], **kwargs: Any) -> None:
        self.user_id = user_id
        self.session = session
        self.config = config
        self.on_event = kwargs.get("on_event")
        self.last_used_at = 0.0

    @classmethod
    def reset(cls) -> None:
        cls.starts.clear()
        cls.stops.clear()
        cls.start_started.clear()
        cls.start_gate.clear()
        cls.fail_first.clear()

    async def start(self) -> None:
        type(self).starts[self.user_id] += 1
        type(self).start_started.setdefault(self.user_id, asyncio.Event()).set()
        if self.user_id in type(self).fail_first and type(self).starts[self.user_id] == 1:
            raise RuntimeError("simulated startup failure")
        gate = type(self).start_gate.get(self.user_id)
        if gate is not None:
            await gate.wait()

    async def stop(self) -> None:
        type(self).stops[self.user_id] += 1


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch):
    ControlledSession.reset()
    try:
        # Pass the factory explicitly: BotManager's documented default is
        # captured at function-definition time and is not monkeypatchable.
        mgr = BotManager(FakeHTTP(), session_factory=ControlledSession)
    except TypeError as exc:
        # Compatibility path for an early migration implementation that only
        # accepts the HTTP client and resolves BotSession at call time.
        monkeypatch.setattr(manager_module, "BotSession", ControlledSession, raising=False)
        try:
            mgr = BotManager(FakeHTTP())
        except TypeError:
            pytest.fail(f"BotManager constructor does not implement the documented API: {exc}")
    yield mgr


async def _cleanup(mgr: Any) -> None:
    stop_all = getattr(mgr, "stop_all", None)
    if stop_all is not None:
        await stop_all()


@pytest.mark.asyncio
async def test_same_user_concurrent_get_or_create_is_deduplicated(manager: Any) -> None:
    ControlledSession.start_gate["alice"] = asyncio.Event()
    ControlledSession.start_started["alice"] = asyncio.Event()
    tasks = [asyncio.create_task(manager.get_or_create("alice", {"owner": "a"})) for _ in range(5)]
    await asyncio.wait_for(ControlledSession.start_started["alice"].wait(), timeout=1)
    await asyncio.sleep(0)
    try:
        assert ControlledSession.starts["alice"] == 1, "same user must have one startup in flight"
        assert not any(task.done() for task in tasks), (
            "callers joining an in-flight startup must await the same readiness barrier"
        )
    finally:
        ControlledSession.start_gate["alice"].set()
        result = await asyncio.gather(*tasks, return_exceptions=True)
        successful = [item for item in result if not isinstance(item, BaseException)]
        if successful:
            assert len({id(item) for item in successful}) == 1, (
                "same user must receive one BotSession instance"
            )
        await _cleanup(manager)


@pytest.mark.asyncio
async def test_existing_session_rejects_conflicting_config(manager: Any) -> None:
    await manager.get_or_create("alice", {"owner": "a"})
    with pytest.raises(ValueError, match="conflicting config"):
        await manager.get_or_create("alice", {"owner": "b"})
    await _cleanup(manager)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_duplicate_shared_start(manager: Any) -> None:
    ControlledSession.start_gate["alice"] = asyncio.Event()
    ControlledSession.start_started["alice"] = asyncio.Event()
    first = asyncio.create_task(manager.get_or_create("alice", {"owner": "a"}))
    await asyncio.wait_for(ControlledSession.start_started["alice"].wait(), timeout=1)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    second = asyncio.create_task(manager.get_or_create("alice", {"owner": "a"}))
    await asyncio.sleep(0)
    assert not second.done()
    assert ControlledSession.starts["alice"] == 1
    ControlledSession.start_gate["alice"].set()
    await asyncio.wait_for(second, timeout=1)
    await _cleanup(manager)


@pytest.mark.asyncio
async def test_failed_permanent_worker_is_evicted(manager: Any) -> None:
    session = await manager.get_or_create("alice", {"owner": "a"})
    assert callable(session.on_event)
    await session.on_event("task_failed", session, {"task": "message"})
    for _ in range(20):
        if "alice" not in manager.sessions:
            break
        await asyncio.sleep(0)
    assert "alice" not in manager.sessions
    assert ControlledSession.stops["alice"] == 1
    await _cleanup(manager)


@pytest.mark.asyncio
async def test_different_users_do_not_block_on_a_slow_start(manager: Any) -> None:
    """One QR scan/login cannot hold a global manager lock for every tenant."""

    ControlledSession.start_gate["alice"] = asyncio.Event()
    ControlledSession.start_started["alice"] = asyncio.Event()
    alice = asyncio.create_task(manager.get_or_create("alice", {"owner": "a"}))
    await asyncio.wait_for(ControlledSession.start_started["alice"].wait(), timeout=1)

    try:
        bob = asyncio.create_task(manager.get_or_create("bob", {"owner": "b"}))
        bob_session = await asyncio.wait_for(bob, timeout=0.5)
        assert bob_session.user_id == "bob"
        assert ControlledSession.starts["bob"] == 1
    finally:
        ControlledSession.start_gate["alice"].set()
        await asyncio.gather(alice, return_exceptions=True)
        await _cleanup(manager)


@pytest.mark.asyncio
async def test_failed_start_is_removed_and_can_be_retried(manager: Any) -> None:
    ControlledSession.fail_first.add("alice")
    with pytest.raises(RuntimeError, match="startup failure"):
        await manager.get_or_create("alice", {"owner": "a"})
    sessions = getattr(manager, "sessions", {})
    assert "alice" not in sessions, "failed startup must not leave a zombie session"

    second = await manager.get_or_create("alice", {"owner": "a"})
    assert second.user_id == "alice"
    assert ControlledSession.starts["alice"] == 2
    await _cleanup(manager)


@pytest.mark.asyncio
async def test_stop_is_idempotent(manager: Any) -> None:
    session = await manager.get_or_create("alice", {"owner": "a"})
    await manager.stop("alice")
    await manager.stop("alice")
    await manager.stop("unknown")
    assert ControlledSession.stops["alice"] == 1
    assert "alice" not in getattr(manager, "sessions", {})
    # stop_all after individual stop must remain harmless.
    await _cleanup(manager)
