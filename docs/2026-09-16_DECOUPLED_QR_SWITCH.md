# 2026-09-16 设计:解耦 QR 切换与 token(server-driven vs user-driven)

> 关联文档: [`2026-09-16_RECONNECT_AND_KEEPALIVE.md`](2026-09-16_RECONNECT_AND_KEEPALIVE.md) · [`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) · [`2026-09-15_SESSION_CONNECTION_KEEPALIVE.md`](2026-09-15_SESSION_CONNECTION_KEEPALIVE.md)

---

## 1. 问题

08:30 事故 + 后续复盘都把"用户点 `/switch` 不扫码 → token 清空 → 长轮询永久沉默"归为头号 UX 问题。**用户行为无法规避**(忘记扫码、手机不在身边、扫码时断网、改主意了),所以不能用"假设用户一定会扫"的逻辑设计后端。

老设计(`78e1dfd` 引入的"清 token 后再 QR")是为修另一个真 bug 写的 —— **binded_redirect 复用旧 token 的死循环**。它没有区分两种触发场景:

| 触发 | 含义 | 旧 token 状态 |
|---|---|---|
| 服务端返 `-14`(iLink 主动判定 token 失效) | token **真的死了** | 必须清,否则新 QR 会被 `already_connected` 短路 |
| 用户主动点 `/switch`(bot 还活着) | token **仍然有效**,用户只是想在另一台设备接管 | 不该清;清掉是 UX 事故 |

老代码对两种场景一视同仁地清 token,导致今天这个事故。

## 2. 设计原则(像开放平台)

参考 [OAuth 2.0](https://www.rfc-editor.org/rfc/rfc6749) / [WeChat 开放平台 OAuth](https://developers.weixin.qq.com/doc/oplatform/Third-party_Platforms/Authorization/Process_of_authorization.html) / [WeChat Open Platform QR Login](https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Authorized_Login.html):

- **QR 是显示物,不是状态机**。展示的 QR 带 TTL,过期就只是图片作废,不影响任何后端连接
- **客户端 token 跟 QR 解耦**。QR 没确认 → 旧 token 继续用;QR 确认 → 原子 swap
- **服务端驱动的失效仍走老路径**。`-14` 来了,说明旧 token **已经死了**,必须清掉才能用新 QR 重新登录

具体到本项目:

- `request_relogin(reason="web switch" | "manual")` → **新路径**: `_request_qr_switch` → `_initiate_qr_switch` → 后台 task `_run_qr_switch` 轮询 QR → 确认后 `_await_qr_confirmation_and_swap` 原子 swap;QR 过期则 no-op,旧 token 不动
- `request_relogin(reason="stale-token" | "session-expiry")` → **老路径**: `_reconnect` → 清 token → QR → 登录(78e1dfd 不变量)
- `request_relogin(reason="force-before" | 其他)` → **老路径**(同 stale-token;保守起见视为服务端驱动)

## 3. 实现

### 3.1 新增 `__init__` 属性(`bot_session.py:148-154`)

```python
self._pending_qr_token: Optional[str] = None          # 当前切换 QR 的 qrcode
self._pending_qr_started_at: float = 0.0              # TTL 起点
self._pending_qr_task: Optional[asyncio.Task] = None  # 后台轮询任务
```

### 3.2 `request_relogin` dispatch(`bot_session.py:413+`)

```python
async def request_relogin(self, reason: str = "manual") -> dict[str, Any]:
    # ... 既有 reason 日志 ...
    if reason in {"web switch", "manual"}:
        return await self._request_qr_switch(reason)
    # 否则走老路径(stale-token / session-expiry / force-before / 其他)
    ...
```

### 3.3 新方法

| 方法 | 作用 |
|---|---|
| `_request_qr_switch(reason)` | 用户驱动入口;立刻返回,后台 task 跑轮询 |
| `_initiate_qr_switch()` | 调 `fetch_login_qrcode`,**不**清 token,**不**写 `_pending_relogin` future |
| `_await_qr_confirmation_and_swap(qrcode, deadline_s)` | 轮询 QR;`confirmed` → 持 `_reconnect_lock` + `_drain_lock` 调 `_apply_login` 原子 swap;`expired` / 超时 → 返 None,旧 token 保持 |
| `_run_qr_switch(qrcode)` | 后台 task;调 `_await_qr_confirmation_and_swap`,记日志,清理 `_pending_qr_token` |

### 3.4 不变量(测试守住)

1. **`_initiate_qr_switch` 不清 token** —— `test_qr_switch_initiate_does_not_clear_token`
2. **QR 过期保留 token** —— `test_qr_switch_expire_preserves_token`
3. **Confirmed swap 在 `_drain_lock` + `_reconnect_lock` 下原子完成** —— `test_qr_switch_confirm_atomic_swap`
4. **`web switch` / `manual` 走解耦路径** —— `test_request_relogin_web_switch_uses_decoupled_path`
5. **`stale-token` / `session-expiry` 仍走老路径(78e1dfd 不变量)** —— `test_request_relogin_stale_token_uses_clearing_path`

## 4. 行为对比

### 老行为(`request_relogin("web switch")`)

```
用户点 /switch
  → 清 token
  → 拉 QR
  → 用户不扫 → QR 过期 × 3
  → RuntimeError(max_refresh_exceeded)
  → 长轮询永远 sleep(1) ← 事故
```

### 新行为(`request_relogin("web switch")`)

```
用户点 /switch
  → 拉 QR(不动 token)
  → 后台 task 轮询
  → 用户不扫 → QR 过期 → 日志 qr_switch expired (no-op, current token preserved) → 旧 token 不动
  → 长轮询继续工作
  → 用户改主意,不切了 → 一切正常
  → 或者用户后来扫码 → 原子 swap,新 token 接管
```

## 5. 与既有 PR / 文档的关系

- 修复了 `2026-09-15_IN_FLIGHT_RELOGIN_RACE.md` §10 提到的"QR 循环切断数据面上下文"问题 —— 现在 QR 循环不切断老端,新确认后才切
- 与 `2026-09-15_SESSION_CONNECTION_KEEPALIVE.md` §6/7 的"78e1dfd binded_redirect 修复"不冲突 —— 那条修复仅适用于服务端驱动路径(本设计保留)
- 闭环了 `2026-09-16_RECONNECT_AND_KEEPALIVE.md` §5 列出的全部方案:
  - §5.1 reason 日志(`78e1dfd` 已有)
  - §5.2 in-flight mutex(`4f563a5`)
  - §5.3 pending preserve(`4f563a5`)
  - §5 (option D) token 回滚(`708ec8c`)
  - §5 (option C) 自动 backoff retry(`f2eeb12`)
  - **本 PR**: 解耦 user-driven relogin 与 token 生命周期

## 6. 验证

```bash
./venv/bin/python -m py_compile bot.py bot_session.py shared_web.py
./venv/bin/pytest tests/ --ignore=tests/test_shared_web.py -v
./venv/bin/python -m unittest tests.test_shared_web
```

预期: `37 + 8 = 45 passed`。

## 7. 关联日志配方(下次事故直接 grep)

```bash
# user-driven 切换是否被解耦(应该看到 qr_switch initiate 而不是 reconnect clear_token)
grep -E "qr_switch (initiate|fetched|confirmed|expired|cancelled)" logs/clawbot_shared.log

# server-driven -14 仍走老路径(应该看到 reconnect clear_token 而非 qr_switch)
grep -E "reconnect clear_token|message_loop stale_token|login max_refresh" logs/clawbot_shared.log
```