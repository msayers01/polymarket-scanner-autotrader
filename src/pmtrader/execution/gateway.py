"""Execution port. Implementations: NullGateway (scanner mode), PaperGateway, PolymarketLiveGateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pmtrader.core.types import Liquidity, Order, OrderStatus


@dataclass(frozen=True, slots=True)
class ExecFill:
    order_id: str  # our client order id
    price: float
    size: float
    fee: float
    liquidity: Liquidity
    ts: float
    trade_id: str


@dataclass(frozen=True, slots=True)
class PlaceResult:
    accepted: bool
    exchange_id: str | None = None
    status: OrderStatus = OrderStatus.OPEN
    reason: str = ""


FillCallback = Callable[[ExecFill], None]
StatusCallback = Callable[[str, OrderStatus, str], None]  # (order_id, status, reason)


class ExecutionGateway(Protocol):
    name: str

    def bind(self, on_fill: FillCallback, on_status: StatusCallback) -> None: ...

    async def place(self, order: Order) -> PlaceResult: ...

    async def cancel(self, order: Order) -> bool: ...

    async def cancel_all(self) -> int: ...

    async def run(self) -> None:
        """Background loop (fill simulation / user stream). Returns only on cancel."""
        ...


class NullGateway:
    name = "null"

    def bind(self, on_fill: FillCallback, on_status: StatusCallback) -> None:
        pass

    async def place(self, order: Order) -> PlaceResult:
        return PlaceResult(False, status=OrderStatus.REJECTED, reason="scanner mode")

    async def cancel(self, order: Order) -> bool:
        return True

    async def cancel_all(self) -> int:
        return 0

    async def run(self) -> None:
        import asyncio

        await asyncio.Event().wait()
