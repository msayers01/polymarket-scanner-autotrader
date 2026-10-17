import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.core.types import FeeSchedule
from pmtrader.quant.arbitrage import Leg, basket_buy, basket_sell
from pmtrader.quant.fees import fee_per_share

NOFEE = FeeSchedule()
FEE = FeeSchedule(rate=0.07)


def test_simple_complete_set_arb():
    yes = Leg([(0.45, 100), (0.47, 100)], NOFEE)
    no = Leg([(0.50, 50), (0.52, 200)], NOFEE)
    r = basket_buy([yes, no])
    # level 1: .45+.50=.95 for 50; then .45+.52=.97 for 50; then .47+.52=.99 for 100
    assert r.sets == pytest.approx(200)
    assert r.cost == pytest.approx(50 * 0.95 + 50 * 0.97 + 100 * 0.99)
    assert r.profit == pytest.approx(200 - r.cost)
    assert r.limit_prices == (0.47, 0.52)


def test_no_arb_when_sum_at_or_above_one():
    r = basket_buy([Leg([(0.5, 100)], NOFEE), Leg([(0.5, 100)], NOFEE)])
    assert r.sets == 0
    assert r.top_edge == pytest.approx(0.0)


def test_fees_can_kill_arb():
    legs = [Leg([(0.49, 100)], FEE), Leg([(0.50, 100)], FEE)]
    assert basket_buy(legs, epsilon=0).sets == 0  # 0.99 + ~0.035 fees > 1
    assert basket_buy([Leg([(0.49, 100)], NOFEE), Leg([(0.50, 100)], NOFEE)]).sets == 100


def test_epsilon_and_max_sets():
    legs = [Leg([(0.40, 100)], NOFEE), Leg([(0.55, 100)], NOFEE)]
    assert basket_buy(legs, epsilon=0.06).sets == 0
    assert basket_buy(legs, max_sets=10).sets == pytest.approx(10)


def test_neg_risk_no_basket_payoff():
    # 3 outcomes, buying NO on all pays 2; asks sum 1.9 < 2
    legs = [Leg([(0.6, 10)], NOFEE), Leg([(0.6, 10)], NOFEE), Leg([(0.7, 10)], NOFEE)]
    r = basket_buy(legs, payoff_per_set=2.0)
    assert r.sets == pytest.approx(10)
    assert r.profit == pytest.approx(10 * (2 - 1.9))


def test_empty_leg():
    r = basket_buy([Leg([], NOFEE), Leg([(0.3, 1)], NOFEE)])
    assert r.sets == 0 and math.isinf(r.top_set_cost)


def test_sell_side():
    r = basket_sell([Leg([(0.55, 10)], NOFEE), Leg([(0.50, 20)], NOFEE)])
    assert r.sets == pytest.approx(10)
    assert r.profit == pytest.approx(10 * 0.05)


levels = st.lists(
    st.tuples(st.integers(1, 99).map(lambda c: c / 100), st.floats(1, 500)), min_size=1, max_size=6
).map(lambda ls: sorted(dict(ls).items()))


@given(a=levels, b=levels, rate=st.sampled_from([0.0, 0.04, 0.07]))
def test_basket_profit_nonnegative_and_marginally_profitable(a, b, rate):
    fs = FeeSchedule(rate=rate)
    r = basket_buy([Leg(a, fs), Leg(b, fs)])
    if r.sets > 0:
        assert r.profit >= -1e-9
        # every set bought was profitable: worst prices still below payoff
        worst = sum(p + fee_per_share(p, fs) for p in r.limit_prices)
        assert worst < 1.0 + 1e-12
    else:
        assert r.profit == 0
