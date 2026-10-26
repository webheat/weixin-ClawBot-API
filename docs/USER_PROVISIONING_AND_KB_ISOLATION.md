# 每用户自动开通 + 知识库目录隔离

> 相关代码：`/opt/obsidian-xiaolvshu/scripts/xiaolvshu_bridge.py`（开通逻辑在
> `ensure_user`）· `utils/local_kb.py` · `utils/semantic_kb.py` · `bot.py::_AIWithIma`
> · `bot_session.py::_kb_scope`
> 测试：`tests/test_kb_isolation.py`（8 个用例）

---

## 1. 两件事

1. **发贴图号按用户自动开通** —— 管理员只在 `.env` 里写「哪个用户、用哪个
   vault、哪个公众号」，桥在第一次用到该用户时自动把 vault 目录和凭据文件补齐，
   不用手工跑 `xiaolvshu-user init` / `set-creds`。
2. **知识库按用户目录隔离** —— 共享基座 `docs/knowledge/` 所有人可读；每个
   bot 账号在 `docs/kb-users/<账号 key>/` 下有只属于它的笔记和索引。

---

## 2. 自动开通：.env 开通表

`.env` 里每个「发消息的人」一组环境变量：

```ini
XIAOLVSHU_USER_<uid>_VAULT=/opt/clawbot-xiaolvshu/users/{slug}/vault
XIAOLVSHU_USER_<uid>_APP_ID=wx...
XIAOLVSHU_USER_<uid>_APP_SECRET=...
```

- `<uid>` = **from_user_id**（发消息的人）净化后的 key：非字母数字全换 `_`。
  例：`o9cq801uuudERF61XK5qTdTv5Yyg@im.wechat` →
  `o9cq801uuudERF61XK5qTdTv5Yyg_im_wechat`。
- `VAULT` 的值里可以用 `{slug}` / `{uid}` 占位符，桥在建上下文时替换。
- **占位符保护**：值里带 `REPLACE` / `PLACEHOLDER` / `TODO` / `CHANGEME` 的
  当作「还没填」→ 该字段跳过。所以 `.env` 里可以先把模板铺好，填了真值才生效。
  （否则用户会拿一个假 appid 去撞微信接口，报错比「还没配凭据」难懂得多。）

### 这些变量怎么进到桥里

`.env` → systemd `EnvironmentFile=` → 进程环境 → 桥子进程继承 `os.environ`。
**不需要额外管道** —— 桥直接读自己的环境变量。

> ⚠️ 注意区分两套 env：
> - **项目 `.env`** → 进程环境 → 桥子进程（`XIAOLVSHU_*` 走这条）
> - **`/etc/clawbot/{llm,ima}.env`** → 每会话 `runtime_env` → `_AIWithIma`
>   （`CLAWBOT_*` 走这条）
>
> 把 `CLAWBOT_USER_KB_DIR` 写进项目 `.env` 是**不生效**的。

### 桥里发生了什么（`ensure_user`，每个命令都会调）

1. 读 `.env` 里的开通配置（`XIAOLVSHU_USER_<key>_*`）；
2. `VAULT` 有值 → 以它为准（覆盖注册表里的旧值，改配置即时生效）；
3. `APP_ID` + `APP_SECRET` 都有值 → 写
   `/etc/clawbot/xiaolvshu/<slug>.env`（目录 700 / 文件 600，内容带
   `# uid:` 和 `# xiaolvshu-provisioned: 1` 标记）；内容一致就不重写（幂等）；
4. 撞车保护：凭据文件里记着原 uid，对不上就报 `provision_conflict` 而不是覆盖
   —— `a-b@x` 和 `a_b@x` 净化后是同一个 env key，宁可报错也不能串号。

### 凭据读取优先级（改过）

```
该用户自己的凭据文件（含自动开通落盘的）  ← 优先
  ↓ 没有
主人 → 项目 .env（/opt/obsidian-xiaolvshu/.env）  ← 只给主人
  ↓ 没有
{} → no_creds
```

- 主人的凭据**不回落**给其他用户（`read_creds` 的老规矩，防「人人能发到主人号上」）。
- 主人的个人文件里没有 `MINIMAX_API_KEY` 时，从项目 `.env` 补（那是模型服务的
  key，不是公众号凭据）。
- `stale_creds_copy` 不再把「自动开通的那份」当成多余副本。

### 命令行与 .env 的关系

`.env` 是声明式配置，优先级最高：

- `xiaolvshu-user init <uid>` → 先跑 `ensure_user`（.env 生效），命令行
  `--vault` / `--home` 只在 **.env 没配** 时才生效；
- `xiaolvshu-user set-creds <uid>` → 该用户若在 `.env` 里有开通配置，直接
  拒绝并提示「改 .env」（否则改了也会被 .env 覆盖回去）。

---

## 3. 知识库隔离：共享基座 + 每账号私有目录

```
docs/knowledge/                     ← 共享基座，所有人可读（150 篇）
docs/kb-users/<账号 key>/           ← 该账号私有笔记（只有它检索得到）
docs/kb-users/<账号 key>/.semantic_kb.sqlite3   ← 该账号的语义索引
```

- **隔离键 = `ilink_user_id`**（bot 账号，微信账号级稳定），不是
  `ilink_bot_id`（每次扫码轮换），也不是 `from_user_id`（聊天对端）。
  净化规则同 `.env` 开通表（非字母数字全换 `_`）。
- 目录不存在 → 当没有私有库，行为跟以前完全一样。
- `docs/kb-users/` 不进 git（`.gitignore`）。

### 索引层的两层

`LocalKBIndex` / `SemanticKBIndex` 都支持 `extra_dirs`：

- 基座文件的 key = 相对路径；私有文件的 key = `user/<n>/<相对路径>`
  （`<n>` 是 overlay 序号，多个 overlay 也不撞名）；
- 命中项的 `media_id` 带这个前缀（`local:user/0/xxx.md`），
  `url` 能反推回文件真实位置；
- **检索隔离靠目录本身**：每个账号的索引只扫「基座 + 自己那一个私有目录」，
  所以 A 的私有笔记根本不在 B 的索引里 —— 不靠事后过滤，没有过滤失效的风险。

### 语义索引的代价

每个账号的 SQLite 是**自己那份**（不共享），所以基座那 150 篇要在每个账号的
库里各 embed 一次：**该账号第一次触发语义兜底时约 11 秒**（含模型加载），
之后索引落盘持久化，重启不再重来。

- 想省这 11 秒就别开 `SEMANTIC_KB_ENABLED`（本地 BM25 兜底通常先命中）。
- 模型权重仍共享（`.cache/fastembed`），不重复占盘。

---

## 4. 怎么给一个用户开通发贴图号

```bash
# 1) 拿到该用户的 from_user_id（发消息的人，不是登录的 bot 账号）
#    净化：非字母数字全换 _   →  o9cq801uuudERF61XK5qTdTv5Yyg_im_wechat
# 2) 在 .env 里加三行（appid/secret 从公众号后台「设置与开发 → 基本配置」拿）
XIAOLVSHU_USER_<key>_VAULT=/opt/clawbot-xiaolvshu/users/{slug}/vault
XIAOLVSHU_USER_<key>_APP_ID=wx...
XIAOLVSHU_USER_<key>_APP_SECRET=...
# 3) 重载服务环境（改 .env 后要重启才生效）
sudo systemctl restart clawbot-shared.service
# 4) 对方在微信里发一次「/发列表」→ 桥自动建 vault + 写凭据
```

查开通状态：

```bash
cd /opt/obsidian-xiaolvshu && python3 scripts/xiaolvshu_user.py list
python3 scripts/xiaolvshu_user.py check      # 多公众号并存自检
```

---

## 5. 没做 / 已知限制

- **`.env` 开通表没法枚举**：变量名里是净化后的 key，反推不回原始 uid，
  所以「批量给所有配置的用户预建 vault」做不到 —— 现在是**懒开通**（对方第一次
  发指令时才建）。要预建就用 `xiaolvshu-user init <uid>`。
- **每个账号一个公众号** 是当前设计（草稿箱互不串）。共用一个号的方案没做。
- ~~**per-user IMA 云端知识库绑定在共享运行时里没接线**~~ ——
  **已于 2026-10-27 修复**（`fix/multi-account-isolation`）。原先
  `bot_session._ai_chat_kwargs()` 只传 `kb_scope` 从不传 `kb_id`，而
  `_AIWithIma.chat` 里 `kb_id = kwargs.pop("kb_id", None)`，`None` →
  回落到 `IMA_ILINK_DEFAULT_KB` —— **所有账号共用同一个云端知识库**，
  web UI 写的绑定是死的。现在 `_ai_chat_kwargs_async()` 按 `ilink_user_id`
  查 `ima_bindings.json` 把 `kb_id` 传下去；查不到就不传（仍走默认，行为不变）。
  `/bindkb` 命令**仍不在共享路径里** —— 绑定入口目前只有 web UI
  （`/clawbot/ima/bind`）和 `utils/ima_bindings.py` CLI。
- 桥的 README（`/opt/obsidian-xiaolvshu/README.md`）还没写这一节 —— 那个文件
  里有别的任务的在改内容，没敢一起提交。

详见 [`docs/2026-10-27_MULTI_ACCOUNT_AUDIT.md`](2026-10-27_MULTI_ACCOUNT_AUDIT.md)。

