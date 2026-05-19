from typing import Iterator, Protocol, runtime_checkable


class LLMResponse:
    def __init__(self, content: str, tool_calls: list[dict] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...

    def stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> Iterator[str]: ...
