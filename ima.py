"""ima (Tencent ima Knowledge Base) OpenAPI client.

Sync wrapper mirroring the shape of ``dusapi.py`` / ``deepseek.py``.
Ported from ``/opt/ilink_bot/internal/ima`` (Go → Python 3.12).

Reads ``IMA_ILINK_*`` config from ``.env`` (this module is the first env-var
consumer in the project). Missing credentials → silent empty results, never
raises into the bot loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv missing
    load_dotenv = None  # type: ignore[assignment]

import requests


VERSION = "1.0.0"

# 复用项目统一 logger；handler 由 bot.py __main__ 入口注册
log_ima = logging.getLogger("clawbot.ima")


def log(message: str, level: str = "INFO") -> None:
    """兼容旧调用方的 print 风格 logger。

    注意：实际诊断日志走 ``logging.getLogger("clawbot.ima")``；本函数仅
    兜底（外部脚本可能仍依赖这个入口）。新代码应直接用 ``log_ima``。
    """
    getattr(log_ima, level.lower(), log_ima.info)(message)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ImaConfig:
    base_url: str = "https://ima.qq.com"
    client_id: str = ""
    api_key: str = ""
    default_knowledge_base_id: str = ""
    timeout: float = 15.0
    search_limit: int = 5
    rerank_enabled: bool = False  # IMA_ILINK_RERANK=1/true/yes/on 开启
    rerank_top_k: int = 3  # rerank 后保留的命中条数（1~search_limit）
    fetch_body: bool = False  # IMA_ILINK_FETCH_BODY=1 开启：search 后再调 get_doc_content 拿正文
    keyword_extract: bool = False  # IMA_ILINK_KEYWORD_EXTRACT=1 开启：search 前用 LLM 抽取 1-3 个关键词

    @classmethod
    def from_env(cls, env_files: Optional[Iterable[str]] = None) -> "ImaConfig":
        """Build config from a list of ``.env`` files + ``os.environ``.

        ``env_files`` are loaded with ``override=False`` in order — real env
        and earlier-loaded files win. Pass an explicit list to control
        per-user fallback (e.g. ``["/etc/clawbot/alice.env",
        "/etc/clawbot/ima.env"]``). ``None`` (default) skips file loading and
        reads only ``os.environ``; ``bot.py`` orchestrates file loading before
        calling this.
        """
        if load_dotenv is not None and env_files:
            for path in env_files:
                if path and Path(path).exists():
                    load_dotenv(path, override=False)

        def _f(name: str, default: str) -> str:
            v = os.environ.get(name)
            return v if v not in (None, "") else default

        timeout_raw = os.environ.get("IMA_ILINK_TIMEOUT", "")
        try:
            timeout = float(timeout_raw) if timeout_raw else 15.0
        except ValueError:
            timeout = 15.0

        limit_raw = os.environ.get("IMA_ILINK_SEARCH_LIMIT", "")
        try:
            search_limit = int(limit_raw) if limit_raw else 5
        except ValueError:
            search_limit = 5

        rerank_raw = os.environ.get("IMA_ILINK_RERANK", "")
        rerank_enabled = rerank_raw.strip().lower() in ("1", "true", "yes", "on")

        top_k_raw = os.environ.get("IMA_ILINK_RERANK_TOP_K", "")
        try:
            rerank_top_k = int(top_k_raw) if top_k_raw else 3
        except ValueError:
            rerank_top_k = 3
        # 至少 1，至少不超过 search_limit
        rerank_top_k = max(1, min(rerank_top_k, search_limit))

        fetch_body_raw = os.environ.get("IMA_ILINK_FETCH_BODY", "")
        fetch_body = fetch_body_raw.strip().lower() in ("1", "true", "yes", "on")

        keyword_extract_raw = os.environ.get("IMA_ILINK_KEYWORD_EXTRACT", "")
        keyword_extract = keyword_extract_raw.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            base_url=_f("IMA_ILINK_BASE_URL", "https://ima.qq.com").rstrip("/"),
            client_id=_f("IMA_ILINK_CLIENT_ID", ""),
            api_key=_f("IMA_ILINK_API_KEY", ""),
            default_knowledge_base_id=_f("IMA_ILINK_DEFAULT_KB", ""),
            timeout=timeout,
            search_limit=search_limit,
            rerank_enabled=rerank_enabled,
            rerank_top_k=rerank_top_k,
            fetch_body=fetch_body,
            keyword_extract=keyword_extract,
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeBaseSummary:
    id: str = ""
    name: str = ""
    role: str = ""
    base_type: str = ""
    cover: str = ""
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "KnowledgeBaseSummary":
        # ``search_knowledge_base`` 用 ``kb_id``/``kb_name``；
        # ``addable_knowledge_base_list`` 用 ``id``/``name`` —— 两路都接。
        return cls(
            id=d.get("kb_id") or d.get("id") or "",
            name=d.get("kb_name") or d.get("name") or "",
            role=d.get("role_type", ""),
            base_type=d.get("base_type", ""),
            cover=d.get("cover_url", ""),
            description=d.get("description", ""),
        )


@dataclass
class SearchHit:
    media_id: str = ""
    title: str = ""
    content: str = ""
    snippet: str = ""
    highlight_content: str = ""
    url: str = ""
    knowledge_base_id: str = ""
    score: float = 0.0

    @property
    def display_snippet(self) -> str:
        """Go mapping: ``Snippet || Content`` —— plus ``highlight_content``
        (actual field returned by ``search_knowledge``)."""
        return self.snippet or self.content or self.highlight_content

    @classmethod
    def from_dict(cls, d: dict, fallback_kb: str = "") -> "SearchHit":
        return cls(
            media_id=d.get("media_id", ""),
            title=d.get("title", ""),
            content=d.get("content", ""),
            snippet=d.get("snippet", ""),
            highlight_content=d.get("highlight_content", ""),
            url=d.get("url", ""),
            knowledge_base_id=d.get("knowledge_base_id") or fallback_kb,
            score=float(d.get("score", 0.0) or 0.0),
        )


@dataclass
class KnowledgeBaseDetail:
    id: str = ""
    name: str = ""
    cover_url: str = ""
    description: str = ""
    recommended_questions: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "KnowledgeBaseDetail":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            cover_url=d.get("cover_url", ""),
            description=d.get("description", ""),
            recommended_questions=list(d.get("recommended_questions") or []),
        )


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ImaError(RuntimeError):
    """Raised by mutation methods; read methods swallow into ``[]``."""


def _envelope_error(data: dict) -> Optional[str]:
    """Tencent ima error fields are inconsistent across endpoints; check all.

    Successful responses are wrapped: ``{"code": 0, "data": {...}}``; error
    responses keep the same shape with non-zero ``code`` (and ``msg`` /
    ``message`` / ``errmsg`` for the human-readable part).
    """
    code = data.get("code") or data.get("errcode") or data.get("ret") or 0
    if not code:
        return None
    msg = (
        data.get("msg")
        or data.get("message")
        or data.get("errmsg")
        or ""
    )
    return f"ima api error: code={code} message={msg}"


def _unwrap_envelope(data: dict) -> dict:
    """Tencent ima wraps successful payloads in ``{"code": 0, "data": {...}}``.

    Returns the inner dict when present so callers can read fields like
    ``info_list`` / ``is_end`` / ``next_cursor`` directly. When the inner
    ``data`` is missing or not a dict, returns the original payload unchanged.
    """
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


# ---------------------------------------------------------------------------
# KB ID parsing
# ---------------------------------------------------------------------------


_KB_SPLIT_RE = re.compile(r"[,，、]")


def parse_kb_ids(raw: str) -> list[str]:
    """Mirror ``model.ParseKnowledgeIDs`` — split, strip, dedupe, preserve order."""
    if not raw:
        return []
    seen: list[str] = []
    for part in _KB_SPLIT_RE.split(raw):
        p = part.strip()
        if p and p not in seen:
            seen.append(p)
    return seen


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ImaClient:
    """Sync HTTP client for Tencent ima knowledge base OpenAPI.

    Mirrors the public surface of ``internal/ima/client.go`` + ``knowledge.go``.
    Headers (per Go client.go L119-121):

        ima-openapi-clientid: <client_id>
        ima-openapi-apikey:   <api_key>
        Content-Type:         application/json
    """

    HEADER_CLIENT_ID = "ima-openapi-clientid"
    HEADER_API_KEY = "ima-openapi-apikey"

    PATH_SEARCH_KNOWLEDGE_BASE = "openapi/wiki/v1/search_knowledge_base"
    PATH_GET_KNOWLEDGE_BASE = "openapi/wiki/v1/get_knowledge_base"
    PATH_GET_KNOWLEDGE_LIST = "openapi/wiki/v1/get_knowledge_list"
    PATH_SEARCH_KNOWLEDGE = "openapi/wiki/v1/search_knowledge"
    PATH_CREATE_KNOWLEDGE_BASE = "openapi/wiki/v1/create_knowledge_base"
    PATH_CREATE_FOLDER = "openapi/wiki/v1/create_folder"
    PATH_ADD_KNOWLEDGE = "openapi/wiki/v1/add_knowledge"
    PATH_ADDABLE_KNOWLEDGE_BASE_LIST = (
        "openapi/wiki/v1/get_addable_knowledge_base_list"
    )
    # 注意：旧的 `ima.openapi.v1.ImportDoc` 是 gRPC 风格 full method name，
    # REST 实际路径在 note 服务前缀下。响应字段是 ``note_id``（不是 media_id）。
    PATH_IMPORT_DOC = "openapi/note/v1/import_doc"
    # 取 note 正文（search_knowledge 不返回 body；docs §5 的坑可由此端点绕过）
    PATH_GET_DOC_CONTENT = "openapi/note/v1/get_doc_content"

    # KB 类型枚举（接受字符串别名或整型值，两种形式都通过 live 验证）
    KB_TYPE_MINE = "KBT_MINE_KB"      # 1001：个人知识库（仅创建者可见/可写）
    KB_TYPE_SHARED = "KBT_SHARED_KB"  # 1002：共享知识库（团队协作）
    KB_TYPE_SUBSCRIBED = "KBT_SUBSCRIBED_CREATE_KB"  # 1004：订阅型（需开通知识号）
    KB_TYPE_INT_MINE = 1001
    KB_TYPE_INT_SHARED = 1002
    KB_TYPE_INT_SUBSCRIBED = 1004

    # 5 retries, sleeps [2,4,8,16,32]s — same ladder as deepseek.py / dusapi.py.
    _RETRY_DELAYS = (2, 4, 8, 16, 32)

    def __init__(self, config: ImaConfig):
        self.cfg = config
        self._session = requests.Session()
        self._session.headers.update(
            {
                self.HEADER_CLIENT_ID: config.client_id,
                self.HEADER_API_KEY: config.api_key,
                "Content-Type": "application/json",
            }
        )

    # ---- public helpers ---------------------------------------------------

    def configured(self) -> bool:
        """True iff client_id AND api_key are both non-empty (matches Go)."""
        return bool(self.cfg.client_id and self.cfg.api_key)

    def _post(self, endpoint: str, payload: dict) -> dict:
        """POST JSON to ``base_url/endpoint`` with the standard 5-retry ladder.

        Returns parsed JSON dict (empty dict on non-JSON 2xx — tolerated).
        Raises :class:`ImaError` after exhausting retries.

        注意：body 显式以 UTF-8 字节串发送，并带 ``charset=utf-8``。
        之前用 ``data=json.dumps(..., ensure_ascii=False)``（Unicode str）
        在 content 含 ``？``/``：`` 等全角标点时，ima 服务端 Go decoder
        会返回 ``service codec Unmarshal: unexpected EOF``（实测确认）。
        显式 UTF-8 字节 + charset 头可绕开这个隐性兼容性问题。
        """
        url = f"{self.cfg.base_url}/{endpoint.lstrip('/')}"
        # 显式 UTF-8 字节；ensure_ascii=False 保留原文便于服务端检索
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_exc: Optional[BaseException] = None
        attempts = (0,) + self._RETRY_DELAYS  # attempt 1 has no sleep
        total_t0 = time.perf_counter()
        # 显式 charset，避免某些网关在 str body 时猜错编码
        headers = {"Content-Type": "application/json; charset=utf-8"}
        for attempt_idx, delay in enumerate(attempts):
            if delay:
                time.sleep(delay)
            t0 = time.perf_counter()
            log_ima.debug("ima POST %s attempt=%d", endpoint, attempt_idx + 1)
            try:
                resp = self._session.post(
                    url,
                    data=body_bytes,
                    headers=headers,
                    timeout=self.cfg.timeout,
                )
            except Exception as exc:  # network / DNS / TLS / timeout
                elapsed_ms = (time.perf_counter() - t0) * 1000
                last_exc = exc
                log(
                    f"POST {endpoint} attempt {attempt_idx + 1} failed: {exc}",
                    "WARN",
                )
                log_ima.warning("ima POST %s attempt=%d network err=%s elapsed_ms=%.0f",
                                endpoint, attempt_idx + 1, exc, elapsed_ms)
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code // 100 != 2:
                truncated = (resp.text or "")[:512]
                log(
                    f"POST {endpoint} http {resp.status_code}: {truncated}",
                    "ERROR",
                )
                log_ima.error("ima POST %s attempt=%d http=%d body=%s",
                              endpoint, attempt_idx + 1, resp.status_code, truncated)
                last_exc = ImaError(
                    f"ima {endpoint} returned http {resp.status_code}: {truncated}"
                )
                continue
            try:
                data = resp.json()
            except ValueError:
                # Tolerated: non-JSON 2xx → return empty
                log_ima.debug("ima POST %s attempt=%d ok elapsed_ms=%.0f (non-JSON 2xx)",
                              endpoint, attempt_idx + 1, elapsed_ms)
                return {}
            err = _envelope_error(data)
            if err:
                log(f"POST {endpoint} envelope error: {err}", "ERROR")
                log_ima.error("ima POST %s attempt=%d envelope err=%s",
                              endpoint, attempt_idx + 1, err)
                last_exc = ImaError(err)
                continue
            log_ima.debug("ima POST %s attempt=%d ok elapsed_ms=%.0f",
                          endpoint, attempt_idx + 1, elapsed_ms)
            return _unwrap_envelope(data)
        total_ms = (time.perf_counter() - total_t0) * 1000
        log_ima.error("ima POST %s exhausted after %d attempts total_elapsed_ms=%.0f",
                      endpoint, len(attempts), total_ms)
        raise last_exc or ImaError(f"ima {endpoint} failed without exception")

    # ---- read methods: return [] on error (silent degrade) ----------------

    def search_knowledge_base(
        self,
        query: str = "",
        limit: Optional[int] = None,
        cursor: str = "",
        query_user: bool = False,
    ) -> list[KnowledgeBaseSummary]:
        if not self.configured():
            log("client_id / api_key 未配置，跳过 search_knowledge_base", "WARN")
            return []
        try:
            data = self._post(
                self.PATH_SEARCH_KNOWLEDGE_BASE,
                {
                    "query": query,
                    "query_user": query_user,
                    "cursor": cursor,
                    "limit": limit if limit and limit > 0 else 20,
                },
            )
        except ImaError as exc:
            log(f"search_knowledge_base 失败: {exc}", "WARN")
            return []
        return [KnowledgeBaseSummary.from_dict(x) for x in (data.get("info_list") or [])]

    def get_knowledge_base(
        self, ids: Iterable[str]
    ) -> dict[str, KnowledgeBaseDetail]:
        id_list = [i for i in ids if i]
        if not id_list:
            return {}
        if not self.configured():
            log("client_id / api_key 未配置，跳过 get_knowledge_base", "WARN")
            return {}
        try:
            data = self._post(self.PATH_GET_KNOWLEDGE_BASE, {"ids": id_list})
        except ImaError as exc:
            log(f"get_knowledge_base 失败: {exc}", "WARN")
            return {}
        infos = data.get("infos") or {}
        return {k: KnowledgeBaseDetail.from_dict(v) for k, v in infos.items()}

    def get_knowledge_list(
        self,
        knowledge_base_id: str,
        folder_id: str = "",
        limit: Optional[int] = None,
        cursor: str = "",
    ) -> list[dict]:
        if not self.configured():
            return []
        try:
            data = self._post(
                self.PATH_GET_KNOWLEDGE_LIST,
                {
                    "knowledge_base_id": knowledge_base_id,
                    "folder_id": folder_id,
                    "cursor": cursor,
                    "limit": limit if limit and limit > 0 else 20,
                },
            )
        except ImaError as exc:
            log(f"get_knowledge_list 失败: {exc}", "WARN")
            return []
        # 该端点返回的列表字段名是 ``knowledge_list``，不是 info_list
        return list(data.get("knowledge_list") or [])

    def addable_knowledge_bases(
        self, cursor: str = "", limit: int = 0
    ) -> list[KnowledgeBaseSummary]:
        if not self.configured():
            return []
        try:
            data = self._post(
                self.PATH_ADDABLE_KNOWLEDGE_BASE_LIST,
                {"cursor": cursor, "limit": limit if limit > 0 else 20},
            )
        except ImaError as exc:
            log(f"addable_knowledge_bases 失败: {exc}", "WARN")
            return []
        # 该端点返回列表字段名是 ``addable_knowledge_base_list``，不是 info_list
        items = data.get("addable_knowledge_base_list") or []
        return [KnowledgeBaseSummary.from_dict(x) for x in items]

    def search_knowledge(
        self,
        query: str,
        knowledge_base_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: str = "",
    ) -> list[SearchHit]:
        """Search across one or more knowledge bases.

        Mirrors Go ``engine.retrieveMulti``: fan-out per KB, sort by score desc,
        truncate to ``limit``. Comma-separated ``knowledge_base_id`` is split
        just like the Go project's ``ParseKnowledgeIDs``.
        """
        if not self.configured():
            log("client_id / api_key 未配置，跳过 search_knowledge", "WARN")
            return []
        if not query.strip():
            return []
        kb_ids = parse_kb_ids(
            knowledge_base_id if knowledge_base_id is not None else self.cfg.default_knowledge_base_id
        )
        if not kb_ids:
            log("search_knowledge: 未指定 knowledge_base_id，跳过检索", "WARN")
            return []

        eff_limit = limit if limit and limit > 0 else self.cfg.search_limit
        log_ima.info("ima search q=%r kbs=%d limit=%d",
                     (query or "")[:60], len(kb_ids), eff_limit)
        search_t0 = time.perf_counter()
        all_hits: list[SearchHit] = []
        for kb_id in kb_ids:
            try:
                data = self._post(
                    self.PATH_SEARCH_KNOWLEDGE,
                    {
                        "query": query,
                        "knowledge_base_id": kb_id,
                        "cursor": cursor,
                        "limit": eff_limit,
                    },
                )
            except ImaError as exc:
                log(f"search_knowledge[{kb_id}] 失败: {exc}", "WARN")
                log_ima.warning("ima search kb=%s failed err=%s", kb_id, exc)
                continue
            # API inconsistency: results may be in info_list OR list.
            items = data.get("info_list") or data.get("list") or []
            kb_hits = list(SearchHit.from_dict(x, fallback_kb=kb_id) for x in items)
            log_ima.debug("ima search kb=%s hits=%d", kb_id, len(kb_hits))
            all_hits.extend(kb_hits)

        all_hits.sort(key=lambda h: h.score, reverse=True)
        final_hits = all_hits[:eff_limit]
        total_elapsed_ms = (time.perf_counter() - search_t0) * 1000
        log_ima.info("ima search done hits=%d (truncated to %d) total_elapsed_ms=%.0f",
                     len(all_hits), len(final_hits), total_elapsed_ms)
        return final_hits

    # ---- mutation methods: propagate ImaError -----------------------------

    def create_knowledge_base(
        self,
        name: str,
        description: str = "",
        type_: object = 0,
    ) -> tuple[str, str]:
        """创建一个知识库。

        ``type_`` 接受：
          - ``ImaClient.KB_TYPE_MINE`` / ``KB_TYPE_SHARED`` / ``KB_TYPE_SUBSCRIBED``（字符串）
          - 整型 1001 / 1002 / 1004（与字符串别名等价）
          - ``0`` 或省略 → 默认 ``KB_TYPE_SHARED``（1002），跟旧实现保持兼容
          - 单独的 ``1`` / ``2`` 这种短整型已被服务端拒绝（实测 code=51）

        Returns ``(id, name)``。
        """
        if not self.configured():
            raise ImaError("client_id / api_key 未配置")
        # 默认：共享知识库（与旧实现一致，避免破坏现有调用）
        if type_ in (0, "", None):
            resolved_type = self.KB_TYPE_SHARED
        else:
            resolved_type = type_
        data = self._post(
            self.PATH_CREATE_KNOWLEDGE_BASE,
            {
                "name": name,
                "description": description,
                "type": resolved_type,
            },
        )
        return data.get("id", ""), data.get("name", "")

    def create_folder(
        self,
        name: str,
        knowledge_base_id: str,
        folder_id: str = "",
        kb_name: str = "",
    ) -> str:
        if not self.configured():
            raise ImaError("client_id / api_key 未配置")
        if not name:
            raise ImaError("create_folder: name 不能为空")
        data = self._post(
            self.PATH_CREATE_FOLDER,
            {
                "knowledge_base_id": knowledge_base_id,
                "kb_name": kb_name,
                "name": name,
                "folder_id": folder_id,
            },
        )
        return data.get("media_id", "")

    def import_doc(self, title: str, content: str, content_format: int = 1) -> str:
        """在 note 服务下创建一条 Markdown 笔记。

        Returns the new ``note_id``. 注意：响应字段是 ``note_id``，
        不是 ``media_id``（旧实现里的字段名错位）。

        要把这条笔记接入 ``search_knowledge`` 检索范围，需再调用
        :meth:`add_knowledge` 并带 ``media_type=11`` + ``note_info.content_id``。
        """
        if not self.configured():
            raise ImaError("client_id / api_key 未配置")
        if not title:
            raise ImaError("import_doc: title 不能为空")
        data = self._post(
            self.PATH_IMPORT_DOC,
            {
                "title": title,
                "content": content,
                "content_format": content_format if content_format > 0 else 1,
            },
        )
        return data.get("note_id") or data.get("media_id") or ""

    def add_knowledge(
        self,
        knowledge_base_id: str,
        title: str,
        *,
        note_id: str = "",
        media_id: str = "",
        media_type: int = 11,
        folder_id: str = "",
    ) -> str:
        """把 note / 文件挂载到指定知识库，使其能被 ``search_knowledge`` 搜到。

        两种来源：
          - note 笔记：``media_type=11``（Note），传 ``note_id``，落到 ``note_info.content_id``
          - 文件：``media_id`` 来自 :meth:`create_media`（COS 上传凭证），``media_type`` 按真实类型

        Returns ``media_id`` from response (use this to verify linkage).
        """
        if not self.configured():
            raise ImaError("client_id / api_key 未配置")
        if not knowledge_base_id:
            raise ImaError("add_knowledge: knowledge_base_id 不能为空")
        if not title:
            raise ImaError("add_knowledge: title 不能为空")
        payload: dict = {
            "knowledge_base_id": knowledge_base_id,
            "media_type": media_type,
            "title": title,
            "folder_id": folder_id,
        }
        if note_id:
            payload["note_info"] = {"content_id": note_id}
        if media_id:
            payload["media_id"] = media_id
        data = self._post(self.PATH_ADD_KNOWLEDGE, payload)
        return data.get("media_id") or data.get("id") or ""

    def get_doc_content(self, note_id: str) -> str:
        """拿一条 note 的正文 Markdown。

        这是 docs/IMA_KB.md §5 关键坑 2 的补丁：``search_knowledge`` 不返回正文
        （``content``/``snippet`` 字段都空，``highlight_content`` 对 ``media_type=11``
        也常空）。要拿到完整文档内容，必须再调一次本端点。

        ⚠️ 实测权限语义：只有"作者本人"能拿到正文（用 ``doc_id`` 替代 ``note_id``
        会得到 ``code=210005 GetNoteContent not author``），所以本端点只对
        ``import_doc`` 出来的、当前凭据创建者拥有的 note 有效。

        Returns raw content string (Markdown；换行已被服务端规范化)。
        Returns ``""`` on any failure（与 read 类方法一致，软降级）。
        """
        if not self.configured():
            return ""
        if not note_id:
            return ""
        try:
            data = self._post(self.PATH_GET_DOC_CONTENT, {"note_id": note_id})
        except ImaError as exc:
            log(f"get_doc_content[{note_id}] 失败: {exc}", "WARN")
            return ""
        return data.get("content") or data.get("markdown") or ""


# ---------------------------------------------------------------------------
# High-level helper: prompt augmentation
# ---------------------------------------------------------------------------


def build_context_prompt(
    hits: Iterable[SearchHit], max_chars: int = 4000
) -> str:
    """Format retrieved quotes into a ``参考资料`` block for system prompt.

    Mirrors ``brain/retriever.go`` ``BuildContext``: numbered blocks of
    ``### 参考资料 N：<title>\\n<snippet>\\n``, truncated to ``max_chars``.
    Returns ``""`` if ``hits`` is empty so callers can short-circuit.
    """
    parts: list[str] = []
    total = 0
    for i, h in enumerate(hits, 1):
        snippet = (h.display_snippet or "").strip()
        if not snippet:
            continue
        block = f"### 参考资料 {i}：{h.title}\n{snippet}\n"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    if not parts:
        log_ima.debug("ima build_context hits=%d prompt_chars=0 (empty)", len(list(hits) if not isinstance(hits, list) else hits))
        return ""
    result = (
        "以下是检索到的参考资料，请在回答时优先基于这些信息：\n\n"
        + "\n".join(parts)
        + "\n"
    )
    log_ima.debug("ima build_context hits=%d prompt_chars=%d",
                  len(parts), len(result))
    return result


# ---------------------------------------------------------------------------
# High-level helper: optional LLM rerank
# ---------------------------------------------------------------------------


_RERANK_PROMPT_HEADER = (
    "你是一个相关性排序助手。用户问题：\n{query}\n\n"
    "以下候选文档片段按与问题的相关度从高到低排序。"
    "请按顺序输出候选编号，每行一个编号，只输出编号，不要任何解释。\n\n"
    "候选：\n"
)


def _format_rerank_candidate(idx: int, hit: "SearchHit") -> str:
    """One candidate block: ``[N] <title>\\n<snippet>`` (snippet truncated)."""
    snippet = (hit.display_snippet or "").strip().replace("\n", " ")
    if len(snippet) > 240:
        snippet = snippet[:240] + "…"
    return f"[{idx}] {hit.title}\n{snippet}\n"


def _parse_rerank_response(
    resp: str, hits: list["SearchHit"], top_k: int
) -> list["SearchHit"]:
    """Parse ``"3\\n1\\n2\\n"`` style response into reordered hits.

    Dedupes, drops out-of-range / non-integer tokens, preserves LLM-given
    order. If fewer than ``top_k`` valid IDs come back, pads with the
    remaining hits in their original order so the caller always gets
    ``len(hits)`` items back (rerank filters nothing, only reorders).
    """
    if not resp:
        return hits
    int_re = re.compile(r"-?\d+")
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for line in resp.splitlines():
        m = int_re.search(line)
        if not m:
            continue
        try:
            n = int(m.group(0))
        except ValueError:
            continue
        # 1-based → 0-based；范围 [0, len(hits))
        if 1 <= n <= len(hits) and n not in seen:
            seen.add(n)
            ordered_ids.append(n - 1)
            if len(ordered_ids) >= top_k:
                break
    if not ordered_ids:
        return hits
    used_0idx = {i for i in ordered_ids}
    reranked = [hits[i] for i in ordered_ids]
    # 不足 top_k 时用原序剩余的补齐（rerank 不裁剪，只重排）
    for i, h in enumerate(hits):
        if i in used_0idx:
            continue
        reranked.append(h)
        if len(reranked) >= len(hits):
            break
    return reranked


def rerank_hits(
    query: str,
    hits: list["SearchHit"],
    chat_fn,
    top_k: int = 3,
) -> list["SearchHit"]:
    """Optional LLM-based rerank over ``hits`` (same LLM as the chat one).

    Contract:
      - ``len(hits) <= 1`` → return ``hits`` unchanged (no-op, no log).
      - Any failure path (LLM error, parse error, empty/garbled response)
        returns ``hits`` in original order. Never raises.
      - Result keeps ``len(hits)`` items: rerank only reorders, never drops.
    """
    if not query or len(hits) <= 1:
        return list(hits)
    # top_k 是解析阶段的目标保留条数；rerank 本身不裁剪（contract 保持 len(hits)），
    # 所以这里不短路 top_k == len(hits) 的情形——那仍然有意义（重排）

    candidates = "".join(
        _format_rerank_candidate(i + 1, h) for i, h in enumerate(hits)
    )
    prompt = _RERANK_PROMPT_HEADER.format(query=query.strip()[:500]) + candidates
    log_ima.debug("ima rerank prompt_chars=%d hits=%d", len(prompt), len(hits))

    try:
        raw = chat_fn(message="", prompt=prompt) or ""
    except Exception as exc:  # LLM 网络/超时/鉴权等，绝不能让回复失败
        log_ima.warning("ima rerank chat_fn failed err=%s (fallback to original)", exc)
        return list(hits)

    log_ima.debug("ima rerank raw_response=%r", raw[:200])
    # 解析阶段用 top_k 控制截断；这里用 max(1, ...) 防 top_k<=0 退化
    eff_top_k = max(1, top_k)
    reranked = _parse_rerank_response(raw, list(hits), eff_top_k)
    return reranked