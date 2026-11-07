# 2026-10-27 部署审计：诊断埋点上线后的落地情况

> 时间：2026-10-27 06:00–06:20 CST
> 起因：用户问「按 [`docs/WECHAT_CONNECTION_DIAGNOSTICS.md`](WECHAT_CONNECTION_DIAGNOSTICS.md) 处理好并完成新代码部署了吗」
> 结论：**代码部署了，但诊断手册要求的流程没有走完** —— 埋点已布好却从未被业务流量触发，
> 「消息卡在哪一环」目前仍无证据。

本文档记录审计发现的 4 个问题 + 已执行的处置。

---

## 1. 部署现状

### 已上线

| 项 | 状态 | 证据 |
|---|---|---|
| 埋点代码 | ✅ 在跑 | `clawbot.diagnostics` logger 有输出，`bot_session.py:339/1084/1140/1238/1367/1372/1378/1491/1505` |
| 账号 token | ✅ 未掉线 | 两个会话 `bot_token` 均非空，`reauth_required=False` |
| 长轮询 | ✅ 健康 | 重启后 `getupdates → 200` 持续返回，2 个会话各 1 次/18s，无 `worker_failed` / `getupdates_error` |
| 契约测试 | ✅ 全过 | 74 pytest + 8 unittest + 桥契约 9 passed |

### 部署记录

| 时间 | A（ClawBot） | 桥 | 服务 | 备注 |
|---|---|---|---|---|
| 2026-10-26 20:42:42 CST | `ce8ce14` | `1840340` | 重启 | 埋点首次上线（原「正式代码」版本） |
| 2026-10-27 06:17:44 CST | `b36d4b0` | `1840340` | 重启 | 本次审计后重新部署，让进程拾取 `.env` 里更新的 pin |

> ⚠️ **本文档之前没有任何地方记录部署 commit**（`deploy/README.md`、systemd unit、git tag 都没有）。
> `deploy/deploy.sh` 只 `echo` 到 stdout，退出后不留痕。现补此表。

---

## 2. 问题清单

### P0 — 埋点从未被业务流量触发，诊断仍无证据

诊断手册的核心目的是回答「扫码成功但无回复，消息卡在哪一环」。但埋点各 stage 的实际触发次数：

| stage | 次数 | 说明 |
|---|---|---|
| `notifystart` | 6 | 每次重启 × 2 会话 |
| `message_worker_created` | 6 | 同上 |
| `inbound` | **0** | 从未有过消息进入处理器 |
| `ai_begin` / `ai_complete` | **0** | AI 从未被调用 |
| `reply_sent` | **0** | 回复从未发出 |
| `getupdates_messages` | **0** | `getupdates` 永远返回 `msgs:[]` |

**最后一次真实入站是 2026-10-26 11:44**（当时 `sendmessage → 200` 是通的，即链路本身没问题）。
此后 19 小时零消息 —— 埋点布好之后没有任何人用微信发过一条消息。

**后果**：手册的「复现」步骤（扫码 → 发 `/help` → 发普通文字 → 记录时间 → 记录截图时间 → 多账号互验）
一步都没走。`inbound` 及之后的 stage 从未被触发过，**这些代码路径的正确性在生产上从未被验证过**。

**待办**：需要有人在微信里对一个已登录账号发一条消息（哪怕只是 `/help`），
才能让 `inbound → ai_begin → ai_complete → reply_sent` 这条链真正跑一遍并留下证据。
在此之前，任何关于「消息为什么没回复」的结论都是猜测。

#### 现场验证（2026-10-27 06:20）

用户在 `7I@im.wechat` 发了 `/help`，链路走通：

```
06:20:54.120 diag stage=getupdates_messages count=1
06:20:54.125 diag stage=inbound from_suffix=wechat
06:20:54.125 recv msg from=7I@im.wechat type=text len=5 preview='/help'
06:20:54.531 POST ilink/bot/sendmessage → 200 {"message_id":7520610437408708488}
```

用户确认**微信客户端收到了帮助内容** → 「扫码成功但无回复」在 `7I@im.wechat` 上**不复现**，
`getupdates` / `inbound` / `sendmessage` 三段都正常。

**但这次验证暴露了第五个问题**（见下）——`reply_sent` 没触发，尽管回复确实发出去了。

### P0 — `reply_sent` 埋点只覆盖 1/7 条回复路径（已修）

上面那次 `/help` 走完全程、`sendmessage` 返回 200，但日志里**没有 `diag stage=reply_sent`**。

原因：`reply_sent` 原来挂在 `handle_message` 的 **AI 分支末尾**（旧 `bot_session.py:1378`），
而 7 条回复路径里只有 1 条经过那里：

| 路径 | `_send_reliable(kind=…)` | 原来有 `reply_sent`？ |
|---|---|---|
| 重连确认 Y/N | `reconnect-confirm` | ❌ |
| 语音无转写提示 | `voice-transcript-missing` | ❌ |
| `/help` | `help` | ❌ |
| `/time` | `time` | ❌ |
| `/重新连接` 提示 | `reconnect-prompt` | ❌ |
| 发稿桥回复 | （1352 行） | ❌ |
| **AI 回复** | `ai-reply` | ✅ 仅此一条 |

命令分支全部在 `reply_sent` 之前 `return`。

**为什么这是 P0**：这正好是本次排查要用的那个信号。「扫码成功但无回复」的排查逻辑是
「看 `reply_sent` 有没有」—— 但命令类回复发出去了却没有这个标记，
**排查时会误判成「回复根本没发出去」，从而把方向带到完全错误的地方**。
换句话说，这个埋点本身会制造假线索。

**处置**：把埋点下沉到 `_send_reliable`（所有可靠回复的唯一出口），并加 `kind=` 字段
区分是哪条路径发的：

```python
# bot_session.py:_send_reliable 末尾，ensure_business_success 之后
get_logger("diagnostics").info(
    "diag stage=reply_sent session=%s kind=%s from_suffix=%s",
    self.session_id, kind, to_id[-6:],
)
```

配套契约测试 `tests/test_bot_session_contract.py::test_reply_sent_marker_covers_every_reply_path`
（命令分支 + AI 分支各应一条，且都带 `kind=`）。已验证该测试在修复前**确实失败**
（只拿到 1 条标记）、修复后通过 —— 不是空跑的护栏。

### P1 — 诊断手册的采集命令 grep 配方有误

手册 §采集日志 给的配方：

```bash
journalctl -u clawbot-shared.service --since '30 minutes ago' --no-pager -o cat \
  | grep -E 'diag stage=|notify_lifecycle|send_reliable|task_failed|Traceback|ERROR|WARNING'
```

问题在 `task_failed`：

- 它是 `bot_session.py:1142` 里 `_emit("task_failed", ...)` 的**内部事件名**，
  走的是 `_on_event` 回调（web UI 事件流），**从来不写进日志**。实测两个日志文件里 0 命中。
- 真正的日志文本是 `bot_session.py:1140` 的 `diag stage=worker_failed`。
  它已被配方里的 `diag stage=` 覆盖 —— 所以配方**能跑通**，但 `task_failed` 这个词会让人误以为
  存在一个叫 task_failed 的日志阶段。手册 §日志解释 里描述的也是 `worker_failed`，前后不一致。

`send_reliable` 则是**方法名**（`bot_session.py:1148`），其日志文本是
`send_reliable failed path=sendmessage err=...`（`bot_session.py:1187`），
**只在发送失败时出现**，正常路径永远不命中。

**处置**：手册里 `task_failed` 已改为 `diag stage=worker_failed`（见该文档修订）。
`send_reliable` 保留（失败时确实能命中），但注释说明它只在失败路径出现。

### P1 — 没有地方记录「实际部署的 commit」

手册 §部署 明确要求「记录实际部署的 commit」，但：
- `deploy/README.md` 只在示例里提到过某个 commit（`8a88d6f`），不是部署记录
- systemd unit 里没有版本标记
- git 没有 tag
- `deploy/deploy.sh` 只把 commit `echo` 到 stdout，不落盘

**处置**：
- 本文档 §1 补上部署记录表
- `deploy/README.md` 增加「部署记录」小节，约定每次部署追加一行

### P1 — 进程 env 里的 pin 与 `.env` 不同步（已修）

- `.env` 的 `XIAOLVSHU_BRIDGE_REF=1840340...`（mtime 2026-10-26 20:44:19）
- 运行中的进程 env 却是 `f0c30c36...`（进程启动于 20:42:42，**早于 `.env` 的修改**）

systemd 的 `EnvironmentFile` 只在进程启动时读一次 `.env`。此后改 `.env` 不会影响已运行的进程。
而 `utils/xiaolvshu.py:37` 的 `_check_bridge_version()` 读的是 `os.environ`（进程 env），
所以它拿旧 pin 去比对桥的 HEAD → **必然误报 version drift**。

**处置**：2026-10-27 06:17:44 重启，进程 env 已是 `1840340`，与 `.env` 和桥 HEAD 三方一致。
实测新进程 env：

```
XIAOLVSHU_BRIDGE_REF=184034081d0d2c15d0d5bf04bbcc0d72ad2f2890
```

> ⚠️ **运维提醒**：改 `.env` 后必须重启服务才生效。`deploy.sh` 的默认流程会重启，
> 但 `--no-restart` 只改代码不重启 —— 那时 `.env` 的改动不会进进程 env。

---

## 3. 已执行的处置

```bash
./deploy/deploy.sh --app-only
```

**为什么用 `--app-only` 而不是完整部署**：

- app 侧 `main` 已与 origin 同步（`b36d4b0`），`git pull` 是 no-op
- 桥的 HEAD `1840340` 已等于 `.env` 的 pin，桥侧无需 checkout
- **桥的工作区是脏的**（见下），完整部署会在 checkout 前直接拒绝退出

### 桥仓库工作区脏（未处置，需人工决策）

`/opt/obsidian-xiaolvshu` 当前有未提交改动：

```
 M README.md                  |  34 ++-
 M scripts/gzh_xiaolvshu.py  | 157 ++++++++++++++++++++++++++++++---
 M xiaolvshu                  |   8 ++
?? scripts/_old_gzh.py
?? scripts/fix_draft_title.py
```

内容是**笔记内嵌图 / 自定义封面图**功能（在建的正式功能，不是正式代码）。
`deploy.sh` 会因工作区脏而拒绝部署 —— 这个行为是对的（脏工作区意味着「装到生产的不是 pin 那版」，
不可复现）。

本次**没有**提交、stash 或丢弃这些改动：它们是别人在建的正式功能，不属于本次审计范围。

**两个后果**：

1. `utils/xiaolvshu.py:60` 的 dirty 检查会记一条
   `xiaolvshu bridge has uncommitted changes` 告警（`clawbot.message`）。
   这是**如实反映**，不是误报。
2. 完整部署（不带 `--app-only`）目前**跑不通**。要恢复完整发布能力，需要先决定：
   这些改动是提交（进正式代码）还是 stash（留在本地）。

契约测试对**脏工作区**（即实际在跑的那份）跑过：9 passed —— 桥的 JSON 契约仍与消费者一致。

---

## 4. 遗留待办

| # | 事项 | 责任 | 状态 |
|---|---|---|---|
| 1 | 在微信里发一条**普通文字**，验证 AI 路径（`ai_begin` → `ai_complete` → `reply_sent`） | 用户 | ⬜ 进行中 |
| 2 | 决定桥工作区那 157 行改动：提交还是 stash | 桥的作者 | ⬜ 待决 |
| 3 | 提交并推送后，`deploy.sh` 恢复完整部署能力 | 运维 | ⬜ 阻塞于 #2 |

第 1 项进展：`/help` 已验证通过（命令路径 → 服务端 200 → 客户端收到）。
`/help` 是命令、走不到 AI，所以 `ai_begin` / `ai_complete` 仍是 0 次 ——
**AI 路径仍待验证**。这一步很关键：AI 路径才是「暂无法连接 OpenClaw」最可能出问题的地方
（AI 超时/报错会导致整条回复失败，而命令路径不经过 AI）。

---

## 5. 相关文件

- [`docs/WECHAT_CONNECTION_DIAGNOSTICS.md`](WECHAT_CONNECTION_DIAGNOSTICS.md) — 诊断手册（本次修订了 grep 配方）
- [`deploy/README.md`](../deploy/README.md) — 部署流程（本次增加「部署记录」小节）
- `bot_session.py:339/1084/1140/1238/1367/1372/1491/1505` — 埋点位置
- `bot_session.py:_send_reliable` — `reply_sent` 埋点现址（本次从 AI 分支下沉到此）
- `tests/test_bot_session_contract.py::test_reply_sent_marker_covers_every_reply_path` — 回归护栏
- `utils/xiaolvshu.py:37` — `_check_bridge_version()`（读进程 env 的 pin）
