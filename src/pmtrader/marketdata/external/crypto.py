"""Crypto reference data: Binance spot (the resolution source), realized vol, Deribit DVOL, candle opens."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from pmtrader.config import ExternalConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import ReferencePrice
from pmtrader.quant.volatility import blend_vol, parkinson_vol, seasonal_profile, window_variance_multiplier

log = structlog.get_logger(__name__)

DVOL_CURRENCIES = {"bitcoin": "BTC", "ethereum": "ETH"}


@dataclass(slots=True)
class RefState:
    spot: float | None = None
    spot_ts: float = 0.0
    realized_vol: float | None = None  # 7-day hourly Parkinson (a full weekly cycle ⇒ de-seasonalised)
    implied_vol: float | None = None  # Deribit DVOL (30-day implied)
    vol_ts: float = 0.0
    hod: list[float] = field(default_factory=lambda: [1.0] * 24)
    wk: list[float] = field(default_factory=lambda: [1.0, 1.0])

    def vol(self, w_implied: float = 0.5) -> float | None:
        if self.realized_vol is None and self.implied_vol is None:
            return None
        return blend_vol(self.realized_vol or 0.0, self.implied_vol, w_implied)


class CryptoReference:
    def __init__(self, bus: EventBus, clock: Clock, cfg: ExternalConfig, client: httpx.AsyncClient | None = None):
        self.bus, self.clock, self.cfg = bus, clock, cfg
        self._http = client or httpx.AsyncClient(timeout=10.0)
        self.state: dict[str, RefState] = {a: RefState() for a in cfg.symbols}
        self._opens: dict[tuple[str, int], float] = {}
        self._open_requests: set[tuple[str, int]] = set()
        self.enabled_assets: set[str] = set(cfg.symbols)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ sync accessors (used by strategies)
    def spot(self, asset: str, max_age: float = 10.0) -> float | None:
        st = self.state.get(asset)
        if st is None or st.spot is None or self.clock.now() - st.spot_ts > max_age:
            return None
        return st.spot

    def vol(self, asset: str) -> float | None:
        """Base (week-average) annualised volatility."""
        st = self.state.get(asset)
        return st.vol() if st else None

    def vol_for_window(self, asset: str, start: float, end: float) -> float | None:
        """Annualised σ appropriate for [start, end]: base σ scaled by the seasonal variance profile."""
        st = self.state.get(asset)
        base = st.vol() if st else None
        if st is None or base is None:
            return None
        return base * math.sqrt(window_variance_multiplier(st.hod, st.wk, start, end))

    def open_at(self, asset: str, ts: float) -> float | None:
        """Open price of the 1m candle starting at ``ts`` (None until fetched; triggers a fetch)."""
        key = (asset, int(ts // 60) * 60)
        if key in self._opens:
            return self._opens[key]
        if ts <= self.clock.now():
            self._open_requests.add(key)
        return None

    # ------------------------------------------------------------ fetchers
    async def _binance(self, path: str, params: dict[str, Any]) -> Any:
        r = await self._http.get(f"{self.cfg.binance_base_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def refresh_spot(self) -> None:
        syms = {self.cfg.symbols[a]: a for a in self.enabled_assets}
        data = await self._binance("/api/v3/ticker/price", {"symbols": json.dumps(list(syms), separators=(",", ":"))})
        now = self.clock.now()
        for row in data:
            asset = syms.get(row["symbol"])
            if asset:
                st = self.state[asset]
                st.spot, st.spot_ts = float(row["price"]), now
                self.bus.publish(ReferencePrice(asset, st.spot, now))

    async def refresh_vol(self) -> None:
        end_ms = int(time.time() * 1000)
        for asset in self.enabled_assets:
            sym = self.cfg.symbols[asset]
            try:
                kl = await self._binance("/api/v3/klines", {"symbol": sym, "interval": "1h", "limit": 1000})
                ts = [int(k[0]) / 1000 for k in kl]
                highs, lows = [float(k[2]) for k in kl], [float(k[3]) for k in kl]
                st = self.state[asset]
                st.realized_vol = parkinson_vol(highs[-168:], lows[-168:], 8760.0)
                st.hod, st.wk = seasonal_profile(ts, highs, lows)
                cur = DVOL_CURRENCIES.get(asset)
                if cur:
                    r = await self._http.get(
                        f"{self.cfg.deribit_base_url}/api/v2/public/get_volatility_index_data",
                        params={
                            "currency": cur,
                            "start_timestamp": end_ms - 3 * 3600_000,
                            "end_timestamp": end_ms,
                            "resolution": "60",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json().get("result", {}).get("data", [])
                    if rows:
                        self.state[asset].implied_vol = float(rows[-1][4]) / 100.0
                self.state[asset].vol_ts = self.clock.now()
            except Exception as exc:
                log.warning("crypto.vol_failed", asset=asset, error=repr(exc))

    async def _fetch_opens(self) -> None:
        reqs, self._open_requests = self._open_requests, set()
        for asset, ts in reqs:
            try:
                kl = await self._binance(
                    "/api/v3/klines",
                    {"symbol": self.cfg.symbols[asset], "interval": "1m", "startTime": ts * 1000, "limit": 1},
                )
                if kl and int(kl[0][0]) // 1000 == ts:
                    self._opens[(asset, ts)] = float(kl[0][1])
            except Exception as exc:
                log.warning("crypto.open_failed", asset=asset, ts=ts, error=repr(exc))
                self._open_requests.add((asset, ts))

    async def run(self) -> None:
        last_vol = 0.0
        while True:
            try:
                await self.refresh_spot()
                if self.clock.now() - last_vol > self.cfg.vol_refresh_seconds:
                    await self.refresh_vol()
                    last_vol = self.clock.now()
                if self._open_requests:
                    await self._fetch_opens()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("crypto.refresh_failed", error=repr(exc))
            await self.clock.sleep(self.cfg.refresh_seconds)
