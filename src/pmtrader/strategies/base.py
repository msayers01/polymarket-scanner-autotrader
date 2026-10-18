"""Strategy contract and the read-only context strategies see.

``detect`` and ``manage`` must be pure functions of the ``ScanContext`` (no I/O), so strategies
run identically live, in paper trading and in replay/backtests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from pmtrader.config import StrategyConfig
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Market, Opportunity, Order, OrderIntent, OrderType, Position, Side, round_size

if TYPE_CHECKING:
    from pmtrader.marketdata.external.crypto import CryptoReference


@dataclass
class ScanContext:
    now: float
    markets: Mapping[str, Market]  # universe, by market id
    books: Mapping[str, OrderBook]
    market_for_asset: Callable[[str], Market | None]
    event_markets: Callable[[str], list[Market]]
    bankroll: float
    positions: Mapping[str, Position] = field(default_factory=dict)  # by asset id (open only)
    open_orders: Mapping[str, list[Order]] = field(default_factory=dict)  # by asset id
    crypto: CryptoReference | None = None
    stale_after: float = 5.0

    def book(self, asset_id: str) -> OrderBook | None:
        """Fresh book or None (stale books must never drive decisions)."""
        b = self.books.get(asset_id)
        if b is None or b.age(self.now) > self.stale_after:
            return None
        return b

    def days_to_close(self, m: Market) -> float | None:
        s = m.seconds_to_close(self.now)
        return None if s is None else max(s, 0.0) / 86400.0


class Strategy(ABC):
    name: ClassVar[str]
    needs_judgment: ClassVar[bool] = False
    allows_near_expiry: ClassVar[bool] = False

    def __init__(self, cfg: StrategyConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        """Find opportunities in the current snapshot."""

    @abstractmethod
    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        """Turn an approved opportunity into order intents (risk is applied downstream)."""

    def manage(self, positions: Iterable[Position], ctx: ScanContext) -> list[OrderIntent]:
        """Exits / requotes for positions this strategy owns. Default: hold to resolution."""
        return []

    def param(self, key: str, default: float) -> float:
        return float(self.cfg.params.get(key, default))

    # ------------------------------------------------------------- shared helpers
    def taker_intents(self, opp: Opportunity, ctx: ScanContext, sets: float, group: str) -> list[OrderIntent]:
        """FAK buy for each leg at its computed limit price (multi-leg arbs)."""
        out = []
        for asset, side, price in opp.legs:
            m = ctx.market_for_asset(asset)
            if m is None:
                return []
            size = round_size(sets)
            if size < m.min_order_size:
                return []
            out.append(
                OrderIntent(
                    strategy=self.name,
                    market_id=m.id,
                    asset_id=asset,
                    side=side,
                    price=price,
                    size=size,
                    order_type=OrderType.FAK,
                    group_id=group,
                    reason=opp.note or self.name,
                )
            )
        return out


def exit_intent(pos: Position, price: float, reason: str, strategy: str, taker: bool = True) -> OrderIntent:
    return OrderIntent(
        strategy=strategy,
        market_id=pos.market_id,
        asset_id=pos.asset_id,
        side=Side.SELL,
        price=price,
        size=round_size(pos.qty),
        order_type=OrderType.FAK if taker else OrderType.GTC,
        reduce_only=True,
        reason=reason,
        post_only=not taker,
    )
