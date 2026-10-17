"""Claude via the official ``anthropic`` SDK: structured JSON output + cached system prompt."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic
import structlog

from pmtrader.config import LLMProviderConfig
from pmtrader.llm.providers.base import LLMResult

log = structlog.get_logger(__name__)

_FALLBACK_MODELS = ("claude-opus", "claude-sonnet", "claude-fable")


class AnthropicProvider:
    def __init__(self, cfg: LLMProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        # with no explicit key the SDK resolves ANTHROPIC_API_KEY / auth profile itself
        self.client = anthropic.AsyncAnthropic(api_key=key, timeout=cfg.timeout_s, max_retries=2)

    async def close(self) -> None:
        await self.client.close()

    async def decide(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        try:
            if self.model.startswith(_FALLBACK_MODELS):
                # server-side refusal fallback (routes by refusal category)
                resp = await self.client.beta.messages.create(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            return LLMResult(error=f"rate_limited: {exc.message}", model=self.model)
        except anthropic.APIStatusError as exc:
            return LLMResult(error=f"api_error {exc.status_code}: {exc.message}", model=self.model)
        except anthropic.APIConnectionError as exc:
            return LLMResult(error=f"connection: {exc!r}", model=self.model)

        u = resp.usage
        cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        res = LLMResult(
            input_tokens=int(u.input_tokens) + cached + written,
            output_tokens=int(u.output_tokens),
            cached_tokens=cached,
            model=str(resp.model),
        )
        res.usd = (
            (u.input_tokens + 1.25 * written + 0.1 * cached) * self.cfg.input_usd_per_mtok
            + u.output_tokens * self.cfg.output_usd_per_mtok
        ) / 1e6
        if resp.stop_reason == "refusal":
            res.error = "refusal"
            return res
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        try:
            res.decisions = list(json.loads(text).get("d", []))
        except (json.JSONDecodeError, AttributeError) as exc:
            res.error = f"bad_json: {exc}"
        return res
