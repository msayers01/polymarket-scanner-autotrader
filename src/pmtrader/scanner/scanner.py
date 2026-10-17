"""Scanner: runs every strategy's detector over the live universe and ranks opportunities.

Works standalone (``pmtrader scan`` / scanner mode) as an information tool, and drives the
autotrader through ``ScanCompleted`` events.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping

import structlog

from pmtrader.config import ScannerConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import ScanCompleted
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Opportunity, Order, Position
from pmtrader.marketdata.catalog import MarketCatalog
from pmtrader.persistence.repository import Repository
from pmtrader.strategies.base import ScanContext, Strategy

log = structlog.get_logger(__name__)


class Scanner:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        cfg: ScannerConfig,
        catalog: MarketCatalog,
        books: Mapping[str, OrderBook],
        strategies: Mapping[str, Strategy],
        bankroll: Callable[[], float],
        positions: Callable[[], Mapping[str, Position]],
        open_orders: Callable[[], Mapping[str, list[Order]]],
        crypto: object | None = None,
        stale_after: Callable[[], float] = lambda: 5.0,
        repo: Repository | None = None,
    ) -> None:
        self.bus, self.clock, self.cfg = bus, clock, cfg
        self.catalog, self.books, self.strategies = catalog, books, strategies
        self._bankroll, self._positions, self._open_orders = bankroll, positions, open_orders
        self.crypto = crypto
        self._stale_after = stale_after
        self.repo = repo
        self.latest: list[Opportunity] = []
        self.latest_by_key: dict[str, Opportunity] = {}
        self.last_duration_ms = 0.0
        self.errors: dict[str, int] = {}
        self._last_persist = 0.0

    def context(self) -> ScanContext:
        universe = {mid: self.catalog.markets[mid] for mid in self.catalog.universe if mid in self.catalog.markets}
        return ScanContext(
            now=self.clock.now(),
            markets=universe,
            books=self.books,
            market_for_asset=self.catalog.market_for_asset,
            event_markets=self.catalog.event_markets,
            bankroll=self._bankroll(),
            positions=self._positions(),
            open_orders=self._open_orders(),
            crypto=self.crypto,  # type: ignore[arg-type]
            stale_after=self._stale_after(),
        )

    def scan_once(self) -> list[Opportunity]:
        t0 = time.perf_counter()
        ctx = self.context()
        found: list[Opportunity] = []
        for name, strat in self.strategies.items():
            try:
                found.extend(strat.detect(ctx))
            except Exception:
                self.errors[name] = self.errors.get(name, 0) + 1
                log.exception("scanner.strategy_failed", strategy=name)
        found = [o for o in found if o.score >= self.cfg.min_score]
        found.sort(key=lambda o: (o.score, o.ev_usd), reverse=True)
        self.latest = found[: self.cfg.top_k]
        self.latest_by_key = {o.key: o for o in found}
        self.last_duration_ms = (time.perf_counter() - t0) * 1000
        self.bus.publish(ScanCompleted(tuple(self.latest), ctx.now, self.last_duration_ms, len(ctx.markets)))
        return self.latest

    async def run(self) -> None:
        interval = self.cfg.interval_ms / 1000.0
        while True:
            self.scan_once()
            now = self.clock.now()
            if self.repo and self.latest and now - self._last_persist >= self.cfg.record_opportunities_every_s:
                self._last_persist = now
                try:
                    await self.repo.add_opportunities(now, self.latest[:10])
                except Exception:
                    log.exception("scanner.persist_failed")
            await asyncio.sleep(interval)
