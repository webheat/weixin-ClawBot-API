"""Focused lifecycle tests for the ephemeral-only shared runtime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from aiohttp import web

from shared_runtime import (
    SharedRuntimeConfig,
    load_default_config,
    parse_env_file,
    run_shared,
)


class FakeManager:
    """Stand-in for ``BotManager`` that records session-creation calls.

    The ephemeral-only runtime creates sessions on demand via the web layer
    (``POST /ephemeral/start``); ``run_shared`` itself does not eagerly start
    any user.  This fake records every ``create_background`` call so tests
    can assert the runtime does not auto-spawn anything.
    """

    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped = False

    async def create_background(self, user_id, config, **kwargs):
        self.started.append(user_id)

    def get(self, user_id):
        return None

    async def stop_all(self):
        self.stopped = True


def _write_config(path: Path, payload: dict) -> None:
    (path / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_run_shared_does_not_eagerly_start_users(tmp_path: Path):
    """Ephemeral sessions start on demand via ``POST /ephemeral/start``; the
    shared listener itself must never auto-spawn a session."""
    _write_config(tmp_path, {
        "provider": "dusapi",
        "providers": {"dusapi": {"model": "base"}},
    })
    (tmp_path / "llm.env").write_text(
        "CLAWBOT_LLM_MODEL=overridden\n", encoding="utf-8"
    )
    manager = FakeManager()
    stop = asyncio.Event()
    task = asyncio.create_task(run_shared(
        SharedRuntimeConfig(host="127.0.0.1", port=0, config_dir=tmp_path,
                            state_dir=tmp_path, env_dir=tmp_path),
        manager=manager, app=web.Application(), stop_event=stop,
    ))
    # Give the listener a tick to settle; nothing should be started.
    await asyncio.sleep(0.05)
    assert manager.started == []
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert manager.stopped


def test_parse_env_file_is_pure(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "llm.env"
    env_file.write_text(
        "export CLAWBOT_LLM_API_KEY='secret'\n"
        "CLAWBOT_LLM_MODEL=small # comment\n"
        "IMA_ILINK_API_KEY=ima-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAWBOT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CLAWBOT_LLM_MODEL", raising=False)
    monkeypatch.delenv("IMA_ILINK_API_KEY", raising=False)
    parsed = parse_env_file(env_file)
    assert parsed["CLAWBOT_LLM_API_KEY"] == "secret"
    assert parsed["CLAWBOT_LLM_MODEL"] == "small"
    assert parsed["IMA_ILINK_API_KEY"] == "ima-key"
    # ``parse_env_file`` must never mutate the process environment — that
    # would leak one ephemeral session's IMA/LLM credentials to every other.
    assert "CLAWBOT_LLM_API_KEY" not in os.environ
    assert "CLAWBOT_LLM_MODEL" not in os.environ
    assert "IMA_ILINK_API_KEY" not in os.environ
    # Missing file is also a normal case (shared llm.env / ima.env are optional).
    assert parse_env_file(tmp_path / "absent.env") == {}


def test_load_default_config_is_deep_copied_per_call(tmp_path: Path):
    """Mutating one returned dict must not affect a fresh call's result."""
    _write_config(tmp_path, {
        "provider": "dusapi",
        "providers": {"dusapi": {"model": "base"}},
    })
    first = load_default_config(config_dir=tmp_path)
    first["providers"]["dusapi"]["model"] = "mutated"
    second = load_default_config(config_dir=tmp_path)
    assert second["providers"]["dusapi"]["model"] == "base"
