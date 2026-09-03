# 多用户支持现状与未来规划

## 当前架构（单租户）

`bot.py` 启动一个 Python 进程，同一时间只持有一个 iLink `bot_token`，对应一个微信个人号。`qr_web.py` 在同一个进程里起 `aiohttp` 服务（绑 `127.0.0.1:18300`），共享一份 `QrFlowState` 单例和一份 `weixin_state.json`。

iLink OpenClaw 协议本身允许多个 `bot_token` 并发，但当前实现没有用到这个能力。

## 两种多用户语义

### 场景 A：多人共用同一个 bot 账号（已支持，无需改动）

一个 bot 进程登录一个微信账号 X，X 的任意微信好友发消息都会收到 AI 回复。`welcomed_users` / `last_contact` / `manual_reconnect_pending` 都是 per-peer 设计，互不干扰。

适用：把 bot 当作群助手、家庭号、企业号对外客服。

### 场景 B：每个用户各自扫码绑定自己的微信（**当前不支持**）

每个用户要独立登录自己的微信个人号、看到自己的 QR、用自己的 token 鉴权。当前问题：

- `weixin_state.json` 单文件，多用户会互相覆盖 token
- `QrFlowState` 是模块级单例，多用户的 QR 互相串
- `:18300` 单端口，反代也只能指向一个进程
- `CLAWBOT_WEB_TOKEN` 单值，无法隔离用户

## 推荐路径

**先用场景 A**：覆盖 99% 个人 bot 需求，零改动。

**若未来确实需要场景 B**：走多进程方案（半天可落地），具体步骤：

1. `bot.py` 增加 `--user <name>` CLI 参数，派生 `STATE_FILE = f"weixin_state_{name}.json"`、`CLAWBOT_WEB_PORT = 18300 + idx`、独立的 `CLAWBOT_WEB_TOKEN`（放在 `/etc/clawbot/<user>.env`）
2. 新建 systemd 模板单元 `/etc/systemd/system/clawbot@.service`，`%i` 替换为用户名，开用户 = `systemctl enable --now clawbot@<user>`
3. nginx 加 `location ^~ /clawbot/([^/]+)/ { proxy_pass http://127.0.0.1:1830${1}; }` 路由
4. ima 凭据可以共用（一份 `.env` 由 systemd `EnvironmentFile` 指向 `/etc/clawbot/ima.env`），节省管理成本

**不推荐单进程多租户**：复杂度高、长轮询并发可能踩协议限制、故障域变大，性价比不如多进程。

## 触发重新评估的信号

满足任一条件时启动场景 B 改造：

- 单账号下的活跃用户数 > 50 且彼此消息需要完全隔离
- 出现"我希望用我自己的微信号登录，不是某个公用号"的明确需求
- 需要给不同用户配置不同的 AI provider / system prompt（顺带做 per-user config）
