# 每账号私有知识库

`CLAWBOT_USER_KB_DIR=docs/kb-users` 指向这里。每个 **bot 账号**
（`ilink_user_id` 净化成 `[A-Za-z0-9_]` 后的 key）一个子目录：

```
docs/kb-users/<ilink_user_id 净化值>/
├── *.md              ← 该账号私有的笔记（只有它检索得到）
└── .semantic_kb.sqlite3   ← 该账号的语义索引（不与别人共享）
```

检索时的顺序是「共享基座 + 该账号的私有目录」两层：

- 共享基座 `docs/knowledge/` —— 所有人可读（项目的公共 Q&A）；
- 私有目录 —— 只有这个账号看得到，命中项的 media_id 带 `user/0/` 前缀。

目录不存在就当没有私有库，行为跟以前完全一样。

> 目录里的内容不进 git（见 `.gitignore`）。
