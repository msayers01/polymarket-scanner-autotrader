import itertools
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.core.types import is_on_tick
from pmtrader.quant import avellaneda_stoikov as asq
from pmtrader.quant import calibration as cal
from pmtrader.quant import rewards


def test_inventory_skews_reservation():
    flat = asq.quote(0.5, 0, sigma_x=0.01, gamma=0.5, k=100, tau_s=3600, tick=0.01)
    long = asq.quote(0.5, 100, sigma_x=0.01, gamma=0.5, k=100, tau_s=3600, tick=0.01)
    assert long.reservation < flat.reservation
    assert flat.bid is not None
    assert flat.ask is not None
    assert flat.bid < flat.ask


@given(mid=st.floats(0.05, 0.95), inv=st.floats(-500, 500))
def test_quotes_on_tick_and_ordered(mid, inv):
    q = asq.quote(mid, inv, sigma_x=0.005, gamma=0.2, k=150, tau_s=1800, tick=0.01, inventory_cap=300)
    for px in (q.bid, q.ask):
        if px is not None:
            assert is_on_tick(px, 0.01)
            assert 0 < px < 1
    if q.bid is not None and q.ask is not None:
        assert q.bid < q.ask
    if inv >= 300:
        assert q.bid is None


def test_fit_arrival_decay_recovers_k():
    ds = [0.005, 0.01, 0.02, 0.03, 0.05]
    counts = [100 * 2.718281828 ** (-80 * x) for x in ds]
    assert asq.fit_arrival_decay(ds, counts) == pytest.approx(80, rel=1e-6)


def test_rewards_doc_example():
    # docs worked example: v = 3c, mid 0.50
    q_one = rewards.side_score([(0.49, 100), (0.48, 200)], 0.50, 3, 0) + rewards.side_score([(0.51, 100)], 0.50, 3, 0)
    assert q_one == pytest.approx((2 / 3) ** 2 * 100 + (1 / 3) ** 2 * 200 + (2 / 3) ** 2 * 100)
    assert rewards.order_score(3, 3) == 0
    assert rewards.q_min(10, 1, mid=0.5) == pytest.approx(10 / 3)
    assert rewards.q_min(10, 1, mid=0.95) == 1


def test_pav_monotone():
    xs, ys = cal.pav([0.1, 0.2, 0.3, 0.4], [1, 0, 1, 1])
    assert all(a <= b for a, b in itertools.pairwise(ys))
    assert xs[0] == pytest.approx(0.15)  # first two pooled


def test_calibration_shrinks_to_identity_with_huge_prior():
    rng = random.Random(1)
    prices = [rng.random() for _ in range(2000)]
    outcomes = [1 if rng.random() < p * 0.7 else 0 for p in prices]  # longshot-style overpricing
    curve = cal.CalibrationCurve.fit(prices, outcomes, kappa=1e9)
    q, _ = curve.predict(0.3)
    assert q == pytest.approx(0.3, abs=1e-3)
    curve2 = cal.CalibrationCurve.fit(prices, outcomes, kappa=5)
    q2, se = curve2.predict(0.3)
    assert q2 < 0.27
    assert se > 0


def test_beta_shrink():
    mean, sd = cal.beta_shrink(0, 0, prior_mean=0.2, kappa=10)
    assert mean == pytest.approx(0.2)
    assert sd > 0
