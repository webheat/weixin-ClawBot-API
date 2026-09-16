"""Contract tests for one isolated multi-tenant BotSession.

These tests intentionally import the new module lazily.  During the migration
the old single-tenant bot remains runnable, while the tests become active as
soon as ``bot_session.py`` is introduced.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from .conftest import FakeAI, FakeHTTP, message


session_module = pytest.importorskip("bot_session")
BotSession = getattr(session_module, "BotSession", None)
if BotSession is None:  # pragma: no cover - useful migration diagnostic
    pytest.skip("bot_session.BotSession is not implemented yet", allow_module_level=True)


def _make_session(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
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
        "llm": {"provider": "deepseek", "api_key": f"key-{session_id}"},
        "state_dir": "test-state",
    }
    try:
        kwargs = {"state_file": state_file} if state_file is not None else {}
        sess = BotSession(session_id, FakeHTTP(), cfg, **kwargs)
    except TypeError:
        # A few implementations make the shared HTTP client keyword-only.
        try:
            kwargs = {"state_file": state_file} if state_file is not None else {}
            sess = BotSession(session_id=session_id, session=FakeHTTP(), config=cfg, **kwargs)
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
async def test_voice_transcript_uses_the_same_ai_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """iLink voice_item.text is treated as the user's text input."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    voice_message = message("ignored raw placeholder")
    voice_message["message_id"] = "voice-1"
    voice_message["item_list"] = [{
        "type": 3,
        "voice_item": {
            "encode_type": 6,
            "text": "这是语音转写的问题",
        },
    }]

    await sess.handle_message(voice_message)

    assert sess._contract_ai.calls == ["这是语音转写的问题"]
    assert any("AI reply" in item for item in sent)
    assert not any("翼claw" in item for item in sent)


@pytest.mark.asyncio
async def test_voice_transcript_does_not_break_forwarded_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A standard voice transcript plus card metadata still uses the AI route."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    voice_message = message("ignored raw placeholder")
    voice_message["message_id"] = "voice-compat-1"
    voice_message["item_list"] = [{
        "type": 3,
        "voice_item": {"text": "语音问题"},
        "title": "附带卡片标题",
    }]

    await sess.handle_message(voice_message)

    assert sess._contract_ai.calls == ["语音问题\n附带卡片标题"]
    assert any("AI reply" in item for item in sent)


@pytest.mark.asyncio
async def test_voice_transcript_can_enter_command_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A slash command contained in voice transcription is still a command."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    voice_message = message("/help")
    voice_message["message_id"] = "voice-help-1"
    voice_message["item_list"] = [{
        "type": 3,
        "voice_item": {"text": "/help"},
    }]

    await sess.handle_message(voice_message)

    assert sess._contract_ai.calls == []
    assert any("指令" in item for item in sent)


@pytest.mark.asyncio
async def test_voice_without_transcript_gets_actionable_feedback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A voice without iLink ASR text gets a useful retry instruction."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    voice_message = message("ignored raw placeholder")
    voice_message["message_id"] = "voice-no-transcript-1"
    voice_message["item_list"] = [{
        "type": 3,
        "voice_item": {"encode_type": 6, "media": {"encrypt_query_param": "x"}},
    }]

    await sess.handle_message(voice_message)

    assert sess._contract_ai.calls == []
    assert any("重新发送一次语音" in item and "转文字" in item for item in sent)
    assert not any("当前版本支持文字" in item for item in sent)
    assert not any("翼claw" in item for item in sent)


@pytest.mark.asyncio
async def test_untranscribed_voice_with_metadata_still_gets_feedback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Card metadata must not make an untranscribed voice look transcribed."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sent = _install_reply_spy(monkeypatch, sess)
    voice_message = message("ignored raw placeholder")
    voice_message["message_id"] = "voice-no-transcript-metadata-1"
    voice_message["item_list"] = [{
        "type": 3,
        "voice_item": {"media": {"encrypt_query_param": "x"}},
        "title": "附带标题",
    }]

    await sess.handle_message(voice_message)

    assert sess._contract_ai.calls == []
    assert any("重新发送一次语音" in item for item in sent)


@pytest.mark.asyncio
async def test_reconnect_does_not_reuse_stale_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A -14 recovery must request a genuinely new iLink credential."""

    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token

    observed: dict[str, Any] = {}

    async def fake_login(*, reconnect: bool = False) -> dict[str, Any]:
        observed["reconnect"] = reconnect
        observed["token_during_login"] = sess.bot_token
        observed["saved_token_during_login"] = sess.runtime_state.get("bot_token")
        observed["persisted_token_during_login"] = json.loads(
            sess.state_file.read_text(encoding="utf-8")
        ).get("bot_token")
        observed["authenticated_during_login"] = sess.has_authenticated_connection
        return {
            "bot_token": "fresh-token",
            "baseurl": "https://new.invalid",
            "ilink_bot_id": "bot-id",
        }

    async def fake_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(sess, "_login", fake_login)
    import bot as protocol
    monkeypatch.setattr(protocol, "notify_lifecycle", fake_notify)

    result = await sess._reconnect()

    assert observed == {
        "reconnect": True,
        "token_during_login": "",
        "saved_token_during_login": "",
        "persisted_token_during_login": "",
        "authenticated_during_login": True,
    }
    assert result["bot_token"] == "fresh-token"
    assert sess.bot_token == "fresh-token"


@pytest.mark.asyncio
async def test_concurrent_relogin_requests_share_one_reconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Concurrent server-driven relogin requests share one _reconnect.

    Only ``stale-token`` (and other server-driven reasons) still go through
    the listener / ``_reconnect`` clearing path.  ``web switch`` and
    ``manual`` now use the decoupled path (the current bot_token stays
    alive while the QR is pending); see
    docs/2026-09-16_DECOUPLED_QR_SWITCH.md §3.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "authenticated-token"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"bot_token": "fresh-token"}

    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)
    listener = asyncio.create_task(sess._relogin_listener())
    try:
        first = asyncio.create_task(sess.request_relogin("stale-token"))
        second = asyncio.create_task(sess.request_relogin("session-expiry"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert calls == 1
        release.set()
        assert await asyncio.gather(first, second) == [
            {"bot_token": "fresh-token"}, {"bot_token": "fresh-token"}
        ]
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_failure_restores_previous_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A failed _reconnect must restore bot_token so /api/* stays alive.

    Regression test for the "half-dead" state observed on 2026-09-16:
    after MAX_QR_REFRESH_COUNT, bot_token stayed empty and the long-poll
    loop spun forever on `if not token: await asyncio.sleep(1)`. With the
    rollback in _reconnect, the bot keeps the previous token and the loop
    can still call iLink (which will then return -14 again, triggering a
    fresh request_relogin cycle). See docs/2026-09-16 §5 (option D).
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token

    async def failing_login(*, reconnect: bool = False) -> dict[str, Any]:
        raise RuntimeError("二维码多次失效或登录失败，请稍后重试。")

    async def noop_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(sess, "_login", failing_login)
    import bot as protocol
    monkeypatch.setattr(protocol, "notify_lifecycle", noop_notify)

    with pytest.raises(RuntimeError, match="二维码多次失效"):
        await sess._reconnect()

    assert sess.bot_token == "old-token"
    assert sess._token_ref[0] == "old-token"
    assert sess.runtime_state["bot_token"] == "old-token"
    # The web UI must still observe the failure surface.
    assert sess.qr_state.status == "error"
    assert "二维码多次失效" in sess.qr_state.last_error


@pytest.mark.asyncio
async def test_reconnect_binded_redirect_does_not_rollback_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """binded_redirect failures must NOT restore bot_token (78e1dfd invariant).

    Without the substring guard, rolling back the token would re-arm the
    ``login_with_qrcode already_connected`` reuse loop on the next attempt.
    See docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md §4.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token

    async def binded_redirect_login(*, reconnect: bool = False) -> dict[str, Any]:
        raise RuntimeError("iLink 返回 binded_redirect，但没有可复用的本地 token")

    async def noop_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(sess, "_login", binded_redirect_login)
    import bot as protocol
    monkeypatch.setattr(protocol, "notify_lifecycle", noop_notify)

    with pytest.raises(RuntimeError, match="binded_redirect"):
        await sess._reconnect()

    # Token must stay cleared — restoring it would re-arm the
    # login_with_qrcode already_connected reuse loop on the next attempt.
    assert sess.bot_token == ""
    assert sess._token_ref[0] == ""
    assert sess.runtime_state["bot_token"] == ""
    # _reauthentication_required should still be set so the binding TTL
    # does not reap the session during this transient no-token window.
    assert sess._reauthentication_required is True


@pytest.mark.asyncio
async def test_scheduled_retry_relogin_calls_reconnect_on_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """After request_relogin('stale-token') fails, background retries call
    _reconnect on the 60s/300s/900s schedule until it succeeds.

    See docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md §5 (option C).
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"

    sleep_delays: list[float] = []
    call_count = 0
    done_event = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        # Skip event-loop yields (delay=0) used to give background tasks
        # a chance to run; only record real backoff sleeps.
        if delay < 1:
            return
        sleep_delays.append(delay)

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            done_event.set()
            return {"bot_token": "fresh-token", "ilink_bot_id": "new-bot"}
        raise RuntimeError(f"transient QR failure #{call_count}")

    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        with pytest.raises(Exception, match="transient QR failure #1"):
            await sess.request_relogin("stale-token")
        await asyncio.wait_for(done_event.wait(), timeout=2)
        await asyncio.sleep(0)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    assert call_count == 3, f"_reconnect called {call_count} times, expected 3"
    assert sleep_delays == [60, 300], (
        f"expected backoff [60, 300] (before 2nd & 3rd attempt), got {sleep_delays}"
    )
    assert sess._pending_relogin is None


@pytest.mark.asyncio
async def test_scheduled_retry_relogin_skipped_for_user_driven_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """User-driven reasons ('web switch', 'manual') MUST NOT trigger background
    retries — we must respect explicit user cancellation.

    ``request_relogin`` for these reasons uses the new decoupled path
    (see :py:meth:`BotSession._request_qr_switch`); the listener /
    ``_reconnect`` / ``_scheduled_retry_relogin`` machinery is never touched.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"

    reconnect_calls = 0
    initiate_calls = 0
    sleep_calls: list[float] = []

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal reconnect_calls
        reconnect_calls += 1
        raise RuntimeError("user cancelled via web")

    async def fake_initiate() -> str:
        nonlocal initiate_calls
        initiate_calls += 1
        raise RuntimeError("user cancelled via web")

    async def fake_sleep(delay: float) -> None:
        if delay < 1:
            return
        sleep_calls.append(delay)

    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)
    monkeypatch.setattr(sess, "_initiate_qr_switch", fake_initiate)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        with pytest.raises(Exception, match="user cancelled via web"):
            await sess.request_relogin("web switch")
        await asyncio.sleep(0.05)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    assert reconnect_calls == 0, (
        f"decoupled path must not call _reconnect, got {reconnect_calls}"
    )
    assert initiate_calls == 1, (
        f"expected one _initiate_qr_switch call, got {initiate_calls}"
    )
    assert sleep_calls == [], f"expected no backoff sleeps, got {sleep_calls}"


@pytest.mark.asyncio
async def test_manual_relogin_does_not_schedule_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """'/重新连接' command → request_relogin('manual') → decoupled path,
    no retry on failure.

    User-driven reason takes the new decoupled path (no listener,
    no scheduled retries)."""
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"

    reconnect_calls = 0
    initiate_calls = 0

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal reconnect_calls
        reconnect_calls += 1
        raise RuntimeError("manual relogin failed")

    async def fake_initiate() -> str:
        nonlocal initiate_calls
        initiate_calls += 1
        raise RuntimeError("manual relogin failed")

    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)
    monkeypatch.setattr(sess, "_initiate_qr_switch", fake_initiate)

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        with pytest.raises(Exception, match="manual relogin failed"):
            await sess.request_relogin("manual")
        await asyncio.sleep(0.05)
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    assert reconnect_calls == 0
    assert initiate_calls == 1


@pytest.mark.asyncio
async def test_scheduled_retry_relogin_exhausts_after_three_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """All three retry attempts fail → no further _reconnect calls; backoff
    schedule is exactly (60, 300, 900)."""
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"

    sleep_delays: list[float] = []
    call_count = 0

    async def fake_sleep(delay: float) -> None:
        # Skip event-loop yields (delay=0); only record real backoff sleeps.
        if delay < 1:
            return
        sleep_delays.append(delay)

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"persistent failure #{call_count}")

    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        with pytest.raises(Exception, match="persistent failure #1"):
            await sess.request_relogin("session-expiry")
        for _ in range(30):
            await asyncio.sleep(0)
            if call_count >= 4:
                break
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    assert call_count == 4, (
        f"expected 4 _reconnect calls (1 initial + 3 retries), got {call_count}"
    )
    assert sleep_delays == [60, 300, 900], (
        f"expected backoff [60, 300, 900], got {sleep_delays}"
    )


# ---------------------------------------------------------------------------
# Decoupled QR switch tests (docs/2026-09-16_DECOUPLED_QR_SWITCH.md §4)
#
# "web switch" and "manual" reasons must NOT touch the current bot_token
# until the user actually scans the new QR.  The long-poll connection it
# backs keeps running on the old token throughout the QR wait.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qr_switch_initiate_does_not_clear_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``_initiate_qr_switch`` fetches a fresh QR without clearing bot_token.

    The current ``bot_token`` and ``_token_ref[0]`` must stay "old-token"
    before, during, and after the call; only the QR state and the persisted
    ``ilink_bot_id`` etc. (which the existing flow already preserves) are
    allowed to be touched.  No ``_apply_login`` / ``_reconnect`` call is
    expected because the user has not scanned yet.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token
    sess.ilink_bot_id = "old-bot-id"

    apply_calls = 0
    reconnect_calls = 0

    async def track_apply(*args: Any, **kwargs: Any) -> None:
        nonlocal apply_calls
        apply_calls += 1

    async def track_reconnect() -> dict[str, Any]:
        nonlocal reconnect_calls
        reconnect_calls += 1
        return {"bot_token": "fresh-token"}

    monkeypatch.setattr(sess, "_apply_login", track_apply)
    monkeypatch.setattr(sess, "_reconnect", track_reconnect)

    import bot as protocol

    async def fake_fetch_login_qrcode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "qrcode": "switch-qr-abc",
            "qrcode_img_content": "data:image/png;base64,AAA",
        }

    monkeypatch.setattr(protocol, "fetch_login_qrcode", fake_fetch_login_qrcode)

    qrcode = await sess._initiate_qr_switch()
    assert qrcode == "switch-qr-abc"

    assert sess.bot_token == "old-token", (
        "initiate must not mutate bot_token (decoupled path invariant)"
    )
    assert sess._token_ref[0] == "old-token", (
        "initiate must not mutate _token_ref (decoupled path invariant)"
    )
    assert sess.baseurl == "https://old.invalid"
    assert sess._base_url_ref[0] == "https://old.invalid"
    assert sess.runtime_state["bot_token"] == "old-token"
    # The web UI sees a freshly generated QR, but the session's auth state
    # is unchanged.  No atomic swap ran, and no reconnect was triggered.
    assert apply_calls == 0
    assert reconnect_calls == 0


@pytest.mark.asyncio
async def test_qr_switch_expire_preserves_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """QR expire / timeout / already_connected → no atomic swap, token stays.

    Mirrors open-platform / OAuth behaviour: the QR is just a display
    artifact with a TTL.  NOT scanning the QR must not affect the backend
    server connection.  ``_apply_login`` MUST NOT be called.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token
    sess.ilink_bot_id = "old-bot-id"

    apply_calls = 0

    async def track_apply(*args: Any, **kwargs: Any) -> None:
        nonlocal apply_calls
        apply_calls += 1

    monkeypatch.setattr(sess, "_apply_login", track_apply)

    import bot as protocol

    async def fake_fetch_login_qrcode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"qrcode": "switch-qr-expired", "qrcode_img_content": "x"}

    async def fake_wait_login_confirmation(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"expired": True}

    monkeypatch.setattr(protocol, "fetch_login_qrcode", fake_fetch_login_qrcode)
    monkeypatch.setattr(protocol, "wait_login_confirmation", fake_wait_login_confirmation)

    # Drive the full path: request_relogin("web switch") → _request_qr_switch
    # → _initiate_qr_switch (background task) + _await_qr_confirmation_and_swap.
    result = await sess.request_relogin("web switch")
    assert result["status"] == "qr_pending"
    assert sess._pending_qr_task is not None
    # Let the background switch task finish (it polls then expires).
    await asyncio.wait_for(sess._pending_qr_task, timeout=2)

    assert sess.bot_token == "old-token", (
        "expired switch must preserve bot_token"
    )
    assert sess._token_ref[0] == "old-token", (
        "expired switch must preserve _token_ref"
    )
    assert sess.ilink_bot_id == "old-bot-id", (
        "expired switch must preserve ilink_bot_id"
    )
    assert sess.runtime_state["bot_token"] == "old-token"
    assert apply_calls == 0, (
        "expired switch must NOT call _apply_login (no atomic swap)"
    )
    # qr_state downgrades to error so the UI doesn't keep showing a dead QR.
    assert sess.qr_state.status == "error"
    assert "未扫描" in (sess.qr_state.last_error or "")


@pytest.mark.asyncio
async def test_qr_switch_confirm_atomic_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Confirmed scan → atomic swap under ``_reconnect_lock`` + ``_drain_lock``.

    The token flip from "old-token" to "new-token" must happen inside the
    ``_reconnect_lock`` / ``_drain_lock`` pair so it cannot race an
    in-flight ``_drain_batch`` that is reading ``_token_ref[0]`` (invariant
    from docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md §5.2).
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token
    sess.ilink_bot_id = "old-bot-id"
    sess.runtime_state["get_updates_buf"] = "old-cursor"
    sess.runtime_state["pending_messages"] = []
    sess.runtime_state["processed_message_ids"] = []
    sess.welcomed_users = set()
    sess.contexts = {}
    sess.typing_ticket_cache = {}

    apply_lock_state: dict[str, Any] = {}
    locks_held_during_apply: list[tuple[bool, bool]] = []
    apply_called = asyncio.Event()

    original_apply = sess._apply_login

    async def spying_apply(result: dict[str, Any], *, initial: bool = False) -> None:
        apply_lock_state["reconnect_lock_held"] = sess._reconnect_lock.locked()
        apply_lock_state["drain_lock_held"] = sess._drain_lock.locked()
        locks_held_during_apply.append(
            (sess._reconnect_lock.locked(), sess._drain_lock.locked())
        )
        try:
            await original_apply(result, initial=initial)
        finally:
            apply_called.set()

    monkeypatch.setattr(sess, "_apply_login", spying_apply)

    # An in-flight drain holds _drain_lock.  The atomic swap MUST wait for
    # this drain to finish (otherwise it would race with a mid-flight send).
    drain_entered = asyncio.Event()
    drain_release = asyncio.Event()

    async def slow_drain() -> None:
        async with sess._drain_lock:
            drain_entered.set()
            await drain_release.wait()

    drain_task = asyncio.create_task(slow_drain())
    await asyncio.wait_for(drain_entered.wait(), timeout=1)

    import bot as protocol

    async def fake_fetch_login_qrcode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"qrcode": "switch-qr-confirmed", "qrcode_img_content": "x"}

    async def fake_wait_login_confirmation(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "bot_token": "new-token",
            "baseurl": "https://new.invalid",
            "ilink_bot_id": "new-bot-id",
            "ilink_user_id": "new-user-id",
        }

    async def fake_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(protocol, "fetch_login_qrcode", fake_fetch_login_qrcode)
    monkeypatch.setattr(protocol, "wait_login_confirmation", fake_wait_login_confirmation)
    monkeypatch.setattr(protocol, "notify_lifecycle", fake_notify)

    result = await sess.request_relogin("web switch")
    assert result["status"] == "qr_pending"

    # Capture the background task before _run_qr_switch's finally block
    # nulls out self._pending_qr_task on completion.
    pending_task = sess._pending_qr_task
    assert pending_task is not None

    # The background task should be blocked on _drain_lock while the slow
    # drain is in flight; token must NOT yet be swapped.
    await asyncio.sleep(0.05)
    assert sess.bot_token == "old-token"
    assert not apply_called.is_set(), (
        "atomic swap must wait for in-flight _drain_batch to finish"
    )

    # Release the in-flight drain; the background task must now acquire
    # both locks and atomically swap the credential.
    drain_release.set()
    await drain_task
    await asyncio.wait_for(pending_task, timeout=2)
    assert apply_called.is_set()

    assert sess.bot_token == "new-token", (
        "confirmed switch must atomically swap bot_token"
    )
    assert sess._token_ref[0] == "new-token"
    assert sess.baseurl == "https://new.invalid"
    assert sess._base_url_ref[0] == "https://new.invalid"
    assert sess.ilink_bot_id == "new-bot-id"
    assert sess.ilink_user_id == "new-user-id"
    # Atomicity: the swap held both locks.
    assert locks_held_during_apply == [(True, True)], (
        f"atomic swap must run with both _reconnect_lock and _drain_lock "
        f"held, got {locks_held_during_apply}"
    )
    # Status reflects the new login.
    assert sess.qr_state.status == "logged_in"


@pytest.mark.asyncio
async def test_request_relogin_web_switch_uses_decoupled_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``request_relogin('web switch')`` takes the decoupled path.

    Specifically:
      - It calls ``_initiate_qr_switch`` (NOT ``_login`` or ``_reconnect``).
      - It does NOT block on a QR confirmation future; the background
        ``_pending_qr_task`` handles the actual swap.
      - It does NOT touch ``_relogin_event`` (the listener / clearing path
        used by server-driven reasons is left alone).
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "old-token"
    sess._token_ref[0] = sess.bot_token
    sess.runtime_state["bot_token"] = sess.bot_token

    initiate_calls = 0
    login_calls = 0
    reconnect_calls = 0

    async def fake_initiate() -> str:
        nonlocal initiate_calls
        initiate_calls += 1
        return "switch-qr-token"

    async def fake_login(*, reconnect: bool = False) -> dict[str, Any]:
        nonlocal login_calls
        login_calls += 1
        return {"bot_token": "fresh-token"}

    async def fake_reconnect() -> dict[str, Any]:
        nonlocal reconnect_calls
        reconnect_calls += 1
        return {"bot_token": "fresh-token"}

    monkeypatch.setattr(sess, "_initiate_qr_switch", fake_initiate)
    monkeypatch.setattr(sess, "_login", fake_login)
    monkeypatch.setattr(sess, "_reconnect", fake_reconnect)

    # Make the background switch task a no-op so the test determinates fast.
    async def fake_run_qr_switch(qrcode: str) -> None:
        return None

    monkeypatch.setattr(sess, "_run_qr_switch", fake_run_qr_switch)

    relogin_event_fired = False
    original_set = sess._relogin_event.set

    def tracking_set() -> None:
        nonlocal relogin_event_fired
        relogin_event_fired = True
        original_set()

    sess._relogin_event.set = tracking_set  # type: ignore[method-assign]

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        result = await sess.request_relogin("web switch")
        # Background task is scheduled but the function returned immediately.
        assert isinstance(result, dict)
        assert result["status"] == "qr_pending"
        assert result["reason"] == "web switch"
        assert sess._pending_qr_task is not None

        assert initiate_calls == 1, "decoupled path must call _initiate_qr_switch"
        assert login_calls == 0, (
            "decoupled path must NOT call _login (that's the clearing path)"
        )
        assert reconnect_calls == 0, (
            "decoupled path must NOT call _reconnect directly"
        )
        # The decoupled path leaves the existing token intact.
        assert sess.bot_token == "old-token"
        assert sess._token_ref[0] == "old-token"
        assert sess.runtime_state["bot_token"] == "old-token"
        # Listener was not poked — only server-driven reasons wake it.
        assert relogin_event_fired is False, (
            "decoupled path must not signal _relogin_event (preserves "
            "the listener / clearing path for server-driven reasons)"
        )
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        if sess._pending_qr_task is not None and not sess._pending_qr_task.done():
            sess._pending_qr_task.cancel()
            await asyncio.gather(sess._pending_qr_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_request_relogin_stale_token_uses_clearing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``request_relogin('stale-token')`` MUST keep the clearing path.

    The 78e1dfd invariant must not regress: ``_reconnect`` clears
    ``bot_token`` BEFORE ``_login`` so that iLink's binded_redirect cannot
    return a stale token.  ``stale-token`` is a server-driven reason and
    therefore continues to flow through ``_relogin_lock`` /
    ``_reconnect`` — NOT through the decoupled path.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"
    sess.baseurl = "https://old.invalid"
    sess._token_ref[0] = sess.bot_token
    sess._base_url_ref[0] = sess.baseurl
    sess.runtime_state["bot_token"] = sess.bot_token

    observed: dict[str, Any] = {}
    login_called = asyncio.Event()

    async def fake_login(*, reconnect: bool = False) -> dict[str, Any]:
        observed["reconnect"] = reconnect
        observed["token_during_login"] = sess.bot_token
        observed["saved_token_during_login"] = sess.runtime_state.get("bot_token")
        login_called.set()
        return {
            "bot_token": "fresh-token",
            "baseurl": "https://new.invalid",
            "ilink_bot_id": "new-bot-id",
        }

    async def fake_apply_login(*args: Any, **kwargs: Any) -> None:
        pass

    async def fake_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(sess, "_login", fake_login)

    async def fake_apply_login(result: dict[str, Any], *, initial: bool = False) -> None:
        # Mirror the real _apply_login just enough to advance the credential,
        # because this test asserts on the post-_apply_login values.  The
        # binded_redirect defence (78e1dfd) is verified separately in
        # test_reconnect_binded_redirect_does_not_rollback_token.
        new_token = str(result.get("bot_token") or "").strip()
        new_base = str(result.get("baseurl") or sess.baseurl)
        new_id = str(result.get("ilink_bot_id") or sess.ilink_bot_id)
        sess.bot_token = new_token
        sess._token_ref[0] = new_token
        sess.baseurl = new_base
        sess._base_url_ref[0] = new_base
        sess.ilink_bot_id = new_id
        sess.runtime_state["bot_token"] = new_token
        sess.qr_state.status = "logged_in"

    monkeypatch.setattr(sess, "_apply_login", fake_apply_login)

    # The decoupled entry point MUST NOT be called for stale-token.
    initiate_calls = 0

    async def fake_initiate() -> str:
        nonlocal initiate_calls
        initiate_calls += 1
        return "never-called-qr"

    monkeypatch.setattr(sess, "_initiate_qr_switch", fake_initiate)

    import bot as protocol
    monkeypatch.setattr(protocol, "notify_lifecycle", fake_notify)

    listener = asyncio.create_task(sess._relogin_listener())
    try:
        result = await sess.request_relogin("stale-token")
        await asyncio.wait_for(login_called.wait(), timeout=1)
        # The clearing path is intact: bot_token was emptied BEFORE _login.
        assert observed["reconnect"] is True
        assert observed["token_during_login"] == "", (
            "stale-token path MUST clear bot_token before _login "
            "(78e1dfd invariant)"
        )
        assert observed["saved_token_during_login"] == ""
        assert result["bot_token"] == "fresh-token"
        assert sess.bot_token == "fresh-token"
        # The decoupled path was never reached.
        assert initiate_calls == 0, (
            "stale-token must NOT take the decoupled path"
        )
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


def test_only_explicit_minus_14_is_classified_as_stale_token() -> None:
    from bot import ILinkAPIError

    assert ILinkAPIError("stale", ret=-14).is_stale_token
    for code in (-1, -13, 1, 500, None):
        assert not ILinkAPIError("other", ret=code).is_stale_token


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
    assert any("后台持续维护" in item for item in sent)


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


@pytest.mark.asyncio
async def test_drain_lock_serializes_drain_and_reconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """In-flight _drain_batch must finish before _reconnect clears bot_token.

    Regression test for the in-flight AI / reconnect race described in
    docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md and §5.2 of
    docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md. Without _drain_lock,
    _reconnect would atomically clear bot_token while _drain_batch is mid
    AI call; the in-flight _send_reliable would then hit -14 and the
    message would be silently lost.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "valid-token"
    sess._token_ref[0] = "valid-token"
    sess.runtime_state["bot_token"] = "valid-token"

    drain_started = asyncio.Event()
    drain_release = asyncio.Event()
    reconnect_finished = asyncio.Event()
    login_called = asyncio.Event()

    # Pretend an AI/send reply is in flight: hold _drain_lock for a while.
    async def slow_drain() -> None:
        async with sess._drain_lock:
            drain_started.set()
            await drain_release.wait()

    drain_task = asyncio.create_task(slow_drain())
    await asyncio.wait_for(drain_started.wait(), timeout=1)

    # Patch _login so we can prove _reconnect actually waited, not just that
    # it queued.
    async def fake_login(*, reconnect: bool = False) -> dict[str, Any]:
        login_called.set()
        reconnect_finished.set()
        return {
            "bot_token": "fresh-token",
            "baseurl": "https://new.invalid",
            "ilink_bot_id": "bot-id",
        }

    monkeypatch.setattr(sess, "_login", fake_login)
    import bot as protocol

    async def fake_notify(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(protocol, "notify_lifecycle", fake_notify)

    reconnect_task = asyncio.create_task(sess._reconnect())

    # Give the event loop a chance to schedule _reconnect.
    await asyncio.sleep(0.1)
    assert not login_called.is_set(), (
        "_reconnect started login while _drain_lock was held — "
        "in-flight race is not protected"
    )
    assert sess.bot_token == "valid-token", (
        "_reconnect cleared bot_token while _drain_lock was held — "
        "in-flight race is not protected"
    )

    # Release the drain; reconnect must now proceed.
    drain_release.set()
    await drain_task
    await asyncio.wait_for(reconnect_finished.wait(), timeout=1)
    assert login_called.is_set()
    assert sess.bot_token == "fresh-token"
    await reconnect_task


@pytest.mark.asyncio
async def test_stale_token_during_drain_preserves_pending_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A -14 from _send_reliable must NOT drop in-flight messages.

    Regression test for §5.3 of docs/2026-09-16_RECONNECT_AND_KEEPALIVE.md:
    when handle_message raises ILinkAPIError(ret=-14), _drain_batch must
    preserve unprocessed messages in pending_messages (so _message_loop
    can replay them after request_relogin succeeds) and re-raise so the
    outer loop's request_relogin("stale-token") path fires.
    """
    sess = _make_session(monkeypatch, "alice", state_file=tmp_path / "alice.json")
    sess.bot_token = "stale-token"
    sess._token_ref[0] = "stale-token"
    sess.runtime_state["bot_token"] = "stale-token"
    sess.runtime_state["get_updates_buf"] = "old-cursor"
    _install_reply_spy(monkeypatch, sess)

    import bot as protocol

    async def stale_handler(msg: dict[str, Any]) -> None:
        # Simulate _send_reliable raising -14 mid-reply.
        raise protocol.ILinkAPIError("session timeout", path="sendmessage", ret=-14)

    monkeypatch.setattr(sess, "handle_message", stale_handler)

    m1 = message("first")
    m2 = message("second")
    m3 = message("third")
    payload = {"get_updates_buf": "new-cursor", "msgs": [m1, m2, m3]}

    # process_update_batch should propagate the stale-token error.
    with pytest.raises(protocol.ILinkAPIError) as excinfo:
        await sess.process_update_batch(payload)
    assert excinfo.value.is_stale_token

    # Unprocessed messages (m1 onward, since handler raised on m1) must be
    # preserved in pending_messages so _message_loop can replay after
    # request_relogin completes.
    assert sess.runtime_state["pending_messages"], (
        "pending_messages was wiped on stale-token — messages will be lost"
    )
    assert sess.runtime_state["pending_cursor"] == "new-cursor"
    # Cursor must NOT advance so the batch replays.
    assert sess.runtime_state["get_updates_buf"] == "old-cursor"
    # No message was successfully processed.
    assert not sess.runtime_state["processed_message_ids"]


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
