# 2026-09-15 会话记录：iLink 连接保活与无感恢复

## 1. 用户问题

用户反馈：微信扫码登录成功后，如果长时间不使用，应用会自动退出，之后无法继续与 OpenClaw 交互。目标是让系统在后台自动、无感地维护登录后的连接状态。

## 2. 排查结论

问题来自两个生命周期被错误地绑在一起：

1. 共享 Web 入口的 `BrowserSessions` 有浏览器闲置 TTL。TTL 到期后，`shared_web.py` 的 sweeper 会调用 `manager.stop(user_id)`，直接停止已经扫码成功并正在运行的 `BotSession`。
2. `bot.py` 和 `bot_session.py` 原本还按本地 24 小时计时器主动进入二维码重连流程。这个计时器不是 iLink 服务端的 token 生命周期承诺；它会把仍然有效的连接误判为需要重新扫码。

因此，用户即使只是没有打开网页，微信侧的数据连接也可能被停止；或者到达本地计时窗口后被要求重新扫码。

## 3. 采用的设计

将系统拆成两个独立生命周期：

```text
浏览器控制面：登录页、二维码、配对码、切换用户
        │
        │ 浏览器 TTL 到期只清理网页绑定
        ▼
微信数据面：BotSession + getupdates 长轮询 + 消息收发
        │
        └─ 仅服务端明确返回 -14 时进入重新扫码恢复
```

- 登录成功后，后台 `BotSession` 独立于浏览器页面继续运行。
- `getupdates` 长轮询本身就是 iLink 连接保活机制；正常超时、空消息、短暂网络故障都不触发扫码。
- `ret=-14` 或 `errcode=-14` 表示服务端明确判定 `bot_token` 已失效，才启动统一的受控重登录流程。
- 真正失效的 token 仍然需要用户重新扫码，这是服务端认证要求，无法由客户端无感伪造恢复。
- 未完成扫码的临时会话仍按浏览器 TTL 回收，避免 QR 登录会话无限占用资源。

## 4. 具体实现

### `bot.py`

- `RECONNECT_CONFIG` 增加 `proactive_relogin=False`，默认关闭旧的本地计时主动扫码。
- legacy 单用户入口仅在显式开启 `proactive_relogin` 时创建 `reconnect_timer_task`。
- `/time` 在默认模式下改为提示“当前连接由后台持续维护”。
- 保留 `getupdates` 收到 `-14` 后的统一 `request_relogin()` 路径。

### `bot_session.py`

- 增加 `has_authenticated_connection` 属性，用于区分“已拿到 token 的后台连接”和“仍在扫码的临时会话”。
- 默认只启动消息长轮询，不启动本地到期重连计时器。
- `/TIME` 与 legacy 行为保持一致：默认说明后台持续维护，兼容模式才显示本地计时窗口。

### `shared_web.py`

- 浏览器绑定 TTL 到期后仍移除 opaque cookie 绑定，保持 Web 控制面的安全边界。
- sweeper 检查关联 session 是否已经拥有有效 token：
  - 已认证：保留 `BotManager` 中的后台 session，不调用 `manager.stop()`。
  - 未认证：继续停止未完成扫码的临时 session。

### 文档与测试

- 同步更新 `README.md`、`CLAUDE.md`、`docs/0_DESIGN_INTENT.md`、`docs/1_BOT_SESSION_ARCHITECTURE.md` 和 `docs/multi-user.md`，明确浏览器 TTL 与微信连接生命周期的区别。
- 新增回归测试：浏览器绑定过期后，已认证 BotSession 仍然存在；未认证临时 session 仍被回收。

## 5. 验证结果

```text
23 passed
python -m py_compile bot.py bot_session.py shared_web.py bot_manager.py shared_runtime.py
git diff --check
```

当前实现不再因为用户长时间不访问网页或本地 24 小时计时而中断 OpenClaw 交互。只有 iLink 服务端返回 `-14` 时，系统才会生成新的二维码请求真实重新认证。

