"""Composition root: the only place that builds concrete adapters and wires them (ARCHITECTURE.md §3)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import structlog

from pmtrader.config import Settings
from pmtrader.control.plane import ControlPlane
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock, SystemClock
from pmtrader.core.events import Alert
from pmtrader.core.supervisor import Supervisor
from pmtrader.core.types import FeeSchedule, Mode
from pmtrader.execution.gateway import ExecutionGateway, NullGateway
from pmtrader.execution.oms import OrderManager
from pmtrader.execution.paper import PaperGateway
from pmtrader.llm.gate import LLMGate, build_providers
from pmtrader.marketdata.catalog import MarketCatalog
from pmtrader.marketdata.clob_rest import ClobRestClient
from pmtrader.marketdata.clob_ws import MarketDataService
from pmtrader.marketdata.external.crypto import CryptoReference
from pmtrader.marketdata.gamma import GammaClient
from pmtrader.persistence.repository import Repository
from pmtrader.portfolio.ledger import Ledger
from pmtrader.portfolio.service import PortfolioService
from pmtrader.risk.engine import RiskEngine
from pmtrader.scanner.scanner import Scanner
from pmtrader.strategies.engine import StrategyEngine
from pmtrader.strategies.registry import build_strategies

log = structlog.get_logger(__name__)

LIVE_CONFIRM_VALUE = "I_ACCEPT_RISK"


class Engine:
    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = s = settings
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.repo = Repository(s.database_url)
        self.http = httpx.AsyncClient(timeout=20.0, http2=True)
        self.started_at = self.clock.now()

        # ---- market data
        self.gamma = GammaClient(s.polymarket.gamma_url, self.http)
        self.clob = ClobRestClient(s.polymarket.clob_url, self.http)
        self.feed = MarketDataService(
            self.bus,
            self.clock,
            s.polymarket.ws_market_url,
            s.polymarket.ws_ping_seconds,
            s.polymarket.ws_max_assets_per_conn,
            asset_to_market=lambda a: self.catalog.market_id_for_asset(a),
        )
        self.ledger = Ledger()
        self.catalog = MarketCatalog(
            self.bus,
            self.clock,
            self.gamma,
            s.universe,
            on_universe=self.feed.set_universe,
            pinned_markets=lambda: (
                {p.market_id for p in self.ledger.positions.values()}
                | {o.intent.market_id for o in self.oms.open_orders()}
            ),
        )
        self.crypto = CryptoReference(self.bus, self.clock, s.external, self.http)

        # ---- strategies / risk / execution
        self.strategies = build_strategies(s)
        self.control: ControlPlane  # set below (risk needs a state accessor)
        self.risk = RiskEngine(
            s.risk,
            s.risk_ceilings,
            self.clock,
            s.mode,
            self.ledger,
            market=self.catalog.get,
            book=self.feed.book,
            open_orders=lambda: self.oms.open_orders(),
            trading_state=lambda: self.control.state,
            strategy_allows_near_expiry=lambda n: bool(getattr(self.strategies.get(n), "allows_near_expiry", False)),
        )
        self.gateway = self._build_gateway()
        self.oms = OrderManager(self.bus, self.clock, self.gateway, self.risk, self.repo, self.catalog.get)
        self.control = ControlPlane(
            self.bus, self.clock, self.repo, s, self.oms, self.risk, self.ledger, self.catalog.get, self.feed.book
        )
        self.portfolio = PortfolioService(
            self.bus,
            self.clock,
            self.ledger,
            self.repo,
            self.risk,
            self.catalog.get,
            self.feed.book,
            s.reports,
            Path(s.data_dir) / "reports",
            on_breach=self.control.on_breach,
            gamma=self.gamma,
            starting_cash=s.execution.paper.starting_cash_usd,
        )
        self.scanner = Scanner(
            self.bus,
            self.clock,
            s.scanner,
            self.catalog,
            self.feed.books,
            self.strategies,
            bankroll=lambda: self.ledger.equity(self.portfolio.last_marks),
            positions=lambda: self.ledger.positions,
            open_orders=self.oms.open_orders_by_asset,
            crypto=self.crypto,
            stale_after=lambda: self.risk.limits.stale_book_seconds,
            repo=self.repo,
        )
        self.gate = LLMGate(
            s.llm, build_providers(s.llm), self.bus, self.clock, self.repo, self.catalog.get, self.feed.book
        )
        self.strategy_engine = StrategyEngine(
            self.bus,
            self.clock,
            self.strategies,
            self.scanner,
            self.oms,
            self.risk,
            self.ledger,
            self.gate,
            self.control,
            llm_fallback=s.llm.fallback,
        )
        self.supervisor = Supervisor(on_escalate=self.control.on_supervisor_escalation)
        self.discord: Any = None

    def _fees_for(self, market_id: str) -> FeeSchedule:
        m = self.catalog.get(market_id)
        return m.fees if m else FeeSchedule()

    def _build_gateway(self) -> ExecutionGateway:
        s = self.settings
        if s.mode is Mode.SCANNER:
            return NullGateway()
        if s.mode is Mode.PAPER:
            return PaperGateway(self.bus, self.clock, self.feed.book, self._fees_for, s.execution.paper.latency_ms)
        if (s.secrets.live_confirm or os.environ.get("PMT_LIVE_CONFIRM")) != LIVE_CONFIRM_VALUE:
            raise RuntimeError(f"live mode requires PMT_LIVE_CONFIRM={LIVE_CONFIRM_VALUE} (see docs/RUNBOOK.md)")
        from pmtrader.execution.polymarket_live import PolymarketLiveGateway

        return PolymarketLiveGateway(s.secrets, s.execution.live, self.clock, self._fees_for)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        s = self.settings
        await self.repo.init()
        await self.portfolio.load()
        await self.control.load()
        await self.gate.load_budget()
        if s.mode is Mode.LIVE:
            await self.gateway.connect()  # type: ignore[attr-defined]
        sup = self.supervisor
        sup.start("catalog", self.catalog.run, critical=True)
        sup.start("feed", self.feed.run, critical=True)
        sup.start("crypto", self.crypto.run)
        sup.start("scanner", self.scanner.run, critical=True)
        sup.start("portfolio", self.portfolio.run, critical=True)
        sup.start("orders-persist", self.oms.run_persistence)
        if s.mode is not Mode.SCANNER:
            sup.start("gateway", self.gateway.run, critical=True)
            sup.start("strategy-engine", self.strategy_engine.run, critical=True)
        if s.discord.enabled and s.secrets.discord_token:
            from pmtrader.discord_bot.bot import TraderBot

            self.discord = TraderBot(self)
            sup.start("discord", self.discord.run_bot)
        self.bus.publish(Alert("info", "pmtrader started", f"mode={s.mode.value} state={self.control.state.value}"))
        log.info("engine.started", mode=s.mode.value, strategies=list(self.strategies), state=self.control.state.value)

    async def stop(self) -> None:
        log.info("engine.stopping")
        if self.settings.mode is Mode.LIVE:
            # resting quotes must not outlive the process
            mm_orders = [o for o in self.oms.open_orders() if o.intent.post_only]
            for o in mm_orders:
                await self.oms.cancel(o.id)
        await self.supervisor.stop()
        if self.discord is not None:
            await self.discord.close()
        await self.gate.close()
        await self.http.aclose()
        await self.repo.close()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    # ------------------------------------------------------------------ read model for UI / bot
    def opportunities_view(self, limit: int = 25) -> list[dict[str, Any]]:
        out = []
        for o in self.scanner.latest[:limit]:
            v = self.gate.cached(o)
            legs = []
            for asset, side, px in o.legs:
                m = self.catalog.market_for_asset(asset)
                oc = m.outcome_for(asset) if m else None
                legs.append(
                    {
                        "asset_id": asset,
                        "side": side.value,
                        "price": px,
                        "outcome": oc.name if oc else "?",
                        "market_id": m.id if m else "",
                    }
                )
            out.append(
                {
                    "key": o.key,
                    "id": o.id,
                    "strategy": o.strategy,
                    "market_id": o.market_id,
                    "question": o.question,
                    "legs": legs,
                    "fair": o.fair,
                    "edge": o.edge,
                    "size": round(o.size_hint, 2),
                    "ev_usd": round(o.ev_usd, 2),
                    "score": round(o.score, 2),
                    "days": o.days_to_close,
                    "needs_judgment": o.needs_judgment,
                    "note": o.note,
                    "category": o.category,
                    "llm": None if v is None else {"enter": v.enter, "c": v.confidence, "r": v.reason, "p": v.provider},
                }
            )
        return out

    def orders_view(self) -> list[dict[str, Any]]:
        out = []
        for o in sorted(self.oms.open_orders(), key=lambda x: -x.created_ts):
            it = o.intent
            m = self.catalog.get(it.market_id)
            oc = m.outcome_for(it.asset_id) if m else None
            out.append(
                {
                    "id": o.id,
                    "strategy": it.strategy,
                    "market_id": it.market_id,
                    "question": m.question if m else it.market_id,
                    "outcome": oc.name if oc else "?",
                    "side": it.side.value,
                    "price": it.price,
                    "size": it.size,
                    "filled": round(o.filled_size, 2),
                    "status": o.status.value,
                    "type": it.order_type.value,
                    "created_ts": o.created_ts,
                }
            )
        return out

    def health(self) -> dict[str, Any]:
        now = self.clock.now()
        return {
            "feed_connected": any(sh.ws is not None for sh in self.feed._shards),
            "feed_age_s": round(now - self.feed.last_message_ts, 2) if self.feed.last_message_ts else None,
            "subscribed": len(self.feed.subscribed),
            "books": len(self.feed.books),
            "universe": len(self.catalog.universe),
            "catalog_markets": len(self.catalog.markets),
            "scan_ms": round(self.scanner.last_duration_ms, 1),
            "tasks": self.supervisor.status(),
            "llm": self.gate.stats(),
            "uptime_s": round(now - self.started_at),
            "risk_rejects": dict(self.risk.reject_counts),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "control": self.control.snapshot(),
            "portfolio": self.portfolio.summary(),
            "opportunities": self.opportunities_view(),
            "orders": self.orders_view(),
            "health": self.health(),
            "strategies": {
                n: {
                    "enabled": True,
                    "auto": self.control.strategy_auto.get(n, False),
                    "needs_judgment": s.needs_judgment,
                }
                for n, s in self.strategies.items()
            },
        }
