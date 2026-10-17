"""Avellaneda–Stoikov quoting adapted to bounded prediction-market prices (docs/STRATEGIES.md §5).

Prices follow a diffusion in logit space with vol σ_x; locally σ_p = p(1−p)·σ_x.
    r = mid − I·γ·σ_p²·τ
    δ = γ·σ_p²·τ + (2/γ)·ln(1 + γ/k)
σ_x is per √second and τ in seconds, so σ_p²·τ is in probability² units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pmtrader.core.types import Side, round_price


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float | None
    ask: float | None
    reservation: float
    spread: float


def quote(
    mid: float,
    inventory: float,
    sigma_x: float,
    gamma: float,
    k: float,
    tau_s: float,
    tick: float,
    min_spread_ticks: int = 1,
    inventory_cap: float = math.inf,
) -> Quote:
    """Optimal bid/ask (tick-rounded). ``inventory`` is YES-equivalent shares (NO counts negative).

    At |inventory| ≥ cap the side that would increase exposure is withdrawn (None).
    """
    sigma_p = mid * (1.0 - mid) * sigma_x
    var_term = gamma * sigma_p * sigma_p * max(tau_s, 0.0)
    r = mid - inventory * var_term
    spread = var_term + (2.0 / gamma) * math.log1p(gamma / k)
    spread = max(spread, min_spread_ticks * tick)
    raw_bid, raw_ask = r - spread / 2, r + spread / 2

    bid: float | None = round_price(raw_bid, tick, Side.BUY) if raw_bid > tick / 2 else None
    ask: float | None = round_price(raw_ask, tick, Side.SELL) if raw_ask < 1 - tick / 2 else None
    if bid is not None and ask is not None and bid >= ask:
        ask = round(bid + tick, 10)
        if ask >= 1:
            ask = None
    if inventory >= inventory_cap:
        bid = None
    if inventory <= -inventory_cap:
        ask = None
    return Quote(bid, ask, r, spread)


def fit_arrival_decay(distances: list[float], counts: list[float]) -> float | None:
    """Fit λ(δ) = A·exp(−kδ) by least squares on log counts; returns k (per probability unit)."""
    pts = [(d, math.log(c)) for d, c in zip(distances, counts, strict=False) if c > 0]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(d for d, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((d - mx) ** 2 for d, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((d - mx) * (y - my) for d, y in pts) / sxx
    return -slope if slope < 0 else None
