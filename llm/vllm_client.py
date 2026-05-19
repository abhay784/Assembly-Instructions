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
    ) -> LLMResponse:
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict = {"model": self._model, "messages": full_messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0].message

        tool_calls = []
        if choice.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in choice.tool_calls
            ]

        return LLMResponse(content=choice.content or "", tool_calls=tool_calls)

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
