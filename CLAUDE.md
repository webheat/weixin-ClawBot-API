# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python 3.12+ client that speaks Tencent's [OpenClaw Weixin](https://github.com/Tencent/openclaw-weixin) iLink 2.4.6 HTTP protocol to log in to a personal WeChat account via QR code, long-poll `getupdates` for incoming text messages, and route them through an AI provider (DusAPI / DeepSeek) with optional Tencent ima knowledge-base RAG context. Protocol details and version diffs live in `weixin-openclaw-api-py-docs.md` (51 KB reference).

## Current state (read first)

- **Recent cleanup (2026-09-15) — ephemeral-only**. `python bot.py` 默认进入 `shared_runtime.py`：一个 `BotManager` 管理 N 个隔离的 `BotSession`，共享一个 `aiohttp` 连接池和一个 `shared_web` 登录入口。**只有 ephemeral 用户**（`eph_<hex>` 在 `shared_web.py:397` 铸造）—— 没有命名用户、没有 OAuth、没有 `--user` / `--legacy-single` / `--named-user` CLI。opaque cookie + CSRF + 限流 + TTL GC 是唯一入口。已删除：`qr_portal.py`、`utils/bot_launcher.py`、`utils/oauth_bindings.py`、`wechat_oauth.py`、`docs/WECHAT_OAUTH.md`、`docs/multi-user.md`、`docs/EPHEMERAL_BOT_LIFECYCLE.md`。
- **Recent feature (2026-09-16) — per-user IMA KB binding**. Each bot owner (`ilink_user_id`, NOT `ilink_bot_id` which rotates per QR) can bind their own IMA knowledge base; the binding survives browser cookie death, device switch, and server restart because it lives in a separate file `CLAWBOT_STATE_DIR/ima_bindings.json`. Read [`docs/IMA_PER_USER_BINDING.md`](docs/IMA_PER_USER_BINDING.md) before touching `_AIWithIma.chat` for per-user routing. Three binding entry points: web UI `/clawbot/ima/bind`, WeChat `/bindkb <kb_id>` (owner-gated), `utils/ima_bindings.py` CLI for ops.
- **Active P0 — per-user conversation history** (in progress). Bot currently sees only the last message; "我刚才说了什么" / "继续上次" both fail. Plan: persist `runtime_state["contexts"][from_id]["history"]` (list of `{"role","content","ts"}`) into `weixin_state.json` so it survives restart; sliding-window cap N=20 (or token-budget trim); rename `ai.chat(text)` → `ai.chat_with_history(messages, system_prompt)` and update `_AIWithIma` to merge history with `ima` snippets. Exclude `/help` / `/time` / `/重新连接` and the `COMMANDS_MSG` welcome from history; private chat only (no group messages). Risk: context bloat → token cost; need a cap and a monitor. Full TODO in `TODO.md`.
- **Recent big change (2026-09-07)** — full IMA pipeline rewrite + 150 Q&A ingested into a fresh KB. Full timeline, decisions, and sharp edges are in `docs/SESSION_2026-09-07_ima_pipeline.md`. The four new IMA knobs (`IMA_ILINK_KEYWORD_EXTRACT` / `IMA_ILINK_FETCH_BODY` / local fallback / `llm_caveat`) all live in `ima.py` and are routed via `_AIWithIma.chat`'s 3-state machine — read that section before changing AI routing.
- **Fast Q&A lookup** — `docs/knowledge/` holds ~30 short notes indexed `qa-NNN-<topic>` (ima search keywords, context_token, X-WECHAT-UIN, ret=-14, etc.). Grep by topic when you hit an unfamiliar failure mode before reading the full 51 KB protocol reference.

## Product context: IMA vs Obsidian

This project references two very different products. **Don't conflate them.**

- **Tencent ima** (`ima.qq.com`) — Tencent's cloud **AI knowledge workspace**. KBs live on Tencent servers (`KBT_MINE_KB` personal / `KBT_SHARED_KB` team / `KBT_SUBSCRIBED_CREATE_KB` publishable "知识号"). Built-in AI answering, indexing is **server-side async with 5–15 min delay**, retrieval in the public OpenAPI is **literal substring match** (no embedding on the API path), and bodies often come back empty until `highlight_content` fills in. Implemented in `ima.py`.
- **Obsidian** — a **local Markdown editor + reverse-link note tool**. Data lives as plain `.md` files (optionally with YAML frontmatter) under a directory; Obsidian.app reads them directly, no server. Anything that edits text can edit them; `git diff`, `cp`, `grep` all work natively. AI is a plugin, not core. **Not yet wired into this project** — only listed as future work in `docs/SESSION_2026-09-07_ima_pipeline.md:236` ("Obsidian 当编辑源、IMA 当检索源" 双向同步方案 A).

The 197 files in `docs/knowledge/` follow the Obsidian-vault convention (frontmatter + directory layout) but are read by `utils/local_kb.py` directly — no Obsidian.app process is involved. They can later be edited in Obsidian.app without code changes (frontmatter is stripped before scoring, `local_kb.py:29-40`).

One unified decision table covers both the product choice and the runtime code path:

| If you need… | Use | Why |
|---|---|---|
| Multi-user editing, cross-device access, official AI answering | **ima** (production) | Service-side KB; supports `KEYWORD_EXTRACT` + `FETCH_BODY` + `RERANK`. Shared creds via `/etc/clawbot/llm.env` + `/etc/clawbot/ima.env` (no per-user env files) |
| **One KB per bot owner (each WeChat user has their own)** | **ima + `utils/ima_bindings.py`** | Bindings keyed by `ilink_user_id` (WeChat-account-stable), persisted to `CLAWBOT_STATE_DIR/ima_bindings.json`. Survives `eph_*` cookie death, device switch, server restart. See [`docs/IMA_PER_USER_BINDING.md`](docs/IMA_PER_USER_BINDING.md). UI: `/clawbot/ima/bind` or WeChat `/bindkb <kb_id>`. |
| Publishing content externally ("知识号") | **ima** | `KBT_SUBSCRIBED_CREATE_KB` is the native publish channel |
| New content not yet indexed by IMA (5–15 min lag) | **ima + local fallback + semantic** (推荐) | 三层 fallback：IMA 命中走 `llm+ima`；0 命中走 `llm+local`（CLAWBOT_LOCAL_FALLBACK=1）；仍 0 命中走 `llm+semantic`（SEMANTIC_KB_ENABLED=1）。路由转换在 `logs/clawbot.log` 看 `mode=... reason=...` |
| Offline / no `ima.qq.com` egress / privacy-sensitive | **LocalKBIndex** only | `CLAWBOT_LOCAL_FALLBACK=1`; clear all `IMA_ILINK_*` to force `mode=llm+local` |
| Already maintain an Obsidian vault to reuse as KB | **LocalKBIndex pointed at vault** | `CLAWBOT_LOCAL_KB_DIR=/path/to/vault` — `LocalKBIndex.rglob("*.md")` + mtime auto-rebuild; frontmatter stripped to avoid `tags:` false hits (`local_kb.py:29-40`) |
| Fast RAG for an LLM agent / low latency | **LocalKBIndex** | Zero network, no auth, no index delay; substring match but enough for keyword queries |
| Long-term personal/team second-brain with Git history, never lose data | **Obsidian** | Plain files; format doesn't rot; `git diff` works on `.md` |
| Data sovereignty / cannot send content to any cloud | **Obsidian** or **LocalKBIndex** | 100% local; no API call ever leaves the machine |
| Want the LLM to **know** it's answering without KB | **Plain LLM** | Leave IMA creds empty → `mode=llm-only reason=ima-not-configured`; `_LLM_ONLY_CAVEAT_PROMPT` (`bot.py:64`) injects "（未参考知识库）" caveat |
| Bot 端需要**语义检索**（自然语言 → 整句不切词） | **`utils/semantic_kb.py`** (`SEMANTIC_KB_ENABLED=1`) | BAAI/bge-small-zh-v1.5 + fastembed（onnxruntime，无 torch），零网络（除首次模型下载）；落库到 `docs/.semantic_kb.sqlite3`；150 chunks 在毫秒级返回。**国内服务器必加 `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`** |
| Debugging baseline LLM with no KB interference | **Plain LLM** | Same as above; fastest way to isolate the model's own answer |
| Mix: edit in Obsidian, serve/answer from ima | **方案 A** (future) | `SESSION_2026-09-07_ima_pipeline.md:236` — not yet built |

Hard limits both share — **neither does semantic/embedding retrieval by default** (IMA's OpenAPI surface lacks it; `LocalKBIndex` is zero-deps by design: `local_kb.py:1-14`). If you need true semantic search at the bot level, that's a **new module + dependency** (`utils/semantic_kb.py` + fastembed) — see the "Bot 端需要语义检索" row above and `_AIWithIma`'s 4-mode routing.

## Common commands

All commands assume the project root; the venv is `venv/`. First-run setup (provider selection, API key entry, QR scan) is in `README.md`; this block is the day-2 cheat sheet.

```bash
# Run the default one-process service (:18300/clawbot/) — only entry point.
# Sessions are created on demand by anonymous web visitors hitting
# POST /ephemeral/start; user_id is `eph_<hex(16)>` minted at shared_web.py:397.
python bot.py
./venv/bin/python bot.py

# Diagnose ima knowledge bases (see docs/IMA_KB.md §3)
./venv/bin/python utils/list_ima_kb.py
./venv/bin/python utils/list_ima_kb.py --pick <name>          # fuzzy match + show env line
./venv/bin/python utils/list_ima_kb.py --pick <name> --update # rewrite .env in place

# Per-user IMA KB bindings (see docs/IMA_PER_USER_BINDING.md)
./venv/bin/python utils/ima_bindings.py list                                    # show all ilink_user_id → kb_id bindings
./venv/bin/python utils/ima_bindings.py lookup o9...@im.wechat                 # show one binding
./venv/bin/python utils/ima_bindings.py unbind  o9...@im.wechat                 # ops-only unbind (web UI path is /clawbot/ima/unbind)

# Debug-level protocol trace (writes clawbot.api DEBUG to logs/clawbot.log)
CLAWBOT_LOG_LEVEL=DEBUG python bot.py 2>&1 | tee /tmp/debug.log

# Build portable U盘 bundle (one-dir PyInstaller; ~105 MB; see docs/PORTABLE.md)
./venv/bin/pyinstaller weixin-clawbot.spec
# Output: dist/weixin-clawbot/ — copy directory + .env to USB root
```

There is no test suite, no linter config, and no `Makefile`. The `pyinstaller` and `cairosvg` entries in `requirements.txt` are optional. 5-line diagnosis grep recipes live in `README.md` — do not duplicate them here.

## High-level architecture

The project is one `asyncio` event loop with five concurrent concerns wired through a single `aiohttp.ClientSession`. Read in this order:

### 1. Shared runtime and account sessions

- `bot.py` remains the stateless iLink protocol/AI compatibility module; its default CLI dispatches to `shared_runtime.main()`.
- `shared_runtime.py` owns one `DummyCookieJar` HTTP pool, one `BotManager`, the web listener, named-user discovery, explicit per-user env/config loading, signal handling and shutdown.
- `bot_manager.py` deduplicates same-user startup without holding its lock across network awaits and evicts sessions whose permanent task dies.
- `bot_session.py` owns every mutable per-account value and the login/message/timer/relogin tasks. Inbound batches are persisted before handling and their cursor advances only after confirmed reply delivery.

### 2. `bot.py` — iLink protocol and legacy runner
Top-to-bottom responsibilities:
- **Constants & config I/O** (`bot.py:39-326`): `RECONNECT_CONFIG`, `BASE_URL`, `CHANNEL_VERSION`, `ILINK_APP_*`, `PROVIDERS`, `mask_key`, `load_or_create_config`. `load_config_file` auto-migrates old flat configs into the multi-provider `{provider, providers: {...}}` shape.
- **iLink HTTP layer** (`bot.py:339-575`): `ILinkAPIError` carries `path / status / ret / errcode / network_type`; `api_get` / `api_post` wrap `aiohttp`, treat `asyncio.TimeoutError` as `{status: "wait"}` only when `long_poll=True`, otherwise raise. `ensure_business_success` checks both `ret` and `errcode` (HTTP 200 ≠ success). `make_headers` generates the random base64 `X-WECHAT-UIN` per request — do not cache it.
- **Redaction** (`bot.py:395-422`): `_redact_text` / `_redact_path` are the primary defense for `bot_token / context_token / verify_code / qrcode / ...`. `RedactFilter` in `utils/logging_setup.py` is a fallback only — every new `logger.info(...)` must pre-redact.
- **Send helpers** (`bot.py:578-711`): `send_msg_safe` degrades to console on failure (used for welcome / `/help` / reconnect warnings — see `docs/multi-user.md` §4); `send_typing_safe` re-raises on stale token; `get_typing_ticket_safe` caches `typing_ticket` per `user_id` for `CONFIG_CACHE_TTL` (24 h) with exponential backoff on failure.
- **Login** (`bot.py:984-1327`): `render_terminal_qr` / `save_qrcode_content` / `fetch_login_qrcode` / `poll_login_status` / `wait_login_confirmation` (verifies via web state OR stdin input) / `login_with_qrcode` (max `MAX_QR_REFRESH_COUNT=3` QR cycles within `qrcode_scan_timeout`). `BASE_URL` (`https://ilinkai.weixin.qq.com`) is only used for the first QR; after `scaned_but_redirect`, baseurl switches to the node returned by the server.
- **Lifecycle + main** (`bot.py:1329-1790`): `notify_lifecycle` posts `notifystart` / `notifystop`. `main()` orchestrates login → `apply_new_login` (atomic credential swap; clears cursor + contexts + `last_contact` if account id changes) → starts the `message_loop` (the iLink long poll is the keepalive) and only enables the legacy `reconnect_timer_task` when `proactive_relogin=True` → `asyncio.gather` for graceful cancel of all tasks + final `notifystop`.
- **`message_loop` & `handle_message`** (`bot.py:1479-1761`): long-polls `getupdates`, advances `get_updates_buf`, dispatches each `msg` to `handle_message`. Stale responses (token/baseurl changed mid-poll) are dropped. On `is_stale_token` (`ret/errcode == -14`) it clears `bot_token`, sleeps `RETRY_DELAY`, runs a fresh `login_with_qrcode`, and continues. **Per-peer state** lives in `runtime_state["contexts"][from_id]` and is overwritten on each inbound message; `welcomed_users` is derived from `contexts.keys()` on startup so first-time users get `COMMANDS_MSG`.
- **AI layer** (`bot.py:1804-1886`): `_AIWithIma` is a transparent wrapper that calls `ImaClient.search_knowledge` before `base.chat` and injects snippets (`### 参考资料 N: title\nsnippet`) into the system prompt. If `ima.configured()` is false (no `IMA_ILINK_CLIENT_ID`/`API_KEY`), the wrapper passes through. `create_ai_client` picks `DeepSeekAPI` or `DusAPI` from `raw_cfg["provider"]`.
- **4-mode AI routing** (新增；详见 `utils/semantic_kb.py` 和 `utils/local_kb.py`): `_AIWithIma.chat` 是 **4 档 state machine**，按 `ima → local → semantic → llm_only` 顺序 fallback：
  - `mode=llm+ima` (`reason=hits-injected`) —— IMA 命中并注入 prompt
  - `mode=llm+local` (`reason=local-fallback`) —— IMA 0 命中，BM25 substring 兜底（`CLAWBOT_LOCAL_FALLBACK=1`）
  - `mode=llm+semantic` (`reason=semantic-fallback`) —— IMA + local 都 0 命中，向量相似度兜底（`SEMANTIC_KB_ENABLED=1` + fastembed）
  - `mode=llm-only` —— 全部未命中或未配置，注入 `_LLM_ONLY_CAVEAT_PROMPT`（`bot.py:64`）让 LLM 自我标记"未参考知识库"

### 3. AI provider wrappers (`dusapi.py`, `deepseek.py`)
Same shape: `@dataclass *Config` + `*API` class with `chat(message, model=None, stream=False, prompt=None, history=None)`. Both are **synchronous `requests`**, 5 retries with `[2, 4, 8, 16, 32]`s backoff. DusAPI uses Anthropic `/v1/messages` format (model name decides parser: `claude` vs `gpt`). DeepSeek uses OpenAI `/chat/completions`; for `deepseek-v4-flash` it sets `thinking: {type: disabled}`. Both cap `max_tokens=1024`.

### 4. `ima.py` — Tencent ima Knowledge Base OpenAPI client
Sync `requests` wrapper, mirrors `dusapi.py`/`deepseek.py`. **First `.env` consumer** in the project: `ImaConfig.from_env()` calls `python-dotenv` with `override=False`. Missing creds → `configured()` returns False → caller passes through silently. Search is keyword-match (not embedding); see `docs/IMA_KB.md` §4–§5 for sharp edges.

**Five runtime knobs (env-driven)**; defaults match what production runs today — change only with care:
- `IMA_ILINK_RERANK` (default `0`) — call back into base AI to re-score hits after search.
- `IMA_ILINK_RERANK_TOP_K` (default `3`) — how many reranked hits to keep.
- `IMA_ILINK_FETCH_BODY` (default `0`) — after search, hit `get_doc_content` per hit to fill in missing bodies (works around `highlight_content` being empty; +1–3 s).
- `IMA_ILINK_KEYWORD_EXTRACT` (default `0`) — pre-search LLM call extracts 1–3 keywords so natural-language questions like "IMA是什么？" can hit the KB.
- `IMA_ILINK_LOCAL_FALLBACK` — when IMA returns 0 hits, transparently try `utils/local_kb.py` (offline KB shipped with the project) before falling back to plain LLM. Set `llm_caveat` mode to make the LLM advertise when it's answering without KB context.
- `ImaClient.list_searchable_kbs()` (`ima.py:520`) — enumerates `KBT_SHARED_KB` + `KBT_SUBSCRIBED_CREATE_KB` for the web UI dropdown; filters out `KBT_MINE_KB` because `search_knowledge` returns `code=220004` for personal KBs. Constant `ImaClient.SEARCHABLE_KB_TYPES` (`ima.py:310`) is the whitelist; per-user binding via `IMABindings.bind(..., kb_type=...)` also enforces the same set.

`_AIWithIma.chat` is a 4-state machine (`ima` / `local` / `semantic` / `llm_only`) and logs `mode=` / `reason=` / `hits=` / `ctx_chars=` on every call — that line in `logs/clawbot.log` is the canonical answer to "did the KB fire?". For per-user routing, `chat()` also accepts `kb_id=...` (a `knowledge_base_id` override resolved from `IMABindings.lookup_kb_id(ilink_user_id)` at `handle_message` time); when `kb_id is None`, behavior is unchanged (falls back to `ImaConfig.default_knowledge_base_id`). Local and semantic fallbacks are owner-independent and intentionally NOT gated by `kb_id`.

**Hard limit:** neither IMA nor `LocalKBIndex` does embedding/semantic search (substring keyword match only). If you need true semantic retrieval, that's a new dependency, not a config knob. See the decision table under "Product context: IMA vs Obsidian" for which code path to use.

### 5. `shared_web.py` and `qr_web.py`
`shared_web.py` is the default single listener. It maps opaque browser cookies to Manager sessions and exposes QR state, verification, account switching and ephemeral creation under `/clawbot`. `qr_web.py` still provides the per-session `QrFlowState` and QR conversion helpers; its old standalone per-user server is legacy only.

### 6. `utils/` — diagnostics, KB tools, logging

All five utilities are runnable as `python utils/<name>.py` from the project root.

- **`logging_setup.py`** — `setup_logging()` is called once at `__main__` entry. Idempotent. Console respects `CLAWBOT_LOG_LEVEL`, file always DEBUG, rotates at midnight local time, keeps 7 backups. Sub-loggers use `get_logger("qr")` → `clawbot.qr`. `RedactFilter` takes the `_redact_text` callback and is wired onto both handlers.
- **`list_ima_kb.py`** — diagnostic CLI. Lists ima knowledge bases for the configured account; `--pick <substr>` fuzzy-matches and prints the env line; `--update` rewrites `.env` in place. Useful when `IMA_ILINK_DEFAULT_KB` needs to change.
- **`ima_bindings.py`** — per-user IMA KB binding store. Keyed by `ilink_user_id` (WeChat-account-stable, NOT `eph_<hex>` cookie id). Persists `ilink_user_id → {kb_id, kb_name, kb_type, bound_at, bot_id_at_bind}` to `CLAWBOT_STATE_DIR/ima_bindings.json` with `asyncio.Lock` + atomic write + `chmod 0o600`. CLI: `list` / `lookup <user>` / `unbind <user>` — `bind` is intentionally NOT exposed (must go through `/clawbot/ima/bind` for CSRF + cookie auth). Rejects `kb_type=1001` (`KBT_MINE_KB`) because search returns `code=220004` (`docs/IMA_KB.md:126-139`).
- **`local_kb.py`** — offline fallback KB (JSON-serialised Q&A pairs) consulted when IMA returns 0 hits and `IMA_ILINK_LOCAL_FALLBACK` is on. Format is the same one `import_business_lang.py` writes — keep them in sync.
- **`seed_ima_kb.py`** — bulk-ingest a directory of markdown / text into a target ima KB using `ImaClient.import_doc`. Used for the 150 Q&A bootstrap on 2026-09-07.
- **`import_business_lang.py`** — convenience importer that normalises a 客服话术 corpus into the local KB + ima KB shape (covers chunking, dedupe, keyword tagging). Run before `seed_ima_kb.py` if the corpus is in the raw 话术 / transcript form.

## State, config, and secrets layout

| Path | Lifecycle | Sensitive? |
|---|---|---|
| `config.json` | first run, rewritten on every config edit | yes (API keys) |
| `weixin_state_eph_<hex>.json` | atomically rewritten per session after messages, cursor commits and reconnects | yes (`bot_token`, `context_token`) |
| `CLAWBOT_STATE_DIR/ima_bindings.json` | rewritten per `IMABindings.bind` / `unbind` (web UI or `/bindkb`); survives `eph_*` cookie death | yes (`ilink_user_id` is sensitive; `kb_id` is not) |
| `logs/clawbot_shared.log` | daily rolling shared log with a per-record user dimension | partial (filter redacts) |
| `/etc/clawbot/{llm,ima}.env` | explicitly parsed into per-session dictionaries without mutating `os.environ` | yes (`IMA_ILINK_API_KEY`) |

State keys: `bot_token`, `baseurl`, `ilink_bot_id`, `ilink_user_id`, `get_updates_buf`, `pending_messages`, `processed_message_ids`, `contexts` and `last_contact`.

## Multi-user layout (ephemeral-only)

`python bot.py` is the only entry point. Anonymous web visitors receive a random `eph_<hex(16)>` id minted at `shared_web.py:397` and known only to the server-side cookie store. No named users, no OAuth, no per-user subprocess / port / systemd unit — sessions live as in-process `BotSession` tasks under one shared `BotManager` and one shared `aiohttp` connection pool.

| Path | Form |
|---|---|
| state file | `CLAWBOT_STATE_DIR/weixin_state_eph_<hex>.json` |
| config file | ephemeral deep-copies `CLAWBOT_CONFIG_DIR/config.json` at session create time |
| log file | `logs/clawbot_shared.log`, every protocol task carries `[user=<id>]` |
| env files | `/etc/clawbot/llm.env` → `ima.env`, parsed into per-session dictionaries without mutating `os.environ` |
| web port | one process-level `CLAWBOT_WEB_PORT` (default 18300) |
| systemd unit | one shared service only; none per user |

The shared page never lists users. `POST /ephemeral/start` allocates one random in-process session and sets an opaque server-mapped cookie; `/switch` retains that binding while requesting a new iLink QR. Browser TTL cleanup removes the web binding, but keeps a session that already owns an iLink token running so WeChat/OpenClaw interaction is independent of browser activity; only an unauthenticated QR session is stopped. Cookie Secure is auto-selected from the request scheme; set `CLAWBOT_TRUST_PROXY=1` only when a trusted edge overwrites forwarding headers, or force it with `CLAWBOT_COOKIE_SECURE=1`.

Typical service operation:
```bash
sudo systemctl enable --now clawbot-shared.service
sudo nginx -t && sudo systemctl reload nginx
# 日志：journalctl -u clawbot-shared -f  或  tail -f logs/clawbot_shared.log
```

## Critical invariants

- **HTTP 200 ≠ success**: always call `ensure_business_success(result, path)` after `api_get` / `api_post`. The `ret` and `errcode` fields are checked independently.
- **`ret/errcode == -14` (stale token) must propagate**, not be swallowed. `send_msg_safe` re-raises; `message_loop` catches it and runs a controlled relogin.
- **Long-poll timeouts are normal**: in `api_get`/`api_post`, an `asyncio.TimeoutError` with `long_poll=True` returns `{"status": "wait", "_timeout": True}` (GET) or `{"ret": 0, "msgs": [], "get_updates_buf": fallback_cursor, "_timeout": True}` (POST). Do not treat these as failures.
- **`get_updates_buf` is opaque**: store verbatim, forward verbatim. Only the iLink server can advance it.
- **`context_token` must come from the inbound message** and is required by `sendmessage`. Token rotation across reconnect/reinstall is expected.
- **Account switch (`ilink_bot_id` change) clears `get_updates_buf`, `contexts`, `last_contact`, and `welcomed_users`**. Same-account relogin preserves them.
- **`ilink_user_id` is the stable identity for per-user IMA KB binding** — NOT `ilink_bot_id` (rotates per QR; see `docs/2026-09-15_IN_FLIGHT_RELOGIN_RACE.md:392-410` + commit `f298d82`) and NOT `from_user_id` (chat peer, not bot owner). Bindings live in `ima_bindings.json`, **not** inside `weixin_state_eph_*.json` (the latter dies with the browser cookie and would break cross-device survival). Web UI `/ima/bind` always re-validates submitted `kb_id` against live `ImaClient.list_searchable_kbs()` to defeat client spoofing of `kb_name` / `kb_type`.
- **Portal never leaks user list (privacy)**: 无 cookie 访客的页面**必须**是单一登录按钮（OAuth / ephemeral / 错误页），**不得**渲染任何用户、端口、env 文件路径、bot 状态。`/healthz` 只返 `{ok: True, login_mode: ...}` 不带 `users` 字段。proxy 502 fallback 不暴露 name/port/exception。`/select` 已删除（OAuth 模式下任何人 GET `/select?name=` 已知用户即拿到 cookie = 账号劫持；现在 cookie 只能由 `/oauth/cb` 或 `/ephemeral/start` 设置）。
- **First QR is fixed endpoint `BASE_URL = https://ilinkai.weixin.qq.com`**. After `scaned_but_redirect`, switch to the server-returned `baseurl`. This switch is one-way per session.
- **Headers**: never set `Content-Length` manually (aiohttp computes it). The `X-WECHAT-UIN` is a fresh random uint32 → base64 per request. Each POST body includes `base_info: {channel_version: "2.4.6", bot_agent: "weixin-ClawBot-API/1.2.0 (python)"}` (sanitized via `sanitize_bot_agent`).
- **Media messages are out of scope** for now: image / file are not passed to AI; an untranscribed voice gets an actionable retry/“转文字” prompt and is not passed to AI. AES-128-ECB + CDN upload/download not implemented.
- **Forwarded message text extraction** (`bot.py:821` `extract_message_text`): reads `text_item.text`, `voice_item.text`, `ref_msg.{title,message_item.text_item.text}`, `title` / `description` / `des`, `app_msg.{title,des}`, and `url` (rewritten as `[链接] url`) from every item in `item_list`. Deduplicates with `text not in parts` so quoted / ref_msg overlap doesn't double-feed the LLM. URL is treated as text only — never fetched. See `docs/FORWARDED_MESSAGES.md`.

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
| `clawbot.ima_bindings` | per-user `ima_bindings.json` lookup / bind / unbind |

User-facing output (banners, menu, command echo) stays on `print` — don't move it to logger.

## References inside this repo

- `README.md` — quickstart, `RECONNECT_CONFIG` table, OpenClaw protocol summary, 5-line diagnosis grep recipes for `logs/clawbot.log`.
- `docs/IMA_KB.md` — ima integration sharp edges (keyword-match vs semantic, no body return, rerank behavior) + `list_ima_kb.py` usage.
- `docs/IMA_PER_USER_BINDING.md` — per-user KB design + invariants (read before touching `_AIWithIma.chat` for per-user routing, `utils/ima_bindings.py`, or `/clawbot/ima/bind`).
- `docs/IMA_WEB_UI.md` — three binding entry points (web UI + `/bindkb` command + CLI) + auth model + cross-device recovery flow.
- `docs/IMA_PER_USER_BINDING_REVIEW.md` — outstanding review items from 2026-09-16 implementation; P0/P1/P2 checklist for the next reviewer.
- `docs/FORWARDED_MESSAGES.md` — what `extract_message_text` reads from `item_list` (text / voice / ref_msg / app_msg / url), dedup strategy, 8-case smoke test patterns, URL is text-only (never fetched).
- `docs/multi-user.md` — single-tenant limits, scenario A vs B, comparison with XTmai reference impl, recommended fixes (#2 broadcast, #3 gather, #4 retry) ranked by ROI.
- `docs/PORTABLE.md` — PyInstaller `--onedir` build steps, USB layout, `noexec` mount workaround.
- `docs/SESSION_2026-09-07_ima_pipeline.md` — chronological record of the IMA rewrite + 4 new knobs + KB bootstrap. **Read this before touching `_AIWithIma.chat`** — it documents the 3-state machine (`ima` / `local` / `llm_only`) and the reasons each knob exists.
- `docs/knowledge/` — ~30 short topic notes (`qa-NNN-<area>-<topic>.md`). First place to grep when debugging an unfamiliar failure mode (e.g. `grep -l "context_token" docs/knowledge/`).
- `TODO.md` — current priorities (per-user history is P0; reconnect broadcast / message-loop gather are P1; tracked with `bot.py:line` refs).
- `weixin-openclaw-api-py-docs.md` — full 2.4.6 protocol reference; consult before changing header shape, request body schema, or response parsing.
- `weixin-clawbot.spec` — PyInstaller recipe; regenerate via `pyi-makespec` whenever `requirements.txt` or top-level imports change.

## Adding a new logger / feature

When adding code that emits logs or handles a new error path:
1. Use `get_logger("subsystem")` from `utils/logging_setup.py` — do not call `setup_logging()` again.
2. New failure paths in `message_loop` must distinguish stale-token (`exc.is_stale_token`) from transient network (`exc.network_type` ∈ `dns / tcp / tls / timeout / unknown`) from generic `Exception` — see the canonical handler at `bot.py:1707-1761`.
3. `handle_message` currently serializes per message via `for msg in msgs: await handle_message(msg)` (`bot.py:1705`). If converting to `asyncio.gather` (TODO P1 #3), watch `welcomed_users.add` / `last_contact` writes — they need a lock or hoisting outside the gather.

Pre-redaction of sensitive values is already covered by the Redaction bullet in the architecture section above — do not restate it here.
