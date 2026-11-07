# TODO

## P0 — 最关键

- [ ] **IMA 默认知识库 ID 已失效** ⭐ — 2026-10-27 发现。`.env` 的 `IMA_ILINK_DEFAULT_KB=-jDsbK-UEdF5xYyP7kZaoWbMrTyDUDuMNuvOt3oorvI=` 在生产上持续报 `code=220004 invalid or outdated knowledge_base_id`，**每一次 IMA 检索都失败**（2026-10-26 11:55 的日志可见）。
  - 后果：IMA 4 档路由**永远降级**——`ima` 档失效 → 退到 `local` / `semantic` / `llm-only`。功能不挂，但知识库检索能力实质上没开。
  - 注意：这个 KB id 格式（`-jDsbK-...`）与 `utils/list_ima_kb.py` 列出的 `ima_kb_XXXX` **不是同一套 id**。`list_ima_kb.py` 列出 19 个库，但没有一个对应 `-jDsbK-...`。
  - 修复：用 `utils/list_ima_kb.py --pick <名称>` 找出目标库，把新的 id 写进 `.env` 的 `IMA_ILINK_DEFAULT_KB`（或改走 per-user binding，见 `docs/IMA_PER_USER_BINDING.md`）。
  - **注意**：改完 `.env` 必须重启服务 —— systemd 的 `EnvironmentFile` 只在进程启动时读一次（详见 `deploy/README.md` 的「部署记录」小节）。
  - 待确认：目标库是哪个？`.env` 里旧 id 已完全查不到名字，得由用户指定。

- [ ] **per-user 对话历史** ⭐ — Bot 当前无跨消息记忆，每次只看到当前一条消息。用户问"我刚才说了什么"或"继续上次"都答不上来。
  - 存储：`runtime_state["contexts"][from_id]["history"]` 持久化到 `weixin_state.json`，重启不丢
  - 形态：`[{"role": "user"|"assistant", "content": "...", "ts": float}, ...]`
  - 上限：每个用户最近 N 轮（建议 20），超长用滑动窗口或按 token 数裁剪
  - AI 调用：`ai.chat(text)` 改造成 `ai.chat_with_history(messages, system_prompt)`；`_AIWithIma` 同步加一层把 history + ima 检索结果合并成 system prompt / message 列表
  - 例外：指令消息（`/help` `/time` `/重新连接`）不写入历史；欢迎语 `COMMANDS_MSG` 不写入历史
  - 群消息暂不进入历史（仅 1:1 私聊）
  - 风险：context 膨胀 → token 成本涨；需要设上限 + 监控

## P1 — 多用户共享（场景 A）下的体验改进

> 2026-10-27 隔离审计的完整结论见 [`docs/2026-10-27_MULTI_ACCOUNT_AUDIT.md`](2026-10-27_MULTI_ACCOUNT_AUDIT.md)。
> 本轮已修 3 个（KB 绑定失效 / 限流全站一桶 / 线程池互堵），下面是其余未修项。

- [ ] **语义库 `db_path` 会退化成共享文件**（审计 A2）— `bot.py:2519` 的 `db_path = str(Path(extra)/...) if extra else None`，`extra` 为空（`CLAWBOT_USER_KB_DIR` 未配或 scope 为空）时 `db_path=None` → `utils/semantic_kb.py:110` 回落到 `docs/.semantic_kb.sqlite3`，**所有账号共用同一个 SQLite 文件**。
  - 后果：不同账号的语义库混在一起；每实例一把 `threading.Lock` → 两个 session 并发 rebuild 会同时写同一文件 → SQLITE_BUSY / 索引互相覆盖。
  - 当前不触发（`CLAWBOT_USER_KB_DIR=docs/kb-users` 已配）。属于**配置退化时的隐患**。
  - 修法：`scope` 为空时也强制落到账号自己的目录，或显式禁用语义兜底而不是回落共享。

- [ ] **`LocalKBIndex` 的 `extra_dirs` 永久缓存**（审计 A3）— `bot.py:2474` 用 `_local_kb_attempted` + `_local_kb_scope` 缓存，scope 不变就永远返回第一次建的索引；而 `LocalKBIndex.__init__` 只在构造时扫一次 `extra_dirs`（且要求 `is_dir()`）。
  - 后果：私有目录在索引已建之后才出现时，**该账号的私有笔记永远进不了 BM25 索引**（要等 scope 变化或重启进程）。
  - 修法：加 mtime 检查重建，或每次 chat 前校验私有目录是否存在。

- [ ] **私有 KB 目录没有任何人自动创建**（审计 A4）— `docs/kb-users/<key>/` 现在只有 `README.md`。`LocalKBIndex` 不建目录，只有 `SemanticKBIndex._init_db()` 的 `mkdir(parents=True)` 副作用会建一个**只有 `.semantic_kb.sqlite3` 的空壳**。
  - 对比：发贴图号的 vault 有 `ensure_user` 自动开通（`xiaolvshu_bridge.ensure_user`），**知识库私有目录缺一个对应的自动化**。
  - 修法：仿照 `ensure_user` 加一个 `ensure_kb_scope(scope)`。

- [ ] **连接池无 per-account 配额**（审计 A7）— `shared_runtime.py:405` 的 `TCPConnector(limit=300, limit_per_host=0)`。`limit_per_host=0` = 对 ilinkai.weixin.qq.com 不限连接数 → 单个账号理论上能占满 300 条。
  - 而每个活跃会话都有一条**常驻长轮询连接**（35s 超时循环）→ N 个会话 = N 条常驻连接，N 接近 300 时新会话全排队。
  - 修法：给 `limit_per_host` 一个上限，或加 per-account 配额。

- [ ] **`_kb_scope` 净化非单射**（审计 A10）— `bot_session.py:1200` 用 `re.sub(r"[^A-Za-z0-9]+", "_", ilink_user_id)`，而 `ilink_user_id` 前缀是 base64url，含 `-` 和 `_`，**两者都映射成 `_`** → `xxx-a` 与 `xxx_a` 会落到同一私有目录。
  - 发稿桥那边有 `provision_conflict` 防护，**KB scope 这边没有**。
  - 修法：净化时保留 `-`（或用 hash 后缀），或加冲突检测。

- [ ] **`CLAWBOT_STATE_DIR` 未设**（审计 A11）— `utils/ima_bindings.py:43` 的 `DEFAULT_STATE_DIR = Path(os.environ.get("CLAWBOT_STATE_DIR", "."))` → `ima_bindings.json` 落在 CWD（`/opt/weixin-ClawBot-API/`），与 `weixin_state_*.json` 同目录（一致），但与 CLAUDE.md 声称的 `CLAWBOT_STATE_DIR/ima_bindings.json` 不符。
  - 修法：在 `.env` 里显式设 `CLAWBOT_STATE_DIR`，或改文档。

- [ ] **`last_contact` 单发 → broadcast 给所有活跃用户**（`docs/multi-user.md` #2）
  - 当前 `bot.py:1264` `last_contact` 只跟踪最近一个；`reconnect_timer_task:868` 重连警告只发给一人，其他用户不知情
  - 改造：`reconnect_timer_task` 维护活跃用户集合（24h 内发过消息），警告群发
  - 风险低，影响大（多人共享 bot 时感知到 bot"对所有人一视同仁"）

- [ ] **`message_loop` 串行 → `asyncio.gather` 并发处理**（`docs/multi-user.md` #3）
  - 当前 `bot.py:1507` `for msg in result.get("msgs"): await handle_message(msg)` 串行
  - 单次 `getupdates` 返回的多个消息改 `asyncio.gather(*[handle_message(m) for m in msgs])`
  - 缓解"一人慢全员等"的尾延迟叠加
  - 注意：`welcomed_users.add(from_id)` / `last_contact` 写入需在 gather 外做或加锁

## P2 — 长期 / 边缘问题

- [ ] **`send_msg_safe` 静默吞错 → 加 retry 或告警日志**（`docs/multi-user.md` #4）
  - 当前 `bot.py:588-590` catch 所有 Exception 只 print 不通知
  - 影响：welcome / `/help` / 错误提示 / 重连警告 4 类消息偶发不显示
  - 改造：失败时记录到 `last_send_error[from_id]`，下条消息优先提示该用户；或加 retry 1-2 次

- [ ] **`runtime_state["contexts"]` 无限膨胀**（`docs/multi-user.md` #5）
  - 1000 用户后 `weixin_state.json` 几十 MB；`save_runtime_state` 每次全量重写
  - 改造：切到 SQLite 或 per-user 分文件
  - 触发：活跃用户 > 100 时启动

## 待评估（按需求触发）

- [ ] **多用户支持（场景 A 应用层痛点）** — 场景 B 已通过多进程 + nginx map 实现（`docs/multi-user.md` 场景 B）。
  场景 A 下的应用层痛点（`last_contact` 单发 / `message_loop` 串行 / `send_msg_safe` 静默吞错 / `contexts` 膨胀）按 `docs/multi-user.md` 优先级处理。
  - 触发信号：单进程用户活跃 > 50 且需要完全应用层隔离
  - 注意：多进程方案已隔离跨用户串扰，但单用户进程内的痛点仍然存在

## 已完成

- [x] **多用户支持（场景 B）—— portal 单入口** — 所有用户共用 `https://bx.mengxa.com/clawbot/`，portal 根据 cookie 派发
  - `bot.py` 加 `--user <name>` 参数（向后兼容：不传 = 单租户旧行为）— **已于 2026-09-15 session-based cleanup 时移除**
  - `_resolve_user_paths()` 推导 `config_<user>.json` / `weixin_state_<user>.json` / `logs/clawbot_<user>.log`；非法字符（除 `_.-` 外）自动替换为 `_`
  - `ImaConfig.from_env(env_files=list)` 支持按顺序 `override=False` 加载；默认 `None` 跳过文件加载，env 由 `bot.py` 提前装好
  - `setup_logging(log_file=...)` 接受完整日志路径，覆盖默认 `logs/clawbot.log`
  - **`qr_portal.py` 单入口 portal**（`:18300`）：picker 页 + cookie 派发 + 反代到 `qr_web.py` + HTML 注入"切换用户"按钮 — **已删除（2026-09-15）**
  - `/etc/clawbot/ima.env` —— 共享 ima 凭据（兜底）
  - `/etc/systemd/system/clawbot@.service` —— per-user systemd 模板，`EnvironmentFile=/etc/clawbot/%i.env`，`ExecStart=... bot.py --user %i` — **已删除（2026-09-15）**
  - `/etc/systemd/system/clawbot-portal.service` —— portal systemd 单元 — **已删除（2026-09-15）**
  - nginx `/clawbot/` location 恢复单一 `proxy_pass :18300`（撤销前一轮的 map）
  - IMA 独立：用户在 `<user>.env` 写 `IMA_ILINK_*` 即可覆盖；不写则用 `/etc/clawbot/ima.env` 共享
  - `docs/multi-user.md` 场景 B 章节"+ portal 单入口"+ 完整部署步骤 — **已删除（2026-09-15）**
- [x] 接入数据链路关键节点日志
  - 新建 `utils/logging_setup.py`：标准库 `logging`，终端 + `logs/clawbot.log` 按天滚动，保留 7 天；`CLAWBOT_LOG_LEVEL` / `CLAWBOT_LOG_DIR` / `CLAWBOT_LOG_BACKUPS` 环境变量可覆盖
  - 8 个子 logger：`clawbot.web` / `clawbot.qr` / `clawbot.message` / `clawbot.reconnect` / `clawbot.ai` / `clawbot.api` / `clawbot.state` / `clawbot.ima`
  - `qr_web.py` 接入 logger + access_log 中间件
  - `bot.py` 主链路 ~80 处 print 替换为 logger（保留用户态 UI print）；AI 调用加 `time.perf_counter()` 耗时
  - `ima.py` 替换旧 `log()` print 为标准 logger；`_post` 加 elapsed_ms + 重试耗尽 ERROR；`search_knowledge` 加总耗时；`build_context_prompt` 加 prompt 字符数；`_AIWithIma.chat` 加注入 prompt before/after
  - `RedactFilter` 兜底脱敏（防御层；主防线在调用方）
  - README 新增"日志与排障"章节（5 个常见异常定位脚本）
- [x] 二维码/登录态暴露到 `https://bx.mengxa.com/clawbot/`（commit `a7b4857`）
  - `qr_web.py` 内嵌 aiohttp，daemon 化后用户无需 TTY 即可扫码
  - 数字配对码走 web 提交，替代 stdin `input()` 阻塞
  - `/etc/nginx/sites-available/bx.mengxa.com` 加 `location ^~ /clawbot/`
  - `/etc/systemd/system/clawbot.service` 守护进程
  - CLI 向导 `input()` 改 `_safe_input()` 防 EOFError 阻塞 daemon
- [x] `_AIWithIma.chat` 加检索命中日志（query / hits / titles），便于排查 ima 知识库效果
- [x] 多用户痛点调研 + XTmai/WeChat-iLinkBot 参考实现对比（`docs/multi-user.md`）
