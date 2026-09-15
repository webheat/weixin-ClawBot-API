"""Focused lifecycle tests for the shared-process entry point."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from aiohttp import web

from shared_runtime import (
    SharedRuntimeConfig,
    _safe_user,
    load_user_config,
    parse_env_file,
    run_shared,
)


class FakeManager:
    def __init__(self, users: list[str]):
        self.expected = set(users)
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = False
        self.sessions = {}

    async def create_background(self, user_id, config, **kwargs):
        self.started.append(user_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if set(self.started) == self.expected:
            self.all_started.set()
        await self.release.wait()
        self.active -= 1
        self.sessions[user_id] = type("Session", (), {"config": config})()

    def get(self, user_id):
        return self.sessions.get(user_id)

    async def stop_all(self):
        self.stopped = True
        self.sessions.clear()


@pytest.mark.asyncio
async def test_named_users_start_in_parallel_and_cleanup(tmp_path: Path):
    users = ["alice", "bob", "carol"]
    for user in users:
        (tmp_path / f"config_{user}.json").write_text(
            json.dumps({"provider": "dusapi", "providers": {"dusapi": {"model": user}}}),
            encoding="utf-8",
        )
    manager = FakeManager(users)
    stop = asyncio.Event()
    task = asyncio.create_task(run_shared(
        SharedRuntimeConfig(host="127.0.0.1", port=0, config_dir=tmp_path,
                            state_dir=tmp_path, env_dir=tmp_path),
        manager=manager, app=web.Application(), stop_event=stop,
    ))
    await asyncio.wait_for(manager.all_started.wait(), timeout=2)
    assert manager.max_active == len(users)
    assert set(manager.started) == set(users)
    manager.release.set()
    # Let the startup gather complete before requesting normal shutdown.
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert manager.stopped


def test_env_files_are_explicit_and_do_not_mutate_process(tmp_path: Path, monkeypatch):
    env_file = tmp_path / "alice.env"
    env_file.write_text(
        "export CLAWBOT_LLM_API_KEY='secret'\n"
        "CLAWBOT_LLM_MODEL=small # comment\n"
        "IMA_ILINK_API_KEY=ima-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAWBOT_LLM_API_KEY", raising=False)
    assert parse_env_file(env_file)["CLAWBOT_LLM_API_KEY"] == "secret"
    assert "CLAWBOT_LLM_API_KEY" not in os.environ

    (tmp_path / "config_alice.json").write_text(
        json.dumps({"provider": "dusapi", "providers": {"dusapi": {"model": "base"}}}),
        encoding="utf-8",
    )
    cfg = load_user_config("alice", config_dir=tmp_path, env_dir=tmp_path)
    assert cfg["providers"]["dusapi"]["model"] == "small"
    assert cfg["ima_env"]["IMA_ILINK_API_KEY"] == "ima-key"
    cfg["providers"]["dusapi"]["model"] = "mutated"
    cfg2 = load_user_config("alice", config_dir=tmp_path, env_dir=tmp_path)
    assert cfg2["providers"]["dusapi"]["model"] == "small"


def test_unsafe_user_names_cannot_collide_on_disk():
    assert _safe_user("a/b") != _safe_user("a_b")
    assert "/" not in _safe_user("a/b")
