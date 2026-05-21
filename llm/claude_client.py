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
        max_tokens: int = 8096,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = tools

        tool_calls = []
        with self._client.messages.stream(**kwargs) as stream:
            final = stream.get_final_message()
            content_text = ""
            for block in final.content:
                if block.type == "text":
                    content_text += block.text
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


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """
    Convert OpenAI-style messages to Anthropic format.

    OpenAI assistant+tool pattern:
      {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "arguments"}]}
      {"role": "tool", "tool_call_id": "...", "content": "..."}

    Anthropic equivalent:
      {"role": "assistant", "content": [text_block, tool_use_block, ...]}
      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}
    """
    out = []
    for msg in messages:
        role = msg["role"]

        if role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }],
            })
            continue

        if role == "assistant" and msg.get("tool_calls"):
            content: list = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": json.loads(tc["arguments"]),
                })
            out.append({"role": "assistant", "content": content})
            continue

        out.append(msg)

    return out
