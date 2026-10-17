"""MarketCatalog: periodic Gamma refresh, indices, and universe selection for the WS feed."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable

import structlog

from pmtrader.config import UniverseConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import Alert, MarketsUpdated
from pmtrader.core.types import Market
from pmtrader.marketdata.gamma import GammaClient

log = structlog.get_logger(__name__)


def eligible(m: Market, cfg: UniverseConfig, now: float) -> bool:
    if m.closed or not m.active or not m.accepting_orders:
        return False
    ttc = m.seconds_to_close(now)
    if ttc is not None and (ttc < cfg.min_minutes_to_close * 60 or ttc > cfg.max_days_to_close * 86400):
        return False
    if m.volume_24h_usd < cfg.min_volume_24h_usd:
        return False
    if m.liquidity_usd and m.liquidity_usd < cfg.min_liquidity_usd:
        return False
    q = m.question.lower()
    if any(k.lower() in q for k in cfg.exclude_keywords):
        return False
    return not (cfg.include_tags and not set(cfg.include_tags) & set(m.tags))


class MarketCatalog:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        gamma: GammaClient,
        cfg: UniverseConfig,
        on_universe: Callable[[set[str]], object] | None = None,
        pinned_markets: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.bus, self.clock, self.gamma, self.cfg = bus, clock, gamma, cfg
        self.markets: dict[str, Market] = {}
        self.by_asset: dict[str, str] = {}
        self.by_event: dict[str, list[str]] = defaultdict(list)
        self.universe: list[str] = []  # market ids in the active universe, ranked
        self._on_universe = on_universe
        self._pinned = pinned_markets or (lambda: ())
        self.last_refresh = 0.0
        self._failures = 0

    # --------------------------------------------------------------- lookups
    def get(self, market_id: str) -> Market | None:
        return self.markets.get(market_id)

    def market_for_asset(self, asset_id: str) -> Market | None:
        mid = self.by_asset.get(asset_id)
        return self.markets.get(mid) if mid else None

    def market_id_for_asset(self, asset_id: str) -> str | None:
        return self.by_asset.get(asset_id)

    def event_markets(self, event_id: str) -> list[Market]:
        return [self.markets[m] for m in self.by_event.get(event_id, []) if m in self.markets]

    # --------------------------------------------------------------- refresh
    def load(self, markets: Iterable[Market]) -> MarketsUpdated:
        """Replace the catalog (pure; used by refresh and tests)."""
        new = {m.id: m for m in markets}
        added = tuple(k for k in new if k not in self.markets)
        removed = tuple(k for k in self.markets if k not in new)
        # keep markets we hold positions in even if Gamma stops listing them as active
        for mid in self._pinned():
            if mid not in new and mid in self.markets:
                new[mid] = self.markets[mid]
        self.markets = new
        self.by_asset = {a: m.id for m in new.values() for a in m.asset_ids}
        self.by_event = defaultdict(list)
        for m in new.values():
            self.by_event[m.event_id].append(m.id)
        self.last_refresh = self.clock.now()
        return MarketsUpdated(total=len(new), added=added, removed=removed)

    def select_universe(self) -> list[str]:
        now = self.clock.now()
        ranked = sorted(
            (m for m in self.markets.values() if eligible(m, self.cfg, now)),
            key=lambda m: m.volume_24h_usd,
            reverse=True,
        )
        chosen: list[str] = []
        seen: set[str] = set()
        # whole neg-risk events must be subscribed together, otherwise baskets can't be priced
        budget = self.cfg.max_markets
        for m in ranked:
            if m.id in seen:
                continue
            group = [m.id]
            ev = m.event
            if ev and ev.neg_risk and ev.basket_complete and not ev.neg_risk_augmented:
                # basket arbitrage needs every outcome, including thinly traded ones
                group = [x for x in self.by_event[m.event_id] if self.markets[x].accepting_orders] or [m.id]
            elif ev and len(self.by_event[m.event_id]) > 1:
                group = [x for x in self.by_event[m.event_id] if eligible(self.markets[x], self.cfg, now)] or [m.id]
            if len(chosen) + len(group) > budget and chosen:
                continue
            chosen.extend(g for g in group if g not in seen)
            seen.update(group)
        for mid in self._pinned():
            if mid in self.markets and mid not in seen:
                chosen.append(mid)
        self.universe = chosen
        return chosen

    def universe_assets(self) -> set[str]:
        return {a for mid in self.universe if mid in self.markets for a in self.markets[mid].asset_ids}

    async def refresh(self) -> None:
        markets = await self.gamma.markets_from_events(max_pages=self.cfg.catalog_max_pages)
        ev = self.load(markets)
        self.select_universe()
        self.bus.publish(ev)
        if self._on_universe:
            res = self._on_universe(self.universe_assets())
            if asyncio.iscoroutine(res):
                await res
        log.info("catalog.refreshed", markets=ev.total, universe=len(self.universe))

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
                self._failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                log.warning("catalog.refresh_failed", error=repr(exc), failures=self._failures)
                if self._failures == 3:
                    self.bus.publish(Alert("warning", "Market catalog refresh failing", repr(exc)))
            await self.clock.sleep(self.cfg.catalog_refresh_seconds if self._failures == 0 else 10.0)
