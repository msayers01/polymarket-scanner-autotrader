import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.core.types import Fill, Liquidity, Side
from pmtrader.portfolio.ledger import Ledger


def fill(side, price, size, fee=0.0, asset="a", strategy="s"):
    return Fill("o", "m", asset, side, price, size, fee, 0.0, Liquidity.TAKER, strategy)


def test_buy_sell_round_trip():
    lg = Ledger(cash=100)
    lg.apply_fill(fill(Side.BUY, 0.40, 10, fee=0.1))
    lg.apply_fill(fill(Side.BUY, 0.60, 10))
    pos = lg.positions["a"]
    assert pos.avg_cost == pytest.approx(0.50)
    assert lg.cash == pytest.approx(100 - 4 - 0.1 - 6)
    lg.apply_fill(fill(Side.SELL, 0.70, 20, fee=0.2))
    assert "a" not in lg.positions
    assert lg.realized_total == pytest.approx(20 * 0.2 - 0.1 - 0.2)
    assert lg.cash == pytest.approx(100 + lg.realized_total)


def test_settlement_complete_set_is_riskless():
    lg = Ledger(cash=100)
    lg.apply_fill(fill(Side.BUY, 0.45, 10, asset="yes"))
    lg.apply_fill(fill(Side.BUY, 0.50, 10, asset="no"))
    lg.settle("yes", 1.0, 1)
    lg.settle("no", 0.0, 1)
    assert lg.realized_total == pytest.approx(10 * (1 - 0.95))
    assert lg.cash == pytest.approx(100 + 0.5)


@given(
    trades=st.lists(st.tuples(st.booleans(), st.floats(0.01, 0.99), st.floats(1, 100), st.floats(0, 1)), max_size=30),
    mark=st.floats(0.0, 1.0),
)
def test_equity_identity(trades, mark):
    """equity − initial cash == realized + unrealized, always."""
    lg = Ledger(cash=10_000)
    for is_buy, p, s, fee in trades:
        if not is_buy and "a" not in lg.positions:
            continue
        size = s if is_buy else min(s, lg.positions["a"].qty)
        if size <= 0:
            continue
        lg.apply_fill(fill(Side.BUY if is_buy else Side.SELL, p, size, fee))
    marks = {"a": mark}
    assert lg.equity(marks) - 10_000 == pytest.approx(lg.realized_total + lg.unrealized(marks), abs=1e-4)
