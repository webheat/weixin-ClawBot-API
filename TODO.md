# TODO

## 待评估（按需求触发）

- [ ] **多用户支持（场景 B）** — 每个用户各自扫码绑定自己的微信个人号。当前为单租户架构，仅支持"多人共用一个 bot 账号"（场景 A）。详见 [`docs/multi-user.md`](docs/multi-user.md)。
  - 触发信号：单账号活跃用户 > 50 且需要完全隔离；明确出现"用自己的微信号登录"的需求；需要 per-user AI 配置。
  - 预设方案：多进程（`bot.py --user` + systemd 模板单元 `clawbot@.service` + nginx `/clawbot/<user>/` 路由）。

- [ ] **per-user 对话历史** — 当前 AI 是 stateless 的，每次只看到当前一条消息。用户问"我刚才说了什么"或"继续上次的思路"都答不上来。
  - 存储：`runtime_state["contexts"][from_id]["history"]` 持久化到 `weixin_state.json`，重启不丢
  - 形态：`[{"role": "user"|"assistant", "content": "...", "ts": float}, ...]`
  - 上限：每个用户最近 N 轮（建议 20），超长用滑动窗口或按 token 数裁剪
  - AI 调用：`ai.chat(text)` 改造成 `ai.chat_with_history(messages, system_prompt)`；`_AIWithIma` 同步加一层把 history + ima 检索结果合并成 system prompt / message 列表
  - 例外：指令消息（`/help` `/time` `/重新连接`）不写入历史；欢迎语 `COMMANDS_MSG` 不写入历史
  - 群消息暂不进入历史（仅 1:1 私聊）
  - 风险：context 膨胀 → token 成本涨；需要设上限 + 监控

## 已完成

- [x] 二维码/登录态暴露到 `https://bx.mengxa.com/clawbot/`（commit `a7b4857`）
  - `qr_web.py` 内嵌 aiohttp，daemon 化后用户无需 TTY 即可扫码
  - 数字配对码走 web 提交，替代 stdin `input()` 阻塞
  - `/etc/nginx/sites-available/bx.mengxa.com` 加 `location ^~ /clawbot/`
  - `/etc/systemd/system/clawbot.service` 守护进程
  - CLI 向导 `input()` 改 `_safe_input()` 防 EOFError 阻塞 daemon
- [x] `_AIWithIma.chat` 加检索命中日志（query / hits / titles），便于排查 ima 知识库效果
