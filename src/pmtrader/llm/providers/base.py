"""LLM provider port."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMResult:
    decisions: list[dict[str, Any]] = field(default_factory=list)  # parsed "d" items
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    usd: float = 0.0
    error: str = ""
    model: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class LLMProvider(Protocol):
    name: str
    model: str

    async def decide(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult: ...

    async def close(self) -> None: ...
