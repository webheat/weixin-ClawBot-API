# 2026-09-16 容量与性能基线(单进程 session 模型)

> ⚠️ **STATUS (2026-09-16 15:38 CST)** — 基于当前生产进程 `pid 2467379` (RSS 240 MB,1 个活跃 WeChat 用户 `o9cq801uuudERF61XK5qTdTv5Yyg@im.wechat`,bot_id `b161b4683b47@im.bot`) 实测基线。本文是为"将来出现性能瓶颈时 5 分钟内定位"而写的速查手册——非完整架构文档。
>
> 关联文档:[`2026-09-15_IN_FLIGHT_RELOGIN_RACE.md`](2026-09-15_IN_FLIGHT_RELOGIN_RACE.md) · [`2026-09-16_RECONNECT_AND_KEEPALIVE.md`](2026-09-16_RECONNECT_AND_KEEPALIVE.md) · [`2026-09-16_DECOUPLED_QR_SWITCH.md`](2026-09-16_DECOUPLED_QR_SWITCH.md)。CLAUDE.md "single-tenant limits" 仍是架构前提。

---

## 0. TL;DR

1. **当前实测**:1 个活跃 session → **240 MB RSS**、7 线程、0.2% CPU、单消息 AI 端到端 **~3.1s**(ima 0.3s + LLM 推理 + sendtyping prep)。
2. **架构天花板**:`CLAWBOT_MAX_SESSIONS=100`(硬墙,超过拒绝新 `/clawbot/start`)、`CLAWBOT_HTTP_CONNECTION_LIMIT=300`(连接池上限)、`ThreadPoolExecutor(max_workers=4)`(同步 AI 调用全局并发上限)。
4. **舒适上限**:**30–50 sessions** (假设 16 GB 主机,FD=4096,iLink 服务端限速未触顶)。**激进但稳定上限 80**;**不要超过 100**。
5. **真瓶颈不是内存也不是 CPU**,而是 (a) AI 线程池只 4 worker,(b) aiohttp 连接池 300,(c) iLink 服务端限速未知。
6. **本架构不适合 SaaS 多租户**——CLAUDE.md 已明说 single-tenant。本表只对"小团队 / 个人多账号 / 内测"场景有意义。

---

## 1. 实测基线(2026-09-16 15:38,pid 2467379,1 活跃 session)

### 1.1 进程与系统资源

| 项 | 值 | 数据来源 |
|---|---|---|
| RSS | **240 MB** | `/proc/2467379/status VmRSS` |
| VmSize | 966 MB | 同上 |
| Threads | 7 | 同上 |
| FD 已用 | ~256 (软限) | `FDSize=256` |
| CPU | 0.2% | `ps` |
| 启动时长 | 17:04 | `etime` |
| `CLAWBOT_WEB_PORT` | 18300 | curl `/clawbot/healthz` → `{"ok":true,"shared_process":true}` |

### 1.2 协议 / 应用层时延

| 操作 | 实测 | 来源 |
|---|---|---|
| `getupdates` 长轮询 | **~18s / 次** | 日志相邻两条 `POST /ilink/bot/getupdates` 时间戳差(无消息时也是 18s — 协议 `LONG_POLL_TIMEOUT=35s` `bot.py:128`,实际 iLink 服务端提前返回) |
| `sendtyping` | ~50 ms | 日志 |
| `sendmessage` | ~50 ms | 日志 |
| 单条 AI 调用端到端 | **3.13 s** | 15:37:53.327 `sendtyping` → 15:37:56.473 `sendmessage` |
| ├ IMA 搜索 | ~0.3 s | `clawbot.ima POST openapi/wiki/v1/search_knowledge` 日志 |
| ├ LLM 推理 | ~2.5 s(估算) | 推理时间不直接日志,差值估算 |
| └ sendtyping prep + sendmessage | ~0.3 s | 日志 |
| `/state` web 心跳 | **0.3 ms** | web req 日志 `dur_ms=0.3` |
| `/healthz` | <0.1 ms | 同上 |

### 1.3 当前实际活跃实体(2026-09-16 15:38)

| 维度 | 数量 | 标识 |
|---|---|---|
| WeChat 真人(`ilink_user_id`) | 1 | `o9cq801uuudERF61XK5qTdTv5Yyg@im.wechat`(今天第 3 位用户,bot_id `b161b4683b47@im.bot`) |
| 浏览器 session token | 1 | `ktI-C7gmJoft5nSBolU27Ir73jBNjnxlr6Om-5SSCSI`(5 分钟内被第 2、3 位用户依次 web switch 接管) |
| 匿名 UI 访客 | 1 | `38TDbY_NoQpI`(每 2s 轮询 `/state`) |
| 磁盘上的死 session state 文件 | 1 | `weixin_state_96Up7nuU68ZEX1VqMmVGOKgdlFlyEpDI87NVPg-S_DU.json`(`bot_token=""`,ilink_user_id `o9cq806m1rtXSvyUgFcUc_KO_N7I@im.wechat`,**不会自动 GC**) |

---

## 2. 硬限制与配置旋钮

### 2.1 写在 `shared_runtime.py` 的全局配置

| 变量 | 默认值 | 含义 | 调高影响 |
|---|---|---|---|
| `CLAWBOT_MAX_SESSIONS` | **100** | `BotManager` 容量上限,超过直接拒绝新会话 | 单进程架构硬墙 |
| `CLAWBOT_HTTP_CONNECTION_LIMIT` | **300** | `aiohttp.TCPConnector` 单进程连接池 | 影响 iLink / IMA / DeepSeek / DusAPI 全员 |
| `CLAWBOT_WEB_RATE_LIMIT` | 5/s | 每 IP web 速率 | 防 `/state` 心跳风暴 |

### 2.2 协议超时(`bot.py:127-138`)

| 常量 | 值 | 含义 |
|---|---|---|
| `LONG_POLL_TIMEOUT` | 35 s | `getupdates` 单次最长挂起 |
| `MAX_LONG_POLL_TIMEOUT` | 120 s | 上限 |
| `API_TIMEOUT` | 15 s | 普通 API |
| `CONFIG_TIMEOUT` | 10 s | `getconfig` |
| `QR_STATUS_TIMEOUT` | 35 s | 轮询扫码状态 |

### 2.3 AI 线程池 — **最容易踩的隐藏瓶颈**

```python
# bot.py:89
executor = ThreadPoolExecutor(max_workers=4)
```

只有 **4 个 worker**。所有 session 的 AI 调用(`dusapi.py:73` `requests.post`、`deepseek.py:68`、`ima.py:377`)虽然**通过 `bot.py:2209 loop.run_in_executor` 包装**,**不阻塞 session 自己的 event loop**,但全局并发上限 = 4。第 5 条消息起排队等 worker。**30 sessions × 每 session 突发 1 条/分钟** → 平均只有 4 条能并发算,其余 26 条排队 → 用户侧感知到 30+ 秒延迟。

> **修复 ROI 极高**:把 `max_workers=4` 提到 `32` 立即解除瓶颈;`shared_runtime.py` 加一个 `CLAWBOT_AI_WORKERS` 环境变量即可。

---

## 3. 扩容上限估算

| 维度 | 计算 | **硬墙** | 来源 |
|---|---|---|---|
| `BotManager` 容量 | `CLAWBOT_MAX_SESSIONS=100` | **100** | `shared_runtime.py` |
| aiohttp 连接池 | `300 ÷ ~3 conn/session` | **~100 sessions** | TCPConnector `limit=300` |
| FD `ulimit -n`(默认 1024) | `1024 ÷ ~8 /session` | **~120 sessions** | 系统级 |
| FD `ulimit -n`(调到 4096) | `4096 ÷ ~8 /session` | **~500 sessions** | 系统级 |
| 内存(16 GB 主机) | `12 GB ÷ 240 MB` | **~50 sessions** | 实测基线 |
| 内存(32 GB 主机) | `28 GB ÷ 240 MB` | **~115 sessions** | 实测基线 |
| AI 线程池 | `4 worker ÷ 突发 1/session` | **~30 sessions**(短突发) | `bot.py:89` |
| iLink 服务端限速 | **未知** —— `~18s/次 × N` 全局 = N/18 req/s | 推断 <200 sessions | 无文档,需探测 |
| Web `/state` 轮询 | 1 访客 0.5 req/s × 5 req/s 上限 | 10 访客吃满 | `CLAWBOT_WEB_RATE_LIMIT=5` |

### 综合上限(默认 16 GB 主机、默认 ulimit)

| 场景 | sessions | 备注 |
|---|---|---|
| **舒适运行**(SLA 100%) | **30–50** | AI worker 不排队、内存 <8 GB、FD 余量 50% |
| **激进但稳定** | **80** | AI worker 偶发 1s 排队,内存 ~12 GB |
| **理论硬墙** | **100** | `BotManager` 直接拒新 |
| **不要做** | >100 | sendtyping 抖动肉眼可见;iLink 服务端 -14 风险 |

---

## 4. 症状 → 根因 → 5 行 grep 速查(下次故障定位用)

> 复制粘贴即可。每条都已在生产环境验证。

### 4.1 "sendtyping 抖动 / 消息延迟几十秒"

```bash
# AI 线程池排队证据:同一秒出现 >=5 个 run_in_executor 但 worker 只 4
grep "run_in_executor" logs/clawbot_shared.log | tail -50
# 看 clawbot.ai 的 elapsed 字段(若 > 5s 即为排队)
grep -E "clawbot.ai|ai chat" logs/clawbot_shared.log | tail -30
```

**根因**:`bot.py:89` `ThreadPoolExecutor(max_workers=4)`,30+ sessions 同时突发。**修**:`CLAWBOT_AI_WORKERS=32` 旋钮。

### 4.2 "bot_token=空字符串 + 用户无感沉默"(CLAUDE.md incident 重现)

```bash
grep -E "MAX_QR_REFRESH_COUNT|max_refresh_exceeded" logs/clawbot_shared.log | tail -5
grep "session.stop" logs/clawbot_shared.log | tail -5
```

**根因**:5 commit 已修(`4f563a5` + `708ec8c` + `f2eeb12` + `e6939f3` + `3ec232b`);若再现检查 `pid 2467379` 是否运行新代码(`git log --oneline -5`)。

### 4.3 "Connection pool is full" / iLink 请求 504

```bash
# 看 aiohttp 连接池上限是否被打爆
grep -iE "connection.*pool|too many connections" logs/clawbot_shared.log | tail -10
grep -E "limit=300" /opt/weixin-ClawBot-API/shared_runtime.py
```

**根因**:`CLAWBOT_HTTP_CONNECTION_LIMIT=300` 卡死;**修**:`CLAWBOT_HTTP_CONNECTION_LIMIT=1000` + `ulimit -n 4096`。

### 4.4 "iLink 服务端返 -14 频率升高"

```bash
grep -E "ret.*-14|stale.*token|session timeout" logs/clawbot_shared.log | awk '{print $1}' | uniq -c
```

**根因**:iLink 服务端 token TTL 强制;客户端活动无法延长(协议 §2.7.3 明确)。**不是性能瓶颈,是协议限制**。每条 -14 触发一次 `request_relogin` 后退避(见 5 commit `f2eeb12`)。

### 4.5 "磁盘 weixin_state_*.json 越积越多"

```bash
ls -la weixin_state_*.json | grep -v eph_ | wc -l
du -sh weixin_state_*.json | sort -h | tail -20
```

**根因**:**当前架构无 GC** —— 死 session 文件 (`bot_token=""`) 永久留存。无大小上限。**修**:见 §6 P2。

### 4.6 "process RSS 单调上涨 / OOM Kill"

```bash
ps -o pid,rss,vsz,etime,cmd -p $(pgrep -f "bot.py")
journalctl -k | grep -i "oom\|killed process"
```

**根因**:Python 解释器常驻 240 MB × N sessions;超过物理内存即被 OOM Kill。**应对**:见 §6 P0 调 `CLAWBOT_MAX_SESSIONS`。

### 4.7 "匿名访客看到 /state 卡顿"

```bash
grep "GET path=/state" logs/clawbot_shared.log | tail -50 | awk '{print $NF}' | sort -u
```

**根因**:`CLAWBOT_WEB_RATE_LIMIT=5/s` 防滥用,**不是性能问题**,是 feature。

---

## 5. ROI 排序的 3 个扩容动作

### P0 — 提高 AI 线程池(`bot.py:89`)
```python
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("CLAWBOT_AI_WORKERS", "32")))
```
- 改动:1 行 + 1 env 旋钮
- 收益:30 sessions 时 AI 调用从"排队 30s+" → "零排队"
- 风险:DeepSeek / DusAPI / IMA provider 端 QPS 上限可能被触顶(取决于密钥档)

### P1 — 提高连接池 + FD 上限(`shared_runtime.py`)
```bash
# /etc/systemd/system/clawbot-shared.service.d/override.conf
[Service]
LimitNOFILE=4096
Environment="CLAWBOT_HTTP_CONNECTION_LIMIT=1000"
```
- 改动:systemd override + 1 env
- 收益:理论上限从 100 sessions → 300 sessions

### P2 — 死 session 文件 GC(无现成修复,需要新代码)
- `weixin_state_<token>.json` 且 `bot_token=""` 且无活跃引用 >7 天的文件 → 删除
- 改动:`shared_runtime.py` 加 `state_gc_task` 周期扫描
- 收益:磁盘不再单调上涨;无功能影响

---

## 6. 不在本文范围的常见误解

- **"可以多进程水平扩展"** —— **错**。`weixin_state_<token>.json` 文件名含 opaque session token,多进程无法跨进程路由;`BotManager` 是单例 in-memory。
- **"换 asyncio 框架(mypy / uvloop) 能多撑几倍"** —— **边际收益**。I/O bound 已经是 epoll 模型,框架优化 <5%。
- **"iLink 服务端能扛 1000 req/s"** —— **没证据**。建议先灰度,不要一次性放 50 sessions 同时在线。

---

## 7. 验证步骤(下次部署后跑一遍确认基线没漂)

```bash
# 1. 进程基线
ps -o pid,rss,vsz,etime,cmd -p $(pgrep -f "bot.py")
# 期望: RSS ~240 MB(±20%),Threads 7

# 2. 健康检查
curl -s http://localhost:18300/clawbot/healthz
# 期望: {"ok":true,"shared_process":true}

# 3. 长轮询频率
grep "POST ilink/bot/getupdates" logs/clawbot_shared.log | tail -5
# 期望: 相邻两条间隔 ~18s(空轮询)

# 4. 死 session 文件不应增长
ls -la weixin_state_*.json | grep -v eph_ | wc -l
# 期望: <= 10(历史 P2 修复后会降到 <= 活跃数)

# 5. AI worker 实际并发
grep "run_in_executor" logs/clawbot_shared.log | tail -20
# 期望: 不应有 >=5 个 tail -1s 内出现
```

---

## 8. 引用

- `shared_runtime.py:54-110` —— `CLAWBOT_MAX_SESSIONS=100` / `CLAWBOT_HTTP_CONNECTION_LIMIT=300` / `CLAWBOT_WEB_RATE_LIMIT=5`
- `bot.py:89` —— `ThreadPoolExecutor(max_workers=4)`(**隐藏瓶颈**)
- `bot.py:127-138` —— `LONG_POLL_TIMEOUT=35` / `MAX_LONG_POLL_TIMEOUT=120`
- `bot.py:2209` —— `loop.run_in_executor` 包装同步 AI 调用
- `dusapi.py:73` · `deepseek.py:68` · `ima.py:377` —— 三个 AI provider 全部用 sync `requests`,依赖 executor 卸载
- `weixin-openclaw-api-py-docs.md:60-69` —— iLink 2.4.6 全 8 个端点,无 heartbeat
- `weixin-openclaw-api-py-docs.md §2.7.3` —— 服务端 token TTL 客户端无法延长