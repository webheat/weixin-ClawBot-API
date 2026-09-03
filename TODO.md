# TODO

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
