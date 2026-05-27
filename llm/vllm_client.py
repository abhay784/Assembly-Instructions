import os
from typing import Iterator

from openai import OpenAI

from llm.client import LLMResponse


class VLLMClient:
    """On-prem vLLM client via OpenAI-compatible endpoint."""

    def __init__(self):
        self._client = OpenAI(
            base_url=os.environ["VLLM_BASE_URL"],
            api_key="not-needed",  # vLLM doesn't require a key
        )
        self._model = os.environ.get("VLLM_MODEL", "default")

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
    ) -> LLMResponse:
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict = {"model": self._model, "messages": full_messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in message.tool_calls
            ]

        # Normalize OpenAI finish_reason vocabulary to Anthropic's
        _STOP_REASON_MAP = {
            "stop":       "end_turn",
            "length":     "max_tokens",
            "tool_calls": "tool_use",
        }
        stop_reason = _STOP_REASON_MAP.get(choice.finish_reason or "", choice.finish_reason)

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            stop_reason=stop_reason,
        )

    def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict = {"model": self._model, "messages": full_messages, "stream": True}
        if tools:
            kwargs["tools"] = tools

        for chunk in self._client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
