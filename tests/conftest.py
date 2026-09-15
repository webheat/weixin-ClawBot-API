"""Small test doubles shared by the BotSession/BotManager contract tests.

The production refactor is intentionally allowed to choose its concrete HTTP
client and persistence implementation.  These doubles keep the tests focused
on the lifecycle contracts instead of network or disk I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeHTTP:
    """A deliberately tiny stand-in for the shared aiohttp session."""

    closed = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeAI:
    """Deterministic AI client used by first-message tests."""

    calls: list[str] = field(default_factory=list)
    answer: str = "AI reply"

    def chat(self, text: str, **_: Any) -> str:
        self.calls.append(text)
        return self.answer


def message(text: str = "hello") -> dict[str, Any]:
    """Build the smallest iLink-style private text message."""

    return {
        "message_type": 1,
        "from_user_id": "contact-1",
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
