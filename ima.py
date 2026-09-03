"""ima (Tencent ima Knowledge Base) OpenAPI client.

Sync wrapper mirroring the shape of ``dusapi.py`` / ``deepseek.py``.
Ported from ``/opt/ilink_bot/internal/ima`` (Go → Python 3.12).

Reads ``IMA_ILINK_*`` config from ``.env`` (this module is the first env-var
consumer in the project). Missing credentials → silent empty results, never
raises into the bot loop.
"""

from __future__ import annotations

import json
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


def log(message: str, level: str = "INFO") -> None:
    """Project-style print logger. Mirrors ``dusapi.log`` / ``deepseek.log``."""
    print(f"[ima:{level}] {message}")


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

    @classmethod
    def from_env(cls, env_file: Optional[str] = ".env") -> "ImaConfig":
        """Build config from ``.env`` + ``os.environ``.

        ``.env`` is loaded with ``override=False`` so real environment wins,
        matching the Go project's "env > YAML > defaults" precedence.
        """
        if load_dotenv is not None and env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)

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

        return cls(
            base_url=_f("IMA_ILINK_BASE_URL", "https://ima.qq.com").rstrip("/"),
            client_id=_f("IMA_ILINK_CLIENT_ID", ""),
            api_key=_f("IMA_ILINK_API_KEY", ""),
            default_knowledge_base_id=_f("IMA_ILINK_DEFAULT_KB", ""),
            timeout=timeout,
            search_limit=search_limit,
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
    PATH_ADDABLE_KNOWLEDGE_BASE_LIST = (
        "openapi/wiki/v1/get_addable_knowledge_base_list"
    )
    PATH_IMPORT_DOC = "ima.openapi.v1.ImportDoc"

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
        """
        url = f"{self.cfg.base_url}/{endpoint.lstrip('/')}"
        last_exc: Optional[BaseException] = None
        attempts = (0,) + self._RETRY_DELAYS  # attempt 1 has no sleep
        for attempt_idx, delay in enumerate(attempts):
            if delay:
                time.sleep(delay)
            try:
                resp = self._session.post(
                    url,
                    data=json.dumps(payload, ensure_ascii=False),
                    timeout=self.cfg.timeout,
                )
            except Exception as exc:  # network / DNS / TLS / timeout
                last_exc = exc
                log(
                    f"POST {endpoint} attempt {attempt_idx + 1} failed: {exc}",
                    "WARN",
                )
                continue
            if resp.status_code // 100 != 2:
                truncated = (resp.text or "")[:512]
                log(
                    f"POST {endpoint} http {resp.status_code}: {truncated}",
                    "ERROR",
                )
                last_exc = ImaError(
                    f"ima {endpoint} returned http {resp.status_code}: {truncated}"
                )
                continue
            try:
                data = resp.json()
            except ValueError:
                # Tolerated: non-JSON 2xx → return empty
                return {}
            err = _envelope_error(data)
            if err:
                log(f"POST {endpoint} envelope error: {err}", "ERROR")
                last_exc = ImaError(err)
                continue
            return _unwrap_envelope(data)
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
                continue
            # API inconsistency: results may be in info_list OR list.
            items = data.get("info_list") or data.get("list") or []
            all_hits.extend(
                SearchHit.from_dict(x, fallback_kb=kb_id) for x in items
            )

        all_hits.sort(key=lambda h: h.score, reverse=True)
        return all_hits[:eff_limit]

    # ---- mutation methods: propagate ImaError -----------------------------

    def create_knowledge_base(
        self,
        name: str,
        description: str = "",
        type_: int = 0,
    ) -> tuple[str, str]:
        if not self.configured():
            raise ImaError("client_id / api_key 未配置")
        data = self._post(
            self.PATH_CREATE_KNOWLEDGE_BASE,
            {
                "name": name,
                "description": description,
                "type": type_ if type_ > 0 else 1002,  # default shared
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

    def import_doc(self, title: str, content: str, content_format: int = 0) -> str:
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
        return data.get("media_id", "")


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
        return ""
    return (
        "以下是检索到的参考资料，请在回答时优先基于这些信息：\n\n"
        + "\n".join(parts)
        + "\n"
    )