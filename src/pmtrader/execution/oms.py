"""OrderManager: single owner of order state. Every order passes RiskEngine.check() first."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import structlog

from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import Alert, FillEvent, OrderUpdated, RiskRejected
from pmtrader.core.types import Fill, Market, Order, OrderIntent, OrderStatus, OrderType, round_size
from pmtrader.execution.gateway import ExecFill, ExecutionGateway
from pmtrader.persistence.repository import Repository
from pmtrader.risk.engine import RiskDecision, RiskEngine

log = structlog.get_logger(__name__)


class OrderManager:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        gateway: ExecutionGateway,
        risk: RiskEngine,
        repo: Repository | None,
        market: Callable[[str], Market | None],
    ) -> None:
        self.bus, self.clock, self.gateway, self.risk, self.repo = bus, clock, gateway, risk, repo
        self._market = market
        self.orders: dict[str, Order] = {}
        self._persist_q: asyncio.Queue[Order] = asyncio.Queue()
        gateway.bind(self._on_fill, self._on_status)

    # ------------------------------------------------------------------ queries
    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open]

    def open_orders_by_asset(self) -> dict[str, list[Order]]:
        out: dict[str, list[Order]] = {}
        for o in self.open_orders():
            out.setdefault(o.intent.asset_id, []).append(o)
        return out

    # ------------------------------------------------------------------ submission
    async def submit(self, intent: OrderIntent) -> tuple[Order | None, RiskDecision]:
        decision = self.risk.check(intent)
        if not decision.ok:
            self._rejected(intent, decision)
            return None, decision
        return await self._send(intent), decision

    async def submit_group(self, intents: list[OrderIntent]) -> tuple[list[Order], RiskDecision]:
        """Multi-leg (arb) submission: all legs pass risk together; legs after the first are sized to
        the first leg's fill to limit leg risk. A shortfall raises an alert (residual position)."""
        decision = self.risk.check_group(intents)
        if not decision.ok:
            self._rejected(intents[0], decision)
            return [], decision
        orders: list[Order] = []
        target = intents[0].size
        for i, it in enumerate(intents):
            if i > 0:
                it.size = round_size(min(it.size, target))
                m = self._market(it.market_id)
                if m and it.size < m.min_order_size:
                    self.bus.publish(
                        Alert(
                            "warning",
                            "Arb leg shortfall",
                            f"{it.strategy}: leg {i} skipped; residual exposure from earlier legs",
                            {"group": it.group_id},
                        )
                    )
                    break
            o = await self._send(it)
            orders.append(o)
            if it.order_type in (OrderType.FAK, OrderType.FOK):
                target = min(target, o.filled_size)
                if target <= 0:
                    if i > 0:
                        self.bus.publish(
                            Alert(
                                "warning",
                                "Arb leg unfilled",
                                f"{it.strategy}: leg {i} got no fill; residual exposure",
                                {"group": it.group_id},
                            )
                        )
                    break
        return orders, decision

    async def _send(self, intent: OrderIntent) -> Order:
        now = self.clock.now()
        order = Order(intent=intent, status=OrderStatus.SUBMITTING, created_ts=now, updated_ts=now)
        self.orders[order.id] = order
        m = self._market(intent.market_id)
        self.risk.on_submitted(intent, m.event_id if m else intent.market_id)
        log.info(
            "order.submitted",
            order=order.id,
            strategy=intent.strategy,
            side=intent.side.value,
            price=intent.price,
            size=intent.size,
            type=intent.order_type.value,
            market=intent.market_id,
            reason=intent.reason,
        )
        res = await self.gateway.place(order)
        self.risk.on_exchange_result(res.accepted)
        order.exchange_id = res.exchange_id
        if not res.accepted:
            order.reject_reason = res.reason
            self._set_status(order, OrderStatus.REJECTED)
        elif order.status is OrderStatus.SUBMITTING or not order.status.is_terminal:
            # fills may already have arrived synchronously during place()
            if res.status.is_terminal:
                self._set_status(order, OrderStatus.FILLED if order.remaining <= 1e-9 else res.status)
            elif order.status is OrderStatus.SUBMITTING:
                self._set_status(order, OrderStatus.PARTIALLY_FILLED if order.filled_size > 0 else OrderStatus.OPEN)
        return order

    def _rejected(self, intent: OrderIntent, d: RiskDecision) -> None:
        log.info("risk.reject", code=d.code, msg=d.message, strategy=intent.strategy, market=intent.market_id)
        self.bus.publish(RiskRejected(intent, d.code, d.message))

    # ------------------------------------------------------------------ cancels
    async def cancel(self, order_id: str) -> bool:
        o = self.orders.get(order_id)
        if o is None or not o.is_open:
            return False
        ok = await self.gateway.cancel(o)
        if ok:
            self._set_status(o, OrderStatus.CANCELLED)
        return ok

    async def cancel_where(self, pred: Callable[[Order], bool]) -> int:
        n = 0
        for o in [o for o in self.open_orders() if pred(o)]:
            n += int(await self.cancel(o.id))
        return n

    async def cancel_all(self) -> int:
        n = await self.gateway.cancel_all()
        for o in self.open_orders():
            self._set_status(o, OrderStatus.CANCELLED)
        return n

    # ------------------------------------------------------------------ gateway callbacks
    def _on_fill(self, ef: ExecFill) -> None:
        o = self.orders.get(ef.order_id)
        if o is None:
            log.warning("fill.unknown_order", order=ef.order_id)
            return
        size = min(ef.size, o.remaining) if o.remaining > 0 else ef.size
        total = o.filled_size + size
        o.avg_fill_price = (o.avg_fill_price * o.filled_size + ef.price * size) / total if total else 0.0
        o.filled_size = total
        o.fees_paid += ef.fee
        it = o.intent
        fill = Fill(
            o.id,
            it.market_id,
            it.asset_id,
            it.side,
            ef.price,
            size,
            ef.fee,
            ef.ts,
            ef.liquidity,
            it.strategy,
            ef.trade_id,
        )
        log.info(
            "fill",
            order=o.id,
            strategy=it.strategy,
            side=it.side.value,
            price=ef.price,
            size=size,
            fee=ef.fee,
            liquidity=ef.liquidity.value,
            market=it.market_id,
        )
        self.bus.publish(FillEvent(fill))
        if o.status is not OrderStatus.SUBMITTING:
            self._set_status(o, OrderStatus.FILLED if o.remaining <= 1e-9 else OrderStatus.PARTIALLY_FILLED)

    def _on_status(self, order_id: str, status: OrderStatus, reason: str) -> None:
        o = self.orders.get(order_id)
        if o is not None and o.is_open:
            o.reject_reason = reason or o.reject_reason
            self._set_status(o, status)

    def _set_status(self, o: Order, status: OrderStatus) -> None:
        if o.status is status:
            return
        o.status = status
        o.updated_ts = self.clock.now()
        self.bus.publish(OrderUpdated(o))
        self._persist_q.put_nowait(o)
        if status.is_terminal and len(self.orders) > 5000:
            # keep memory bounded: drop old terminal orders
            for oid in [k for k, v in self.orders.items() if v.status.is_terminal][:1000]:
                self.orders.pop(oid, None)

    async def run_persistence(self) -> None:
        """Writes order state changes to the DB off the hot path."""
        while True:
            o = await self._persist_q.get()
            if self.repo is not None:
                try:
                    await self.repo.upsert_order(o)
                except Exception:
                    log.exception("order.persist_failed", order=o.id)
