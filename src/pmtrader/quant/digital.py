"""Model prices for crypto price markets under zero-drift GBM (docs/STRATEGIES.md §4).

X_t = ln(S_t/S_0) = νt + σW_t with ν = −σ²/2 (martingale price, r = 0).
All τ in years, σ annualised.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _guard(spot: float, sigma: float, tau: float) -> bool:
    return spot > 0 and sigma > 0 and tau > 0


def prob_above(spot: float, strike: float, sigma: float, tau: float) -> float:
    """P(S_T > K) = Φ(d₂)."""
    if strike <= 0:
        return 1.0
    if not _guard(spot, sigma, tau):
        return 1.0 if spot > strike else 0.0
    st = sigma * math.sqrt(tau)
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * tau) / st
    return norm_cdf(d2)


def prob_between(spot: float, lo: float | None, hi: float | None, sigma: float, tau: float) -> float:
    """P(lo ≤ S_T < hi); ``None`` means unbounded."""
    p_lo = 1.0 if lo is None else prob_above(spot, lo, sigma, tau)
    p_hi = 0.0 if hi is None else prob_above(spot, hi, sigma, tau)
    return max(p_lo - p_hi, 0.0)


def prob_touch_up(spot: float, barrier: float, sigma: float, tau: float, running_max: float | None = None) -> float:
    """P(max_{t≤T} S_t ≥ H) for H above spot (reflection principle with drift)."""
    if barrier <= spot or (running_max is not None and running_max >= barrier):
        return 1.0
    if not _guard(spot, sigma, tau):
        return 0.0
    b = math.log(barrier / spot)
    nu = -0.5 * sigma * sigma
    st = sigma * math.sqrt(tau)
    return min(norm_cdf((-b + nu * tau) / st) + math.exp(2 * nu * b / (sigma * sigma)) * norm_cdf((-b - nu * tau) / st), 1.0)


def prob_touch_down(
    spot: float, barrier: float, sigma: float, tau: float, running_min: float | None = None
) -> float:
    """P(min_{t≤T} S_t ≤ L) for L below spot."""
    if barrier >= spot or (running_min is not None and running_min <= barrier):
        return 1.0
    if not _guard(spot, sigma, tau):
        return 0.0
    b = math.log(barrier / spot)  # < 0
    nu = -0.5 * sigma * sigma
    st = sigma * math.sqrt(tau)
    return min(norm_cdf((b - nu * tau) / st) + math.exp(2 * nu * b / (sigma * sigma)) * norm_cdf((b + nu * tau) / st), 1.0)


@dataclass(frozen=True, slots=True)
class FairBand:
    lo: float
    mid: float
    hi: float

    @property
    def stderr(self) -> float:
        """Treat the band as ≈ ±2σ of model uncertainty."""
        return max((self.hi - self.lo) / 4.0, 1e-4)


def band(pricer: Callable[[float], float], sigma: float, m_lo: float = 0.8, m_hi: float = 1.4) -> FairBand:
    """Evaluate ``pricer(σ')`` at σ·m_lo, σ, σ·m_hi and return the sorted probability band.

    Every pricer above is monotone in σ for a fixed moneyness side, so lo/hi bracket the
    plausible range under volatility uncertainty (fat tails ⇒ use m_hi > 1/m_lo).
    """
    vals = [pricer(sigma * m) for m in (m_lo, 1.0, m_hi)]
    return FairBand(lo=min(vals), mid=vals[1], hi=max(vals))
