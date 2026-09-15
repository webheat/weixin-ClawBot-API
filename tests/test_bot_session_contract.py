"""Contract tests for one isolated multi-tenant BotSession.

These tests intentionally import the new module lazily.  During the migration
the old single-tenant bot remains runnable, while the tests become active as
soon as ``bot_session.py`` is introduced.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from .conftest import FakeAI, FakeHTTP, message


session_module = pytest.importorskip("bot_session")
BotSession = getattr(session_module, "BotSession", None)
if BotSession is None:  # pragma: no cover - useful migration diagnostic
    pytest.skip("bot_session.BotSession is not implemented yet", allow_module_level=True)


def _make_session(
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    config: dict[str, Any] | None = None,
    state_file: Any = None,
):
    """Construct the documented BotSession shape without touching real state."""

    state = {
        "bot_token": "",
        "baseurl": "https://example.invalid",
        "contexts": {},
        "last_contact": {"from_id": None, "context_token": None},
        "get_updates_buf": "old-cursor",
    }
    cfg = config or {
        "llm": {"provider": "deepseek", "api_key": f"key-{user_id}"},
        "state_dir": "test-state",
    }
    try:
        kwargs = {"state_file": state_file} if state_file is not None else {}
        sess = BotSession(user_id, FakeHTTP(), cfg, **kwargs)
    except TypeError:
        # A few implementations make the shared HTTP client keyword-only.
        try:
            kwargs = {"state_file": state_file} if state_file is not None else {}
            sess = BotSession(user_id=user_id, session=FakeHTTP(), config=cfg, **kwargs)
        except TypeError as exc:  # make an incomplete migration explicit
            pytest.fail(f"BotSession constructor does not implement the documented API: {exc}")
    sess._contract_ai = FakeAI()
    # Accept either of the common names while the implementation settles.
    for attr in ("ai", "ai_client", "_ai"):
        if hasattr(sess, attr):
            setattr(sess, attr, sess._contract_ai)
    return sess


def _install_reply_spy(monkeypatch: pytest.MonkeyPatch, sess: Any) -> list[str]:
    """Patch protocol-level send helpers and return captured text replies."""

    sent: list[str] = []

    async def send_reply(*args: Any, **kwargs: Any) -> bool:
        # send_msg_safe(session, to, context, text, ...)
        text_value = kwargs.get("text")
        if text_value is None and len(args) >= 4:
            text_value = args[3]
        if text_value is None and args and isinstance(args[-1], str):
            text_value = args[-1]
        sent.append(str(text_value or ""))
        return True

    # BotSession imports protocol helpers lazily from ``bot`` so that the old
    # CLI and the new manager share one protocol implementation.
    try:
        import bot as protocol
    except ImportError as exc:  # pragma: no cover - dependency setup issue
        pytest.skip(f"protocol dependencies unavailable: {exc}")
    for name in ("send_msg_safe", "send_message", "_send_message", "send_reply", "_send_reply"):
        if hasattr(protocol, name):
            monkeypatch.setattr(protocol, name, send_reply)
    for name in ("send_message", "_send_message", "send_reply", "_send_reply"):
        if hasattr(sess, name) and callable(getattr(sess, name)):
            monkeypatch.setattr(sess, name, send_reply)

    async def typing(*_: Any, **__: Any) -> bool:
        return True

    async def ticket(*_: Any, **__: Any) -> str:
        return "typing-ticket"

    for name in ("send_typing_safe", "get_typing_ticket_safe"):
        if hasattr(protocol, name):
            monkeypatch.setattr(protocol, name, typing if name == "send_typing_safe" else ticket)
    if hasattr(protocol, "api_post"):
        async def api_post(*args: Any, **__: Any) -> dict[str, Any]:
            if len(args) >= 3 and args[1] == "ilink/bot/sendmessage":
                items = args[2].get("msg", {}).get("item_list", [])
                if items:
                    sent.append(str(items[0].get("text_item", {}).get("text", "")))
            return {}
        monkeypatch.setattr(protocol, "api_post", api_post)
    return sent


@pytest.mark.asyncio
async def test_first_message_is_processed_instead_of_being_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A first contact may get a welcome, but its actual question must reach AI."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    handler = getattr(sess, "handle_message", None)
    if handler is None:
        pytest.skip("BotSession.handle_message is not implemented yet")

    # Some implementations call a method-level AI hook; make all documented
    # injection points deterministic without changing production code.
    for name in ("_chat", "chat_ai", "call_ai"):
        if hasattr(sess, name):
            monkeypatch.setattr(sess, name, sess._contract_ai.chat)
    await handler(message("first question"))

    assert sess._contract_ai.calls == ["first question"], (
        "the first message must be sent to the AI even when a welcome is emitted"
    )
    assert any("AI reply" in item for item in sent), "the first question must receive an AI reply"


@pytest.mark.asyncio
async def test_lowercase_help_and_time_are_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    help_message = message("/help")
    help_message["message_id"] = "help-1"
    time_message = message("/time")
    time_message["message_id"] = "time-1"
    await sess.handle_message(help_message)
    await sess.handle_message(time_message)
    assert sess._contract_ai.calls == []
    assert any("指令" in item for item in sent)
    assert any("剩余时间" in item for item in sent)


def _find_batch_processor(sess: Any):
    """Locate the small, unit-testable update-batch hook used by implementations."""

    for name in (
        "process_update_batch",
        "_process_update_batch",
        "process_updates",
        "_process_updates",
        "handle_updates",
        "message_loop_once",
    ):
        fn = getattr(sess, name, None)
        if callable(fn):
            return fn
    return None


async def _call_batch_processor(fn: Any, payload: dict[str, Any]) -> Any:
    """Call a batch hook using its conventional result/updates argument."""

    sig = inspect.signature(fn)
    required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
    if len(required) == 0:
        result = fn()
    elif len(required) == 1:
        result = fn(payload)
    else:
        pytest.skip("batch processor requires implementation-specific dependencies")
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.mark.asyncio
async def test_cursor_is_committed_only_after_all_messages_succeed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A crash during handling must leave the old cursor replayable."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    processor = _find_batch_processor(sess)
    if processor is None:
        pytest.skip("BotSession has no unit-testable update-batch processor yet")

    state = getattr(sess, "runtime_state", None)
    if not isinstance(state, dict):
        state = {"get_updates_buf": "old-cursor"}
        sess.runtime_state = state
    state["get_updates_buf"] = "old-cursor"
    if hasattr(sess, "get_updates_buf"):
        sess.get_updates_buf = "old-cursor"
    saves: list[str] = []

    def save_state() -> None:
        saves.append(str(getattr(sess, "runtime_state", {}).get("get_updates_buf", "")))

    if hasattr(sess, "save_state"):
        monkeypatch.setattr(sess, "save_state", save_state)

    async def fail_handler(_: dict[str, Any]) -> None:
        raise RuntimeError("simulated handler crash")

    monkeypatch.setattr(sess, "handle_message", fail_handler)
    payload = {"get_updates_buf": "new-cursor", "msgs": [message("must replay")]} 
    with pytest.raises(RuntimeError):
        await _call_batch_processor(processor, payload)
    assert state.get("get_updates_buf") == "old-cursor", "cursor advanced before message success"

    async def ok_handler(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(sess, "handle_message", ok_handler)
    await _call_batch_processor(processor, payload)
    assert state.get("get_updates_buf") == "new-cursor", "cursor was not committed after success"


@pytest.mark.asyncio
async def test_reply_delivery_failure_keeps_batch_replayable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.runtime_state["get_updates_buf"] = "old-cursor"
    _install_reply_spy(monkeypatch, sess)
    import bot as protocol

    async def failed_post(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if len(args) >= 2 and args[1] == "ilink/bot/sendmessage":
            raise protocol.ILinkAPIError("send failed", path="sendmessage")
        return {}

    monkeypatch.setattr(protocol, "api_post", failed_post)
    payload = {"get_updates_buf": "new-cursor", "msgs": [message("retry me")]}
    with pytest.raises(protocol.ILinkAPIError):
        await sess.process_update_batch(payload)
    assert sess.runtime_state["get_updates_buf"] == "old-cursor"
    assert sess.runtime_state["pending_messages"]
    assert not sess.runtime_state["processed_message_ids"]

    async def successful_post(*_: Any, **__: Any) -> dict[str, Any]:
        return {"ret": 0}

    monkeypatch.setattr(protocol, "api_post", successful_post)
    await sess.process_update_batch(payload)
    assert sess.runtime_state["get_updates_buf"] == "new-cursor"
    assert not sess.runtime_state["pending_messages"]


def test_two_sessions_have_no_mutable_per_user_state_in_common(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Token, contexts, QR, persistence state, and AI config are per session."""

    a = _make_session(monkeypatch, "alice", {"ai": {"api_key": "a"}}, tmp_path / "alice.json")
    b = _make_session(monkeypatch, "bob", {"ai": {"api_key": "b"}}, tmp_path / "bob.json")

    required = ("bot_token", "contexts", "qr_state", "runtime_state")
    missing = [name for name in required if not hasattr(a, name) or not hasattr(b, name)]
    if missing:
        pytest.skip(f"BotSession isolation fields not implemented yet: {', '.join(missing)}")

    a.bot_token = "token-a"
    a.baseurl = "https://a.invalid"
    a.contexts["contact"] = "ctx-a"
    a.qr_state.status = "qr_pending"
    a.runtime_state["get_updates_buf"] = "cursor-a"
    a.config["ai"]["api_key"] = "changed-a"

    assert b.bot_token != a.bot_token
    assert getattr(b, "baseurl", "") != a.baseurl
    assert "contact" not in b.contexts
    assert getattr(b.qr_state, "status", None) != a.qr_state.status
    assert b.runtime_state.get("get_updates_buf") != "cursor-a"
    assert b.config["ai"]["api_key"] == "b"
    assert a.qr_state is not b.qr_state
    assert a.runtime_state is not b.runtime_state
    assert a.config is not b.config
    if hasattr(a, "state_file") and hasattr(b, "state_file"):
        assert a.state_file != b.state_file


def test_ima_and_fallback_options_are_scoped_to_one_session() -> None:
    cfg = {
        "provider": "dusapi",
        "api_key": "llm-key",
        "base_url": "https://llm.invalid",
        "model": "model",
        "prompt": "prompt",
        "ima_env": {
            "IMA_ILINK_CLIENT_ID": "client-a",
            "IMA_ILINK_API_KEY": "ima-a",
            "IMA_ILINK_SEARCH_LIMIT": "7",
        },
        "runtime_env": {
            "CLAWBOT_LOCAL_FALLBACK": "1",
            "CLAWBOT_LOCAL_KB_DIR": "kb-a",
            "CLAWBOT_LLM_CAVEAT": "0",
        },
    }
    ai = BotSession._make_ai(cfg)
    assert ai._ima.cfg.client_id == "client-a"
    assert ai._ima.cfg.search_limit == 7
    assert ai._env["CLAWBOT_LOCAL_KB_DIR"] == "kb-a"
