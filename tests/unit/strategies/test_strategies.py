import pytest

from pmtrader.config import StrategyConfig
from pmtrader.core.types import FeeSchedule, Side, is_on_tick
from pmtrader.strategies.base import ScanContext
from pmtrader.strategies.crypto_fair_value import CryptoFairValue
from pmtrader.strategies.structural_arb import CompleteSetArb, LadderArb, NegRiskBasketArb
from tests.conftest import NOW, make_market


def ctx_for(world, crypto=None, bankroll=1000.0):
    return ScanContext(
        now=world.clock.now(),
        markets=world.markets,
        books=world.books,
        market_for_asset=world.market_for_asset,
        event_markets=lambda e: [m for m in world.markets.values() if m.event_id == e],
        bankroll=bankroll,
        crypto=crypto,
        stale_after=15,
    )


def test_complete_set_arb_detect_and_plan(world):
    m = world.add(make_market())
    world.book(m.yes.asset_id, [(0.40, 100)], [(0.45, 100)])
    world.book(m.no.asset_id, [(0.48, 100)], [(0.50, 100)])
    s = CompleteSetArb(StrategyConfig(min_edge=0.003, max_usd_per_trade=50))
    ctx = ctx_for(world)
    [opp] = s.detect(ctx)
    assert opp.edge == pytest.approx(0.05)
    assert not opp.needs_judgment
    intents = s.plan_entry(opp, ctx, 50)
    assert len(intents) == 2 and intents[0].group_id == intents[1].group_id
    assert intents[0].size * 0.95 <= 50 + 1e-9


def test_complete_set_no_arb_after_fees(world):
    m = world.add(make_market(fees=FeeSchedule(rate=0.07)))
    world.book(m.yes.asset_id, [(0.40, 100)], [(0.48, 100)])
    world.book(m.no.asset_id, [(0.48, 100)], [(0.50, 100)])
    assert CompleteSetArb(StrategyConfig()).detect(ctx_for(world)) == []


def _negrisk_world(world, asks, n_open=None, augmented=False):
    from dataclasses import replace

    for i, a in enumerate(asks):
        m = make_market(mid=f"n{i}", event="E", neg_risk=True, n_open=n_open or len(asks))
        if augmented:
            m = replace(m, event=replace(m.event, neg_risk_augmented=True))
        world.add(m)
        world.book(m.yes.asset_id, [(a - 0.01, 100)], [(a, 100)])
        world.book(m.no.asset_id, [(1 - a - 0.01, 100)], [(1 - a + 0.01, 100)])


def test_negrisk_basket_arb(world):
    _negrisk_world(world, [0.30, 0.30, 0.30])
    opps = NegRiskBasketArb(StrategyConfig(min_edge=0.005)).detect(ctx_for(world))
    assert len(opps) == 1 and opps[0].edge == pytest.approx(0.10)
    assert len(opps[0].legs) == 3


@pytest.mark.parametrize(("n_open", "augmented"), [(4, False), (3, True)])
def test_negrisk_requires_exhaustive_event(world, n_open, augmented):
    _negrisk_world(world, [0.30, 0.30, 0.30], n_open=n_open, augmented=augmented)
    assert NegRiskBasketArb(StrategyConfig()).detect(ctx_for(world)) == []


def test_ladder_arb_above(world):
    lo = world.add(make_market("k1", "L", question="Will the price of Bitcoin be above $70,000 on October 18?"))
    hi = world.add(make_market("k2", "L", question="Will the price of Bitcoin be above $72,000 on October 18?"))
    world.book(lo.yes.asset_id, [(0.48, 100)], [(0.50, 100)])
    world.book(hi.no.asset_id, [(0.43, 100)], [(0.45, 100)])
    world.book(hi.yes.asset_id, [(0.53, 100)], [(0.56, 100)])
    world.book(lo.no.asset_id, [(0.48, 100)], [(0.52, 100)])
    [opp] = LadderArb(StrategyConfig(min_edge=0.005)).detect(ctx_for(world))
    assert {a for a, _, _ in opp.legs} == {lo.yes.asset_id, hi.no.asset_id}
    assert opp.edge == pytest.approx(0.05)


class FakeCrypto:
    def __init__(self, spot, vol):
        self._spot, self._vol = spot, vol

    def spot(self, asset, max_age=10.0):
        return self._spot

    def vol_for_window(self, asset, start, end):
        return self._vol

    def vol(self, asset):
        return self._vol

    def open_at(self, asset, ts):
        return self._spot


def test_crypto_fair_value_buys_underpriced_digital(world):
    m = world.add(make_market(question="Will the price of Bitcoin be above $70,000 on October 18?", end_in_days=1))
    world.book(m.yes.asset_id, [(0.78, 500)], [(0.80, 500)])
    world.book(m.no.asset_id, [(0.19, 500)], [(0.22, 500)])
    s = CryptoFairValue(StrategyConfig(min_edge=0.03, max_usd_per_trade=40, kelly_fraction=0.2))
    opps = s.detect(ctx_for(world, FakeCrypto(80_000, 0.4)))
    assert len(opps) == 1
    opp = opps[0]
    asset, side, limit = opp.legs[0]
    assert asset == m.yes.asset_id and side is Side.BUY
    assert is_on_tick(limit, m.tick_size) and limit >= 0.80
    assert 0.8 < (opp.fair or 0) < 1.0
    assert opp.size_hint * opp.capital_per_unit <= 40 + 1e-6


def test_crypto_fair_value_no_trade_when_fairly_priced(world):
    m = world.add(make_market(question="Will the price of Bitcoin be above $80,000 on October 18?", end_in_days=1))
    world.book(m.yes.asset_id, [(0.47, 500)], [(0.49, 500)])
    world.book(m.no.asset_id, [(0.51, 500)], [(0.53, 500)])
    s = CryptoFairValue(StrategyConfig(min_edge=0.03))
    assert s.detect(ctx_for(world, FakeCrypto(80_000, 0.4))) == []


def test_crypto_updown_waits_for_window_start(world):
    end = NOW + 3600
    m = world.add(make_market(question="Bitcoin Up or Down - October 17, 6PM ET", end_in_days=1 / 24))
    assert m.end_time == pytest.approx(end)
    world.book(m.yes.asset_id, [(0.30, 500)], [(0.32, 500)])
    world.book(m.no.asset_id, [(0.66, 500)], [(0.68, 500)])
    s = CryptoFairValue(StrategyConfig(min_edge=0.03))
    # window = 1h ending at end → started exactly now; open == spot ⇒ fair ≈ 0.5 ⇒ YES at 0.32 is cheap
    [opp] = s.detect(ctx_for(world, FakeCrypto(80_000, 0.4)))
    assert opp.legs[0][0] == m.yes.asset_id
