"""Domain types shared by every layer.

Conventions (see docs/adr/0003):
* prices are probabilities in [0, 1] as ``float``; rounded to tick only at the order boundary
* sizes are shares; cash is USDC
* timestamps are epoch seconds (``float``) from ``core.clock``
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- enums


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    GTC = "GTC"  # resting limit
    GTD = "GTD"  # resting limit with expiry
    FAK = "FAK"  # fill-and-kill (IOC)
    FOK = "FOK"  # fill-or-kill


class OrderStatus(StrEnum):
    NEW = "NEW"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


class TradingState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"


class Mode(StrEnum):
    SCANNER = "scanner"
    PAPER = "paper"
    LIVE = "live"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


# --------------------------------------------------------------------------- rounding helpers

_EPS = 1e-9


def round_price(price: float, tick: float, side: Side | None = None) -> float:
    """Snap ``price`` to the tick grid.

    BUY rounds down and SELL rounds up (never pay more / receive less than intended);
    ``side=None`` rounds to nearest. The result is clamped to [tick, 1 - tick].
    """
    steps = price / tick
    if side is Side.BUY:
        n = math.floor(steps + _EPS)
    elif side is Side.SELL:
        n = math.ceil(steps - _EPS)
    else:
        n = round(steps)
    max_n = round(1.0 / tick) - 1
    n = min(max(n, 1), max_n)
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(n * tick, decimals)


def is_on_tick(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6


def round_size(size: float) -> float:
    """Floor share size to 0.01."""
    return math.floor(size * 100 + _EPS) / 100


def round_usd(x: float) -> float:
    return round(x, 6)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- market reference data


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Venue taker-fee curve: fee = C · rate · (p(1-p))^exponent (see quant/fees.py)."""

    rate: float = 0.0
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.rate > 0


@dataclass(frozen=True, slots=True)
class RewardsConfig:
    min_size: float
    max_spread_cents: float
    daily_rate_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str
    asset_id: str  # CLOB token id (v1/CTF) or position id (v2)


@dataclass(frozen=True, slots=True)
class EventRef:
    id: str
    title: str
    slug: str
    neg_risk: bool = False
    neg_risk_augmented: bool = False


@dataclass(frozen=True, slots=True)
class Market:
    id: str
    condition_id: str
    question: str
    slug: str
    outcomes: tuple[Outcome, ...]
    tick_size: float = 0.01
    min_order_size: float = 5.0
    end_time: float | None = None
    neg_risk: bool = False
    neg_risk_other: bool = False
    accepting_orders: bool = True
    active: bool = True
    closed: bool = False
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    rewards: RewardsConfig | None = None
    event: EventRef | None = None
    category: str = ""
    tags: tuple[str, ...] = ()
    game_start_time: float | None = None
    resolution_source: str = ""
    description: str = ""
    version: str = "v1"
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    one_hour_change: float | None = None
    one_day_change: float | None = None

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2

    @property
    def yes(self) -> Outcome:
        return self.outcomes[0]

    @property
    def no(self) -> Outcome:
        return self.outcomes[1]

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(o.asset_id for o in self.outcomes)

    def outcome_for(self, asset_id: str) -> Outcome | None:
        for o in self.outcomes:
            if o.asset_id == asset_id:
                return o
        return None

    def complement(self, asset_id: str) -> str | None:
        """The other token of a binary market."""
        if not self.is_binary:
            return None
        a, b = self.asset_ids
        return b if asset_id == a else a if asset_id == b else None

    def seconds_to_close(self, now: float) -> float | None:
        return None if self.end_time is None else self.end_time - now

    @property
    def event_id(self) -> str:
        return self.event.id if self.event else self.id


# --------------------------------------------------------------------------- trading objects


@dataclass(slots=True)
class Opportunity:
    """A scanner finding. Produced by Strategy.detect(); pure data."""

    strategy: str
    market_id: str
    question: str
    legs: tuple[tuple[str, Side, float], ...]  # (asset_id, side, executable price incl. depth)
    fair: float | None  # fair probability of the primary leg's outcome (None for pure arb)
    edge: float  # net edge per share-set in probability points (after fees)
    size_hint: float  # shares (per leg) the strategy would take at current depth
    capital_per_unit: float  # USDC locked per share-set
    days_to_close: float | None
    needs_judgment: bool
    confidence: float = 1.0
    event_id: str = ""
    category: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    id: str = field(default_factory=lambda: new_id("op_"))

    @property
    def ev_usd(self) -> float:
        return self.edge * self.size_hint

    @property
    def roi(self) -> float:
        return self.edge / self.capital_per_unit if self.capital_per_unit > 0 else 0.0

    @property
    def score(self) -> float:
        """Annualised net return, capped (docs/STRATEGIES.md §0.2)."""
        days = max(self.days_to_close if self.days_to_close is not None else 30.0, 0.25)
        return min(self.roi * 365.0 / days, 50.0) * self.confidence

    @property
    def key(self) -> str:
        legs = ",".join(f"{a[:10]}:{s.value}" for a, s, _ in self.legs)
        return f"{self.strategy}|{self.market_id}|{legs}"


@dataclass(slots=True)
class OrderIntent:
    strategy: str
    market_id: str
    asset_id: str
    side: Side
    price: float
    size: float
    order_type: OrderType = OrderType.GTC
    post_only: bool = False
    reduce_only: bool = False
    group_id: str | None = None  # multi-leg grouping (arb legs)
    reason: str = ""
    expiration: float | None = None
    source: str = "strategy"  # strategy | manual | flatten

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(slots=True)
class Order:
    intent: OrderIntent
    id: str = field(default_factory=lambda: new_id("o_"))
    exchange_id: str | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    fees_paid: float = 0.0
    created_ts: float = 0.0
    updated_ts: float = 0.0
    reject_reason: str = ""

    @property
    def remaining(self) -> float:
        return max(self.intent.size - self.filled_size, 0.0)

    @property
    def is_open(self) -> bool:
        return not self.status.is_terminal


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    market_id: str
    asset_id: str
    side: Side
    price: float
    size: float
    fee: float
    ts: float
    liquidity: Liquidity
    strategy: str
    trade_id: str = field(default_factory=lambda: new_id("f_"))

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(slots=True)
class Position:
    asset_id: str
    market_id: str
    outcome: str
    event_id: str
    strategy: str
    qty: float = 0.0
    avg_cost: float = 0.0  # USDC per share, excluding fees
    realized_pnl: float = 0.0  # net of fees
    fees_paid: float = 0.0
    opened_ts: float = 0.0
    updated_ts: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_cost

    @property
    def is_open(self) -> bool:
        return self.qty > 1e-9
