import itertools
import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.quant import digital as d

YEAR = d.SECONDS_PER_YEAR


def test_atm_digital_slightly_below_half():
    # zero drift in price ⇒ median below spot ⇒ P(S_T > S_0) < 0.5
    p = d.prob_above(100, 100, sigma=0.6, tau=30 / 365)
    assert 0.45 < p < 0.5


@given(k1=st.floats(50, 150), dk=st.floats(0.1, 50), sigma=st.floats(0.1, 2.0), days=st.floats(0.01, 200))
def test_prob_above_monotone_in_strike(k1, dk, sigma, days):
    tau = days / 365
    assert d.prob_above(100, k1, sigma, tau) >= d.prob_above(100, k1 + dk, sigma, tau) - 1e-12


@given(h=st.floats(100.5, 300), sigma=st.floats(0.1, 2.0), days=st.floats(0.01, 200))
def test_touch_dominates_terminal(h, sigma, days):
    tau = days / 365
    touch = d.prob_touch_up(100, h, sigma, tau)
    assert touch >= d.prob_above(100, h, sigma, tau) - 1e-12
    assert 0 <= touch <= 1


def test_touch_already_hit():
    assert d.prob_touch_up(100, 120, 0.5, 0.1, running_max=121) == 1.0
    assert d.prob_touch_down(100, 80, 0.5, 0.1, running_min=79) == 1.0


def test_buckets_sum_to_one():
    edges = [None, 80, 90, 100, 110, 120, None]
    total = sum(d.prob_between(100, lo, hi, 0.7, 0.05) for lo, hi in itertools.pairwise(edges))
    assert total == pytest.approx(1.0)


def _mc_touch(spot, barrier, sigma, tau, up, n=40_000, steps=2_000, seed=7):
    rng = np.random.default_rng(seed)
    dt = tau / steps
    x = np.zeros(n)
    ext = np.zeros(n)
    nu = -0.5 * sigma**2
    for _ in range(steps):
        x += nu * dt + sigma * math.sqrt(dt) * rng.standard_normal(n)
        ext = np.maximum(ext, x) if up else np.minimum(ext, x)
    b = math.log(barrier / spot)
    return float(np.mean(ext >= b)) if up else float(np.mean(ext <= b))


def test_touch_formulas_match_monte_carlo():
    # discrete MC slightly underestimates continuous touching; allow a small one-sided gap
    up = d.prob_touch_up(100, 115, 0.8, 30 / 365)
    mc_up = _mc_touch(100, 115, 0.8, 30 / 365, up=True)
    assert abs(up - mc_up) < 0.03
    down = d.prob_touch_down(100, 85, 0.8, 30 / 365)
    mc_down = _mc_touch(100, 85, 0.8, 30 / 365, up=False)
    assert abs(down - mc_down) < 0.03


def test_band_brackets_mid():
    b = d.band(lambda s: d.prob_above(100, 120, s, 7 / 365), sigma=0.6)
    assert b.lo <= b.mid <= b.hi
    assert b.stderr > 0


def test_seasonal_profile_and_window_multiplier():
    from pmtrader.quant.volatility import seasonal_profile, window_variance_multiplier

    # 4 weeks of hourly bars: hour 14 UTC is 4x as volatile, weekends half as volatile
    ts, hi, lo = [], [], []
    start = 20836 * 86400  # a Monday 00:00 UTC (epoch day 0 was a Thursday)
    for i in range(24 * 28):
        t = start + i * 3600
        hour, weekend = (t // 3600) % 24, ((t // 86400) + 3) % 7 >= 5
        v = 0.0001 * (4 if hour == 14 else 1) * (0.5 if weekend else 1)
        ts.append(t)
        hi.append(math.exp(math.sqrt(v)))
        lo.append(1.0)
    hod, wk = seasonal_profile(ts, hi, lo, shrink=0.0)
    assert hod[14] > 3 * hod[3]
    assert wk[1] < wk[0]
    full_week = window_variance_multiplier(hod, wk, start, start + 7 * 86400)
    assert full_week == pytest.approx(1.0, rel=0.1)
    sat = start + 5 * 86400 + 20 * 3600
    assert window_variance_multiplier(hod, wk, sat, sat + 3 * 3600) < 0.7
