from pmtrader.config import UniverseConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import ManualClock
from pmtrader.core.types import EventRef, Market, Outcome
from pmtrader.marketdata.catalog import MarketCatalog


def mk(mid, ev, vol, neg=True, complete=True, aug=False, n_open=3):
    return Market(
        id=mid,
        condition_id=mid,
        question=f"q{mid}",
        slug=mid,
        outcomes=(Outcome("Yes", f"{mid}y"), Outcome("No", f"{mid}n")),
        end_time=1_800_000_000 + 86400 * 5,
        volume_24h_usd=vol,
        event=EventRef(ev, ev, ev, neg, aug, complete, n_open),
    )


def test_complete_negrisk_event_subscribed_whole():
    cat = MarketCatalog(EventBus(), ManualClock(), None, UniverseConfig(max_markets=10, min_volume_24h_usd=1000))  # type: ignore[arg-type]
    cat.load([mk("a", "E", 5000), mk("b", "E", 10), mk("c", "E", 0), mk("x", "F", 9000, neg=False, n_open=1)])
    uni = cat.select_universe()
    assert {"a", "b", "c", "x"} <= set(uni)
    assert len(cat.universe_assets()) == 8


def test_augmented_event_not_forced_whole():
    cat = MarketCatalog(EventBus(), ManualClock(), None, UniverseConfig(max_markets=10, min_volume_24h_usd=1000))  # type: ignore[arg-type]
    cat.load([mk("a", "E", 5000, aug=True), mk("b", "E", 10, aug=True)])
    assert cat.select_universe() == ["a"]
