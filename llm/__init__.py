import os

from llm.client import LLMClient, LLMResponse
from llm.claude_client import ClaudeClient
from llm.vllm_client import VLLMClient


def get_client() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "vllm").lower()
    if backend == "claude":
        return ClaudeClient()
    return VLLMClient()


__all__ = ["LLMClient", "LLMResponse", "VLLMClient", "ClaudeClient", "get_client"]
