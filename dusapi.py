"""DusAPI 兼容封装：Anthropic ``/v1/messages`` 格式。

两种模型共用同一端点：
  - model 名含 ``claude`` → 按 ``content[0]['text']`` 解析
  - model 名含 ``gpt`` 或其它 → 遍历 ``content`` 找 ``type=='text'``

⚠️ **2026-09-11 改动**：移除原 5 次梯度重试（``[2,4,8,16,32]s``，共 62s 退避 + 30s
timeout/次 ≈ 60-200s 总延迟）。原因是长跑 bot 进程首次调 DusAPI 时，
5 次重试全部返回 ``401 Unauthorized``，把一次 1 秒能成功的请求拖成 70 秒。
直接降级到用户可见的"API接口失效"反而比让用户等 70 秒更友好。

需要重试时改用调用方（如 ``_AIWithIma`` 的 fallback 链），不在单次 HTTP 调用里塞。
"""

import requests
from dataclasses import dataclass


version = "1.0.1"


def log(message: str, level: str = "INFO") -> None:
    print(f"[{level}] {message}")


@dataclass
class DusConfig:
    api_key: str
    base_url: str
    model1: str = "claude-sonnet-4-5"
    prompt: str = "你是一个有帮助的AI助手。"


class DusAPI:
    """单次 ``POST /v1/messages``；失败立即抛 ``requests`` 异常（让上层 fallback 处理）。"""

    def __init__(self, config: DusConfig):
        self.config = config
        self.DS_NOW_MOD = config.model1
        self.api_key = config.api_key
        self.base_url = config.base_url.rstrip("/")

    def chat(self, message, model=None, stream=False, prompt=None, history=None):
        if model is None:
            model = self.DS_NOW_MOD
        if prompt is None:
            prompt = self.config.prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": f"siver-weixin_clawbot-api/{version}",
        }
        # Anthropic /v1/messages：system 顶层、messages 只允许 user/assistant
        messages = []
        if history:
            for h in history:
                role = "assistant" if h.get("attr") == "self" else "user"
                t = h.get("time", "")
                content = f"[{t}] {h.get('content', '')}" if t else h.get("content", "")
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": prompt,
            "messages": messages,
        }
        api_endpoint = f"{self.base_url}/v1/messages"

        # 单次请求；让 ``requests`` 的网络异常 / 4xx / 5xx 直接往上抛
        response = requests.post(
            api_endpoint, headers=headers, json=payload, timeout=30
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        response_data = response.json()

        if "claude" in model.lower():
            return response_data["content"][0]["text"]

        for content_block in response_data["content"]:
            if content_block.get("type") == "text":
                return content_block["text"]

        log(level="WARN", message="DusAPI 响应中未找到文本内容")
        return "AI 未返回有效内容"