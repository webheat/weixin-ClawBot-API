# TODO

## P0 — 最关键

- [ ] **per-user 对话历史** ⭐ — Bot 当前无跨消息记忆，每次只看到当前一条消息。用户问"我刚才说了什么"或"继续上次"都答不上来。
  - 存储：`runtime_state["contexts"][from_id]["history"]` 持久化到 `weixin_state.json`，重启不丢
  - 形态：`[{"role": "user"|"assistant", "content": "...", "ts": float}, ...]`
  - 上限：每个用户最近 N 轮（建议 20），超长用滑动窗口或按 token 数裁剪
  - AI 调用：`ai.chat(text)` 改造成 `ai.chat_with_history(messages, system_prompt)`；`_AIWithIma` 同步加一层把 history + ima 检索结果合并成 system prompt / message 列表
  - 例外：指令消息（`/help` `/time` `/重新连接`）不写入历史；欢迎语 `COMMANDS_MSG` 不写入历史
  - 群消息暂不进入历史（仅 1:1 私聊）
  - 风险：context 膨胀 → token 成本涨；需要设上限 + 监控

## P1 — 多用户共享（场景 A）下的体验改进

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
  - `bot.py` 加 `--user <name>` 参数（向后兼容：不传 = 单租户旧行为）
  - `_resolve_user_paths()` 推导 `config_<user>.json` / `weixin_state_<user>.json` / `logs/clawbot_<user>.log`；非法字符（除 `_.-` 外）自动替换为 `_`
  - `ImaConfig.from_env(env_files=list)` 支持按顺序 `override=False` 加载；默认 `None` 跳过文件加载，env 由 `bot.py` 提前装好
  - `setup_logging(log_file=...)` 接受完整日志路径，覆盖默认 `logs/clawbot.log`
  - **`qr_portal.py` 单入口 portal**（`:18300`）：picker 页 + cookie 派发 + 反代到 `qr_web.py` + HTML 注入"切换用户"按钮
  - `/etc/clawbot/ima.env` —— 共享 ima 凭据（兜底）
  - `/etc/clawbot/user.env.example` —— 用户 env 模板
  - `/etc/systemd/system/clawbot@.service` —— per-user systemd 模板，`EnvironmentFile=/etc/clawbot/%i.env`，`ExecStart=... bot.py --user %i`
  - `/etc/systemd/system/clawbot-portal.service` —— portal systemd 单元
  - nginx `/clawbot/` location 恢复单一 `proxy_pass :18300`（撤销前一轮的 map）
  - IMA 独立：用户在 `<user>.env` 写 `IMA_ILINK_*` 即可覆盖；不写则用 `/etc/clawbot/ima.env` 共享
  - `docs/multi-user.md` 场景 B 章节"+ portal 单入口"+ 完整部署步骤
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
