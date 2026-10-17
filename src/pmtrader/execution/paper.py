"""Paper exchange that fills against the *live* order book.

Fill model (conservative by design — paper P&L should under-, not over-state reality):
* Taker (FAK/FOK, or marketable GTC without post_only): after ``latency_ms``, walk the current
  opposite book up to the limit price; pay the venue taker fee.
* Maker (resting GTC/GTD): fills only when the opposite best price crosses our limit
  (best ask ≤ our bid / best bid ≥ our ask), or a trade prints *through* our price. No queue
  priority is assumed at the touch. Maker fee 0 (rebates ignored).
* Post-only orders that would cross are rejected (as the venue does).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import structlog

from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import BookUpdated, TradePrint
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import FeeSchedule, Liquidity, Order, OrderStatus, OrderType, Side, new_id
from pmtrader.execution.gateway import ExecFill, FillCallback, PlaceResult, StatusCallback
from pmtrader.quant.fees import fee as venue_fee
from pmtrader.quant.microstructure import walk_book

log = structlog.get_logger(__name__)


class PaperGateway:
    name = "paper"

    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        book: Callable[[str], OrderBook | None],
        fees_for: Callable[[str], FeeSchedule],
        latency_ms: int = 150,
    ) -> None:
        self.bus, self.clock = bus, clock
        self._book = book
        self._fees_for = fees_for
        self.latency_s = latency_ms / 1000.0
        self.resting: dict[str, Order] = {}
        self._on_fill: FillCallback = lambda _f: None
        self._on_status: StatusCallback = lambda *_a: None

    def bind(self, on_fill: FillCallback, on_status: StatusCallback) -> None:
        self._on_fill, self._on_status = on_fill, on_status

    # ------------------------------------------------------------------ placement
    async def place(self, order: Order) -> PlaceResult:
        it = order.intent
        book = self._book(it.asset_id)
        if book is None:
            return PlaceResult(False, status=OrderStatus.REJECTED, reason="no book")
        crosses = self._crosses(it.side, it.price, book)
        if it.post_only and crosses:
            return PlaceResult(False, status=OrderStatus.REJECTED, reason="post-only would cross")
        exch_id = new_id("paper_")
        if it.order_type in (OrderType.FAK, OrderType.FOK) or crosses:
            if self.latency_s > 0:
                await asyncio.sleep(self.latency_s)
            book = self._book(it.asset_id) or book
            levels = book.asks() if it.side is Side.BUY else book.bids()
            w = walk_book(levels, order.remaining, limit=it.price, is_buy=it.side is Side.BUY)
            if it.order_type is OrderType.FOK and w.filled + 1e-9 < order.remaining:
                return PlaceResult(True, exch_id, OrderStatus.CANCELLED, "FOK not fully fillable")
            if w.filled > 0:
                self._emit_fill(order, w.vwap, w.filled, taker=True)
            if it.order_type in (OrderType.FAK, OrderType.FOK):
                status = OrderStatus.FILLED if w.filled + 1e-9 >= order.intent.size else OrderStatus.CANCELLED
                return PlaceResult(True, exch_id, status, "" if w.filled else "no liquidity at limit")
        self.resting[order.id] = order
        return PlaceResult(True, exch_id, OrderStatus.OPEN)

    @staticmethod
    def _crosses(side: Side, price: float, book: OrderBook) -> bool:
        if side is Side.BUY:
            return book.best_ask is not None and book.best_ask <= price + 1e-12
        return book.best_bid is not None and book.best_bid >= price - 1e-12

    def _emit_fill(self, order: Order, price: float, size: float, taker: bool) -> None:
        fees = self._fees_for(order.intent.market_id)
        self._on_fill(
            ExecFill(
                order_id=order.id,
                price=price,
                size=size,
                fee=venue_fee(size, price, fees, taker=taker),
                liquidity=Liquidity.TAKER if taker else Liquidity.MAKER,
                ts=self.clock.now(),
                trade_id=new_id("pt_"),
            )
        )

    async def cancel(self, order: Order) -> bool:
        self.resting.pop(order.id, None)
        return True

    async def cancel_all(self) -> int:
        n = len(self.resting)
        self.resting.clear()
        return n

    # ------------------------------------------------------------------ resting-order simulation
    def _check_resting(self, asset_id: str, trade_px: float | None = None, trade_size: float = 0.0) -> None:
        book = self._book(asset_id)
        now = self.clock.now()
        for oid, order in list(self.resting.items()):
            it = order.intent
            if it.asset_id != asset_id:
                continue
            if not order.is_open:
                self.resting.pop(oid, None)
                continue
            if it.expiration and now >= it.expiration:
                self.resting.pop(oid, None)
                self._on_status(oid, OrderStatus.EXPIRED, "expired")
                continue
            fill_size = 0.0
            if book is not None and self._crosses(it.side, it.price, book):
                lvls = book.asks() if it.side is Side.BUY else book.bids()
                fill_size = walk_book(lvls, order.remaining, limit=it.price, is_buy=it.side is Side.BUY).filled
            elif trade_px is not None and (
                (it.side is Side.BUY and trade_px < it.price) or (it.side is Side.SELL and trade_px > it.price)
            ):
                fill_size = min(order.remaining, trade_size)
            if fill_size > 0:
                self._emit_fill(order, it.price, fill_size, taker=False)
                if order.remaining <= 1e-9:
                    self.resting.pop(oid, None)

    async def run(self) -> None:
        sub = self.bus.subscribe(BookUpdated, TradePrint, maxsize=50_000, name="paper-gateway")
        try:
            async for ev in sub:
                if not self.resting:
                    continue
                if isinstance(ev, BookUpdated):
                    self._check_resting(ev.asset_id)
                elif isinstance(ev, TradePrint):
                    self._check_resting(ev.asset_id, ev.price, ev.size)
        finally:
            sub.close()
