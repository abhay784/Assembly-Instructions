from typing import Iterator, Protocol, runtime_checkable


class LLMResponse:
    def __init__(
        self,
        content: str,
        tool_calls: list[dict] | None = None,
        stop_reason: str | None = None,
    ):
        self.content = content
        self.tool_calls = tool_calls or []
        # Normalized stop reason — uses Anthropic's vocabulary across backends:
        #   "end_turn" | "max_tokens" | "stop_sequence" | "tool_use" | None
        self.stop_reason = stop_reason

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 8096,
        model: str | None = None,
        cache_system: bool = False,
    ) -> LLMResponse: ...

    def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> Iterator[str]: ...
