import json
import os
from typing import Iterator

import anthropic

from llm.client import LLMResponse


class ClaudeClient:
    """Anthropic Claude API client — swap-in replacement for VLLMClient."""

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._model = (
            os.environ.get("FINETUNED_MODEL")
            or os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
        )

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        content_text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    }
                )

        return LLMResponse(content=content_text, tool_calls=tool_calls)

    def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        with self._client.messages.stream(**kwargs) as stream:
            yield from stream.text_stream
