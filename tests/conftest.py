"""Shared fixtures: a fully wired engine core with fakes (no network)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pmtrader.config import RiskCeilings, RiskLimits, Settings
from pmtrader.control.plane import ControlPlane
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import ManualClock
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import EventRef, FeeSchedule, Market, Mode, Outcome
from pmtrader.execution.oms import OrderManager
from pmtrader.execution.paper import PaperGateway
from pmtrader.persistence.repository import Repository
from pmtrader.portfolio.ledger import Ledger
from pmtrader.risk.engine import RiskEngine

NOW = 1_800_000_000.0


def make_market(
    mid: str = "m1",
    event: str = "e1",
    fees: FeeSchedule | None = None,
    question: str | None = None,
    end_in_days: float = 5,
    neg_risk: bool = False,
    n_open: int = 2,
    tick: float = 0.01,
) -> Market:
    return Market(
        id=mid,
        condition_id=f"c{mid}",
        question=question or f"Question {mid}?",
        slug=mid,
        outcomes=(Outcome("Yes", f"{mid}-yes"), Outcome("No", f"{mid}-no")),
        tick_size=tick,
        min_order_size=5,
        end_time=NOW + end_in_days * 86400,
        fees=fees or FeeSchedule(),
        event=EventRef(event, event, event, neg_risk, False, neg_risk, n_open),
        resolution_source="https://www.binance.com/en/trade/BTC_USDT",
        volume_24h_usd=10_000,
    )


@dataclass
class World:
    clock: ManualClock
    bus: EventBus
    markets: dict[str, Market] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)

    def add(self, m: Market) -> Market:
        self.markets[m.id] = m
        return m

    def book(self, asset: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OrderBook:
        b = self.books.get(asset) or OrderBook(asset)
        b.apply_snapshot(bids, asks, self.clock.now(), self.clock.now())
        self.books[asset] = b
        return b

    def market_for_asset(self, asset: str) -> Market | None:
        return next((m for m in self.markets.values() if asset in m.asset_ids), None)


@pytest.fixture
def world() -> World:
    return World(ManualClock(NOW), EventBus())


@dataclass
class Core:
    world: World
    ledger: Ledger
    risk: RiskEngine
    gateway: PaperGateway
    oms: OrderManager
    control: ControlPlane
    repo: Repository


@pytest.fixture
async def core(world: World, tmp_path) -> Core:  # type: ignore[no-untyped-def]
    settings = Settings(mode=Mode.PAPER, data_dir=tmp_path, log_dir=tmp_path)
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await repo.init()
    ledger = Ledger(cash=1000.0)
    holder: dict[str, ControlPlane] = {}
    limits = RiskLimits(
        allowed_strategies=["complete_set_arb", "crypto_fair_value", "negrisk_basket_arb", "ladder_arb", "manual"]
    )
    risk = RiskEngine(
        limits,
        RiskCeilings(),
        world.clock,
        Mode.PAPER,
        ledger,
        market=world.markets.get,
        book=world.books.get,
        open_orders=lambda: oms.open_orders(),
        trading_state=lambda: holder["c"].state,
    )
    gateway = PaperGateway(
        world.bus,
        world.clock,
        world.books.get,
        lambda mid: world.markets[mid].fees if mid in world.markets else FeeSchedule(),
        latency_ms=0,
    )
    oms = OrderManager(world.bus, world.clock, gateway, risk, None, world.markets.get)
    control = ControlPlane(
        world.bus, world.clock, repo, settings, oms, risk, ledger, world.markets.get, world.books.get
    )
    holder["c"] = control
    await control.load()
    yield Core(world, ledger, risk, gateway, oms, control, repo)
    await repo.close()
