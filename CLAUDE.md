# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python 3.12+ client that speaks Tencent's [OpenClaw Weixin](https://github.com/Tencent/openclaw-weixin) iLink 2.4.6 HTTP protocol to log in to a personal WeChat account via QR code, long-poll `getupdates` for incoming text messages, and route them through an AI provider (DusAPI / DeepSeek) with optional Tencent ima knowledge-base RAG context. Protocol details and version diffs live in `weixin-openclaw-api-py-docs.md` (51 KB reference).

## Common commands

All commands assume the project root; the venv is `venv/`.

```bash
# Setup
pip install -r requirements.txt

# Run (first time: interactively choose provider, paste API key, scan QR)
python bot.py                              # or ./venv/bin/python bot.py

# Run with daemon-friendly web QR UI (default; CLAWBOT_WEB_ENABLED=1)
# Exposed via nginx: location ^~ /clawbot/ → 127.0.0.1:18300

# Diagnose ima knowledge bases (see docs/IMA_KB.md §3)
./venv/bin/python utils/list_ima_kb.py
./venv/bin/python utils/list_ima_kb.py --pick <name>          # fuzzy match + show env line
./venv/bin/python utils/list_ima_kb.py --pick <name> --update # rewrite .env in place

# Debug-level protocol trace (writes clawbot.api DEBUG to logs/clawbot.log)
CLAWBOT_LOG_LEVEL=DEBUG python bot.py 2>&1 | tee /tmp/debug.log

# Build portable U盘 bundle (one-dir PyInstaller; ~105 MB; see docs/PORTABLE.md)
./venv/bin/pyinstaller weixin-clawbot.spec
# Output: dist/weixin-clawbot/ — copy directory + .env to USB root
```

There is no test suite, no linter config, and no `Makefile`. The `pyinstaller` and `cairosvg` entries in `requirements.txt` are optional.

## High-level architecture

The project is one `asyncio` event loop with five concurrent concerns wired through a single `aiohttp.ClientSession`. Read in this order:

### 1. `bot.py` — main loop (~1900 lines, single file by design)
Top-to-bottom responsibilities:
- **Constants & config I/O** (`bot.py:39-326`): `RECONNECT_CONFIG`, `BASE_URL`, `CHANNEL_VERSION`, `ILINK_APP_*`, `PROVIDERS`, `mask_key`, `load_or_create_config`. `load_config_file` auto-migrates old flat configs into the multi-provider `{provider, providers: {...}}` shape.
- **iLink HTTP layer** (`bot.py:339-575`): `ILinkAPIError` carries `path / status / ret / errcode / network_type`; `api_get` / `api_post` wrap `aiohttp`, treat `asyncio.TimeoutError` as `{status: "wait"}` only when `long_poll=True`, otherwise raise. `ensure_business_success` checks both `ret` and `errcode` (HTTP 200 ≠ success). `make_headers` generates the random base64 `X-WECHAT-UIN` per request — do not cache it.
- **Redaction** (`bot.py:395-422`): `_redact_text` / `_redact_path` are the primary defense for `bot_token / context_token / verify_code / qrcode / ...`. `RedactFilter` in `utils/logging_setup.py` is a fallback only — every new `logger.info(...)` must pre-redact.
- **Send helpers** (`bot.py:578-711`): `send_msg_safe` degrades to console on failure (used for welcome / `/help` / reconnect warnings — see `docs/multi-user.md` §4); `send_typing_safe` re-raises on stale token; `get_typing_ticket_safe` caches `typing_ticket` per `user_id` for `CONFIG_CACHE_TTL` (24 h) with exponential backoff on failure.
- **Login** (`bot.py:984-1327`): `render_terminal_qr` / `save_qrcode_content` / `fetch_login_qrcode` / `poll_login_status` / `wait_login_confirmation` (verifies via web state OR stdin input) / `login_with_qrcode` (max `MAX_QR_REFRESH_COUNT=3` QR cycles within `qrcode_scan_timeout`). `BASE_URL` (`https://ilinkai.weixin.qq.com`) is only used for the first QR; after `scaned_but_redirect`, baseurl switches to the node returned by the server.
- **Lifecycle + main** (`bot.py:1329-1790`): `notify_lifecycle` posts `notifystart` / `notifystop`. `main()` orchestrates login → `apply_new_login` (atomic credential swap; clears cursor + contexts + `last_contact` if account id changes) → spawns `reconnect_timer_task` (lifecycle warnings at `warning_before`, force reconnect at `force_before`) and `message_loop` → `asyncio.gather` for graceful cancel of all tasks + final `notifystop`.
- **`message_loop` & `handle_message`** (`bot.py:1479-1761`): long-polls `getupdates`, advances `get_updates_buf`, dispatches each `msg` to `handle_message`. Stale responses (token/baseurl changed mid-poll) are dropped. On `is_stale_token` (`ret/errcode == -14`) it clears `bot_token`, sleeps `RETRY_DELAY`, runs a fresh `login_with_qrcode`, and continues. **Per-peer state** lives in `runtime_state["contexts"][from_id]` and is overwritten on each inbound message; `welcomed_users` is derived from `contexts.keys()` on startup so first-time users get `COMMANDS_MSG`.
- **AI layer** (`bot.py:1804-1886`): `_AIWithIma` is a transparent wrapper that calls `ImaClient.search_knowledge` before `base.chat` and injects snippets (`### 参考资料 N: title\nsnippet`) into the system prompt. If `ima.configured()` is false (no `IMA_ILINK_CLIENT_ID`/`API_KEY`), the wrapper passes through. `create_ai_client` picks `DeepSeekAPI` or `DusAPI` from `raw_cfg["provider"]`.

### 2. AI provider wrappers (`dusapi.py`, `deepseek.py`)
Same shape: `@dataclass *Config` + `*API` class with `chat(message, model=None, stream=False, prompt=None, history=None)`. Both are **synchronous `requests`**, 5 retries with `[2, 4, 8, 16, 32]`s backoff. DusAPI uses Anthropic `/v1/messages` format (model name decides parser: `claude` vs `gpt`). DeepSeek uses OpenAI `/chat/completions`; for `deepseek-v4-flash` it sets `thinking: {type: disabled}`. Both cap `max_tokens=1024`.

### 3. `ima.py` — Tencent ima Knowledge Base OpenAPI client
Sync `requests` wrapper, mirrors `dusapi.py`/`deepseek.py`. **First `.env` consumer** in the project: `ImaConfig.from_env()` calls `python-dotenv` with `override=False`. Missing creds → `configured()` returns False → caller passes through silently. Search is keyword-match (not embedding); see `docs/IMA_KB.md` §4–§5 for sharp edges. Optional rerank via `IMA_ILINK_RERANK=1` calls back into the base AI to re-score hits.

### 4. `qr_web.py` — embedded aiohttp web login UI (per-user backend)
Standalone aiohttp app bound to a per-user port (env `CLAWBOT_WEB_{ENABLED,TOKEN,HOST,PORT}`). Exposes: QR PNG, login state machine (`idle / qr_pending / scanned / logged_in / error`), and a verify-code POST endpoint. `QrFlowState` is the single source of truth — `bot.py` writes via `make_web_on_qrcode`, handlers read. Port collision → logs warning, returns, does not block the bot loop. `wait_for_verify_code` is shared between web and `stdin _safe_input` so the same code path works in TTY and daemon.

### 5. `qr_portal.py` — single-entry multi-user portal (`:18300`)
aiohttp multiplexer that reads `/etc/clawbot/*.env` to enumerate users. `GET /` shows picker if no cookie, otherwise proxies to the selected user's `qr_web.py`. `GET /select?name=<user>` sets 30-day `clawbot_user` cookie; `GET /switch` clears it. HTML responses get an injected "切换用户" bar (z-index:99999, top-right) so users can return to the picker from any proxied backend page. Non-HTML responses stream through. Auth is handled by injecting `Authorization: Bearer <user.CLAWBOT_WEB_TOKEN>` from each user's env file — qr_web's `_check_bearer` validates it.

### 6. `utils/logging_setup.py` — single log init
`setup_logging()` is called once at `__main__` entry. Idempotent. Console respects `CLAWBOT_LOG_LEVEL`, file always DEBUG, rotates at midnight local time, keeps 7 backups. Sub-loggers use `get_logger("qr")` → `clawbot.qr`. `RedactFilter` takes the `_redact_text` callback and is wired onto both handlers.

### 7. `utils/list_ima_kb.py` — diagnostic CLI
Lists ima knowledge bases for the configured account; `--pick <substr>` fuzzy-matches and prints the env line; `--update` rewrites `.env` in place. Useful when `IMA_ILINK_DEFAULT_KB` needs to change.

## State, config, and secrets layout

| Path | Lifecycle | Sensitive? |
|---|---|---|
| `config.json` | first run, rewritten on every config edit | yes (API keys) |
| `weixin_state.json` | rewritten on every `getupdates` cursor advance, every `contexts` write, every reconnect | yes (`bot_token`, `context_token`) |
| `logs/clawbot.log` | daily rolling, 7-day retention | partial (filter redacts) |
| `.env` | read once at startup by `ImaConfig.from_env` (override=False, env wins) | yes (`IMA_ILINK_API_KEY`) |

`weixin_state.json` keys: `bot_token`, `baseurl`, `ilink_bot_id`, `ilink_user_id`, `get_updates_buf`, `contexts` (dict keyed by `from_id`, value = `context_token`), `last_contact` (`{from_id, context_token}`).

## Multi-user layout (`--user` + portal)

Single-tenant is the default (no `--user` → `weixin_state.json`, `config.json`, `logs/clawbot.log`, port from env). With `--user <name>`:

| Path | Form |
|---|---|
| state file | `weixin_state_<name>.json` |
| config file | `config_<name>.json` |
| log file | `logs/clawbot_<name>.log` |
| env files | `/etc/clawbot/<name>.env` (user) → `/etc/clawbot/ima.env` (shared ima fallback) |
| web port | `CLAWBOT_WEB_PORT` from user env (e.g. 18301+) |
| systemd unit | `clawbot@<name>.service` |

`/etc/clawbot/ima.env` is the shared ima fallback (currently the production ima key). A user gets their own ima by adding `IMA_ILINK_CLIENT_ID` / `IMA_ILINK_API_KEY` / `IMA_ILINK_DEFAULT_KB` to their `<user>.env`; `load_dotenv(override=False)` puts user values first.

`qr_portal.py` runs on `:18300` as the single entry point: `https://bx.mengxa.com/clawbot/`. Picker reads `/etc/clawbot/*.env`, dispatch uses 30-day cookie `clawbot_user`, HTML gets a "切换用户" injection bar, upstream auth is `Authorization: Bearer <user.CLAWBOT_WEB_TOKEN>` injected per-request. Backends (per-user `qr_web.py`) keep their existing routes — `qr_web.py`'s `?token=` query path still works.

Operations (nginx stays simple):
```bash
sudo systemctl stop clawbot.service           # disable legacy single-tenant
sudo systemctl enable --now clawbot-portal.service
sudo cp /etc/clawbot/user.env.example /etc/clawbot/alice.env
sudo $EDITOR /etc/clawbot/alice.env          # CLAWBOT_WEB_PORT, CLAWBOT_WEB_TOKEN
sudo systemctl enable --now clawbot@alice
sudo nginx -t && sudo systemctl reload nginx
```

## Critical invariants

- **HTTP 200 ≠ success**: always call `ensure_business_success(result, path)` after `api_get` / `api_post`. The `ret` and `errcode` fields are checked independently.
- **`ret/errcode == -14` (stale token) must propagate**, not be swallowed. `send_msg_safe` re-raises; `message_loop` catches it and runs a controlled relogin.
- **Long-poll timeouts are normal**: in `api_get`/`api_post`, an `asyncio.TimeoutError` with `long_poll=True` returns `{"status": "wait", "_timeout": True}` (GET) or `{"ret": 0, "msgs": [], "get_updates_buf": fallback_cursor, "_timeout": True}` (POST). Do not treat these as failures.
- **`get_updates_buf` is opaque**: store verbatim, forward verbatim. Only the iLink server can advance it.
- **`context_token` must come from the inbound message** and is required by `sendmessage`. Token rotation across reconnect/reinstall is expected.
- **Account switch (`ilink_bot_id` change) clears `get_updates_buf`, `contexts`, `last_contact`, and `welcomed_users`**. Same-account relogin preserves them.
- **First QR is fixed endpoint `BASE_URL = https://ilinkai.weixin.qq.com`**. After `scaned_but_redirect`, switch to the server-returned `baseurl`. This switch is one-way per session.
- **Headers**: never set `Content-Length` manually (aiohttp computes it). The `X-WECHAT-UIN` is a fresh random uint32 → base64 per request. Each POST body includes `base_info: {channel_version: "2.4.6", bot_agent: "weixin-ClawBot-API/1.2.0 (python)"}` (sanitized via `sanitize_bot_agent`).
- **Media messages are out of scope** for now: image / file / untranscribed voice return a capability hint and are not passed to AI. AES-128-ECB + CDN upload/download not implemented.

## Logger namespace

All diagnostic logs go through `logging.getLogger("clawbot.<subsystem>")`:

| Logger | Covers |
|---|---|
| `clawbot.web` | aiohttp web server (bind / req / QR png / svg render / verify submit) |
| `clawbot.qr` | QR login lifecycle (fetch / poll / wait / refresh) |
| `clawbot.message` | long-poll + sendmessage + getconfig + sendtyping |
| `clawbot.reconnect` | reconnect flow + lifecycle notifications |
| `clawbot.ai` | AI wrapper (elapsed, exceptions) |
| `clawbot.api` | every iLink HTTP (DEBUG only) |
| `clawbot.state` | `weixin_state.json` read/write |
| `clawbot.ima` | ima search / write / config |

User-facing output (banners, menu, command echo) stays on `print` — don't move it to logger.

## References inside this repo

- `README.md` — quickstart, `RECONNECT_CONFIG` table, OpenClaw protocol summary, 5-line diagnosis grep recipes for `logs/clawbot.log`.
- `docs/IMA_KB.md` — ima integration sharp edges (keyword-match vs semantic, no body return, rerank behavior) + `list_ima_kb.py` usage.
- `docs/multi-user.md` — single-tenant limits, scenario A vs B, comparison with XTmai reference impl, recommended fixes (#2 broadcast, #3 gather, #4 retry) ranked by ROI.
- `docs/PORTABLE.md` — PyInstaller `--onedir` build steps, USB layout, `noexec` mount workaround.
- `TODO.md` — current priorities (per-user history is P0; reconnect broadcast / message-loop gather are P1; tracked with `bot.py:line` refs).
- `weixin-openclaw-api-py-docs.md` — full 2.4.6 protocol reference; consult before changing header shape, request body schema, or response parsing.
- `weixin-clawbot.spec` — PyInstaller recipe; regenerate via `pyi-makespec` whenever `requirements.txt` or top-level imports change.

## Adding a new logger / feature

When adding code that emits logs or errors:
1. Use `get_logger("subsystem")` from `utils/logging_setup.py` — do not call `setup_logging()` again.
2. Pre-redact any sensitive value with `bot._redact_text()` / `_redact_path()` or print only first 8 chars (`token={tok[:8]}…`). The `RedactFilter` is a safety net, not the primary defense.
3. New failure paths in `message_loop` should distinguish stale-token (`exc.is_stale_token`) from transient network (`exc.network_type` ∈ `dns / tcp / tls / timeout / unknown`) from generic `Exception` — see the existing handler at `bot.py:1707-1761` for the canonical pattern.
4. `handle_message` currently serializes per message via `for msg in msgs: await handle_message(msg)` (`bot.py:1705`). If converting to `asyncio.gather`, watch `welcomed_users.add` / `last_contact` writes — they need a lock or hoisting outside the gather (see `TODO.md` P1 #3).
