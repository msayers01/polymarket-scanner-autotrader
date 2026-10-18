"""Gamma REST client (market/event catalog) and parsing into domain ``Market`` objects."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import structlog

from pmtrader.core.types import EventRef, FeeSchedule, Market, Outcome, RewardsConfig

log = structlog.get_logger(__name__)


def _f(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v: Any) -> float | None:
    if not v:
        return None
    s = str(v).strip().replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _jlist(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_market(m: dict[str, Any], event: dict[str, Any] | None = None) -> Market | None:
    """Parse one Gamma market dict. Returns None for markets we cannot trade (no order book / ids)."""
    names = _jlist(m.get("outcomes"))
    version = str(m.get("version") or "v1")
    ids = _jlist(m.get("positionIds")) if version == "v2" else _jlist(m.get("clobTokenIds"))
    if not ids:
        ids = _jlist(m.get("clobTokenIds"))
    if not names or len(names) != len(ids) or not m.get("enableOrderBook", True):
        return None

    ev = event or ((m.get("events") or [None])[0])
    ev_ref = None
    tags: tuple[str, ...] = ()
    if ev:
        ev_ref = EventRef(
            id=str(ev.get("id", "")),
            title=str(ev.get("title", "")),
            slug=str(ev.get("slug", "")),
            neg_risk=bool(ev.get("negRisk") or ev.get("enableNegRisk")),
            neg_risk_augmented=bool(ev.get("negRiskAugmented")),
            basket_complete=bool(ev.get("_basket_complete", False)),
            n_open_markets=int(ev.get("_n_open", 0)),
        )
        tags = tuple(str(t.get("slug", "")) for t in (ev.get("tags") or []) if isinstance(t, dict))

    fs = m.get("feeSchedule") or {}
    fees = (
        FeeSchedule(
            rate=_f(fs.get("rate"), 0.0) or 0.0,
            exponent=_f(fs.get("exponent"), 1.0) or 1.0,
            taker_only=bool(fs.get("takerOnly", True)),
            rebate_rate=_f(fs.get("rebateRate"), 0.0) or 0.0,
        )
        if m.get("feesEnabled") and fs
        else FeeSchedule()
    )
    rewards = None
    max_spread = _f(m.get("rewardsMaxSpread"))
    if max_spread:
        rewards = RewardsConfig(min_size=_f(m.get("rewardsMinSize"), 0.0) or 0.0, max_spread_cents=max_spread)

    return Market(
        id=str(m.get("id")),
        condition_id=str(m.get("conditionId", "")),
        question=str(m.get("question", "")),
        slug=str(m.get("slug", "")),
        outcomes=tuple(Outcome(str(n), str(i)) for n, i in zip(names, ids, strict=True)),
        tick_size=_f(m.get("orderPriceMinTickSize"), 0.01) or 0.01,
        min_order_size=_f(m.get("orderMinSize"), 5.0) or 5.0,
        end_time=_ts(m.get("endDate")),
        neg_risk=bool(m.get("negRisk")),
        neg_risk_other=bool(m.get("negRiskOther")),
        accepting_orders=bool(m.get("acceptingOrders", True)),
        active=bool(m.get("active", True)),
        closed=bool(m.get("closed", False)),
        liquidity_usd=_f(m.get("liquidityNum") or m.get("liquidity"), 0.0) or 0.0,
        volume_24h_usd=_f(m.get("volume24hr"), 0.0) or 0.0,
        fees=fees,
        rewards=rewards,
        event=ev_ref,
        category=tags[0] if tags else str(m.get("category") or ""),
        tags=tags,
        game_start_time=_ts(m.get("gameStartTime")),
        resolution_source=str(m.get("resolutionSource") or ""),
        description=str(m.get("description") or "")[:2000],
        version=version,
        best_bid=_f(m.get("bestBid")),
        best_ask=_f(m.get("bestAsk")),
        last_trade_price=_f(m.get("lastTradePrice")),
        one_hour_change=_f(m.get("oneHourPriceChange")),
        one_day_change=_f(m.get("oneDayPriceChange")),
    )


def _tradable(raw: dict[str, Any]) -> bool:
    return bool(
        raw.get("active", True)
        and not raw.get("closed")
        and raw.get("acceptingOrders", True)
        and raw.get("enableOrderBook", True)
    )


def annotate_event(ev: dict[str, Any]) -> None:
    """Mark whether the event's open markets still form an exhaustive set (basket-arb safety)."""
    raws = ev.get("markets") or []
    ok = bool(raws)
    n_open = 0
    for raw in raws:
        if _tradable(raw):
            n_open += 1
            continue
        prices = _jlist(raw.get("outcomePrices"))
        resolved_no = bool(raw.get("closed")) and bool(prices) and _f(prices[0], -1) == 0.0
        ok = ok and resolved_no
    ev["_basket_complete"] = ok and n_open >= 2
    ev["_n_open"] = n_open


class GammaClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = client or httpx.AsyncClient(timeout=20.0, http2=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        r = await self._http.get(f"{self._base}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def active_events(self, max_pages: int = 10, page_size: int = 100) -> list[dict[str, Any]]:
        """Active, open events ordered by 24h volume (most active first), markets nested."""
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            batch = await self._get(
                "/events",
                {
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": page_size,
                    "offset": page * page_size,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < page_size:
                break
        return out

    async def markets_from_events(self, max_pages: int = 10) -> list[Market]:
        markets: list[Market] = []
        for ev in await self.active_events(max_pages=max_pages):
            annotate_event(ev)
            for raw in ev.get("markets") or []:
                if raw.get("closed") or not raw.get("active", True):
                    continue
                mk = parse_market(raw, ev)
                if mk is not None:
                    markets.append(mk)
        return markets

    async def market(self, market_id: str) -> Market | None:
        data = await self._get(f"/markets/{market_id}", {})
        return parse_market(data) if data else None

    async def resolution(self, market_id: str) -> dict[str, float] | None:
        """{asset_id: payout} if the market is closed with final prices (1/0, or 0.5 on 50-50)."""
        data = await self._get(f"/markets/{market_id}", {})
        if not data or not data.get("closed"):
            return None
        prices = [_f(x) for x in _jlist(data.get("outcomePrices"))]
        version = str(data.get("version") or "v1")
        ids = _jlist(data.get("positionIds")) if version == "v2" else _jlist(data.get("clobTokenIds"))
        if not prices or len(prices) != len(ids) or any(p is None for p in prices):
            return None
        if not all(p in (0.0, 0.5, 1.0) for p in prices):
            return None  # closed but not yet finalised
        return {str(a): float(p) for a, p in zip(ids, prices, strict=True)}  # type: ignore[arg-type]

    async def closed_markets(self, limit: int = 500, offset: int = 0, **params: Any) -> list[dict[str, Any]]:
        """Resolved markets (for calibration)."""
        res = await self._get("/markets", {"closed": "true", "limit": limit, "offset": offset, **params})
        return list(res or [])
