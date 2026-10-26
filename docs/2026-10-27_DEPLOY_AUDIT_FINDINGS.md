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
| 1 | 在微信里发一条消息，让 `inbound → reply_sent` 链真正跑一遍 | 用户 | ⬜ 待做 |
| 2 | 决定桥工作区那 157 行改动：提交还是 stash | 桥的作者 | ⬜ 待决 |
| 3 | 提交并推送后，`deploy.sh` 恢复完整部署能力 | 运维 | ⬜ 阻塞于 #2 |

第 1 项是本次审计的**主要缺口**：没有它，埋点只是一段没被跑过的代码，
「扫码成功但无回复」这个故障至今没有定位到具体环节。

---

## 5. 相关文件

- [`docs/WECHAT_CONNECTION_DIAGNOSTICS.md`](WECHAT_CONNECTION_DIAGNOSTICS.md) — 诊断手册（本次修订了 grep 配方）
- [`deploy/README.md`](../deploy/README.md) — 部署流程（本次增加「部署记录」小节）
- `bot_session.py:339/1084/1140/1238/1367/1372/1378/1491/1505` — 9 个埋点位置
- `utils/xiaolvshu.py:37` — `_check_bridge_version()`（读进程 env 的 pin）
