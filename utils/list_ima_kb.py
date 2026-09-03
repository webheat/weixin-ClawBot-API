#!/usr/bin/env python3
"""列出腾讯 ima OpenAPI 账号下所有知识库的 ID。

本脚本自洽（不依赖业务工程里的 ``ima.py``），可同时放在两个仓库共用：

- ``/opt/ilink_bot/utils/list_ima_kb.py``（Go 后端，从 ``.env`` 读 ``IMA_ILINK_*``）
- ``/opt/weixin-ClawBot-API/utils/list_ima_kb.py``（Python bot，同样从 ``.env`` 读）

凭据来源：登录 https://ima.qq.com/agent-interface 创建智能体 → 拿
``Client ID`` + ``API Key``，写入 ``.env`` 的 ``IMA_ILINK_CLIENT_ID`` /
``IMA_ILINK_API_KEY`` / ``IMA_ILINK_BASE_URL``。

设计要点：
- 同时拉 ``search_knowledge_base``（空 query 列出全部）与
  ``addable_knowledge_bases`` 两路，按 ``kb_id`` 去重，覆盖率更高；
- cursor / ``is_end`` 自动翻页；
- ``--pick`` 子串匹配名字，命中唯一一个时直接打印可粘到 ``.env`` 的那一行；
- ``--update`` 原地重写 ``.env`` 的 ``IMA_ILINK_DEFAULT_KB``；
- ``--json`` 给后续脚本用；
- ``requests`` 是唯一外部依赖；``python-dotenv`` 若可用则用，否则手写 ``.env`` 解析。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests

try:
    from dotenv import dotenv_values  # type: ignore
except Exception:  # pragma: no cover - python-dotenv 缺失时手写 fallback
    dotenv_values = None  # type: ignore


VERSION = "1.0.0"

PATH_SEARCH_KNOWLEDGE_BASE = "openapi/wiki/v1/search_knowledge_base"
PATH_ADDABLE_KNOWLEDGE_BASE_LIST = (
    "openapi/wiki/v1/get_addable_knowledge_base_list"
)
HEADER_CLIENT_ID = "ima-openapi-clientid"
HEADER_API_KEY = "ima-openapi-apikey"

PAGE_LIMIT = 20  # ima 端硬上限 (0, 20]
RETRY_DELAYS = (2, 4, 8, 16, 32)
REQUEST_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# .env 解析
# ---------------------------------------------------------------------------


def _parse_env_file(path: Path) -> dict[str, str]:
    """手写 ``.env`` 解析：KEY=VALUE，# 开头注释，值可双引号。

    与 ``python-dotenv`` 的差别在于：不支持变量展开、不处理转义，
    对当前项目够用；存在则优先用 dotenv_values，结果等价。
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    line_re = re.compile(
        r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"""
    )
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = line_re.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val.startswith(("'", '"')) and val.endswith(val[0]) and len(val) >= 2:
            val = val[1:-1]
        out[key] = val
    return out


def load_env(env_file: Path) -> dict[str, str]:
    """合并 ``.env`` 与 ``os.environ``（前者优先），便于覆盖已有 shell 变量。"""
    if dotenv_values is not None:
        file_values = {k: v for k, v in (dotenv_values(env_file) or {}).items() if v}
    else:
        file_values = _parse_env_file(env_file)
    merged: dict[str, str] = {}
    merged.update(file_values)
    for k, v in os.environ.items():
        if v:
            merged.setdefault(k, v)
    return merged


# ---------------------------------------------------------------------------
# IMA 客户端
# ---------------------------------------------------------------------------


@dataclass
class ImaConfig:
    base_url: str
    client_id: str
    api_key: str
    default_kb: str


class ImaError(RuntimeError):
    pass


class ImaClient:
    """最小可用 ima OpenAPI 客户端，只覆盖「列知识库」两个接口。"""

    def __init__(self, cfg: ImaConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                HEADER_CLIENT_ID: cfg.client_id,
                HEADER_API_KEY: cfg.api_key,
                "Content-Type": "application/json",
            }
        )

    def configured(self) -> bool:
        return bool(self.cfg.client_id and self.cfg.api_key)

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.cfg.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        last_exc: Optional[BaseException] = None
        for attempt_idx, delay in enumerate((0,) + RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                resp = self._session.post(
                    url,
                    data=json.dumps(payload, ensure_ascii=False),
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_exc = exc
                print(
                    f"[list_ima_kb:WARN] POST {endpoint} attempt "
                    f"{attempt_idx + 1} failed: {exc}",
                    file=sys.stderr,
                )
                continue
            if resp.status_code // 100 != 2:
                truncated = (resp.text or "")[:512]
                last_exc = ImaError(
                    f"ima {endpoint} http {resp.status_code}: {truncated}"
                )
                print(
                    f"[list_ima_kb:ERROR] POST {endpoint} http "
                    f"{resp.status_code}: {truncated}",
                    file=sys.stderr,
                )
                continue
            try:
                data = resp.json()
            except ValueError:
                return {}
            # ima 把成功数据塞在 data.* 下；顶层 code 用于判定错误码。
            outer_code = data.get("code")
            if outer_code:
                msg = (
                    data.get("msg")
                    or data.get("message")
                    or data.get("errmsg")
                    or ""
                )
                raise ImaError(
                    f"ima {endpoint} 返回 code={outer_code} msg={msg}"
                )
            # 成功：把 data 包裹摊平，info_list / is_end / next_cursor 直接可读
            inner = data.get("data")
            if isinstance(inner, dict):
                return inner
            return data
        raise last_exc or ImaError(f"ima {endpoint} failed without exception")

    def _paginate(self, endpoint: str, payload_base: dict, list_key: str) -> list[dict]:
        """cursor + is_end 翻页。"""
        out: list[dict] = []
        cursor = ""
        page = 0
        while True:
            page += 1
            payload = dict(payload_base)
            payload["limit"] = PAGE_LIMIT
            if cursor:
                payload["cursor"] = cursor
            data = self._post(endpoint, payload)
            out.extend(data.get(list_key) or [])
            if data.get("is_end", True):
                break
            cursor = data.get("next_cursor", "") or ""
            if not cursor:
                break
        return out

    # ---- 列知识库（search_knowledge_base + addable_knowledge_base_list 合并去重） ----

    def list_all_knowledge_bases(self) -> list[dict]:
        results: dict[str, dict] = {}

        def _merge(items: Iterable[dict]) -> None:
            for item in items:
                kb_id = item.get("kb_id") or item.get("id") or ""
                if not kb_id:
                    continue
                if kb_id not in results:
                    results[kb_id] = dict(item)
                else:
                    # 后到的补字段（addable 通常只带 id/name）
                    for k, v in item.items():
                        if v and not results[kb_id].get(k):
                            results[kb_id][k] = v

        _merge(
            self._paginate(
                PATH_SEARCH_KNOWLEDGE_BASE,
                {"query": ""},
                list_key="info_list",
            )
        )
        _merge(
            self._paginate(
                PATH_ADDABLE_KNOWLEDGE_BASE_LIST,
                {},
                list_key="addable_knowledge_base_list",
            )
        )

        return sorted(
            results.values(),
            key=lambda x: (x.get("kb_name") or x.get("name") or ""),
        )


# ---------------------------------------------------------------------------
# .env 写回
# ---------------------------------------------------------------------------


def _format_env_value(value: str) -> str:
    """如果值含 ``#``、空格、换行、双引号则加双引号转义。"""
    if not value or re.search(r'[\s"#]', value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_env_default_kb(env_file: Path, new_kb_id: str) -> bool:
    """把 ``IMA_ILINK_DEFAULT_KB`` 行在 ``.env`` 里替换/追加，返回是否真的写了。"""
    if not env_file.is_file():
        env_file.write_text(
            f"IMA_ILINK_DEFAULT_KB={_format_env_value(new_kb_id)}\n",
            encoding="utf-8",
        )
        return True

    text = env_file.read_text(encoding="utf-8")
    new_line = f"IMA_ILINK_DEFAULT_KB={_format_env_value(new_kb_id)}"
    pattern = re.compile(
        r"^(\s*)(?:#\s*)?(IMA_ILINK_DEFAULT_KB)\s*=\s*.*$",
        re.MULTILINE,
    )
    if pattern.search(text):
        new_text, n = pattern.subn(rf"\1\2={_format_env_value(new_kb_id)}", text)
        if n == 0:
            return False
        env_file.write_text(new_text, encoding="utf-8")
        return True

    # 没找到行则追加；保留结尾换行
    if not text.endswith("\n"):
        text += "\n"
    text += f"{new_line}\n"
    env_file.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _norm_id(kb_id: str) -> str:
    """ima 返回的 id 末尾可能带 base64 填充 ``=``，.env 里通常不带；统一后再比对。"""
    return (kb_id or "").rstrip("=")


def _row_text(idx: int, item: dict, default_kb: str) -> str:
    kb_id = item.get("kb_id") or item.get("id") or ""
    name = item.get("kb_name") or item.get("name") or ""
    role = item.get("role_type") or ""
    base_type = item.get("base_type") or ""
    desc = item.get("description") or ""
    is_default = "★" if default_kb and _norm_id(kb_id) == _norm_id(default_kb) else " "
    head = f"{idx:>3}. {is_default} {kb_id}"
    meta_bits = [b for b in (name, role, base_type) if b]
    meta = " | ".join(meta_bits)
    line = f"{head}  {meta}".rstrip()
    if desc and len(desc) <= 80:
        line += f"\n     {desc}"
    return line


def render_table(items: list[dict], default_kb: str) -> str:
    if not items:
        return "(空 — 没拉到任何知识库，请检查 IMA_ILINK_CLIENT_ID / API_KEY)"
    lines = [f"=== ima 知识库（共 {len(items)} 个）==="]
    for idx, item in enumerate(items, 1):
        lines.append(_row_text(idx, item, default_kb))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="list_ima_kb",
        description="列出腾讯 ima 账号下所有知识库 ID（适配 ilink_bot 与 weixin-ClawBot-API）",
    )
    p.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help=".env 文件路径（默认：当前目录下的 .env）",
    )
    p.add_argument(
        "--query",
        default="",
        help="按名字子串过滤（不区分大小写）",
    )
    p.add_argument(
        "--pick",
        default="",
        metavar="NAME",
        help="按名字子串匹配唯一知识库，并打印「可直接粘到 .env 的 IMA_ILINK_DEFAULT_KB=...」",
    )
    p.add_argument(
        "--update",
        action="store_true",
        help="与 --pick 搭配，把命中的 KB ID 写回 .env 的 IMA_ILINK_DEFAULT_KB",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出（每项包含 kb_id/kb_name/role_type/base_type/description）",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    env_file: Path = args.env
    if not env_file.is_file():
        print(
            f"[list_ima_kb:ERROR] 找不到 .env：{env_file}（用 --env 指定）",
            file=sys.stderr,
        )
        return 2

    env = load_env(env_file)
    cfg = ImaConfig(
        base_url=(env.get("IMA_ILINK_BASE_URL") or "https://ima.qq.com").strip(),
        client_id=(env.get("IMA_ILINK_CLIENT_ID") or "").strip(),
        api_key=(env.get("IMA_ILINK_API_KEY") or "").strip(),
        default_kb=(env.get("IMA_ILINK_DEFAULT_KB") or "").strip(),
    )

    if not cfg.client_id or not cfg.api_key:
        print(
            "[list_ima_kb:ERROR] IMA_ILINK_CLIENT_ID / IMA_ILINK_API_KEY 未配置",
            file=sys.stderr,
        )
        return 2

    client = ImaClient(cfg)
    try:
        items = client.list_all_knowledge_bases()
    except ImaError as exc:
        print(f"[list_ima_kb:ERROR] {exc}", file=sys.stderr)
        return 1

    # 子串过滤
    if args.query:
        q = args.query.lower()
        items = [
            i
            for i in items
            if q in ((i.get("kb_name") or i.get("name") or "").lower())
        ]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "kb_id": i.get("kb_id") or i.get("id"),
                        "kb_name": i.get("kb_name") or i.get("name"),
                        "role_type": i.get("role_type", ""),
                        "base_type": i.get("base_type", ""),
                        "description": i.get("description", ""),
                    }
                    for i in items
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(render_table(items, cfg.default_kb))

    # --pick 选唯一匹配
    if args.pick:
        target = args.pick.lower()
        matches = [
            i
            for i in items
            if target in ((i.get("kb_name") or i.get("name") or "").lower())
        ]
        if not matches:
            print(
                f"\n[list_ima_kb:WARN] --pick={args.pick!r} 没有命中任何知识库",
                file=sys.stderr,
            )
            return 1
        if len(matches) > 1:
            print(
                f"\n[list_ima_kb:WARN] --pick={args.pick!r} 命中 {len(matches)} 个，"
                "请用更精确的子串或 --query 过滤后再 --pick：",
                file=sys.stderr,
            )
            for i in matches:
                print(
                    f"  - {(i.get('kb_name') or i.get('name'))}: "
                    f"{i.get('kb_id') or i.get('id')}",
                    file=sys.stderr,
                )
            return 1

        kb_id = matches[0].get("kb_id") or matches[0].get("id")
        kb_name = matches[0].get("kb_name") or matches[0].get("name")
        # .env 历史写法通常不带尾 = ，跟 API 返回的 base64 填充对齐
        kb_id_for_env = _norm_id(kb_id)
        print(f"\n# 唯一匹配：{kb_name}")
        print(f"# 把下面这一行写到 {env_file}：")
        print(f"IMA_ILINK_DEFAULT_KB={_format_env_value(kb_id_for_env)}")

        if args.update:
            if update_env_default_kb(env_file, kb_id):
                print(f"\n[list_ima_kb] 已更新 {env_file} 的 IMA_ILINK_DEFAULT_KB")
            else:
                print(
                    f"[list_ima_kb:ERROR] 更新 {env_file} 失败",
                    file=sys.stderr,
                )
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
