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

- [ ] **多用户支持（场景 B）** — 每个用户各自扫码绑定自己的微信个人号。当前为单租户架构，仅支持"多人共用一个 bot 账号"（场景 A）。详见 [`docs/multi-user.md`](docs/multi-user.md)。
  - 触发信号：单账号活跃用户 > 50 且需要完全隔离；明确出现"用自己的微信号登录"的需求；需要 per-user AI 配置。
  - 预设方案：多进程（`bot.py --user` + systemd 模板单元 `clawbot@.service` + nginx `/clawbot/<user>/` 路由）。

## 已完成

- [x] 二维码/登录态暴露到 `https://bx.mengxa.com/clawbot/`（commit `a7b4857`）
  - `qr_web.py` 内嵌 aiohttp，daemon 化后用户无需 TTY 即可扫码
  - 数字配对码走 web 提交，替代 stdin `input()` 阻塞
  - `/etc/nginx/sites-available/bx.mengxa.com` 加 `location ^~ /clawbot/`
  - `/etc/systemd/system/clawbot.service` 守护进程
  - CLI 向导 `input()` 改 `_safe_input()` 防 EOFError 阻塞 daemon
- [x] `_AIWithIma.chat` 加检索命中日志（query / hits / titles），便于排查 ima 知识库效果
- [x] 多用户痛点调研 + XTmai/WeChat-iLinkBot 参考实现对比（`docs/multi-user.md`）
