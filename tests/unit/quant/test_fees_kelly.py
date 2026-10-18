import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.core.types import FeeSchedule
from pmtrader.quant import fees, kelly

CRYPTO = FeeSchedule(rate=0.07, exponent=1, taker_only=True, rebate_rate=0.2)
SPORTS = FeeSchedule(rate=0.05, exponent=1, taker_only=True, rebate_rate=0.15)


@pytest.mark.parametrize(
    ("sched", "size", "price", "expected"),
    [(CRYPTO, 100, 0.50, 1.75), (CRYPTO, 100, 0.30, 1.47), (SPORTS, 100, 0.10, 0.45)],
)
def test_fee_matches_venue_examples(sched, size, price, expected):
    assert fees.fee(size, price, sched) == pytest.approx(expected)


def test_maker_pays_nothing_when_taker_only():
    assert fees.fee(100, 0.5, CRYPTO, taker=False) == 0
    assert fees.maker_rebate(100, 0.5, CRYPTO) == pytest.approx(1.75 * 0.2)


def test_zero_fee_schedule():
    assert fees.fee_per_share(0.4, FeeSchedule()) == 0


@given(st.floats(0.01, 0.99))
def test_fee_symmetric_in_price(p):
    assert fees.fee_per_share(p, CRYPTO) == pytest.approx(fees.fee_per_share(1 - p, CRYPTO))


def test_kelly_zero_without_edge():
    assert kelly.kelly_fraction(0.5, 0.5) == 0
    assert kelly.kelly_fraction(0.4, 0.5) == 0


@given(q=st.floats(0.05, 0.95), c=st.floats(0.05, 0.95))
def test_kelly_maximises_log_growth(q, c):
    f = kelly.kelly_fraction(q, c)
    if f <= 0 or f >= 0.99:
        return
    g = kelly.expected_log_growth(f, q, c)
    for df in (-0.02, 0.02):
        f2 = min(max(f + df, 1e-6), 0.999)
        assert g >= kelly.expected_log_growth(f2, q, c) - 1e-12


def test_shrinkage_reduces_size():
    full = kelly.sized_fraction(0.6, 0.5, stderr=0.0, lam=1.0)
    shrunk = kelly.sized_fraction(0.6, 0.5, stderr=0.1, lam=1.0)
    assert full == pytest.approx(0.2)
    assert shrunk == pytest.approx(0.2 * 0.01 / (0.01 + 0.01))


def test_kelly_shares_respects_cap():
    shares = kelly.kelly_shares(10_000, 0.9, 0.5, lam=1.0, max_usd=100)
    assert shares * 0.5 == pytest.approx(100)
    assert math.isfinite(shares)
