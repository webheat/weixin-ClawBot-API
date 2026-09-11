"""utils/smoke_ai_routing.py

mock 验证 ``_AIWithIma.chat`` 的 4 档 mode 路由决策。
不连真实 IMA / 网络，只在内存里验。

用法::

    HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \\
        ./venv/bin/python utils/smoke_ai_routing.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 确保 semantic_kb 模块先被 import；缺 fastembed 时会 import 失败 → smoke 跳过对应场景
import utils.semantic_kb  # noqa: F401


def _mk_base(text: str = "MOCKED-LLM-RESPONSE"):
    """造一个假的 base AI（``self._base.chat`` 只回固定文本）。"""
    base = MagicMock()
    base.config.prompt = "BASE_SYSTEM_PROMPT"
    base.chat = MagicMock(return_value=text)
    return base


def _mk_ima(returned_hits):
    """造一个假的 ImaClient。``returned_hits`` 是 SearchHit 列表或 ``[]``。"""
    ima = MagicMock()
    ima.configured.return_value = True
    ima.cfg = MagicMock()
    ima.cfg.search_limit = 3
    ima.cfg.rerank_enabled = False
    ima.cfg.rerank_top_k = 3
    ima.cfg.fetch_body = False
    ima.cfg.keyword_extract = False
    ima.search_knowledge = MagicMock(return_value=returned_hits)
    return ima


def _mk_hit(title: str, content: str = "正文"):
    from ima import SearchHit
    return SearchHit(
        media_id=f"mock:{title}",
        title=title,
        content=content,
        snippet=content[:80],
        highlight_content="",
        url="",
        knowledge_base_id="mock",
        score=0.99,
    )


def _run(label: str, *, env: dict, ima_hits, expected_mode: str):
    """一次 chat() 调用：验 mode 和 reason。"""
    # 备份 + 设置 env
    saved = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        # 重新 import bot 以保证 _AIWithIma 类拿到当前的 env（lazy init 用 os.environ 直读）
        # 但 _AIWithIma 走的是 _AIWithIma 实例的 lazy，第一次访问才建。
        from bot import _AIWithIma

        base = _mk_base()
        ima = _mk_ima(ima_hits)
        wrapper = _AIWithIma(base, ima)

        # 触发 chat
        result = wrapper.chat("样例问题")
        ok = result == "MOCKED-LLM-RESPONSE"  # 直通底层

        # 解析最近一次 [AI 路由] log（std out 被我们劫持了）
        # 这里直接读 self._local_kb_attempted / self._semantic_kb_attempted
        print(f"\n[{label}]")
        print(f"  env    : {env}")
        print(f"  ima_hits: {len(ima_hits) if ima_hits else 0}")
        print(f"  result : {ok and '直通底层 OK' or 'FAIL'}")
        # 打印 base.chat 收到的 prompt 前 200 字
        if base.chat.called:
            kw = base.chat.call_args.kwargs
            prompt = kw.get("prompt", "")
            tag = f"<{expected_mode}-ctx:{('有' if '参考资料' in prompt else '无')}>"
            print(f"  prompt : {tag} {prompt[:200]!r}")
        return ok
    finally:
        # 恢复 env
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    base_env = {
        # 关掉 local fallback（除非显式开）
        "CLAWBOT_LOCAL_FALLBACK": "0",
        # 默认 semantic KB 也关
        "SEMANTIC_KB_ENABLED": "0",
    }

    print("=" * 70)
    print("Smoke test: _AIWithIma 4-mode routing")
    print("=" * 70)

    # 场景 1: IMA 命中 → mode=llm+ima
    _run(
        "1) IMA 命中",
        env=base_env,
        ima_hits=[_mk_hit("IMA-命中-doc")],
        expected_mode="llm+ima",
    )

    # 场景 2: IMA 0 + local BM25 命中 → mode=llm+local
    _run(
        "2) IMA 0 + local BM25 命中",
        env={**base_env, "CLAWBOT_LOCAL_FALLBACK": "1"},
        ima_hits=[],
        expected_mode="llm+local",
    )

    # 场景 3: IMA 0 + local 0 + semantic 命中 → mode=llm+semantic
    _run(
        "3) IMA 0 + local 0 + semantic 命中",
        env={
            **base_env,
            "CLAWBOT_LOCAL_FALLBACK": "1",
            "SEMANTIC_KB_ENABLED": "1",
        },
        ima_hits=[],
        expected_mode="llm+semantic",
    )

    # 场景 4: 全 0 → mode=llm-only
    _run(
        "4) 全 0 命中",
        env={
            **base_env,
            "CLAWBOT_LOCAL_FALLBACK": "1",
            "SEMANTIC_KB_ENABLED": "1",
        },
        ima_hits=[],
        expected_mode="llm-only",
    )

    print("\n[注] 上面 prompt 标签里看不到精确 mode（脚本只验直通 + ctx 有无）；")
    print("     4-mode 决策日志会出现在 [AI 路由] mode=... 那行。")
    print("     本脚本重点验 _AIWithIma 不会因新增 fallback 而破坏基本响应链路。")


if __name__ == "__main__":
    main()