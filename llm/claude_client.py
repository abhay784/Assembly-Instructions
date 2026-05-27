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


# ---------------------------------------------------------------------------
# Usage telemetry
# ---------------------------------------------------------------------------
# Per-family token totals across every complete() call in this process. Each
# call adds to its model family's bucket; get_cost_summary() converts to
# dollars using current public pricing.

_USAGE_TOTALS: dict[str, int] = {
    "sonnet_input":           0,
    "sonnet_cache_read":      0,
    "sonnet_cache_creation":  0,
    "sonnet_output":          0,
    "haiku_input":            0,
    "haiku_cache_read":       0,
    "haiku_cache_creation":   0,
    "haiku_output":           0,
    "opus_input":             0,
    "opus_cache_read":        0,
    "opus_cache_creation":    0,
    "opus_output":            0,
}

# Per-million-token rates in USD. Cache reads are billed at 10% of the input
# rate; cache writes (creation) are billed at 1.25x the input rate.
_PRICING_PER_M_TOKENS = {
    "sonnet": {"input":  3.00, "output": 15.00},
    "haiku":  {"input":  0.80, "output":  4.00},
    "opus":   {"input": 15.00, "output": 75.00},
}


def _model_family(model: str) -> str:
    m = model.lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"  # default: anything else (sonnet variants, custom fine-tuned)


def _accumulate_usage(model: str, usage) -> None:
    family = _model_family(model)
    _USAGE_TOTALS[f"{family}_input"]          += getattr(usage, "input_tokens", 0) or 0
    _USAGE_TOTALS[f"{family}_cache_read"]     += getattr(usage, "cache_read_input_tokens", 0) or 0
    _USAGE_TOTALS[f"{family}_cache_creation"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    _USAGE_TOTALS[f"{family}_output"]         += getattr(usage, "output_tokens", 0) or 0

    if os.environ.get("LLM_DEBUG_USAGE") == "1":
        print(
            f"  [usage] {family} "
            f"in={getattr(usage, 'input_tokens', 0)} "
            f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
            f"cache_create={getattr(usage, 'cache_creation_input_tokens', 0)} "
            f"out={getattr(usage, 'output_tokens', 0)}"
        )


def get_cost_summary() -> dict:
    """Return token totals + estimated cost across all complete() calls so far.

    Cache reads are priced at 10% of the standard input rate; cache writes
    (creation) at 1.25x (the 25% premium Anthropic charges for the initial
    write to the cache). Output is always at the model's output rate.
    """
    totals = dict(_USAGE_TOTALS)
    cost_by_family: dict[str, float] = {}
    total_cost = 0.0

    for family, rates in _PRICING_PER_M_TOKENS.items():
        c  = totals[f"{family}_input"]          * rates["input"]         / 1_000_000
        c += totals[f"{family}_cache_read"]     * rates["input"] * 0.10  / 1_000_000
        c += totals[f"{family}_cache_creation"] * rates["input"] * 1.25  / 1_000_000
        c += totals[f"{family}_output"]         * rates["output"]        / 1_000_000
        cost_by_family[family] = round(c, 4)
        total_cost += c

    return {
        "tokens":             totals,
        "cost_by_family_usd": cost_by_family,
        "estimated_cost_usd": round(total_cost, 4),
    }


def reset_cost_summary() -> None:
    """Zero the running totals — useful when running multiple pipelines in one process."""
    for k in _USAGE_TOTALS:
        _USAGE_TOTALS[k] = 0


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
        model: str | None = None,
        cache_system: bool = False,
    ) -> LLMResponse:
        # Wrap system as a block list with cache_control when caching is
        # requested. Anthropic requires a list of typed blocks (not a string)
        # to attach cache_control. The cached prefix must be >=1024 tokens
        # (Sonnet/Opus) or >=2048 tokens (Haiku); below that, cache_control
        # is ignored and the call is billed at full input rate.
        if cache_system:
            system_payload: list | str = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_payload = system

        kwargs: dict = {
            "model": model or self._model,
            "max_tokens": max_tokens,
            "system": system_payload,
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

                # Telemetry: accumulate token usage for end-of-run cost
                # estimate. Guarded against missing usage attr to stay robust
                # across SDK versions.
                usage = getattr(final, "usage", None)
                if usage is not None:
                    _accumulate_usage(kwargs["model"], usage)

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
