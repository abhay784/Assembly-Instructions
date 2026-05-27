import json
import os
import time
from typing import Iterator

import anthropic
import httpx

from llm.client import LLMResponse


# Network/server failures that are worth retrying. Anthropic's wrappers
# surface as APIConnectionError / APITimeoutError / InternalServerError.
# Raw httpx transport errors (ReadError "WinError 10054", WriteError,
# RemoteProtocolError, ConnectError, ReadTimeout, NetworkError, ...) all
# subclass httpx.TransportError — catching the parent covers every flavour
# of mid-stream connection drop without needing to enumerate them.
# Auth errors, BadRequest, etc. are NOT retried — they won't fix themselves.
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    httpx.TransportError,
)

_MAX_RETRIES = 4
_BACKOFF_BASE_SEC = 2.0


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

        last_error: BaseException | None = None
        for attempt in range(_MAX_RETRIES):
            try:
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

                return LLMResponse(
                    content=content_text,
                    tool_calls=tool_calls,
                    stop_reason=getattr(final, "stop_reason", None),
                )
            except _TRANSIENT_ERRORS as e:
                last_error = e
                if attempt == _MAX_RETRIES - 1:
                    break
                wait = _BACKOFF_BASE_SEC * (2 ** attempt)
                print(
                    f"  LLM call failed ({type(e).__name__}: {e}); "
                    f"retry {attempt + 1}/{_MAX_RETRIES - 1} in {wait:.0f}s"
                )
                time.sleep(wait)

        raise RuntimeError(
            f"LLM call failed after {_MAX_RETRIES} attempts. Last error: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error

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
