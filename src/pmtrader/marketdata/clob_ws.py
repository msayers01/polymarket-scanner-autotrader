"""CLOB market-channel WebSocket → in-memory L2 books (protocol: docs/POLYMARKET_API.md).

The service shards subscriptions across connections, reconnects with backoff, heartbeats with
``PING`` every 10 s, and publishes ``BookUpdated`` / ``TradePrint`` / ``MarketResolved`` /
``FeedHealth`` on the bus. Books are snapshotted by the server on (re)subscribe.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import BookUpdated, FeedHealth, MarketResolved, TradePrint
from pmtrader.core.orderbook import OrderBook

log = structlog.get_logger(__name__)


def _levels(raw: list[dict[str, Any]] | None) -> list[tuple[float, float]]:
    return [(float(x["price"]), float(x["size"])) for x in raw or []]


def _ms(v: Any, fallback: float) -> float:
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return fallback


class _Shard:
    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.assets: set[str] = set()
        self.ws: ClientConnection | None = None
        self.task: asyncio.Task[None] | None = None
        self.last_rx = 0.0  # any frame incl. PONG — proves the books on this connection are current

    @property
    def connected(self) -> bool:
        return self.ws is not None


class MarketDataService:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        url: str,
        ping_seconds: float = 10.0,
        max_assets_per_conn: int = 400,
        asset_to_market: Callable[[str], str | None] | None = None,
        raw_sink: Callable[[float, str | bytes], None] | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.url = url
        self.ping_seconds = ping_seconds
        self.max_per_conn = max_assets_per_conn
        self.books: dict[str, OrderBook] = {}
        self.tick_overrides: dict[str, float] = {}
        self._asset_to_market = asset_to_market or (lambda _a: None)
        self._raw_sink = raw_sink  # recorder hook (backtest/recorder.py)
        self._shards: list[_Shard] = []
        self._running = False
        self.last_message_ts = 0.0
        self.reconnects = 0
        self.messages = 0

    # ------------------------------------------------------------------ public API
    def book(self, asset_id: str) -> OrderBook | None:
        return self.books.get(asset_id)

    @property
    def subscribed(self) -> set[str]:
        return set().union(*(s.assets for s in self._shards)) if self._shards else set()

    async def set_universe(self, asset_ids: set[str]) -> None:
        """Subscribe to exactly ``asset_ids`` (diffed against the current subscription)."""
        current = self.subscribed
        removed, added = current - asset_ids, asset_ids - current
        for shard in self._shards:
            gone = shard.assets & removed
            if gone:
                shard.assets -= gone
                await self._send(shard, {"assets_ids": sorted(gone), "operation": "unsubscribe"})
                for a in gone:
                    self.books.pop(a, None)
        pending = sorted(added)
        for shard in self._shards:
            room = self.max_per_conn - len(shard.assets)
            if room <= 0 or not pending:
                continue
            chunk, pending = pending[:room], pending[room:]
            shard.assets |= set(chunk)
            await self._send(shard, {"assets_ids": chunk, "operation": "subscribe"})
        while pending:
            shard = _Shard(len(self._shards))
            chunk, pending = pending[: self.max_per_conn], pending[self.max_per_conn :]
            shard.assets = set(chunk)
            self._shards.append(shard)
            if self._running:
                shard.task = asyncio.create_task(self._run_shard(shard), name=f"ws-shard-{shard.idx}")
        if added or removed:
            log.info("feed.universe", added=len(added), removed=len(removed), total=len(self.subscribed))

    async def run(self) -> None:
        """Run until cancelled (supervised by the engine)."""
        self._running = True
        for shard in self._shards:
            if shard.task is None or shard.task.done():
                shard.task = asyncio.create_task(self._run_shard(shard), name=f"ws-shard-{shard.idx}")
        try:
            while True:
                await asyncio.sleep(1.0)
                now = self.clock.now()
                self.bus.publish(
                    FeedHealth(
                        connected=any(s.ws is not None for s in self._shards) or not self._shards,
                        subscribed=len(self.subscribed),
                        last_message_age=now - self.last_message_ts if self.last_message_ts else float("inf"),
                        reconnects=self.reconnects,
                    )
                )
        finally:
            self._running = False
            for s in self._shards:
                if s.task:
                    s.task.cancel()

    # ------------------------------------------------------------------ internals
    async def _send(self, shard: _Shard, msg: dict[str, Any]) -> None:
        if shard.ws is None:
            return  # will be included in the initial subscription on (re)connect
        with contextlib.suppress(websockets.ConnectionClosed):
            await shard.ws.send(json.dumps(msg))

    async def _run_shard(self, shard: _Shard) -> None:
        backoff = 1.0
        while self._running:
            if not shard.assets:
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=2**24, open_timeout=15) as ws:
                    shard.ws = ws
                    await ws.send(
                        json.dumps(
                            {"assets_ids": sorted(shard.assets), "type": "market", "custom_feature_enabled": True}
                        )
                    )
                    log.info("feed.connected", shard=shard.idx, assets=len(shard.assets))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            shard.last_rx = self.clock.now()
                            self._on_message(raw, shard)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("feed.disconnected", shard=shard.idx, error=repr(exc))
            finally:
                shard.ws = None
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _pinger(self, ws: ClientConnection) -> None:
        with contextlib.suppress(websockets.ConnectionClosed, asyncio.CancelledError):
            while True:
                await asyncio.sleep(self.ping_seconds)
                await ws.send("PING")

    def _on_message(self, raw: str | bytes, shard: _Shard | None = None) -> None:
        if raw in ("PONG", b"PONG"):
            return
        recv = self.clock.now()
        if self._raw_sink is not None:
            self._raw_sink(recv, raw)
        self.last_message_ts = self.clock.now()
        self.messages += 1
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        msgs = data if isinstance(data, list) else [data]
        touched: dict[str, str] = {}
        for msg in msgs:
            if isinstance(msg, dict):
                self.handle(msg, recv, touched, shard)
        now = self.clock.now()
        for asset, market in touched.items():
            self.bus.publish(BookUpdated(asset, market, now))

    def _get_book(self, asset: str, market: str) -> OrderBook:
        b = self.books.get(asset)
        if b is None:
            b = self.books[asset] = OrderBook(asset, market)
        return b

    def handle(self, msg: dict[str, Any], recv: float, touched: dict[str, str], shard: _Shard | None = None) -> None:
        """Apply one protocol message (public for tests and replay)."""
        et = msg.get("event_type")
        market = str(msg.get("market", ""))
        ts = _ms(msg.get("timestamp"), recv)
        if et == "book":
            asset = str(msg["asset_id"])
            book = self._get_book(asset, market)
            if shard is not None:
                book.link = shard
            book.apply_snapshot(_levels(msg.get("bids")), _levels(msg.get("asks")), ts, recv, str(msg.get("hash", "")))
            touched[asset] = self._asset_to_market(asset) or market
        elif et == "price_change":
            for ch in msg.get("price_changes") or []:
                asset = str(ch.get("asset_id"))
                lvl_book = self.books.get(asset)
                if lvl_book is None:
                    continue
                lvl_book.apply_level(str(ch.get("side")), float(ch["price"]), float(ch["size"]), ts, recv)
                touched[asset] = self._asset_to_market(asset) or market
        elif et == "last_trade_price":
            asset = str(msg.get("asset_id"))
            self.bus.publish(
                TradePrint(
                    asset,
                    self._asset_to_market(asset) or market,
                    float(msg.get("price", 0)),
                    float(msg.get("size", 0)),
                    str(msg.get("side", "")),
                    ts,
                )
            )
        elif et == "tick_size_change":
            self.tick_overrides[str(msg.get("asset_id"))] = float(msg.get("new_tick_size", 0.01))
        elif et == "market_resolved":
            self.bus.publish(MarketResolved(str(msg.get("id") or market), msg.get("winning_asset_id"), ts))
