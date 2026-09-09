"""OAuth openid ↔ bot_user 绑定存储 + 管理员待审批队列。

两个 store 均为有状态的 JSON 文件持久化 + ``threading.Lock`` 保护读写。
设计目标：

  * 单进程多线程使用安全（iLink / aiohttp 回调通常在事件循环，但
    未来若管理页面跑在独立线程也能直接复用）
  * 原子写：``tmp + os.replace``，避免并发写入半截文件
  * 不在 ``PendingStore.resolve`` 里做 ``bind``，调用方拿
    ``bot_user`` 后自行决定何时落到 ``BindingsStore``（解耦）

典型路径：

  * 微信扫码回调 → ``BindingsStore.resolve(openid)`` → 命中转发 / 未命中
    根据 ``first_bind_policy`` 进入 ``pending`` 或拒绝
  * 管理员审批通过 → ``PendingStore.resolve(openid, bot_user)`` →
    ``BindingsStore.bind(openid, bot_user)``
  * 管理员解绑 → ``BindingsStore.unbind(openid)``
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Optional

from utils.logging_setup import get_logger


_LOG = get_logger("oauth")


class BindingsStore:
    """openid → bot_user 映射，JSON 文件持久化，线程安全。

    :param bindings_path: JSON 文件路径，如 ``/etc/clawbot/oauth_bindings.json``。
                          路径不存在时自动初始化为空 dict。
    """

    def __init__(self, bindings_path: str) -> None:
        self._path = bindings_path
        self._lock = threading.Lock()
        # 路径不存在 → 视为空映射；首次写入时落盘
        if not os.path.exists(self._path):
            self._data: dict[str, str] = {}
            self._flush_locked(self._data)
        else:
            self._data = self._read_locked()

    # ---------- 内部 IO ----------

    def _read_locked(self) -> dict[str, str]:
        """读盘。损坏 / 非 dict 时按空映射处理并打 WARN。"""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            _LOG.warning(
                "oauth_bindings.json 读取失败，按空映射处理: path=%s err=%r",
                self._path,
                exc,
            )
            return {}
        if not isinstance(obj, dict):
            _LOG.warning(
                "oauth_bindings.json 顶层不是 dict，按空映射处理: path=%s type=%s",
                self._path,
                type(obj).__name__,
            )
            return {}
        # 过滤非 str → str 项，避免脏数据污染运行时
        return {str(k): str(v) for k, v in obj.items() if isinstance(k, str) and isinstance(v, str)}

    def _flush_locked(self, data: dict[str, str]) -> None:
        """落盘（atomic：tmp + os.replace）。调用方须持锁。"""
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".oauth_bindings.", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            # 临时文件残留不影响主流程；清理一下
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- 公开 API ----------

    def resolve(self, openid: str) -> Optional[str]:
        """openid → bot_user，未绑定返回 ``None``。"""
        with self._lock:
            return self._data.get(openid)

    def bind(self, openid: str, bot_user: str) -> None:
        """绑定并落盘；已存在则覆盖。"""
        with self._lock:
            if self._data.get(openid) == bot_user:
                return  # no-op，避免无谓写盘
            self._data[openid] = bot_user
            self._flush_locked(self._data)
            _LOG.info("绑定: openid=%s bot_user=%s", openid, bot_user)

    def unbind(self, openid: str) -> None:
        """删除绑定并落盘；不存在则 no-op。"""
        with self._lock:
            if openid not in self._data:
                return
            del self._data[openid]
            self._flush_locked(self._data)
            _LOG.info("解绑: openid=%s", openid)

    def list_all(self) -> dict[str, str]:
        """返回完整映射副本（用于管理页面）。"""
        with self._lock:
            return dict(self._data)


class PendingStore:
    """``first_bind_policy=pending_admin`` 时，首次扫码未绑定的 openid 暂存这里。

    每条记录形如 ``{openid: meta}``，其中 ``meta`` 是调用方传入的
    ``{'time': isoformat, 'ua': ..., 'ip': ...}`` 之类的 dict。
    """

    def __init__(self, pending_path: str) -> None:
        self._path = pending_path
        self._lock = threading.Lock()
        if not os.path.exists(self._path):
            self._data: dict[str, dict] = {}
            self._flush_locked(self._data)
        else:
            self._data = self._read_locked()

    # ---------- 内部 IO ----------

    def _read_locked(self) -> dict[str, dict]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            _LOG.warning(
                "oauth_pending.json 读取失败，按空映射处理: path=%s err=%r",
                self._path,
                exc,
            )
            return {}
        if not isinstance(obj, dict):
            _LOG.warning(
                "oauth_pending.json 顶层不是 dict，按空映射处理: path=%s type=%s",
                self._path,
                type(obj).__name__,
            )
            return {}
        # meta 必须是 dict；脏数据丢弃
        cleaned: dict[str, dict] = {}
        for k, v in obj.items():
            if isinstance(k, str) and isinstance(v, dict):
                cleaned[k] = dict(v)
        return cleaned

    def _flush_locked(self, data: dict[str, dict]) -> None:
        parent = os.path.dirname(self._path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".oauth_pending.", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------- 公开 API ----------

    def add(self, openid: str, meta: dict) -> None:
        """:param meta: ``{'time': isoformat, 'ua': ..., 'ip': ...}``。覆盖已有。"""
        if not isinstance(meta, dict):
            raise TypeError(f"meta 必须是 dict，收到 {type(meta).__name__}")
        with self._lock:
            self._data[openid] = dict(meta)
            self._flush_locked(self._data)
            _LOG.info("待审批入队: openid=%s", openid)

    def list(self) -> list[dict]:
        """返回 ``[{openid, meta}, ...]``。顺序按 ``openid`` 字典序。"""
        with self._lock:
            return [
                {"openid": k, "meta": dict(v)} for k, v in sorted(self._data.items())
            ]

    def remove(self, openid: str) -> None:
        """管理员审批（拒绝 / 撤回）后移除。"""
        with self._lock:
            if openid not in self._data:
                return
            del self._data[openid]
            self._flush_locked(self._data)
            _LOG.info("待审批移除: openid=%s", openid)

    def resolve(self, openid: str, bot_user: str) -> None:
        """审批通过：从 pending 删除 + 不在此处做 ``bind``。

        调用方拿到 ``bot_user`` 后自行调 ``BindingsStore.bind(openid, bot_user)``。
        这样解耦的好处：
          1. PendingStore 不知道 BindingsStore 的存在（单向依赖）
          2. 审批失败 / 用户撤回不需要回滚 BindingsStore
        """
        with self._lock:
            if openid not in self._data:
                _LOG.warning(
                    "resolve 时 openid 不在 pending 中: openid=%s", openid
                )
                return
            del self._data[openid]
            self._flush_locked(self._data)
            _LOG.info("待审批通过: openid=%s bot_user=%s", openid, bot_user)


# ---------- 自测 ----------


def _selftest() -> None:  # pragma: no cover
    """直接 ``python -m utils.oauth_bindings`` 跑一遍 happy path。"""
    import tempfile

    print("=== BindingsStore 自测 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bindings.json")
        store = BindingsStore(path)

        assert store.resolve("o1") is None
        print("初始 resolve(o1) =", store.resolve("o1"))

        store.bind("o1", "alice")
        store.bind("o2", "bob")
        print("bind 后 list_all =", store.list_all())
        assert store.resolve("o1") == "alice"
        assert store.resolve("o2") == "bob"

        store.bind("o1", "alice2")  # 覆盖
        print("覆盖后 resolve(o1) =", store.resolve("o1"))
        assert store.resolve("o1") == "alice2"

        store.unbind("o2")
        print("解绑后 list_all =", store.list_all())
        assert store.resolve("o2") is None
        assert store.resolve("o1") == "alice2"

        # 重启实例 → 数据持久化验证
        store2 = BindingsStore(path)
        print("重启实例 list_all =", store2.list_all())
        assert store2.list_all() == {"o1": "alice2"}

    print()
    print("=== PendingStore 自测 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pending.json")
        pend = PendingStore(path)

        pend.add("o3", {"time": "2026-09-09T00:00:00+00:00", "ua": "wx", "ip": "1.2.3.4"})
        pend.add("o4", {"time": "2026-09-09T00:01:00+00:00", "ua": "wx", "ip": "5.6.7.8"})
        print("add 后 list =", pend.list())
        assert len(pend.list()) == 2

        pend.remove("o4")  # 拒绝
        print("拒绝 o4 后 list =", pend.list())
        assert len(pend.list()) == 1
        assert pend.list()[0]["openid"] == "o3"

        pend.resolve("o3", "alice")  # 审批通过 → 仅删 pending，不在此 bind
        print("resolve 后 list =", pend.list())
        assert pend.list() == []

        # resolve 一个不存在的 openid → no-op，不抛
        pend.resolve("never-existed", "alice")
        print("resolve 未存在的 openid → no-op OK")

        # 持久化验证
        pend2 = PendingStore(path)
        print("重启实例 list =", pend2.list())
        assert pend2.list() == []

    print()
    print("ALL OK")


if __name__ == "__main__":  # pragma: no cover
    _selftest()