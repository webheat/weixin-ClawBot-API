# 微信开放平台 OAuth2.0 扫码登录 — 现状与多路径规划

> 本文是 2026-09-09 OAuth 接入完成后的总结 + 后续改造方向分析。
> 完整接入 commit: `7d0de88 feat(oauth): 微信开放平台 OAuth2.0 扫码登录 + bot 进程动态启停`

## 当前架构（单一路径 `/clawbot/`）

```text
                     ┌──────────────────────────────────────────────────┐
                     │  nginx ocbot.aixifs.com / ocbot.aixifs.com       │
                     │                                                  │
                     │  location ^~ /clawbot/  → :18300 (尾斜杠剥离)     │
                     │  location ^~ /oauth/    → :18300 (透传 /oauth/)  │
                     │                                                  │
                     └────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
                     ┌──────────────────────────────────────────────────┐
                     │  qr_portal.py (ClawBot Portal)                   │
                     │  ├ OAuth2.0 入口：/oauth/login  /oauth/cb       │
                     │  ├ 启 bot：utils/bot_launcher.start_or_get()     │
                     │  ├ cookie：clawbot_user=<openid_short> (8h)      │
                     │  └ 反代：proxy_handler → bot 进程 :port         │
                     └────────────┬─────────────────────────────────────┘
                                  │
            ┌─────────────────────┼─────────────────────┐
            ▼                     ▼                     ▼
   clawbot@alice          clawbot@<openid_1>     clawbot@<openid_2>
   :18301 (env_file)      :18302 (OAuth)         :18303 (OAuth)
            │                     │                     │
            ▼                     ▼                     ▼
      qr_web.py (iLink 二维码) 各自一份 weixin_state_*.json
```

**核心约束**：
- `qr_portal.py` 把 `/clawbot/` 路径前缀**写死**（`_absolute_portal_url()` 默认 `/clawbot/`）
- nginx `location ^~ /clawbot/` 也**写死**
- portal 路由注册 `/oauth/{login,cb,poll,logout}`，**写死**

## 开放平台"授权回调域"的实际规则

**同一个 AppID + 同一个"授权回调域"裸域名，可以同时让多个 redirect_uri 合法**——只要它们都在该域名下，路径随便起：

| redirect_uri | 是否合法 | 说明 |
|---|---|---|
| `https://ocbot.aixifs.com/oauth/cb` | ✅ | 顶层路径，最简 |
| `https://ocbot.aixifs.com/clawbot/oauth/cb` | ✅ | 当前用 |
| `https://ocbot.aixifs.com/alice/oauth/cb` | ✅ | 任意层级 |
| `https://ocbot.aixifs.com/team-a/bot/oauth/cb` | ✅ | 多级路径 |
| `https://ocbot.aixifs.com:8443/oauth/cb` | ❌ | 端口必须默认 443 |
| `https://other-domain.com/oauth/cb` | ❌ | 域名必须在"授权回调域"白名单内 |

**关键**：开放平台**只校验域名**（裸域名，例如 `ocbot.aixifs.com`），**不校验路径**。后台"授权回调域"字段填 `ocbot.aixifs.com` 后，`ocbot.aixifs.com/任何路径` 都合法。

**这意味着一个开放平台网站应用可以服务多个 ClawBot 部署 / 多组用户，不需要每个单独申请 AppID**。

## 当前实现的局限

- `qr_portal.py` 写死了 `/clawbot/` 前缀 → 任何想换路径的部署都得改源码 + 改 nginx
- nginx 的两个 `location` 也写死 → 加新路径就得加新 location
- 想要"多实例共享同一开放平台资源"必须改造

## 三种改造方向

### 方案 A：保留单路径，支持多个 REDIRECT_URI 别名

保持 `qr_portal.py` 路径写死，只在 `.env` 加 `WXOAPP_REDIRECT_URI_2` / `_3` ...：

```bash
WXOAPP_REDIRECT_URI=https://ocbot.aixifs.com/clawbot/oauth/cb
WXOAPP_REDIRECT_URI_2=https://ocbot.aixifs.com/portal/oauth/cb
WXOAPP_REDIRECT_URI_3=https://ocbot.aixifs.com/work/oauth/cb
```

portal 在 `_absolute_portal_url()` 里轮询多个候选 URL，或者按某种策略选第一个可达的。

- **优点**：零代码改动，只加几行 env + helper
- **缺点**：每个路径仍然**必须经过 portal 实例**（单点），且 cookie / sessions 全局共享，**不能隔离**用户组
- **适用**：一个 ClawBot 实例 + 多个"登录入口"别名（同一用户可以从不同书签进入）

### 方案 B：portal 改成"路径前缀动态化"（推荐）

让 portal 接受**任意 `/<name>/` 路径前缀**，从 path 提取 namespace，nginx 用正则 location 反代所有：

**nginx 改动**（`/etc/nginx/sites-available/ocbot.aixifs.com`）：
```nginx
# 替换原来的 ^~ /clawbot/ 和 ^~ /oauth/ 两段
location ~ ^/([^/]+)/(oauth|)(?<tail>.*)$ {
    proxy_pass http://127.0.0.1:18300/$1/$2/$3$is_args$args;
    proxy_set_header X-Forwarded-Prefix /$1;
    include /etc/nginx/snippets/proxy-headers.conf;
    # 其余保留：proxy_buffering off / proxy_read_timeout 65s 等
}
```

**portal 改动**（`qr_portal.py` ~120 行）：
1. 路由注册 `/{name}/oauth/{login,cb,poll,logout}` 而非 `/oauth/...`
2. `_absolute_portal_url(request)` 改读 `X-Forwarded-Prefix` 拼路径
3. `enumerate_users()` 按 namespace 分组（或者保留全局共享）
4. `bot_launcher.start_or_get(short_id, openid, namespace=<name>)` 加 namespace 参数，sessions 路径区分
5. `OAUTH_LOGIN_HTML` 的链接改成 `/{name}/oauth/login`

**优势**：
- 一个开放平台 AppID 可以服务**无限个 namespace**（`/alice/`、`/bob/`、`/work-bot/`、`/team-a/bot/` ...）
- 每个 namespace **独立的 sessions / cookie / bot 进程池**（可选项）
- nginx 一次配置，永久扩展
- 部署时只需选 namespace 路径，不需要改 portal / nginx

**缺点**：
- 改造范围中等（portal 路由 / nginx / bot_launcher 三处协调）
- 测试矩阵变大（每个 namespace 都要走一遍 OAuth → 启 bot → iLink 二维码）

**适用**：多实例部署、不同用户组隔离、未来需要扩展多 namespace。

### 方案 C：保持现状不动

`/clawbot/` 单路径就够用，将来真要扩展再改。

- **优点**：零成本
- **缺点**：每个新路径都要改 portal 源码 + nginx 配置 + reload，无法快速响应

## 推荐方案：方案 B

理由：
1. 方案 A 虽然改动小，但**不解决"多 namespace 隔离"问题**——cookie 全局共享意味着 alice 和 bob 在同一 sessions 里串味
2. 方案 C 等于"以后再说"，未来真要扩展还得做一遍方案 B
3. 方案 B **一次投入，长期收益**——改完后加 namespace 只需改 `.env` 一行 + 重启 portal

## 方案 B 实施细节

### Step 1：nginx 改一行正则 location

替换 ocbot.aixifs.com 的两段 `location ^~ /clawbot/` + `location ^~ /oauth/`，合并为：

```nginx
location ~ ^/([^/]+)/(.*)$ {
    proxy_pass http://127.0.0.1:18300/$1/$2$is_args$args;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Port  $server_port;
    proxy_set_header X-Forwarded-Prefix /$1;   # ← 关键：让 portal 知道自己挂在哪个 namespace
    proxy_set_header Connection "";

    proxy_buffering off;
    proxy_read_timeout 65s;
    proxy_send_timeout 65s;

    access_log /var/log/nginx/clawbot_access.log;
    error_log  /var/log/nginx/clawbot_error.log;

    add_header Referrer-Policy "no-referrer" always;
}
```

### Step 2：portal 路由注册加 `{name}` 前缀

```python
# 旧：app.router.add_get("/oauth/login", handle_oauth_login)
# 新：
async def handle_oauth_login_with_name(request):
    request = request  # already has match_info["name"]
    return await handle_oauth_login(request)

for name in ("{name}",):  # aiohttp 用 {name} 占位符
    app.router.add_get(f"/{name}/oauth/login", handle_oauth_login)
    app.router.add_get(f"/{name}/oauth/cb",    handle_oauth_cb)
    app.router.add_get(f"/{name}/oauth/poll",  handle_oauth_poll)
    app.router.add_get(f"/{name}/oauth/logout", handle_oauth_logout)
    app.router.add_get(f"/{name}/",            handle_index)
```

### Step 3：portal helper 改读 `X-Forwarded-Prefix`

```python
def _absolute_portal_url(request, path=""):
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    host   = request.headers.get("Host", "ocbot.aixifs.com")
    prefix = request.headers.get("X-Forwarded-Prefix", "").rstrip("/")
    return f"{scheme}://{host}{prefix}{path}"
```

### Step 4：bot_launcher 加 namespace 参数（可选）

如果要让每个 namespace 独立 bot 进程池：

```python
def start_or_get(self, short_id: str, openid: str, namespace: str = "") -> dict:
    sessions_path = Path(f"var/{namespace}/bot_sessions.json") if namespace else Path("var/bot_sessions.json")
    # 其余逻辑按 namespace 隔离
```

或者保持现状全局共享——所有 namespace 共用 bot 进程池（更轻量）。

### Step 5：测试矩阵

```bash
# 单一 namespace（保持原行为）
curl -sk https://ocbot.aixifs.com/clawbot/ -I    # 应 200 (扫码按钮页)

# 多 namespace
curl -sk https://ocbot.aixifs.com/alice/   -I   # 应 200
curl -sk https://ocbot.aixifs.com/bob/     -I   # 应 200
curl -sk https://ocbot.aixifs.com/work/    -I   # 应 200

# OAuth 路径在每个 namespace 下都应工作
curl -sk "https://ocbot.aixifs.com/alice/oauth/login" -I
curl -sk "https://ocbot.aixifs.com/bob/oauth/login"   -I

# 浏览器实测：访问 /alice/ 走完整 OAuth → bot 启动 → iLink 二维码
```

## 回滚方案

如果方案 B 实施后发现问题，回滚成本低：
- nginx：`nginx -t && systemctl reload nginx` 改回原 `location ^~ /clawbot/` + `location ^~ /oauth/`
- portal：git revert `7d0de88` → `git reset --hard <previous-commit>` → `systemctl restart clawbot-portal.service`
- 数据无损：bot_sessions.json / oauth_bindings.json / env / portal service 文件都没动

预计回滚时间 < 5 分钟。

## 工作量评估

| 阶段 | 行数 | 风险 |
|---|---|---|
| nginx 改正则 location | ~30 行 | 低（仅一处配置 reload） |
| portal 路由加 `{name}` | ~40 行 | 中（要保证 match_info 流转对） |
| `_absolute_portal_url` 改用 header | ~5 行 | 低 |
| `bot_launcher` 加 namespace | ~30 行（如果不隔离可跳过） | 低 |
| 测试矩阵 | ~100 行（脚本） | — |
| **总计** | **~100-200 行** | **中** |

## 触发条件

下列任一情况出现时**强烈建议立刻实施**：
1. 需要在 `/clawbot/` 之外再加一个入口（例如 `/work/` 给另一个团队）
2. 不同 namespace 需要**独立 sessions / cookie 池**（避免 alice 看到 bob 的会话）
3. 想给不同 namespace 不同的 cookie max_age 或 OAuth scope
4. 部署超过 1 个 ClawBot 实例（多机部署）

下列情况**保持现状即可**：
1. 只有单一部署、单一用户组、单路径
2. 不打算扩展多 namespace

## 相关文件

- `wechat_oauth.py` — OAuth2.0 客户端（网站应用专用端点 `connect/qrconnect`）
- `utils/bot_launcher.py` — 按 openid 启停 bot 进程
- `qr_portal.py` — portal 主程序（写死 `/clawbot/` 前缀）
- `/etc/nginx/sites-available/ocbot.aixifs.com` — 反代配置
- `/etc/systemd/system/clawbot@.service` — bot 进程 systemd 模板
- `docs/multi-user.md` — 多用户场景现状（与本文档互补）

## 决策记录

- 2026-09-09: 完成 OAuth 接入（commit `7d0de88`），路径写死 `/clawbot/`
- 待定：方案 B 是否实施