"""Live execution through the official ``polymarket-client`` SDK (ADR-0002).

Isolated here because the SDK is 0.x and changes often. Install with ``uv sync --extra live``.
All SDK calls are wrapped so that an exception becomes a REJECTED order, never a crash.

Fill detection uses the authenticated user stream (``UserSpec``): trade events are matched to
our orders by exchange order id (taker side) or ``maker_orders[].order_id`` (maker side).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import structlog

from pmtrader.config import LiveConfig, Secrets
from pmtrader.core.clock import Clock
from pmtrader.core.types import FeeSchedule, Liquidity, Order, OrderStatus, OrderType, Side
from pmtrader.execution.gateway import ExecFill, FillCallback, PlaceResult, StatusCallback
from pmtrader.quant.fees import fee as venue_fee

log = structlog.get_logger(__name__)


def _fmt(x: float, decimals: int = 4) -> str:
    return format(Decimal(str(round(x, decimals))).normalize(), "f")


class PolymarketLiveGateway:
    name = "live"

    def __init__(self, secrets: Secrets, cfg: LiveConfig, clock: Clock, fees_for: Any) -> None:
        self.secrets, self.cfg, self.clock = secrets, cfg, clock
        self._fees_for = fees_for
        self.client: Any = None
        self._by_exchange_id: dict[str, Order] = {}
        self._seen_trades: set[tuple[str, str]] = set()
        # trade events can arrive before place() returns the exchange id: park them briefly
        self._orphans: dict[str, list[Any]] = {}
        self._on_fill: FillCallback = lambda _f: None
        self._on_status: StatusCallback = lambda *_a: None

    def bind(self, on_fill: FillCallback, on_status: StatusCallback) -> None:
        self._on_fill, self._on_status = on_fill, on_status

    async def connect(self) -> None:
        try:
            from polymarket import AsyncSecureClient, RelayerApiKey
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("live mode requires `uv sync --extra live` (polymarket-client)") from exc
        s = self.secrets
        if not s.polymarket_private_key:
            raise RuntimeError("PMT_SECRETS__POLYMARKET_PRIVATE_KEY is not set")
        kwargs: dict[str, Any] = {"private_key": s.polymarket_private_key.get_secret_value()}
        if s.polymarket_wallet:
            kwargs["wallet"] = s.polymarket_wallet
        if s.polymarket_relayer_api_key and s.polymarket_relayer_api_key_address:
            kwargs["api_key"] = RelayerApiKey(
                key=s.polymarket_relayer_api_key.get_secret_value(), address=s.polymarket_relayer_api_key_address
            )
        self.client = await AsyncSecureClient.create(**kwargs)
        log.info("live.connected", wallet=getattr(self.client, "wallet", "?"))

    # ------------------------------------------------------------------ orders
    async def place(self, order: Order) -> PlaceResult:
        if self.client is None:
            return PlaceResult(False, status=OrderStatus.REJECTED, reason="not connected")
        it = order.intent
        common: dict[str, Any] = {"asset_id": it.asset_id, "side": it.side.value}
        if self.cfg.builder_code:
            common["builder_code"] = self.cfg.builder_code
        try:
            if it.order_type in (OrderType.FAK, OrderType.FOK):
                if it.side is Side.BUY:
                    resp = await self.client.place_market_order(
                        **common,
                        amount=_fmt(it.size * it.price, 2),
                        max_price=_fmt(it.price),
                        order_type=it.order_type.value,
                    )
                else:
                    resp = await self.client.place_market_order(
                        **common, shares=_fmt(it.size, 2), min_price=_fmt(it.price), order_type=it.order_type.value
                    )
            else:
                kw: dict[str, Any] = {"price": _fmt(it.price), "size": _fmt(it.size, 2), "post_only": it.post_only}
                if it.expiration:
                    kw["expiration"] = int(it.expiration)
                resp = await self.client.place_limit_order(**common, **kw)
        except Exception as exc:
            log.warning("live.place_failed", error=repr(exc), order=order.id)
            return PlaceResult(False, status=OrderStatus.REJECTED, reason=repr(exc)[:300])
        if not getattr(resp, "ok", False):
            return PlaceResult(
                False,
                status=OrderStatus.REJECTED,
                reason=f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}".strip(),
            )
        exch_id = str(resp.order_id)
        self._by_exchange_id[exch_id] = order
        for payload in self._orphans.pop(exch_id, []):
            self._handle_trade(payload)
        status = str(getattr(resp, "status", "live")).lower()
        mapped = {
            "matched": OrderStatus.OPEN,
            "live": OrderStatus.OPEN,
            "delayed": OrderStatus.OPEN,
            "unmatched": OrderStatus.CANCELLED,
        }.get(status, OrderStatus.OPEN)
        return PlaceResult(True, exch_id, mapped)

    async def cancel(self, order: Order) -> bool:
        if self.client is None or not order.exchange_id:
            return False
        try:
            await self.client.cancel_order(order_id=order.exchange_id)
            return True
        except Exception as exc:
            log.warning("live.cancel_failed", error=repr(exc), order=order.id)
            return False

    async def cancel_all(self) -> int:
        if self.client is None:
            return 0
        for attempt in range(3):
            try:
                await self.client.cancel_all()
                return len(self._by_exchange_id)
            except Exception as exc:
                log.error("live.cancel_all_failed", attempt=attempt, error=repr(exc))
                await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError("cancel_all failed 3 times")

    # ------------------------------------------------------------------ user stream
    def _handle_trade(self, p: Any) -> None:
        if str(getattr(p, "status", "")).upper() in ("FAILED",):
            return
        matched: list[tuple[str, float, float, bool]] = []
        if str(getattr(p, "trader_side", "")).upper() == "TAKER":
            matched.append((str(p.taker_order_id), float(p.price), float(p.size), True))
        for mo in getattr(p, "maker_orders", None) or ():
            matched.append((str(mo.order_id), float(mo.price), float(mo.matched_amount), False))
        for exch_id, price, size, taker in matched:
            order = self._by_exchange_id.get(exch_id)
            key = (str(p.id), exch_id)
            if order is None:
                parked = self._orphans.setdefault(exch_id, [])
                if len(parked) < 20 and len(self._orphans) < 1000:
                    parked.append(p)
                continue
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            fees: FeeSchedule = self._fees_for(order.intent.market_id)
            self._on_fill(
                ExecFill(
                    order.id,
                    price,
                    size,
                    venue_fee(size, price, fees, taker=taker),
                    Liquidity.TAKER if taker else Liquidity.MAKER,
                    self.clock.now(),
                    str(p.id),
                )
            )

    def _handle_order(self, p: Any) -> None:
        order = self._by_exchange_id.get(str(p.id))
        if order is None:
            return
        status = str(getattr(p, "status", "") or "").upper()
        if status in ("CANCELED", "UNMATCHED") or str(getattr(p, "order_event_type", "")) == "CANCELLATION":
            self._on_status(order.id, OrderStatus.CANCELLED, status.lower())

    async def run(self) -> None:
        from polymarket.streams import UserSpec

        while True:
            try:
                async with await self.client.subscribe(UserSpec()) as stream:
                    async for event in stream:
                        if event.type == "trade":
                            self._handle_trade(event.payload)
                        elif event.type == "order":
                            self._handle_order(event.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("live.user_stream_error", error=repr(exc))
                await asyncio.sleep(2)

    async def exchange_open_order_ids(self) -> set[str]:
        ids: set[str] = set()
        async for page in self.client.list_open_orders():
            ids.update(str(o.id) for o in page.items)
        return ids
