"""ima KB ↔ iLink-user 绑定的进程内单例持久化。

session-token-only 部署里浏览器 cookie 是唯一句柄；cookie 过期 / 重启后
``session_id`` 就丢。如果绑定只存在 ``shared_web.py`` 内存里，下次扫码
还得重选 KB —— 所以落到跨 cookie / 跨重启 / 多 ``BotSession`` 并发安全
的 JSON 文件 + ``asyncio.Lock``。

``KBT_MINE_KB``（1001）排除：``ImaClient.search_knowledge`` 对个人 KB
返回 ``code=220004``（``docs/IMA_KB.md:126-139``）。仅支持
``KB_TYPE_SHARED=1002`` / ``KB_TYPE_SUBSCRIBED_CREATE_KB=1004``。

完整设计见 ``docs/IMA_PER_USER_BINDING.md``。
"""

from __future__ import annotations

# 允许 ``python utils/ima_bindings.py list`` 直接调用（与
# ``utils/seed_ima_kb.py`` 同模式）：脚本目录会在 sys.path[0]，
# 把项目根显式插入，让 ``utils.logging_setup`` 当成包导入。
import sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logging_setup import get_logger


# 与 ``ima.ImaClient`` 对齐；不 import ``ima`` 以免把整个 ima 依赖链
# 拉进 utils（CLI/测试会单独调本模块）。
KB_TYPE_SHARED = 1002                # KBT_SHARED_KB
KB_TYPE_SUBSCRIBED_CREATE = 1004     # KBT_SUBSCRIBED_CREATE_KB
ALLOWED_KB_TYPES = frozenset({KB_TYPE_SHARED, KB_TYPE_SUBSCRIBED_CREATE})

DEFAULT_STATE_DIR = Path(os.environ.get("CLAWBOT_STATE_DIR", "."))
SCHEMA_VERSION = 1
BINDINGS_FILENAME = "ima_bindings.json"

log = get_logger("ima_bindings")


# ---- Singleton ------------------------------------------------------------

_DEFAULT: Optional["IMABindings"] = None
_DEFAULT_LOCK = asyncio.Lock()


async def get_default_bindings() -> "IMABindings":
    """进程级单例（lazy create）；``shared_runtime`` 一进程多用户共用一份。"""
    global _DEFAULT
    if _DEFAULT is None:
        async with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = IMABindings()
    return _DEFAULT


# ---- Class ----------------------------------------------------------------


class IMABindings:
    """``ilink_user_id -> kb binding`` 持久化存储。

    所有读写都过 ``_WRITE_LOCK`` 序列化 —— 允许多 ``BotSession`` 并发
    ``bind``/``unbind``/``lookup`` 安全，未来构造多实例也不会冲突。
    """

    _WRITE_LOCK = asyncio.Lock()

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = DEFAULT_STATE_DIR / BINDINGS_FILENAME
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("create parent dir %s failed: %s", self.path.parent, exc)
        self._cache: dict[str, dict] = self._load()["bindings"]

    # ---- public ------------------------------------------------------------

    def lookup(self, ilink_user_id: str) -> Optional[dict]:
        """返回绑定 dict 或 ``None``（不抛）；返回值是 cache 的浅 copy。"""
        if not ilink_user_id:
            return None
        binding = self._cache.get(ilink_user_id)
        if binding is None:
            log.debug("lookup user=%s miss", ilink_user_id[-8:])
            return None
        log.debug("lookup user=%s hit kb=%s", ilink_user_id[-8:], binding.get("kb_id", ""))
        return dict(binding)

    def lookup_kb_id(self, ilink_user_id: str) -> Optional[str]:
        """``lookup`` 的便捷版本，只返 ``kb_id``。"""
        b = self.lookup(ilink_user_id)
        return str(b.get("kb_id") or "") or None if b else None

    async def bind(
        self,
        ilink_user_id: str,
        kb_id: str,
        kb_name: str,
        kb_type: int,
        *,
        bound_by: str = "",
        bot_id_at_bind: str = "",
    ) -> None:
        """upsert 绑定；写盘 + 更新 cache。``kb_type`` ∈ ``ALLOWED_KB_TYPES``
        才接受（``KBT_MINE_KB`` 排除的实现点）。"""
        if not ilink_user_id:
            raise ValueError("ilink_user_id 不能为空")
        if not kb_id:
            raise ValueError("kb_id 不能为空")
        if kb_type not in ALLOWED_KB_TYPES:
            raise ValueError(
                f"kb_type={kb_type} 不允许绑定；仅支持 "
                f"KB_TYPE_SHARED=1002 或 KB_TYPE_SUBSCRIBED_CREATE_KB=1004"
            )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing = self._cache.get(ilink_user_id)
        new_binding = {
            "kb_id": str(kb_id),
            "kb_name": str(kb_name or ""),
            "kb_type": int(kb_type),
            "bound_at": now,
            "bound_by": str(bound_by or ""),
            "bot_id_at_bind": str(bot_id_at_bind or ""),
        }
        async with self._WRITE_LOCK:
            data = self._load()  # 重读一次，避免跨实例 cache 不一致
            data["bindings"][ilink_user_id] = new_binding
            self._save_locked(data)
            self._cache = data["bindings"]
        action = "updated" if existing is not None else "created"
        log.info("binding %s user=%s kb_id=%s kb_name=%r kb_type=%d bound_by=%s",
                 action, ilink_user_id[-12:], kb_id, kb_name, kb_type, bound_by or "-")

    async def unbind(self, ilink_user_id: str) -> bool:
        """移除绑定。``True``=原本存在；``False``=原本没有。"""
        if not ilink_user_id or ilink_user_id not in self._cache:
            return False
        async with self._WRITE_LOCK:
            data = self._load()
            removed = data["bindings"].pop(ilink_user_id, None)
            if removed is None:
                return False
            self._save_locked(data)
            self._cache = data["bindings"]
        log.info("binding removed user=%s kb_id=%s",
                 ilink_user_id[-12:], removed.get("kb_id", ""))
        return True

    def list_bindings(self) -> dict[str, dict]:
        """全量快照（浅 copy）。"""
        return {k: dict(v) for k, v in self._cache.items()}

    # ---- internal ----------------------------------------------------------

    def _save_locked(self, data: dict) -> None:
        """原子写：``.tmp`` + fsync + chmod 600 + ``os.replace``。

        模式对齐 ``bot_session.py:227-250``。调用方必须已持 ``_WRITE_LOCK``。
        """
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.path)

    def _load(self) -> dict:
        """读 + 校验 schema。损坏 → warning + 返回空（不抛）。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": SCHEMA_VERSION, "bindings": {}}
        except OSError as exc:
            log.warning("read %s failed: %s；返回空 bindings", self.path, exc)
            return {"version": SCHEMA_VERSION, "bindings": {}}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("ima_bindings file %s corrupt (%s)；返回空", self.path, exc)
            return {"version": SCHEMA_VERSION, "bindings": {}}
        if not isinstance(data, dict):
            log.warning("ima_bindings root 不是 dict (%s)", type(data).__name__)
            return {"version": SCHEMA_VERSION, "bindings": {}}
        if data.get("version") != SCHEMA_VERSION:
            log.warning("ima_bindings version=%r 不匹配当前=%s",
                        data.get("version"), SCHEMA_VERSION)
        bindings = data.get("bindings")
        if not isinstance(bindings, dict):
            log.warning("ima_bindings['bindings'] 不是 dict；视为空")
            return {"version": SCHEMA_VERSION, "bindings": {}}
        # 过滤脏数据
        clean = {k: v for k, v in bindings.items()
                 if isinstance(k, str) and isinstance(v, dict)}
        return {"version": SCHEMA_VERSION, "bindings": clean}


# ---- CLI ------------------------------------------------------------------
# bind 操作走 web UI（CSRF + cookie），不暴露给 CLI。

_FIELDS = ("kb_id", "kb_name", "kb_type", "bound_at", "bound_by", "bot_id_at_bind")


def _print_table(rows: list[tuple[str, dict]]) -> None:
    if not rows:
        print("(空)")
        return
    for ilink_user_id, binding in rows:
        print(f"  ilink_user_id={ilink_user_id}")
        for key in _FIELDS:
            print(f"    {key:14s} = {binding.get(key, '')!r}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("用法:\n"
              "  python utils/ima_bindings.py list\n"
              "  python utils/ima_bindings.py lookup <ilink_user_id>\n"
              "  python utils/ima_bindings.py unbind <ilink_user_id>\n"
              "\nbind 操作不在 CLI 暴露（走 web UI，需 CSRF + cookie）。")
        return 0

    bindings = IMABindings()
    cmd = argv[0]

    if cmd == "list":
        rows = sorted(bindings.list_bindings().items())
        print(f"文件: {bindings.path}\n绑定数: {len(rows)}")
        _print_table(rows)
        return 0
    if cmd == "lookup":
        if len(argv) < 2:
            print("用法: lookup <ilink_user_id>", file=sys.stderr)
            return 2
        b = bindings.lookup(argv[1])
        if b is None:
            print(f"未找到 {argv[1]} 的绑定")
            return 1
        _print_table([(argv[1], b)])
        return 0
    if cmd == "unbind":
        if len(argv) < 2:
            print("用法: unbind <ilink_user_id>", file=sys.stderr)
            return 2
        removed = asyncio.run(bindings.unbind(argv[1]))
        print(f"{'已移除' if removed else '原本无绑定'} {argv[1]}")
        return 0 if removed else 1

    print(f"未知命令: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())