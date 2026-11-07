# 发稿闭环的流程日志

> 范围：用户发消息给 clawbot → clawbot 处理 → 发贴图号 → 把结果回给用户。
> **不含 AI 对话链路**（AI 的埋点在 `clawbot.diagnostics` 里，不在本文范围）。

## 为什么需要它

一次发稿横跨三层，各记各的日志：

```
用户微信            bot_session                utils/xiaolvshu            桥（另一个仓库）
   │              _handle_message                handle                xiaolvshu_bridge.py
   ├─ 消息 ────────►│                              │                          │
   │                 ├─ 认领 ─────────────────────►│                          │
   │                 │                              ├─ 子进程调桥 ────────────►│
   │                 │◄──── stdout 一行 JSON ──────┼──────────────────────────┤
   │                 ├─ 回信 ◄── 格式化文案 ───────┤                          │
   └─◄── sendmessage ┤                              │                          │
```

**没有共同的关联 id 时，排查只能靠时间戳猜哪条是哪条。** 出了问题
（「用户说发了没反应」）要回答的问题只有一个：**卡在哪一环？**
日志必须能直接回答它，而不是让人去脑补时间线。

## flow id

`BotSession._flow_id(msg)` —— 8 位 hex，由 `session_id + message_id` 派生。
同一条消息的每一条相关日志都带同一个 `flow=`：

```bash
grep "flow=3f9a2c71" logs/clawbot.log
```

`clawbot.diagnostics` 的 `diag stage=inbound` / `diag stage=reply_sent`
也带这个 id —— 两套日志用同一个 id 交叉引用。

## 阶段表

| stage | 记在哪 | 级别 | 含义 |
|---|---|---|---|
| `publish_intent` | `utils/xiaolvshu.handle` | INFO | 认领了这条消息。`source=command`（斜杠指令）/ `natural`（自然语言或语音），带 kind + keyword + uid |
| `publish_unclaimed` | 同上 | DEBUG | 不是发稿意图 → 落回 AI 对话 |
| `publish_skipped` | `utils/xiaolvshu._publish` | INFO | 认领了但没有关键词（用户光说「发贴图号」） |
| `bridge_call` | `utils/xiaolvshu._run_bridge` | INFO | 子进程调桥。带完整 args + timeout |
| `bridge_stderr` | 同上 | DEBUG | 桥自己的排障输出（插图找不到、标题超限……）。平时被丢掉，出问题时往往是唯一线索 |
| `bridge_result` | 同上 | INFO | 桥的 JSON 结果：ok / code / elapsed_ms / kind / title / cards / media_id |
| `bridge_error` | 同上 | WARN/ERROR | `timeout` / `spawn_failed` / `no_json_line`（带 stderr 尾巴） |
| `publish_result` | `utils/xiaolvshu.handle` | INFO | 这次发稿的最终结论：ok / code / 回信内容前 120 字 |
| `publish_need_select` | 同上 | INFO | 多命中。带候选标题列表 —— 用户回「/选 N」时靠它对账 |
| `publish_exception` | `bot_session` | ERROR | 桥调用抛异常（带堆栈） |
| `publish_unavailable` | `bot_session` | ERROR | `utils/xiaolvshu` 模块 import 失败 → 发稿功能整个不可用 |
| `reply_sent` | `bot_session` | INFO | 回信已发出（`kind=xiaolvshu`） |

### 回信失败：`send_reliable failed`

回信发不出去时记在 **`clawbot.message`**（不是 `clawbot.publish`）：

```
send_reliable failed path=sendmessage kind=xiaolvshu flow=<id> err=...
```

带 `kind=` 和 `flow=` —— 能跟同一次发稿的前面的阶段对上。
**「有 publish_result 但没有 reply_sent」就是这个。** 桥成功了、文案也生成好了，
但 sendmessage 没成功 → 用户什么都没收到。日志里必须留痕，否则排查时会误判成
「回复发出去了但用户没看到」。

这个失败点有三个来源，`_send_reliable` 全部覆盖：

1. 传输失败（`ILinkAPIError`，如 `-14` token 过期）；
2. 非 iLink 的网络异常（`aiohttp` 之类）；
3. **HTTP 200 但业务失败**（`ret`/`errcode` != 0）—— 以前这一条**完全没有日志**，
   因为 `ensure_business_success` 在 try 块外面抛的。

### flow id 贯穿所有回复路径

`flow=` 不只发稿路径有 —— `/help`、`/time`、`/重新连接`、语音提示、AI 回复
全都带（`_send_reliable` 是所有可靠回复的唯一出口）。所以
`clawbot.diagnostics` 的 `diag stage=inbound` 和 `diag stage=reply_sent`
之间永远能用同一个 id 对上，AI 路径也照样能串。

## 怎么用

### 「用户说发了没反应」

```bash
grep -E "flow=[0-9a-f]{8}" logs/clawbot.log | tail -40
```

按出现的 stage 判断卡点：

| 看到 | 说明 | 下一步 |
|---|---|---|
| 完全没有 `publish_intent` | 命令没被认领（说法没匹配上） | 不是故障；`publish_unclaimed`（DEBUG）说明认领逻辑跑过了 |
| 有 `publish_intent`，没有 `bridge_call` | 桥模块 import 失败 | 看 `publish_unavailable` 的 err |
| 有 `bridge_call`，没有 `bridge_result` | 桥卡住 / 超时 | 看 `bridge_error`（`code=timeout`）或 `bridge_stderr` |
| `bridge_result ok=False code=need_select` | 命中多篇，等用户回 `/选 N` | 不是故障 |
| `bridge_result ok=False code=not_found` | 知识库里没这篇 | 看 `publish_result` 的回信内容 |
| `bridge_result ok=True`，有 `publish_result` | 发稿成功 | 看 `reply_sent` 有没有 |
| 有 `publish_result`，**没有** `reply_sent` | 回信发不出去 | 看 `clawbot.message` 的 `send_reliable failed`（带同一个 flow） |
| 有 `reply_sent`，微信没收到 | 问题在 iLink 侧 | 看 `clawbot.diagnostics` 的 `send_reliable failed` |

### 桥的内部为什么慢

`bridge_result` 带 `elapsed_ms`。出图 + 排版 + 上传是慢的部分，
`XIAOLVSHU_TIMEOUT` 默认 240 秒。超时会记 `bridge_error code=timeout`。

### 用户回「/选 N」之后

两步流程（`/发贴图号 沉香` → 多命中 → `/选 2`）是**两条独立的入站消息，
两个 flow id**。桥把候选存在自己的 `pending.json` 里。
对账办法：

```bash
grep "stage=publish_need_select" logs/clawbot.log | tail -5   # 当时有哪几篇
grep "stage=publish_result action=select" logs/clawbot.log | tail -5   # 选了几号、发成功没
```

## 脱敏

- `utils/xiaolvshu._scrub()` 在写入日志前抹掉绝对路径（回信里的 vault 路径
  对排障没用，却会把每个用户的目录布局摊进共享日志）。
- `uid=` 记的是**完整** `from_user_id` —— 这是有意的：管理员要照着它填
  `.env` 的 `XIAOLVSHU_USER_<uid净化>_` 三元组（见 `bot_session.py` 的
  `xiaolvshu user=` 那行注释）。
- `clawbot.publish` 的日志同样经过 `RedactFilter` 兜底（token 等敏感字段）。

## 回归测试

[`tests/test_publish_flow_logging.py`](../tests/test_publish_flow_logging.py) ——
9 条，钉住：

1. 一次成功发稿，每一环都留下带同一个 flow id 的记录；
2. 桥超时 → 有 `bridge_error code=timeout`（不是静默）；
3. 桥没输出 JSON → 有 `bridge_error code=no_json_line` + stderr 尾巴；
4. 多命中 → 有 `publish_need_select` + 候选标题；
5. 普通聊天 → 不被认领、不调桥；
6. 桥的 stderr → 进日志（DEBUG）；
7. bot_session ↔ xiaolvshu 两层共用同一个 flow id；
8. 日志里不出现绝对路径；
9. 回信发不出去 → 有带 flow 的 `send_reliable failed`。

这些测试在本次改动**之前全部失败**（改前没有 flow id、没有这些 stage），
不是空跑的护栏。
