import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmtrader.core.bus import EventBus
from pmtrader.core.events import Alert, BookUpdated
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Side, is_on_tick, round_price, round_size


@given(p=st.floats(0.0, 1.0), tick=st.sampled_from([0.01, 0.001]))
def test_round_price_on_tick_and_in_range(p, tick):
    for side in (Side.BUY, Side.SELL, None):
        r = round_price(p, tick, side)
        assert is_on_tick(r, tick)
        assert tick - 1e-12 <= r <= 1 - tick + 1e-12


def test_round_price_direction():
    assert round_price(0.537, 0.01, Side.BUY) == 0.53
    assert round_price(0.531, 0.01, Side.SELL) == 0.54
    assert round_price(0.53, 0.01, Side.BUY) == 0.53
    assert round_size(10.019) == 10.01


def test_orderbook_levels():
    b = OrderBook("a")
    b.apply_snapshot([(0.40, 10), (0.42, 5)], [(0.45, 7), (0.44, 3)], ts=1, recv_ts=1)
    assert b.best_bid == 0.42 and b.best_ask == 0.44
    assert b.mid == pytest.approx(0.43)
    b.apply_level("SELL", 0.44, 0, ts=2, recv_ts=2)
    assert b.best_ask == 0.45
    b.apply_level("BUY", 0.43, 2, ts=3, recv_ts=3)
    assert b.bids()[0] == (0.43, 2)
    assert b.depth_usd("BUY", 0.01) == pytest.approx(0.43 * 2 + 0.42 * 5)


async def test_bus_drops_noncritical_keeps_critical():
    bus = EventBus()
    sub = bus.subscribe(BookUpdated, Alert, maxsize=2)
    for i in range(5):
        bus.publish(BookUpdated("a", "m", float(i)))
    bus.publish(Alert("critical", "boom"))
    items = sub.drain()
    assert any(isinstance(e, Alert) for e in items)
    assert sub.dropped >= 3


async def test_bus_async_iteration():
    bus = EventBus()
    sub = bus.subscribe(Alert)
    got: list[str] = []

    async def reader():
        async for ev in sub:
            got.append(ev.title)
            if len(got) == 2:
                return

    t = asyncio.create_task(reader())
    await asyncio.sleep(0)
    bus.publish(Alert("info", "a"))
    bus.publish(BookUpdated("x", "m", 0))  # filtered out
    bus.publish(Alert("info", "b"))
    await asyncio.wait_for(t, 1)
    assert got == ["a", "b"]
