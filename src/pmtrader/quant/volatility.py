"""Volatility estimators (price space and logit space)."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

MINUTES_PER_YEAR = 525_600.0


def log_returns(prices: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in itertools.pairwise(prices) if a > 0 and b > 0]


def ewma_variance(returns: Sequence[float], lam: float = 0.97) -> float:
    """RiskMetrics EWMA of squared returns (zero-mean). Seeds with the sample variance of the first 20."""
    if not returns:
        return 0.0
    seed = returns[: min(20, len(returns))]
    var = sum(r * r for r in seed) / len(seed)
    for r in returns:
        var = lam * var + (1 - lam) * r * r
    return var


def annualised_vol(returns: Sequence[float], periods_per_year: float, lam: float | None = 0.97) -> float:
    """Annualised volatility from per-period log returns (EWMA if ``lam`` else equal-weight)."""
    if len(returns) < 2:
        return 0.0
    var = sum(r * r for r in returns) / len(returns) if lam is None else ewma_variance(returns, lam)
    return math.sqrt(var * periods_per_year)


def parkinson_vol(highs: Sequence[float], lows: Sequence[float], periods_per_year: float) -> float:
    """Parkinson (1980) range-based estimator — ~5x more efficient than close-to-close."""
    terms = [math.log(h / lo) ** 2 for h, lo in zip(highs, lows, strict=False) if h > 0 and lo > 0]
    if not terms:
        return 0.0
    return math.sqrt(sum(terms) / (4 * math.log(2) * len(terms)) * periods_per_year)


def logit(p: float, eps: float = 1e-4) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


def logit_vol(mids: Sequence[float], dt_seconds: float) -> float:
    """Per-second-root volatility of logit(mid) sampled every ``dt_seconds`` (σ_x · √s)."""
    xs = [logit(m) for m in mids]
    diffs = [b - a for a, b in itertools.pairwise(xs)]
    if len(diffs) < 2 or dt_seconds <= 0:
        return 0.0
    var = sum(d * d for d in diffs) / len(diffs)
    return math.sqrt(var / dt_seconds)


def blend_vol(realized: float, implied: float | None, w_implied: float = 0.5) -> float:
    if implied is None or implied <= 0:
        return realized
    if realized <= 0:
        return implied
    # blend variances, not vols (keeps it a proper mixture of variance estimates)
    return math.sqrt(w_implied * implied**2 + (1 - w_implied) * realized**2)


# --------------------------------------------------------------------------- seasonality
# Crypto volatility has strong intraday and weekday/weekend periodicity (cf. Andersen & Bollerslev 1997).
# A flat annualised σ over-prices short-dated digitals on quiet hours (weekend evenings) and under-prices
# them into busy ones. We model variance multiplicatively: v(t) = hod[hour_of_day(t)] × dow[is_weekend(t)],
# each normalised to mean 1, estimated from hourly Parkinson variances.


def seasonal_profile(
    ts: Sequence[float], highs: Sequence[float], lows: Sequence[float], shrink: float = 0.2
) -> tuple[list[float], list[float]]:
    """Return (hour_of_day[24], weekday_weekend[2]) variance multipliers from hourly bars (UTC timestamps).

    ``shrink`` pulls each bucket toward 1 to limit estimation noise.
    """
    hod_sum, hod_n = [0.0] * 24, [0] * 24
    wk_sum, wk_n = [0.0, 0.0], [0, 0]
    for t, h, lo in zip(ts, highs, lows, strict=False):
        if h <= 0 or lo <= 0:
            continue
        v = math.log(h / lo) ** 2
        hour = int(t // 3600) % 24
        weekend = 1 if (int(t // 86400) + 3) % 7 >= 5 else 0  # epoch day 0 = Thursday
        hod_sum[hour] += v
        hod_n[hour] += 1
        wk_sum[weekend] += v
        wk_n[weekend] += 1
    hod = [hod_sum[i] / hod_n[i] if hod_n[i] else 0.0 for i in range(24)]
    wk = [wk_sum[i] / wk_n[i] if wk_n[i] else 0.0 for i in range(2)]

    def _norm(xs: list[float], weights: list[float]) -> list[float]:
        tot_w = sum(weights)
        mean = sum(x * w for x, w in zip(xs, weights, strict=True)) / tot_w if tot_w else 0.0
        if mean <= 0:
            return [1.0] * len(xs)
        return [(1 - shrink) * (x / mean if x > 0 else 1.0) + shrink for x in xs]

    return _norm(hod, [1.0] * 24), _norm(wk, [5.0, 2.0])


def window_variance_multiplier(
    hod: Sequence[float], wk: Sequence[float], start: float, end: float, step: float = 900.0
) -> float:
    """Average seasonal variance multiplier over [start, end] (UTC epoch seconds)."""
    if end <= start or len(hod) != 24 or len(wk) != 2:
        return 1.0
    total, n, t = 0.0, 0, start
    while t < end:
        hour = int(t // 3600) % 24
        weekend = 1 if (int(t // 86400) + 3) % 7 >= 5 else 0
        total += hod[hour] * wk[weekend]
        n += 1
        t += step
    return total / n if n else 1.0
