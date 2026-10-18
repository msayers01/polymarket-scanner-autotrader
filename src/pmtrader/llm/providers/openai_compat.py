"""OpenAI-compatible chat-completions adapter: OpenAI/ChatGPT, Ollama, LM Studio, llama.cpp, vLLM (Qwen etc.)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from pmtrader.config import LLMProviderConfig
from pmtrader.llm.providers.base import LLMResult

_JSON_RE = re.compile(r"\{.*\}", re.S)


class OpenAICompatProvider:
    def __init__(self, cfg: LLMProviderConfig) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        base = (cfg.base_url or "http://localhost:11434/v1").rstrip("/")
        headers = {}
        key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.AsyncClient(base_url=base, headers=headers, timeout=cfg.timeout_s)

    async def close(self) -> None:
        await self.http.aclose()

    async def decide(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "gate", "schema": schema, "strict": True},
            },
        }
        try:
            r = await self.http.post("/chat/completions", json=body)
            if r.status_code == 400:
                # some local servers only support json_object mode
                body["response_format"] = {"type": "json_object"}
                r = await self.http.post("/chat/completions", json=body)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            return LLMResult(error=f"http: {exc!r}", model=self.model)
        usage = data.get("usage") or {}
        res = LLMResult(
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model=str(data.get("model", self.model)),
        )
        res.usd = (
            res.input_tokens * self.cfg.input_usd_per_mtok + res.output_tokens * self.cfg.output_usd_per_mtok
        ) / 1e6
        try:
            content = data["choices"][0]["message"]["content"] or ""
            # reasoning models may wrap JSON in prose / <think> blocks
            m = _JSON_RE.search(content.split("</think>")[-1])
            res.decisions = list(json.loads(m.group(0) if m else content).get("d", []))
        except (KeyError, IndexError, json.JSONDecodeError, AttributeError) as exc:
            res.error = f"bad_json: {exc!r}"
        return res
