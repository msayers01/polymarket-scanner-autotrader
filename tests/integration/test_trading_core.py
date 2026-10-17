"""OMS + risk + paper gateway + ledger + control plane, wired together with fake books."""

from __future__ import annotations

import pytest

from pmtrader.core.events import FillEvent
from pmtrader.core.types import FeeSchedule, OrderIntent, OrderStatus, OrderType, Side, TradingState
from tests.conftest import Core, make_market


def intent(m, asset, side=Side.BUY, price=0.40, size=10.0, otype=OrderType.FAK, strategy="manual", **kw):
    return OrderIntent(strategy, m.id, asset, side, price, size, otype, source=kw.pop("source", "manual"), **kw)


async def test_taker_fill_against_book_with_fees(core: Core):
    w = core.world
    m = w.add(make_market(fees=FeeSchedule(rate=0.07)))
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 6), (0.41, 100)])
    fills = w.bus.subscribe(FillEvent)
    order, d = await core.oms.submit(intent(m, m.yes.asset_id, price=0.41, size=10))
    assert d.ok and order is not None
    assert order.status is OrderStatus.FILLED
    f = fills.get_nowait().fill  # type: ignore[union-attr]
    assert f.size == pytest.approx(10)
    assert f.price == pytest.approx((6 * 0.40 + 4 * 0.41) / 10)
    assert f.fee > 0


async def test_fak_respects_limit(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 6), (0.45, 100)])
    order, _ = await core.oms.submit(intent(m, m.yes.asset_id, price=0.40, size=10))
    assert order is not None and order.filled_size == pytest.approx(6)
    assert order.status is OrderStatus.CANCELLED  # FAK remainder killed


async def test_resting_order_fills_when_book_crosses(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.42, 100)])
    order, d = await core.oms.submit(
        intent(m, m.yes.asset_id, price=0.39, size=10, otype=OrderType.GTC, post_only=True)
    )
    assert d.ok and order is not None and order.status is OrderStatus.OPEN
    w.book(m.yes.asset_id, [(0.37, 100)], [(0.39, 50)])
    core.gateway._check_resting(m.yes.asset_id)
    assert order.status is OrderStatus.FILLED and order.avg_fill_price == pytest.approx(0.39)


async def test_post_only_crossing_rejected(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 100)])
    order, d = await core.oms.submit(intent(m, m.yes.asset_id, price=0.40, otype=OrderType.GTC, post_only=True))
    assert d.ok and order is not None and order.status is OrderStatus.REJECTED


async def test_risk_blocks_shorting_and_oversize(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 1000)])
    _, d = await core.oms.submit(intent(m, m.yes.asset_id, side=Side.SELL, price=0.38))
    assert not d.ok
    _, d = await core.oms.submit(intent(m, m.yes.asset_id, price=0.40, size=1000))
    assert not d.ok and d.code == "max_order_usd"


async def test_stale_book_rejected(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 100)])
    w.clock.advance(60)
    _, d = await core.oms.submit(intent(m, m.yes.asset_id))
    assert not d.ok and d.code == "stale_book"


async def test_daily_market_cap(core: Core):
    w = core.world
    await core.control.set_limits("test", {"max_new_markets_per_day": 2})
    for i in range(3):
        m = w.add(make_market(mid=f"m{i}", event=f"e{i}"))
        w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 100)])
        _, d = await core.oms.submit(intent(m, m.yes.asset_id))
        if i < 2:
            assert d.ok
        else:
            assert not d.ok and d.code == "daily_market_cap"


async def test_pause_blocks_entries_allows_exits_and_kill_cancels(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 100)])
    fills = w.bus.subscribe(FillEvent)
    await core.oms.submit(intent(m, m.yes.asset_id, size=10))
    for ev in fills.drain():
        core.ledger.apply_fill(ev.fill)  # type: ignore[attr-defined]
    w.book(m.no.asset_id, [(0.30, 1)], [(0.62, 100)])
    resting, d = await core.oms.submit(intent(m, m.no.asset_id, price=0.31, otype=OrderType.GTC))
    assert d.ok and resting is not None and resting.is_open

    await core.control.pause("test")
    assert core.control.state is TradingState.PAUSED
    assert not resting.is_open  # entry orders cancelled on pause
    _, d = await core.oms.submit(intent(m, m.yes.asset_id, size=10))
    assert not d.ok and d.code == "paused"
    exit_, d = await core.oms.submit(intent(m, m.yes.asset_id, side=Side.SELL, price=0.38, size=10, reduce_only=True))
    assert d.ok and exit_ is not None

    await core.control.kill("test", "unit test")
    assert core.control.state is TradingState.HALTED
    assert (await core.repo.kv_get("trading_state"))["state"] == "HALTED"  # persisted before side effects
    with pytest.raises(Exception, match="RESUME"):
        await core.control.resume("test")
    await core.control.resume("test", confirm="RESUME")
    assert core.control.state is TradingState.RUNNING


async def test_halt_survives_restart(core: Core):
    await core.control.kill("test", "boom")
    core.control.state = TradingState.RUNNING  # simulate fresh object
    await core.control.load()
    assert core.control.state is TradingState.HALTED


async def test_flatten_sells_positions(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.38, 100)], [(0.40, 100)])
    fills = w.bus.subscribe(FillEvent)
    await core.oms.submit(intent(m, m.yes.asset_id, size=10))
    for ev in fills.drain():
        core.ledger.apply_fill(ev.fill)  # type: ignore[attr-defined]
    await core.control.kill("test", "flatten test", flatten=True)
    for ev in fills.drain():
        core.ledger.apply_fill(ev.fill)  # type: ignore[attr-defined]
    assert m.yes.asset_id not in core.ledger.positions


async def test_group_legs_sized_to_first_fill(core: Core):
    w = core.world
    m = w.add(make_market())
    w.book(m.yes.asset_id, [(0.40, 10)], [(0.45, 6)])  # only 6 available on leg 1
    w.book(m.no.asset_id, [(0.50, 10)], [(0.50, 100)])
    legs = [
        intent(m, m.yes.asset_id, price=0.45, size=20, group_id="g1", strategy="complete_set_arb", source="strategy"),
        intent(m, m.no.asset_id, price=0.50, size=20, group_id="g1", strategy="complete_set_arb", source="strategy"),
    ]
    orders, d = await core.oms.submit_group(legs)
    assert d.ok and len(orders) == 2
    assert orders[0].filled_size == pytest.approx(6)
    assert orders[1].intent.size == pytest.approx(6)
