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
    total = sum(d.prob_between(100, lo, hi, 0.7, 0.05) for lo, hi in zip(edges[:-1], edges[1:], strict=True))
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
