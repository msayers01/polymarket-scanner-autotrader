"""L2 order book for one outcome token, maintained from CLOB WS snapshots + level updates."""

from __future__ import annotations

from collections.abc import Iterable

Level = tuple[float, float]  # (price, size)


class OrderBook:
    """Price-level book. ``bids()`` is sorted descending, ``asks()`` ascending.

    Level updates carry the *new total size* at a price (0 removes the level), matching the
    Polymarket ``price_change`` semantics.
    """

    __slots__ = ("_ask_cache", "_asks", "_bid_cache", "_bids", "asset_id", "hash", "market", "recv_ts", "ts")

    def __init__(self, asset_id: str, market: str = "") -> None:
        self.asset_id = asset_id
        self.market = market
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._bid_cache: list[Level] | None = None
        self._ask_cache: list[Level] | None = None
        self.ts = 0.0  # exchange timestamp (s)
        self.recv_ts = 0.0  # local receive time (s)
        self.hash = ""

    # ------------------------------------------------------------------ mutation
    def apply_snapshot(
        self, bids: Iterable[Level], asks: Iterable[Level], ts: float, recv_ts: float, hash_: str = ""
    ) -> None:
        self._bids = {p: s for p, s in bids if s > 0}
        self._asks = {p: s for p, s in asks if s > 0}
        self._bid_cache = self._ask_cache = None
        self.ts, self.recv_ts, self.hash = ts, recv_ts, hash_

    def apply_level(self, side: str, price: float, size: float, ts: float, recv_ts: float) -> None:
        book = self._bids if side.upper() in ("BUY", "BID", "BIDS") else self._asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        if book is self._bids:
            self._bid_cache = None
        else:
            self._ask_cache = None
        self.ts, self.recv_ts = max(self.ts, ts), recv_ts

    # ------------------------------------------------------------------ views
    def bids(self) -> list[Level]:
        if self._bid_cache is None:
            self._bid_cache = sorted(self._bids.items(), key=lambda x: -x[0])
        return self._bid_cache

    def asks(self) -> list[Level]:
        if self._ask_cache is None:
            self._ask_cache = sorted(self._asks.items(), key=lambda x: x[0])
        return self._ask_cache

    @property
    def best_bid(self) -> float | None:
        b = self.bids()
        return b[0][0] if b else None

    @property
    def best_ask(self) -> float | None:
        a = self.asks()
        return a[0][0] if a else None

    @property
    def best_bid_size(self) -> float:
        b = self.bids()
        return b[0][1] if b else 0.0

    @property
    def best_ask_size(self) -> float:
        a = self.asks()
        return a[0][1] if a else 0.0

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb

    @property
    def is_two_sided(self) -> bool:
        return bool(self._bids) and bool(self._asks)

    def age(self, now: float) -> float:
        return now - self.recv_ts if self.recv_ts else float("inf")

    def depth_usd(self, side: str, within: float) -> float:
        """USDC notional resting within ``within`` (probability points) of the touch."""
        if side.upper() in ("BUY", "BID", "BIDS"):
            levels, touch = self.bids(), self.best_bid
            if touch is None:
                return 0.0
            return sum(p * s for p, s in levels if touch - p <= within + 1e-12)
        levels, touch = self.asks(), self.best_ask
        if touch is None:
            return 0.0
        return sum(p * s for p, s in levels if p - touch <= within + 1e-12)

    def snapshot(self, depth: int = 10) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "bids": self.bids()[:depth],
            "asks": self.asks()[:depth],
            "ts": self.ts,
        }
