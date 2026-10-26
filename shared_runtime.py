"""Single-process, multi-account runtime for ClawBot.

The legacy ``bot.py`` entry point is intentionally not imported here.  This
module owns the process-level resources (one HTTP connection pool and one
aiohttp web listener) and delegates account state to :class:`BotManager`.

An important property of this module is that dotenv files are parsed as data.
They are never loaded into ``os.environ``.  The old CLI uses ``load_dotenv``;
doing that in a shared process would make one account's IMA/LLM credentials
visible to every other account.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import inspect
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp
from aiohttp import web

from bot_manager import BotManager
from shared_web import build_web_app

log = logging.getLogger("clawbot.shared_runtime")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18300
DEFAULT_PREFIX = "/clawbot"
DEFAULT_ENV_DIR = "/etc/clawbot"
DEFAULT_CONFIG_DIR = "."
DEFAULT_STATE_DIR = "."


def _parse_env_value(value: str) -> str:
    """Parse the small dotenv subset needed by ClawBot without side effects."""

    value = value.strip()
    if not value:
        return ""
    if value[:1] in {"'", '"'}:
        quote = value[0]
        end = value.rfind(quote)
        if end > 0:
            value = value[1:end]
        else:
            value = value[1:]
        if quote == '"':
            # dotenv's common escapes; do not interpret arbitrary Python
            # escapes or perform variable expansion here.
            value = value.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
            value = value.replace(r'\"', '"').replace(r"\\", "\\")
        return value
    # An unquoted # starts a comment only when separated from the value.
    value = re.split(r"\s+#", value, maxsplit=1)[0]
    return value.rstrip()


def parse_env_file(path: str | os.PathLike[str] | None) -> dict[str, str]:
    """Read a dotenv file into a new dictionary, never modifying the process.

    Missing files are normal (the shared ``ima.env`` and ``llm.env`` files are
    optional), and therefore return an empty mapping.  Later merging decides
    precedence explicitly instead of relying on dotenv's global precedence.
    """

    if not path:
        return {}
    result: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        result[key] = _parse_env_value(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
        return {}
    return copy.deepcopy(raw) if isinstance(raw, dict) else {}


def _merge_llm_env(config: dict[str, Any], env: Mapping[str, str]) -> None:
    """Apply per-account LLM env values to either supported config shape."""

    mapping = {
        "CLAWBOT_LLM_PROVIDER": "provider",
        "CLAWBOT_LLM_API_KEY": "api_key",
        "CLAWBOT_LLM_BASE_URL": "base_url",
        "CLAWBOT_LLM_MODEL": "model",
        "CLAWBOT_LLM_PROMPT": "prompt",
    }
    overrides = {field: env[name] for name, field in mapping.items()
                 if env.get(name, "").strip()}
    if not overrides:
        return
    provider = overrides.get("provider") or str(config.get("provider") or "dusapi")
    config["provider"] = provider
    providers = config.get("providers")
    if isinstance(providers, dict):
        provider_cfg = copy.deepcopy(providers.get(provider) or {})
        provider_cfg.update({key: value for key, value in overrides.items() if key != "provider"})
        providers[provider] = provider_cfg
    else:
        # Flat configs are accepted by BotSession._make_ai as well.
        config.update({key: value for key, value in overrides.items() if key != "provider"})


def load_default_config(*, config_dir: str | os.PathLike[str] = DEFAULT_CONFIG_DIR) -> dict[str, Any]:
    """Load the shared ``config.json`` used as the session default."""

    return _read_json(Path(config_dir) / "config.json")


# Descriptive aliases keep the loader easy to discover for embedders that use
# the terminology from the deployment documentation.
load_env_file = parse_env_file


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass
class SharedRuntimeConfig:
    """Explicit process-level settings; credentials stay in user configs."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    prefix: str = DEFAULT_PREFIX
    session_ttl: float = 8 * 3600
    session_limit: int = 100
    # 多久之内还在等扫码的会话，在重启后会被复活（见
    # _restore_persisted_sessions）。默认 24h：足够覆盖一次发版重启，
    # 又不会把早就没人管的僵尸账号一直拉起来。
    reauth_restore_window: float = 24 * 3600
    rate_limit: int = 5
    rate_window: float = 60.0
    cookie_secure: bool | None = None
    trust_proxy: bool = False
    http_connection_limit: int = 300
    config_dir: Path = field(default_factory=lambda: Path(DEFAULT_CONFIG_DIR))
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR))
    env_dir: Path = field(default_factory=lambda: Path(DEFAULT_ENV_DIR))

    @classmethod
    def from_env(cls, *, environ: Mapping[str, str] | None = None) -> "SharedRuntimeConfig":
        """Read only runtime controls from env; credentials are file-scoped."""

        env = environ if environ is not None else os.environ
        def value(name: str, default: Any) -> Any:
            return env.get(name, default)
        def integer(name: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(value(name, default)))
            except (TypeError, ValueError):
                return default
        def decimal(name: str, default: float, minimum: float = 0.0) -> float:
            try:
                return max(minimum, float(value(name, default)))
            except (TypeError, ValueError):
                return default
        port = integer("CLAWBOT_WEB_PORT", DEFAULT_PORT)
        ttl = decimal("CLAWBOT_SESSION_TTL", 8 * 3600, 0.01)
        rate_window = decimal("CLAWBOT_WEB_RATE_WINDOW", 60.0, 1.0)
        prefix = str(value("CLAWBOT_WEB_PREFIX", DEFAULT_PREFIX)).strip() or DEFAULT_PREFIX
        return cls(
            host=str(value("CLAWBOT_WEB_HOST", DEFAULT_HOST)), port=port, prefix=prefix,
            session_ttl=ttl,
            session_limit=integer("CLAWBOT_MAX_SESSIONS", 100, 1),
            reauth_restore_window=decimal("CLAWBOT_REAUTH_RESTORE_WINDOW", 24 * 3600, 0.0),
            rate_limit=integer("CLAWBOT_WEB_RATE_LIMIT", 5, 1),
            rate_window=rate_window,
            cookie_secure=(
                None if "CLAWBOT_COOKIE_SECURE" not in env else
                str(value("CLAWBOT_COOKIE_SECURE", "")).strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            trust_proxy=str(value("CLAWBOT_TRUST_PROXY", "0")).strip().lower()
                        in {"1", "true", "yes", "on"},
            http_connection_limit=integer("CLAWBOT_HTTP_CONNECTION_LIMIT", 300, 1),
            config_dir=Path(str(value("CLAWBOT_CONFIG_DIR", DEFAULT_CONFIG_DIR))),
            state_dir=Path(str(value("CLAWBOT_STATE_DIR", DEFAULT_STATE_DIR))),
            env_dir=Path(str(value("CLAWBOT_ENV_DIR", DEFAULT_ENV_DIR))),
        )


# Short name used by integrations that do not need to distinguish this from a
# per-session config object.
RuntimeConfig = SharedRuntimeConfig


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _restore_persisted_sessions(manager: Any, runtime: "SharedRuntimeConfig",
                                      session_config: Mapping[str, Any]) -> list[str]:
    """启动时把「还有凭据的会话」重新拉起来 —— 重启不掉线。

    会话默认只活在内存里：进程一重启，浏览器手里的 cookie 就指不到任何会话，
    页面报「无法连接」，用户只能重新扫码。可 `weixin_state_<sid>.json` 里明明
    存着 ``bot_token``，而 ``BotSession.start()`` 见到 token 会跳过扫码直接
    续用（bot_session.py:866-870）。所以这里扫一遍 state 目录，把还带着
    token 的会话重新建起来。

    token 过期（服务端 -14）时，会话自己走重连流程，页面出二维码 —— 那种情况
    仍然要人扫一次，但那是服务端真的收回了授权，不是本地重启。

    **待重新认证的会话也要复活**（2026-10-26 事故）：token 被清空的那一刻
    会话就把 ``reauth_required=True`` 写进 state 文件。如果进程在二维码窗口
    内被重启（发版），旧逻辑会因为「token 为空」直接跳过这个会话 —— 账号
    就此永久掉线，浏览器里的 resume cookie 也指向一个不存在的会话。
    现在这类会话会被重新拉起：``BotSession.start()`` 看到空 token 会出新的
    二维码，浏览器拿着 30 天的 resume cookie 回来就能续上。
    """
    restored: list[str] = []
    pending_reauth: list[str] = []
    state_dir = Path(runtime.state_dir)
    if not state_dir.is_dir():
        return restored
    window = max(0.0, float(getattr(runtime, "reauth_restore_window", 24 * 3600)))
    now = time.time()
    # 第一类：还带 token 的会话（保持原行为，一个都不少）。
    # 第二类：没有 token 但标了 reauth_required 的会话 —— 只复活最近更新过的，
    # 否则每次重启都会把一堆早就没人管的僵尸账号拉起来。
    live: list[tuple[str, dict, float]] = []
    reauth: list[tuple[str, dict, float]] = []
    for path in sorted(state_dir.glob("weixin_state_*.json")):
        session_id = path.name[len("weixin_state_"):-len(".json")]
        if not session_id:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        has_token = bool(str(data.get("bot_token") or "").strip())
        needs_reauth = bool(data.get("reauth_required"))
        if not has_token and not needs_reauth:
            continue  # 没登录过 / 与本账号无关
        try:
            age = max(0.0, now - path.stat().st_mtime)
        except OSError:
            age = 0.0
        if has_token:
            live.append((session_id, data, age))
        elif age <= window:
            reauth.append((session_id, data, age))
        else:
            log.info("restore skipped stale reauth session sid=%s age_h=%.1f",
                     session_id, age / 3600.0)
    # 一个微信账号只留一个会话：带 token 的优先，同类则最近更新的优先。
    # 否则复活的旧会话一旦被扫描，会把同账号的健康会话踢掉
    # （bot_manager._evict_same_account）。只有**恢复成功**的会话才占用账号 ——
    # 否则健康会话起不来时，它的备用也一起被跳过，账号就彻底没人管了。
    ordered = live + sorted(reauth, key=lambda item: item[2])
    claimed: set[str] = set()
    for session_id, data, _age in ordered:
        uid = str(data.get("ilink_user_id") or "")
        has_token = bool(str(data.get("bot_token") or "").strip())
        if not has_token and uid and uid in claimed:
            log.info("restore skipped duplicate account sid=%s owner=%s",
                     session_id, uid[-12:])
            continue
        try:
            await manager.create_background(session_id, config=dict(session_config))
        except Exception as exc:  # noqa: BLE001 —— 一个会话起不来不能拖垮整个进程
            log.warning("session restore failed sid=%s err=%s", session_id, exc)
            continue
        if uid:
            claimed.add(uid)
        restored.append(session_id)
        if not has_token:
            pending_reauth.append(session_id)
    if restored:
        log.info("restored %d persisted session(s): %s",
                 len(restored), ", ".join(restored))
    if pending_reauth:
        log.warning(
            "restored %d session(s) waiting for a QR re-scan (no token): %s — "
            "该账号需要用户重新扫码才能恢复",
            len(pending_reauth), ", ".join(pending_reauth),
        )
    return restored


async def run_shared(
    config: SharedRuntimeConfig | Mapping[str, Any] | None = None,
    *,
    manager: Any | None = None,
    http: Any | None = None,
    app: web.Application | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the shared listener until cancelled or ``stop_event`` is set.

    All resources are cleaned in the reverse order of creation.  The HTTP
    session uses ``DummyCookieJar`` because iLink authorization is per request
    and cookies must never accidentally cross account boundaries.
    """

    if config is None:
        runtime = SharedRuntimeConfig.from_env()
    elif isinstance(config, SharedRuntimeConfig):
        runtime = config
    else:
        raw_config = dict(config)
        # Accept the names used by the legacy portal while keeping the public
        # dataclass's canonical names unambiguous.
        aliases = {
            "web_host": "host", "web_port": "port", "web_prefix": "prefix",
            "web_session_ttl": "session_ttl",
        }
        for old, new in aliases.items():
            if old in raw_config and new not in raw_config:
                raw_config[new] = raw_config[old]
            raw_config.pop(old, None)
        allowed = {field.name for field in SharedRuntimeConfig.__dataclass_fields__.values()}
        runtime = SharedRuntimeConfig(**{key: value for key, value in raw_config.items()
                                         if key in allowed})
    own_http = http is None
    if http is None:
        connector = aiohttp.TCPConnector(
            limit=runtime.http_connection_limit,
            limit_per_host=0,
            ttl_dns_cache=300,
        )
        http = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(), connector=connector
        )
    if manager is None:
        manager = BotManager(http)
    if app is None:
        # Default bootstrap: deep-copy the shared ``config.json`` and
        # merge the two shared env files (``llm.env`` + ``ima.env``) into a
        # per-session dictionary.  Per-account env files do not exist in
        # the session-token-only topology; see CLAUDE.md "Cleanup 2026-09-15".
        default_cfg = copy.deepcopy(load_default_config(config_dir=runtime.config_dir))
        env: dict[str, str] = {}
        for path in (runtime.env_dir / "llm.env", runtime.env_dir / "ima.env"):
            env.update(parse_env_file(path))
        _merge_llm_env(default_cfg, env)
        ima_env = {key: value for key, value in env.items() if key.startswith("IMA_")}
        if ima_env:
            default_cfg["ima_env"] = copy.deepcopy(ima_env)
        default_cfg["runtime_env"] = copy.deepcopy(env)
        web_config = {
            "session_ttl": runtime.session_ttl,
            "session_limit": runtime.session_limit,
            "rate_limit": runtime.rate_limit,
            "rate_window": runtime.rate_window,
            "cookie_secure": runtime.cookie_secure,
            "trust_proxy": runtime.trust_proxy,
            # cookie→会话绑定落盘：进程重启后浏览器还能接回原来的会话
            "browser_store_path": str(Path(runtime.state_dir) / "browser_sessions.json"),
            "session_config": {
                **copy.deepcopy(default_cfg),
                "state_dir": str(runtime.state_dir),
            },
        }
        app = build_web_app(manager, prefix=runtime.prefix, config=web_config)
        session_config = web_config["session_config"]
    else:
        # 外部注入的 app（测试用）：不猜它的 state_dir，直接跳过会话恢复
        session_config = None
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, runtime.host, runtime.port)
        await site.start()
        # 重启后把还带着 token 的会话续上，别让用户重新扫码
        if session_config:
            try:
                await _restore_persisted_sessions(manager, runtime, session_config)
            except Exception as exc:  # noqa: BLE001
                log.warning("session restore pass failed: %s", exc)
        await (stop_event or asyncio.Event()).wait()
    finally:
        stop_all = getattr(manager, "stop_all", None)
        if callable(stop_all):
            await _maybe_await(stop_all())
        await runner.cleanup()
        if own_http:
            await _maybe_await(http.close())


def _install_stop_signals(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, event.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows and embedded event loops may not support signal handlers;
            # cancellation still reaches run_shared's finally block.
            continue


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point for the one-process runtime."""

    parser = argparse.ArgumentParser(description="ClawBot shared multi-user runtime")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--prefix", default=None)
    args = parser.parse_args(argv)
    runtime = SharedRuntimeConfig.from_env()
    from bot import _redact_text, load_or_create_config
    from utils.logging_setup import setup_logging
    setup_logging(
        level=os.environ.get("CLAWBOT_LOG_LEVEL", "INFO"),
        log_file=Path(os.environ.get("CLAWBOT_SHARED_LOG_FILE", "logs/clawbot_shared.log")),
        redactor=_redact_text,
    )
    default_config_path = runtime.config_dir / "config.json"
    try:
        is_default_config_dir = runtime.config_dir.resolve() == Path(".").resolve()
    except OSError:
        is_default_config_dir = False
    if not default_config_path.exists() and is_default_config_dir:
        import sys
        if sys.stdin.isatty():
            load_or_create_config()
        else:
            log.warning("config.json missing; sessions will start without an AI provider")
    if args.host is not None:
        runtime.host = args.host
    if args.port is not None:
        runtime.port = args.port
    if args.prefix is not None:
        runtime.prefix = args.prefix

    async def runner() -> None:
        event = asyncio.Event()
        _install_stop_signals(asyncio.get_running_loop(), event)
        await run_shared(runtime, stop_event=event)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
