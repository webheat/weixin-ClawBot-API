"""发稿闭环的流程日志契约。

一次「用户发消息 → clawbot 认领 → 调桥 → 桥出结果 → 回信给用户」横跨三层：

    bot_session._handle_message   （微信入站、回信）
      └─ utils/xiaolvshu.handle    （认领、格式化回复）
           └─ utils/xiaolvshu._run_bridge  （子进程调桥）

这三层各记各的日志。没有共同的关联 id 时，排查「用户说发了没反应」只能
靠时间戳猜哪条是哪条 —— 所以每条相关日志都带同一个 ``flow=``。

这份测试钉住两件事：

1. **同一条消息的各阶段共用一个 flow id** —— 闭环能被 grep 出来；
2. **闭环上缺一环时不会静默** —— 桥超时、桥挂了、桥吐的不是 JSON，
   都必须有对应的 stage 记录，否则排查时看到的是「什么都没发生」。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest

import utils.xiaolvshu as mod


FLOW_RE = re.compile(r"flow=([0-9a-f]{8}|-)")


def _records(caplog: Any) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "clawbot.publish"
    ]


def _flows(caplog: Any) -> set[str]:
    flows = set()
    for msg in _records(caplog):
        match = FLOW_RE.search(msg)
        if match:
            flows.add(match.group(1))
    return flows


class _FakeProc:
    """假装是 subprocess.run 的返回值。"""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeHTTP:
    closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_run(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[list[str]]:
    """把 _run_bridge 里的 subprocess.run 换成假的，返回调用过的命令行。"""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_check_bridge_version", lambda: None)
    return calls


@pytest.mark.asyncio
async def test_publish_flow_is_traceable_end_to_end(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """一次成功发稿，每一环都留下带同一个 flow id 的记录。"""
    payload = {
        "ok": True, "action": "publish", "kind": "newspic",
        "title": "沉香", "cards": 3, "media_id": "m-123",
    }
    _patch_run(monkeypatch, _FakeProc(stdout=json.dumps(payload, ensure_ascii=False)))

    caplog.set_level(logging.INFO, logger="clawbot.publish")
    reply = await mod.handle("/发贴图号 沉香", "u1@im.wechat", flow="abc12345")

    assert reply and "沉香" in reply
    messages = _records(caplog)
    stages = [re.search(r"stage=(\w+)", m).group(1) for m in messages
              if re.search(r"stage=(\w+)", m)]
    for expected in ("publish_intent", "bridge_call", "bridge_result",
                      "publish_result"):
        assert expected in stages, (
            f"发稿闭环缺 {expected} 阶段；实际记到：{stages}"
        )
    # 同一 flow id 贯穿全链 —— 否则 grep 不出来
    assert _flows(caplog) == {"abc12345"}, (
        f"flow id 没贯穿：{_flows(caplog)}"
    )


@pytest.mark.asyncio
async def test_bridge_timeout_is_logged_not_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """桥超时（出图 + 上传慢）必须留下 error 记录。

    没有这条记录时，用户看到「⏱ 超过 240 秒」但日志里一片空白 ——
    排查会误以为根本没调用过桥。
    """
    _patch_run(
        monkeypatch,
        mod.subprocess.TimeoutExpired(cmd=["bridge"], timeout=mod.TIMEOUT),
    )

    caplog.set_level(logging.INFO, logger="clawbot.publish")
    reply = await mod.handle("/发贴图号 沉香", "u1@im.wechat", flow="dead1234")

    assert reply and "⏱" in reply
    messages = _records(caplog)
    assert any("stage=bridge_error" in m and "code=timeout" in m
               for m in messages), (
        f"桥超时没被记录：{messages}"
    )
    assert "dead1234" in " ".join(messages)


@pytest.mark.asyncio
async def test_bridge_non_json_output_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """桥崩了（stdout 里没有 JSON 行）也要留记录，带 stderr 尾巴。"""
    _patch_run(
        monkeypatch,
        _FakeProc(stdout="Traceback...", stderr="boom: 炸了", returncode=1),
    )

    caplog.set_level(logging.INFO, logger="clawbot.publish")
    reply = await mod.handle("/发贴图号 沉香", "u1@im.wechat", flow="badf1234")

    assert reply and "❌" in reply
    messages = _records(caplog)
    assert any("stage=bridge_error" in m and "no_json_line" in m
               for m in messages), f"桥没输出 JSON 没被记录：{messages}"
    assert any("炸了" in m for m in messages), (
        f"桥的 stderr 尾巴没进日志：{messages}"
    )


@pytest.mark.asyncio
async def test_need_select_candidates_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """多命中时，候选列表要记下来 —— 否则用户回「/选 2」时无从查证。"""
    payload = {
        "ok": False, "code": "need_select",
        "candidates": [
            {"i": 1, "title": "沉香", "path": "/v/a.md"},
            {"i": 2, "title": "沉香（修订）", "path": "/v/b.md"},
        ],
    }
    _patch_run(monkeypatch, _FakeProc(stdout=json.dumps(payload, ensure_ascii=False)))

    caplog.set_level(logging.INFO, logger="clawbot.publish")
    reply = await mod.handle("/发贴图号 沉香", "u1@im.wechat", flow="cand1234")

    assert reply and "/选" in reply
    messages = _records(caplog)
    assert any("stage=publish_need_select" in m and "candidates=2" in m
               for m in messages), f"候选列表没被记录：{messages}"
    assert any("沉香（修订）" in m for m in messages), (
        f"候选标题没被记录：{messages}"
    )


@pytest.mark.asyncio
async def test_non_publish_message_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """普通聊天不该被发稿链路认领，也不该去调桥。"""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc(stdout="{}")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_check_bridge_version", lambda: None)

    caplog.set_level(logging.DEBUG, logger="clawbot.publish")
    reply = await mod.handle("今天天气怎么样", "u1@im.wechat", flow="chat1234")

    assert reply is None, "非发稿消息不该被认领"
    assert not calls, f"非发稿消息不该调桥：{calls}"
    assert any("stage=publish_unclaimed" in m for m in _records(caplog))


@pytest.mark.asyncio
async def test_bridge_stderr_is_kept_for_debugging(
    monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """桥的 stderr（排障信息：插图找不到、标题超限……）要进日志。

    平时被丢掉，只有出问题时才是唯一线索。记在 DEBUG 级，不刷屏。
    """
    payload = {"ok": True, "kind": "newspic", "title": "t", "media_id": "m"}
    _patch_run(
        monkeypatch,
        _FakeProc(stdout=json.dumps(payload), stderr="⚠️ 标题 25 字，超过贴图上限"),
    )

    caplog.set_level(logging.DEBUG, logger="clawbot.publish")
    await mod.handle("/发贴图号 沉香", "u1@im.wechat", flow="stderr123")

    assert any("stage=bridge_stderr" in m and "超过贴图上限" in m
               for m in _records(caplog)), (
        f"桥的 stderr 没被保留：{_records(caplog)}"
    )


def test_logs_do_not_leak_filesystem_paths() -> None:
    """日志里不能出现绝对路径。

    回信里有 vault 路径（「你的 vault：/opt/clawbot-xiaolvshu/users/xxx/vault」）。
    那条路径对排障没用，却把每个用户的目录布局摊进了共享日志。
    """
    scrubbed = mod._scrub(
        "你的 vault：/opt/clawbot-xiaolvshu/users/yg-abc/vault\n"
        "把 .md 放进去，或者先 /发列表 看看能发什么。"
    )
    assert "/opt/clawbot-xiaolvshu" not in scrubbed, scrubbed
    assert "<path>" in scrubbed, scrubbed
    # 普通文字不能被误伤
    assert "把 .md 放进去" in scrubbed, scrubbed
    # URL 不是本地路径，别被当成路径抹掉
    assert "https://example.com/x" in mod._scrub("https://example.com/x")


@pytest.mark.asyncio
async def test_session_and_bridge_share_one_flow_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: Any
) -> None:
    """微信入站、认领、桥、回信 —— 四处必须是同一个 flow id。

    这是「跨层能串起来」的最后一环：bot_session 生成的 flow id 要一路传到
    utils/xiaolvshu，再回到 _send_reliable 的 reply_sent 标记上。
    """
    import bot_session

    payload = {
        "ok": True, "action": "publish", "kind": "newspic",
        "title": "沉香", "cards": 2, "media_id": "m-9",
    }
    _patch_run(monkeypatch, _FakeProc(stdout=json.dumps(payload, ensure_ascii=False)))

    sent: list[str] = []

    async def fake_api_post(_http, path, body, *args, **kwargs):
        if path == "ilink/bot/sendmessage":
            items = body.get("msg", {}).get("item_list", [])
            if items:
                sent.append(str(items[0].get("text_item", {}).get("text", "")))
        return {"ret": 0, "errcode": 0}

    monkeypatch.setattr(bot_session, "api_post", fake_api_post, raising=False)
    import bot as protocol
    monkeypatch.setattr(protocol, "api_post", fake_api_post)

    sess = bot_session.BotSession(
        "alice", _FakeHTTP(),
        {"llm": {"provider": "deepseek", "api_key": "k"},
         "state_dir": str(tmp_path)},
        state_file=tmp_path / "alice.json",
    )
    caplog.set_level(logging.INFO, logger="clawbot.publish")
    caplog.set_level(logging.INFO, logger="clawbot.diagnostics")
    try:
        await sess.handle_message({
            "message_type": 1,
            "from_user_id": "u1@im.wechat",
            "context_token": "ctx-1",
            "message_id": "msg-flow-1",
            "item_list": [{"type": 1, "text_item": {"text": "/发贴图号 沉香"}}],
        })
    finally:
        await sess.stop()

    assert sent, "回复没发出去"
    publish_flows = _flows(caplog)
    assert len(publish_flows) == 1, (
        f"发稿各阶段没共用一个 flow id：{publish_flows}"
    )
    flow = publish_flows.pop()

    diag = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "clawbot.diagnostics"
    ]
    inbound = [m for m in diag if "stage=inbound" in m]
    reply = [m for m in diag if "stage=reply_sent" in m]
    assert inbound and f"flow={flow}" in inbound[0], (
        f"入站标记的 flow id 对不上：{inbound}"
    )
    assert reply and f"flow={flow}" in reply[0], (
        f"回信标记的 flow id 对不上：{reply}"
    )
    assert "kind=xiaolvshu" in reply[0], f"回信标记缺 kind：{reply[0]}"


@pytest.mark.asyncio
async def test_send_failure_is_logged_with_the_flow_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, caplog: Any
) -> None:
    """回信发不出去时，日志里必须留痕并带 flow id。

    这条闭环最容易被忽略的断点：桥成功了、文案也生成好了，但 sendmessage
    没成功 —— 用户什么都没收到，日志里却「一切正常」（只有 publish_result，
    没有 reply_sent）。必须有一条带 flow 的失败记录，否则排查时会误判成
    「回复发出去了但用户没看到」。
    """
    import bot_session

    payload = {
        "ok": True, "action": "publish", "kind": "newspic",
        "title": "沉香", "cards": 1, "media_id": "m-1",
    }
    _patch_run(monkeypatch, _FakeProc(stdout=json.dumps(payload, ensure_ascii=False)))

    async def failing_api_post(*_args, **_kwargs):
        # HTTP 200，但业务失败（ret != 0）—— 传输成功、投递失败
        return {"ret": 1, "errcode": 0, "errmsg": "token expired"}

    import bot as protocol
    monkeypatch.setattr(protocol, "api_post", failing_api_post)

    sess = bot_session.BotSession(
        "alice", _FakeHTTP(),
        {"llm": {"provider": "deepseek", "api_key": "k"},
         "state_dir": str(tmp_path)},
        state_file=tmp_path / "alice.json",
    )
    caplog.set_level(logging.INFO, logger="clawbot.publish")
    caplog.set_level(logging.INFO, logger="clawbot.message")
    with pytest.raises(Exception):
        await sess.handle_message({
            "message_type": 1,
            "from_user_id": "u1@im.wechat",
            "context_token": "ctx-1",
            "message_id": "msg-flow-fail",
            "item_list": [{"type": 1, "text_item": {"text": "/发贴图号 沉香"}}],
        })
    await sess.stop()

    failures = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "clawbot.message" and "send_reliable failed" in rec.getMessage()
    ]
    assert failures, "回信失败没有留下日志 —— 闭环断在这里却查不到"
    assert any("flow=" in m for m in failures), (
        f"失败日志缺 flow id，无法跟前面的阶段对上：{failures}"
    )
