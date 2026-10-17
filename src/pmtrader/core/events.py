"""Typed events published on the EventBus.

Rule of thumb: events are *facts* (past tense). Commands go through the ControlPlane.
``critical = True`` marks events that must never be dropped by back-pressure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from pmtrader.core.types import Fill, Opportunity, Order, OrderIntent, TradingState


@dataclass(frozen=True, slots=True)
class Event:
    critical: ClassVar[bool] = False


# ---------------------------------------------------------------- market data
@dataclass(frozen=True, slots=True)
class MarketsUpdated(Event):
    total: int
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BookUpdated(Event):
    asset_id: str
    market_id: str
    ts: float


@dataclass(frozen=True, slots=True)
class TradePrint(Event):
    asset_id: str
    market_id: str
    price: float
    size: float
    side: str
    ts: float


@dataclass(frozen=True, slots=True)
class FeedHealth(Event):
    connected: bool
    subscribed: int
    last_message_age: float
    reconnects: int


@dataclass(frozen=True, slots=True)
class ReferencePrice(Event):
    symbol: str
    price: float
    ts: float


@dataclass(frozen=True, slots=True)
class MarketResolved(Event):
    critical: ClassVar[bool] = True
    market_id: str
    winning_asset_id: str | None
    ts: float


# ---------------------------------------------------------------- scanner / gate
@dataclass(frozen=True, slots=True)
class ScanCompleted(Event):
    opportunities: tuple[Opportunity, ...]
    ts: float
    duration_ms: float
    universe: int


@dataclass(frozen=True, slots=True)
class GateDecision(Event):
    opportunity_key: str
    market_id: str
    decision: str  # enter | skip
    confidence: float
    provider: str
    reason: str
    fair: float | None = None
    cached: bool = False


# ---------------------------------------------------------------- orders / portfolio
@dataclass(frozen=True, slots=True)
class OrderUpdated(Event):
    critical: ClassVar[bool] = True
    order: Order


@dataclass(frozen=True, slots=True)
class FillEvent(Event):
    critical: ClassVar[bool] = True
    fill: Fill


@dataclass(frozen=True, slots=True)
class PortfolioUpdated(Event):
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PnLReport(Event):
    critical: ClassVar[bool] = True
    period: str
    report: dict[str, Any]
    text: str


# ---------------------------------------------------------------- risk / control
@dataclass(frozen=True, slots=True)
class RiskRejected(Event):
    intent: OrderIntent
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RiskBreach(Event):
    critical: ClassVar[bool] = True
    code: str
    message: str
    severity: str  # pause | halt


@dataclass(frozen=True, slots=True)
class TradingStateChanged(Event):
    critical: ClassVar[bool] = True
    state: TradingState
    previous: TradingState
    reason: str
    actor: str
    ts: float


@dataclass(frozen=True, slots=True)
class LimitsChanged(Event):
    critical: ClassVar[bool] = True
    changes: dict[str, Any]
    actor: str


@dataclass(frozen=True, slots=True)
class Alert(Event):
    critical: ClassVar[bool] = True
    level: str  # info | warning | critical
    title: str
    message: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
