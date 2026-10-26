"""每账号私有知识库目录 —— 隔离契约。

共享基座（``docs/knowledge``）人人可读；每个 bot 账号
（``ilink_user_id`` 净化后的 key）在 ``CLAWBOT_USER_KB_DIR`` 下有自己的
目录，只有它检索得到。索引层（本地 BM25 + 语义）都必须按 scope 隔离，
**不能**让 A 账号的私有笔记出现在 B 账号的检索结果里。
"""

from __future__ import annotations

import pytest

from utils.local_kb import LocalKBIndex
from utils.semantic_kb import SemanticKBIndex


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def kb_tree(tmp_path):
    """一小块「基座 + 两个账号私有目录」。"""
    base = tmp_path / "base"
    _write(base / "公共.md", "# 公共\n这是公共知识库里的 openclaw 说明\n")
    scope_a = tmp_path / "kb-users" / "acctA"
    scope_b = tmp_path / "kb-users" / "acctB"
    _write(scope_a / "a.md", "# A私有\nalpha 关键词只属于 A\n")
    _write(scope_b / "b.md", "# B私有\nbeta 关键词只属于 B\n")
    return {"base": base, "a": scope_a, "b": scope_b}


def test_local_index_without_overlay_sees_only_base(kb_tree):
    idx = LocalKBIndex(kb_tree["base"])
    assert idx.file_count() == 1
    assert idx.search(["alpha"], limit=5) == []


def test_local_index_overlay_is_private_per_scope(kb_tree):
    """A 的索引里有 A 的私有笔记，但没有 B 的。"""
    idx_a = LocalKBIndex(kb_tree["base"], extra_dirs=[kb_tree["a"]])
    idx_b = LocalKBIndex(kb_tree["base"], extra_dirs=[kb_tree["b"]])

    hits_a = idx_a.search(["alpha"], limit=5)
    hits_b = idx_b.search(["beta"], limit=5)
    assert [h.title for h in hits_a] == ["A私有"]
    assert [h.title for h in hits_b] == ["B私有"]

    # 交叉检索必须为空 —— 这是隔离的核心断言
    assert idx_a.search(["beta"], limit=5) == []
    assert idx_b.search(["alpha"], limit=5) == []


def test_local_index_shared_base_visible_to_every_scope(kb_tree):
    """基座是公共的：两个账号都能搜到。"""
    idx_a = LocalKBIndex(kb_tree["base"], extra_dirs=[kb_tree["a"]])
    idx_b = LocalKBIndex(kb_tree["base"], extra_dirs=[kb_tree["b"]])
    assert idx_a.search(["openclaw"], limit=5)
    assert idx_b.search(["openclaw"], limit=5)


def test_local_index_media_id_marks_private_origin(kb_tree):
    """私有命中项的 media_id 带 user/<n>/ 前缀，基座的不带。"""
    idx = LocalKBIndex(kb_tree["base"], extra_dirs=[kb_tree["a"]])
    private = idx.search(["alpha"], limit=5)
    shared = idx.search(["openclaw"], limit=5)
    assert private[0].media_id.startswith("local:user/")
    assert not shared[0].media_id.startswith("local:user/")


def test_local_index_ignores_missing_overlay(tmp_path):
    """私有目录不存在 → 退化成只有基座，不能报错。"""
    base = tmp_path / "base"
    _write(base / "x.md", "# x\n内容\n")
    idx = LocalKBIndex(base, extra_dirs=[tmp_path / "nope"])
    assert idx.file_count() == 1
    assert idx.exists is True


def test_semantic_key_maps_back_to_absolute_path(kb_tree, tmp_path):
    """DB 里的 key 要能反推回文件在哪（overlay 的 user/<n>/ 前缀）。"""
    idx = SemanticKBIndex(
        kb_tree["base"],
        db_path=tmp_path / "a.sqlite3",
        extra_dirs=[kb_tree["a"]],
    )
    assert idx._path_for_key("公共.md") == str(kb_tree["base"] / "公共.md")
    assert idx._path_for_key("user/0/a.md") == str(kb_tree["a"] / "a.md")


def test_ai_wrapper_scopes_index_to_account_dir(tmp_path):
    """_AIWithIma 按 scope 选私有目录 + 私有 SQLite。"""
    from bot import _AIWithIma

    base = tmp_path / "base"
    _write(base / "x.md", "# x\n内容\n")
    scope_dir = tmp_path / "kb-users" / "acctA"
    _write(scope_dir / "a.md", "# A\nalpha\n")

    class _FakeBase:
        config = type("C", (), {"prompt": ""})()

        def chat(self, message, **kwargs):  # pragma: no cover - 不会走到
            return ""

    class _FakeIma:
        class cfg:  # noqa: N801
            search_limit = 5

        def configured(self):
            return False

    ai = _AIWithIma(
        _FakeBase(), _FakeIma(),
        environ={
            "CLAWBOT_LOCAL_FALLBACK": "1",
            "CLAWBOT_LOCAL_KB_DIR": str(base),
            "CLAWBOT_USER_KB_DIR": str(tmp_path / "kb-users"),
            "SEMANTIC_KB_ENABLED": "1",
        },
    )
    local = ai._maybe_local_kb("acctA")
    assert local is not None
    assert local.file_count() == 2, "基座 1 篇 + 私有 1 篇"

    semantic = ai._maybe_semantic_kb("acctA")
    assert semantic is not None
    assert str(semantic.db_path) == str(scope_dir / ".semantic_kb.sqlite3"), (
        "每个账号的语义索引必须落在自己的目录里，不能共享"
    )
    assert semantic.extra_dirs == [scope_dir]


def test_ai_wrapper_scope_change_rebuilds_index(tmp_path):
    """切换账号（scope 变了）后索引要跟着换，不能拿旧的继续用。"""
    from bot import _AIWithIma

    base = tmp_path / "base"
    _write(base / "x.md", "# x\n内容\n")
    for name in ("acctA", "acctB"):
        _write(tmp_path / "kb-users" / name / f"{name}.md", f"# {name}\n关键词 {name}\n")

    class _FakeBase:
        config = type("C", (), {"prompt": ""})()

        def chat(self, message, **kwargs):  # pragma: no cover
            return ""

    class _FakeIma:
        class cfg:  # noqa: N801
            search_limit = 5

        def configured(self):
            return False

    ai = _AIWithIma(
        _FakeBase(), _FakeIma(),
        environ={
            "CLAWBOT_LOCAL_FALLBACK": "1",
            "CLAWBOT_LOCAL_KB_DIR": str(base),
            "CLAWBOT_USER_KB_DIR": str(tmp_path / "kb-users"),
        },
    )
    idx_a = ai._maybe_local_kb("acctA")
    assert idx_a.search(["acctA"], limit=5)
    idx_b = ai._maybe_local_kb("acctB")
    assert idx_b is not idx_a
    assert idx_b.search(["acctB"], limit=5)
    assert idx_b.search(["acctA"], limit=5) == [], "换账号后不能还留着上一个账号的私有库"


def test_session_kb_scope_uses_account_not_bot_id(monkeypatch):
    """隔离键是 ilink_user_id（账号级稳定），不是 ilink_bot_id（每扫码轮换）。"""
    session_module = pytest.importorskip("bot_session")
    BotSession = getattr(session_module, "BotSession", None)
    if BotSession is None:  # pragma: no cover
        pytest.skip("BotSession 不可用")

    class _Stub:
        ilink_user_id = "o9cq80wkCIvkvrkAjjOqAhvYE_0o@im.wechat"
        ilink_bot_id = "2b41a84451cc@im.bot"

        def _kb_scope(self):
            return session_module.BotSession._kb_scope(self)

    assert _Stub()._kb_scope() == "o9cq80wkCIvkvrkAjjOqAhvYE_0o_im_wechat"
