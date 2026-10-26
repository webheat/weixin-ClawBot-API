# 2026-10-26 事故：发版重启打断二维码窗口 → 账号永久掉线

> 关联代码：`shared_runtime._restore_persisted_sessions` · `bot_session._reconnect` ·
> `bot_session.save_state` · `bot_session._apply_login`
> 关联测试：`tests/test_shared_runtime.py`（4 个恢复用例）·
> `tests/test_bot_session_contract.py`（3 个 reauth_required 用例）
> 状态：**已修复并部署**（2026-10-26 15:28 生产重启验证通过）

---

## 1. 一句话

一个用户的 iLink 凭据过期（服务端 `-14`）后进入「等重新扫码」窗口，
**恰好赶上发版重启**：窗口被打断，state 文件里只剩一个空 `bot_token`，
而恢复逻辑把「token 为空」的会话直接跳过 —— 账号从此永久掉线，
用户在浏览器里也再也拿不回自己的会话。

多用户隔离本身没问题：另一个账号全程健康。

---

## 2. 时间线（2026-10-26）

| 时刻 | 事件 | 证据 |
|---|---|---|
| 10-25 19:08:35 | 服务重启（`systemd`），此后一直空转 | `journalctl -u clawbot-shared` |
| 10-25 19:14:55 | 最后一个会话因 `max_refresh_exceeded` 死掉，进程此后再无业务日志 | `clawbot_shared.log.2026-10-25` |
| 11:44:24 | **发版重启**（`systemctl restart`），新进程恢复 3 个持久化会话 | `restored 3 persisted session(s)` |
| 11:44:25 | `yg@im.wechat`（`eph_edbaaf…`）与旧会话 `ktI-…` 立刻 `-14 session timeout` | `notifystart` / `getupdates` → `errcode=-14` |
| 11:44:25 | `_reconnect` **清空 `bot_token`** → 拉二维码 → `等待扫码…` | `reconnect clear_token had_authenticated=True previous_len=58` |
| 11:45:50 | **再次发版重启**（SIGTERM）→ 二维码窗口被打断 | `session.stop … caller=Task-28` |
| 11:45:50 | state 文件被写成 `bot_token=""` | `weixin_state_eph_edbaaf….json` |
| 11:46 / 11:49 / 11:51 / 11:55 | 之后每次重启都只恢复 1 个会话 —— 挂掉的那个被跳过 | `restored 1 persisted session(s)` |
| 15:28 | 修复后重启：挂掉的账号被复活并出二维码 | `restored 1 session(s) waiting for a QR re-scan` |

**触发器**：11:44 恢复时 token 早已被服务端收回（`notifystart` 一上来就 `-14`）；
而 11:45 的重启把唯一一次重新授权的机会也掐掉了。

---

## 3. 根因

### 3.1 为什么 token 会过期

iLink 2.4.6 **没有心跳/续期端点**（`weixin-openclaw-api-py-docs.md:60-69` 全 8 个端点无
`/heartbeat` / `/ping` / `/refresh_token`），协议 §2.7.3 明确：**客户端活动不能延长服务端
token 的有效期**。所以 `-14` 是**服务端**的判断，客户端唯一能做的就是重新扫码。

### 3.2 为什么重启会永久杀掉账号

三个条件叠加：

1. `_reconnect()` 为了避免 `binded_redirect` 复用死 token，**先清空 `bot_token`**，
   再进二维码窗口。
2. `save_state()` 只写 `bot_token` / `baseurl` / `ilink_bot_id` …，
   **没有写「本会话正在等重新扫码」这个状态**。进程在窗口内被杀掉后，
   state 文件只剩 `bot_token=""` —— 和「从没登录过」完全无法区分。
3. `_restore_persisted_sessions()` 见 `bot_token` 为空就 `continue`，
   于是每次重启都跳过这个会话。浏览器侧的 resume cookie（30 天）
   指向一个**在 manager 里不存在的 session_id**，`resume_existing()` 只能
   把它 revoke 掉 —— 用户回到登录页，之前那个账号的上下文
   （`contexts` / `get_updates_buf` / `last_contact`）再也接不上。

> 顺带一提：`yt` 账号的浏览器绑定本身也早已过期（`DEFAULT_SESSION_TTL` = 8h，
> 而用户只在微信里聊天、很少开网页），所以就算二维码窗口没被打断，
> 也没人看得见那张码 —— 这就是「静默」的来源。

---

## 4. 修复

### 4.1 持久化「待重新认证」状态

| 位置 | 改动 |
|---|---|
| `bot_session.__init__` | 从 state 文件恢复 `reauth_required` → `self._reauthentication_required` |
| `bot_session.save_state` | 多写一个字段 `reauth_required` |
| `bot_session._reconnect` | **清 token 的同一刻**把 `_reauthentication_required` 置 True 并落盘（在二维码窗口打开之前） |
| `bot_session._apply_login` | 登录成功后把标志清回 False |

关键点在 `_reconnect` 里的**时机**：标志在二维码窗口**开始之前**就落盘了，
所以窗口期内任何时刻被重启，磁盘上都留有「这个账号还欠一次扫码」。

### 4.2 恢复路径认这个标志

`shared_runtime._restore_persisted_sessions()` 现在恢复两类会话：

- **带 token 的**（原行为不变，一个都不少）；
- **没 token 但标了 `reauth_required` 的** —— 复活后 `BotSession.start()` 看到空 token
  会走初始二维码流程，页面重新出码；浏览器拿着 30 天的 resume cookie 回来即可续上。

两道护栏，避免复活一堆僵尸：

- **时效窗**：只有最近 `CLAWBOT_REAUTH_RESTORE_WINDOW`（默认 24h）内更新过的才复活，
  否则每次重启都会把早就没人管的账号拉起来；
- **同账号去重**：一个微信账号只留一个会话。带 token 的优先，
  否则复活的旧账号一旦被扫描，会把同账号的健康会话踢掉
  （`bot_manager._evict_same_account`）。

日志会明确说：

```
restored 2 persisted session(s): sTFmBbryl…, eph_edbaaf…
restored 1 session(s) waiting for a QR re-scan (no token): eph_edbaaf… — 该账号需要用户重新扫码才能恢复
```

### 4.3 恢复流程（用户侧）

1. 用户打开 `https://<域名>/clawbot/`；
2. 若 resume cookie 还在 → 直接回到自己的会话页，页面已出好二维码；
3. 否则点「开始」→ 新会话 → 扫码 → 恢复。

---

## 5. 没解决的部分（协议限制，别再指望代码解决）

- **服务端 token 过期无法避免**。没有心跳端点，客户端活动不续期。
  账号每隔一段时间就需要人扫一次码，这是协议事实，不是 bug。
- **过期时无法主动通知用户**：推送通道就是那个已经失效的 token。
  唯一的通知渠道是网页。
- 因此本次修复的目标是「**别再把账号永久打死**」，不是「让用户无感」。

---

## 6. 遗留 / 建议

- [ ] `IMA_ILINK_DEFAULT_KB=-jDsbK-…` 是**旧格式的失效 kb_id**，现在 ima 只认
      `ima_kb_XXXXXXXX`。每次 IMA 检索都会报 `code=220004`（已按 permanent error
      快速失败，不重试），实际回答全部由 `docs/knowledge/` + `utils/semantic_kb.py`
      兜底。**注意**：搜到的客服话术库都是**个人库**（`KBT_MINE_KB`），
      而个人库调 `search_knowledge` 必然返 220004 —— 这才是真正的堵点。
      要么建一个**共享库**并把默认 kb 指过去，要么干脆清空该配置、只走本地/语义检索。
- [ ] 浏览器绑定 8h TTL 对「只在微信里用、很少开网页」的用户偏短；
      建议配合 30 天的 resume cookie 一起看（本次修复已让 resume cookie 在
      会话复活后重新生效）。
- [ ] 关机路径偶发 30s 超时被 SIGKILL（`State 'stop-sigterm' timed out`），
      与本次事故同源：发版窗口打断了重连。值得查一下是什么在拖住退出。
