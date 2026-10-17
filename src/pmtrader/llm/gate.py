"""LLM gate: batched, cached, budgeted market-selection decisions (docs/LLM_GATE.md)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from pmtrader.config import LLMConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import Alert, GateDecision
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Market, Opportunity
from pmtrader.llm.prompts import OUTPUT_SCHEMA, PROMPT_VERSION, SYSTEM_PROMPT, max_output_tokens
from pmtrader.llm.providers.base import LLMProvider, LLMResult
from pmtrader.persistence.repository import Repository
from pmtrader.scanner.digest import build_digest

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    enter: bool
    confidence: float
    fair: float | None
    reason: str
    provider: str
    ts: float
    cached: bool = False


def build_providers(cfg: LLMConfig) -> list[LLMProvider]:
    out: list[LLMProvider] = []
    for p in cfg.providers:
        if p.kind == "anthropic":
            from pmtrader.llm.providers.anthropic_provider import AnthropicProvider

            out.append(AnthropicProvider(p))
        else:
            from pmtrader.llm.providers.openai_compat import OpenAICompatProvider

            out.append(OpenAICompatProvider(p))
    return out


class LLMGate:
    def __init__(
        self,
        cfg: LLMConfig,
        providers: list[LLMProvider],
        bus: EventBus,
        clock: Clock,
        repo: Repository | None,
        market: Callable[[str], Market | None],
        book: Callable[[str], OrderBook | None],
    ) -> None:
        self.cfg, self.providers, self.bus, self.clock, self.repo = cfg, providers, bus, clock, repo
        self._market, self._book = market, book
        self._cache: dict[str, GateVerdict] = {}
        self.day = ""
        self.calls = self.tokens = 0
        self.usd = 0.0
        self._budget_alerted = False
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self.cfg.gate_mode if self.providers else "off"

    # ------------------------------------------------------------------ cache / budget
    def cache_key(self, o: Opportunity) -> str:
        asset = o.legs[0][0] if o.legs else ""
        b = self._book(asset)
        px = b.best_ask if b and b.best_ask is not None else (o.fair or 0.0)
        bucket = int(px / self.cfg.price_bucket)
        return f"{PROMPT_VERSION}|{o.strategy}|{o.market_id}|{asset[:16]}|{bucket}"

    def cached(self, o: Opportunity) -> GateVerdict | None:
        v = self._cache.get(self.cache_key(o))
        if v is None or self.clock.now() - v.ts > self.cfg.cache_ttl_s:
            return None
        return GateVerdict(v.enter, v.confidence, v.fair, v.reason, v.provider, v.ts, cached=True)

    def fallback(self, reason: str) -> GateVerdict:
        enter = self.cfg.fallback == "deterministic"
        return GateVerdict(enter, 1.0 if enter else 0.0, None, reason, "fallback", self.clock.now())

    def _roll_day(self) -> None:
        day = datetime.fromtimestamp(self.clock.now(), UTC).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.calls, self.tokens, self.usd, self._budget_alerted = day, 0, 0, 0.0, False

    async def load_budget(self) -> None:
        self._roll_day()
        if self.repo:
            row = await self.repo.daily(self.day)
            self.calls, self.tokens, self.usd = row.llm_calls or 0, row.llm_tokens or 0, row.llm_usd or 0.0

    def budget_left(self) -> bool:
        self._roll_day()
        c = self.cfg
        return self.calls < c.daily_max_calls and self.tokens < c.daily_max_tokens and self.usd < c.daily_max_usd

    def stats(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "calls": self.calls,
            "tokens": self.tokens,
            "usd": round(self.usd, 4),
            "cache_size": len(self._cache),
            "providers": [p.name for p in self.providers],
        }

    # ------------------------------------------------------------------ evaluation
    async def evaluate(
        self, opps: list[Opportunity], notional: Callable[[Opportunity], float] = lambda o: 0.0
    ) -> dict[str, GateVerdict]:
        """Verdicts keyed by ``Opportunity.key``. Uses cache first; calls providers only for misses."""
        out: dict[str, GateVerdict] = {}
        misses: list[Opportunity] = []
        for o in opps:
            v = self.cached(o)
            if v is not None:
                out[o.key] = v
            else:
                misses.append(o)
        if not misses:
            return out
        if self.mode == "off":
            for o in misses:
                out[o.key] = self.fallback("gate off")
            return out
        async with self._lock:
            if self.clock.now() - self._last_call < self.cfg.min_interval_s:
                return out  # rate-limited: undecided this round (caller retries next scan)
            for i in range(0, len(misses), self.cfg.batch_size):
                if not self.budget_left():
                    if not self._budget_alerted:
                        self._budget_alerted = True
                        self.bus.publish(Alert("warning", "LLM budget exhausted", f"fallback={self.cfg.fallback}"))
                    for o in misses[i:]:
                        out[o.key] = self.fallback("llm budget exhausted")
                    break
                batch = misses[i : i + self.cfg.batch_size]
                out.update(await self._run_batch(batch, notional))
            self._last_call = self.clock.now()
        return out

    async def _call(self, provider: LLMProvider, batch: list[Opportunity]) -> tuple[dict[str, dict], LLMResult]:
        digest, idmap = build_digest(batch, self._market, self._book)
        res = await provider.decide(SYSTEM_PROMPT, digest, OUTPUT_SCHEMA, max_output_tokens(len(batch)))
        self.calls += 1
        self.tokens += res.input_tokens + res.output_tokens
        self.usd += res.usd
        if self.repo:
            row = await self.repo.daily(self.day)
            row.llm_calls, row.llm_tokens, row.llm_usd = self.calls, self.tokens, self.usd
            await self.repo.save_daily(row)
        log.info(
            "llm.call",
            provider=provider.name,
            n=len(batch),
            tokens_in=res.input_tokens,
            tokens_out=res.output_tokens,
            cached=res.cached_tokens,
            usd=round(res.usd, 5),
            error=res.error,
        )
        by_key: dict[str, dict] = {}
        for d in res.decisions:
            o = idmap.get(str(d.get("id")))
            if o is not None:
                by_key[o.key] = d
        return by_key, res

    async def _run_batch(
        self, batch: list[Opportunity], notional: Callable[[Opportunity], float]
    ) -> dict[str, GateVerdict]:
        now = self.clock.now()
        verdicts: dict[str, GateVerdict] = {}
        pending = batch
        lo, hi = self.cfg.borderline
        for idx, provider in enumerate(self.providers):
            if not pending:
                break
            by_key, res = await self._call(provider, pending)
            escalate: list[Opportunity] = []
            for o in pending:
                d = by_key.get(o.key)
                if d is None:
                    v = GateVerdict(False, 0.0, None, f"no decision ({res.error or 'missing'})", provider.name, now)
                else:
                    conf = float(d.get("c") or 0.0)
                    enter = d.get("a") == "enter" and conf >= self.cfg.min_confidence
                    fair = d.get("p")
                    v = GateVerdict(
                        enter,
                        conf,
                        float(fair) if fair is not None else None,
                        str(d.get("r", ""))[:120],
                        provider.name,
                        now,
                    )
                verdicts[o.key] = v
                is_last = idx == len(self.providers) - 1
                if not is_last and (lo <= v.confidence <= hi or notional(o) >= self.cfg.escalate_notional_usd):
                    escalate.append(o)
            pending = escalate
        for o in batch:
            v = verdicts[o.key]
            self._cache[self.cache_key(o)] = v
            self.bus.publish(
                GateDecision(
                    o.key, o.market_id, "enter" if v.enter else "skip", v.confidence, v.provider, v.reason, v.fair
                )
            )
            if self.repo:
                asset = o.legs[0][0] if o.legs else ""
                b = self._book(asset)
                await self.repo.add_llm_decision(
                    ts=now,
                    provider=v.provider,
                    model=next((p.model for p in self.providers if p.name == v.provider), ""),
                    market_id=o.market_id,
                    opportunity_key=o.key,
                    decision="enter" if v.enter else "skip",
                    confidence=v.confidence,
                    fair=v.fair,
                    reason=v.reason,
                    market_price=b.best_ask if b else None,
                )
        if len(self._cache) > 5000:
            cutoff = now - self.cfg.cache_ttl_s
            self._cache = {k: v for k, v in self._cache.items() if v.ts >= cutoff}
        return verdicts

    async def close(self) -> None:
        for p in self.providers:
            await p.close()
