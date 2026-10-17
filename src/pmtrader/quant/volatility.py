"""Volatility estimators (price space and logit space)."""

from __future__ import annotations

import math
from collections.abc import Sequence

MINUTES_PER_YEAR = 525_600.0


def log_returns(prices: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(prices, prices[1:], strict=False) if a > 0 and b > 0]


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
    if lam is None:
        var = sum(r * r for r in returns) / len(returns)
    else:
        var = ewma_variance(returns, lam)
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
    diffs = [b - a for a, b in zip(xs, xs[1:], strict=False)]
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
