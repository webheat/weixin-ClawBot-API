# Ephemeral bot lifecycle & systemd dependency analysis

2026-09-15 · 写于排查 `/switch` QR 互踩 bug 之后

## TL;DR

ephemeral bot **协议层**完全不需要 systemd —— iLink 2.4.6 只看 HTTP 请求里的 token，不管 bot 进程是 systemd 启的还是 portal subprocess 启的。当前用 systemd 主要是 **ops 一致性**（跟 named users / OAuth bot 同一套机制）+ **3 个真正的免费午餐**（崩溃自动重启 / cgroup 进程隔离 / ops 工具链）+ 几个成本项。结论：**现状 70 分，可以一直用**；真要改也是无痛的、可逆的。

## 当前架构

```
访客 (无 cookie)
    ↓ GET /
portal (clawbot-portal.service, :18300)
    ↓ handle_ephemeral_start
bot_launcher.start_or_get("eph_<hex>", openid="")
    ↓
    ├─ _allocate_port     (扫一段空闲 TCP 端口)
    ├─ _create_env_file   (写 /etc/clawbot/eph_<hex>.env)
    ├─ _start_systemd     (daemon-reload + systemctl start clawbot@eph_<hex>.service)
    └─ _wait_port         (等到端口监听)
    ↓
bot.py --user eph_<hex>  (systemd 模板 clawbot@.service 启的)
    ↓ bind :eph_<hex> 端口
iLink 2.4.6 HTTP
```

回收（sweeper 每 5 分钟跑）：

```
bot_launcher.reap_ephemeral(ttl=8h, grace=10min)
    ↓ 对每个 last_used_at 超期的 eph_*
    ├─ systemctl stop clawbot@eph_<x>.service
    ├─ rm /etc/clawbot/eph_<x>.env
    └─ 从 var/bot_sessions.json 移除
```

## systemd 给我们什么（去掉后要自己补）

**真正是"免费午餐"的**（不写代码就有）：

| 能力 | systemd 路径 | 重要性 | 替代成本 |
|---|---|---|---|
| 崩溃自动重启 | `Restart=on-failure` 模板自带 | **高** | portal 要自己 `waitpid` + `Popen` 重启循环 + 写 PID 跟踪 |
| 进程隔离 | cgroup 内存/CPU 限制（v2） | 中 | portal 自己 `prlimit` / 显式 cgroup 配置 |
| ops 工具链 | `systemctl status` / `journalctl` / `list-units` | **高**（debug 时） | 只能 `ps aux` 过滤或维护 sessions.json 索引 |

**表面对我们意义有限的**：

| 能力 | systemd 路径 | 现实情况 |
|---|---|---|
| 日志收集 | journald 自动收 | **没接上**：`bot_launcher.py` 起 systemd 没传 `StandardOutput=journal`，`bot.py` 直接写 `logs/clawbot_eph_*.log`。换 subprocess 完全等价（甚至更好：不用 systemd 转发） |

**subprocess 模式反而赢的**（节省成本，不是免费午餐）：

| 能力 | systemd 路径 | subprocess 路径 | 差距 |
|---|---|---|---|
| 冷启动 | `daemon-reload` ~100ms + `start` ~200ms | `Popen` ~50ms | -250ms（扫码场景下用户感知不到） |
| `/etc/clawbot/` 污染 | 每次留一个 `eph_*.env`，sweeper 异步回收 | 不写 env 文件 | 杜绝孤儿残留 |
| 回收路径 | `systemctl stop` + `rm env` + 改 JSON = 3 步 | `kill -TERM` + 改 JSON = 2 步 | 少 1 步，sweeper 出 bug 时影响小 |
| 端口/env 传递 | 必须写 env 文件让模板 `%i` 看到 | 走 argv 即可 | 少一次磁盘 IO |

> **TL;DR**：真正的免费午餐只有 **3 个**（崩溃重启 / cgroup / ops 工具链）。journald 那个其实没用上。日志写到文件、端口读 env、回收三步这些是"systemd 路径的固有成本"——subprocess 在这些维度反而更优。

## 协议层需求 vs ops 层需求

iLink 2.4.6 要的最小集：

- 一个进程能跑 `bot.py` 完整流程（HTTP client / 长轮询 / 定时重连）
- 该进程能 bind 一个 TCP 端口给 web QR 登录用
- 该进程能持久化 `bot_token` / `baseurl`（到 `weixin_state_eph_<x>.json`）
- 该进程崩了重启后能从持久化恢复（iLink 接受旧 token 直到 -14）

**没有任何一条**写"必须由 systemd unit 启"。

## 各路径评估

### Named users（alice / OAuth 绑定的 `openid[:12]`）

保留 systemd。

理由：
- 稳定身份 —— ops 需要 `systemctl restart clawbot@alice`、`journalctl -u clawbot@alice` 排错
- 长期在线 —— cgroup 限制可防止单用户 OOM 影响其他用户
- 配置持久 —— `/etc/clawbot/alice.env` 长期保留，systemd 是它的天然读者

### Ephemeral

可以改 subprocess，可不改。三个潜在收益：

1. **冷启动快 ~250ms**：但用户要扫 QR 啊，250ms 感知不到
2. **`/etc/clawbot/` 不被临时文件污染**：痛点真实 —— sweeper 删 env 是 sweeper 跑准时（5 min 一次）才生效；中途 portal 重启 / sweeper 挂了就积累
3. **回收路径少一步**：从 `systemctl stop + rm env + JSON` 三步简化到 `kill PID + JSON` 两步

潜在代价：

1. **portal 变 SPOF**：portal 死了所有 ephemeral 跟着死，访客得重新点按钮（比 named user 影响小，因为 ephemeral 本来就要重新扫 QR）
2. **失去 cgroup**：单 ephemeral OOM 影响其他 ephemeral（现实概率低 —— bot.py 单实例内存稳定 ~45MB）
3. **失去自动重启**：ephemeral 崩了 = 访客要重新触发（可接受 —— portal 的 `/ephemeral/start` 是幂等的，重新点会拿一个新 short_id）

## 当前痛点（跟 systemd 强相关，可独立修）

不一定要换 subprocess，下面这些小修就能缓解：

1. **`daemon-reload` 每次都跑**（`utils/bot_launcher.py:130`）：因为要 `eph_*.env` 写完让模板看到。改成 subprocess 彻底绕过。
2. **sweeper 漏跑的 env 残留**：`eph_*.env` 文件如果 sweeper 挂了就留在 `/etc/clawbot/`。两条路：
   - 改成 tmpfs（重启清空）
   - 启动 sweeper 时先扫 `/etc/clawbot/eph_*.env` 对比 sessions.json，孤儿清掉
3. **端口分配竞态**：`utils/bot_launcher.py:_allocate_port` 一次性 bind(0) → 关 fd → 把数字写 env。这个数字如果 bot 还没起 systemd 还没监听就被另一个 visitor 抢到就冲突。当前 `BOT_PORT_TIMEOUT_S` 内 `_wait_port` 能挡住，但浪费了一次 systemd start。subprocess 模式下同样问题。

## 改造方案（如果真要换 subprocess）

**核心改动**：`utils/bot_launcher.py` 新增 `_start_subprocess` 分支，按 `WXOAPP_EPHEMERAL_SUBPROCESS=1` 切换。

```python
def _start_subprocess(self, short_id: str, port: int) -> int:
    """subprocess 模式启 bot：直接 Popen bot.py，PID 写到 sessions.json 备查。"""
    env = {
        **os.environ,
        "CLAWBOT_USER": short_id,  # 或者用 --user argv
        "CLAWBOT_WEB_PORT": str(port),
        # ... 其他 env ...
    }
    log_path = LOG_DIR / f"clawbot_{short_id}.log"
    fp = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", "bot.py", "--user", short_id],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=fp, stderr=fp,
        start_new_session=True,  # 独立进程组，sweeper 可整组 kill
    )
    return proc.pid
```

**sweeper 改写**（按 PID kill，不再 systemctl）：

```python
def reap_ephemeral(self, ttl, grace=600):
    for short_id, sess in self._load_sessions().items():
        if not short_id.startswith(EPHEMERAL_PREFIX):
            continue
        if time.time() - sess["last_used_at"] < ttl + grace:
            continue
        pid = sess.get("pid")
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)  # 整进程组
            except ProcessLookupError:
                pass
        # sessions.json 自动清理
```

**crash 重启**（portal 侧 watchdog）：

- portal 启动时扫 sessions.json，对 `pid` 还存在但进程已死的 ephemeral → 重新 `_start_subprocess`（不分配新 short_id，复用 cookie 兼容性）
- 或干脆清掉，让访客重新点按钮（更简单，可接受）

**配套改动**：

- `CLAUDE.md` Multi-user layout 表里 ephemeral 行加 `(subprocess via portal)` 标记
- `var/bot_sessions.json` schema 加 `pid` 字段
- systemd 模板 `clawbot@.service` 保留（named users 还在用）

## 建议

**按兵不动**。理由：

- 当前 ephemeral 流程稳定，sweeper 跑得动，`/etc/clawbot/` 没积压风险（graces 10 min 够长）
- 改 subprocess 收益不显著（~250ms 用户无感、`/etc/clawbot/` 现状是干净的）
- 改 subprocess 风险：portal SPOF + cgroup 丢失 + crash 重启策略未验证
- 真正要改的触发条件：sweeper 出 bug / env 文件残留 / 单 portal 撑不住 100+ ephemeral（那时再上 systemd-run 或独立 bot pool）

**短期可独立修**（不改架构）：

- sweeper 加孤儿 env 清理（启动时扫 `/etc/clawbot/eph_*.env` 对比 sessions.json）
- `_allocate_port` 记录分配时间，超时未 listen 的端口回收到池子

## 相关代码位置

- `utils/bot_launcher.py:128-138` — `_start_systemd`
- `utils/bot_launcher.py:142-190` — `start_or_get`（编排）
- `utils/bot_launcher.py:205-249` — `reap_ephemeral`（回收）
- `/etc/systemd/system/clawbot@.service` — systemd 模板
- `qr_portal.py:handle_ephemeral_start` — 触发入口
- `CLAUDE.md` "Multi-user layout" 表 — ephemeral 行的当前说明

## 变更日志

- 2026-09-15 · 初版（写于 `/switch` 互踩 bug 修复后讨论）
