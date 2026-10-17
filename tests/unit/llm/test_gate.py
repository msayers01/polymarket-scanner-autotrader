from typing import Any

from pmtrader.config import LLMConfig
from pmtrader.core.types import Opportunity, Side
from pmtrader.llm.gate import LLMGate
from pmtrader.llm.providers.base import LLMResult
from pmtrader.scanner.digest import HEADER, build_digest
from tests.conftest import make_market


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, verdict: str = "enter", conf: float = 0.9, tokens: int = 500) -> None:
        self.calls: list[str] = []
        self.verdict, self.conf, self.tokens = verdict, conf, tokens

    async def decide(self, system: str, user: str, schema: dict[str, Any], max_tokens: int) -> LLMResult:
        self.calls.append(user)
        ids = [line.split("|")[0] for line in user.splitlines()[1:]]
        return LLMResult(
            decisions=[{"id": i, "a": self.verdict, "c": self.conf, "p": None, "r": "ok"} for i in ids],
            input_tokens=self.tokens,
            output_tokens=50,
            usd=0.001,
        )

    async def close(self) -> None:
        pass


def opp(m, price=0.4):
    return Opportunity(
        strategy="crypto_fair_value",
        market_id=m.id,
        question=m.question,
        legs=((m.yes.asset_id, Side.BUY, price),),
        fair=0.5,
        edge=0.05,
        size_hint=10,
        capital_per_unit=price,
        days_to_close=1,
        needs_judgment=True,
    )


def gate_with(world, provider, **cfg):
    c = LLMConfig(gate_mode="required", min_interval_s=0, **cfg)
    return LLMGate(c, [provider], world.bus, world.clock, None, world.markets.get, world.books.get)


async def test_digest_is_compact(world):
    ms = [world.add(make_market(mid=f"m{i}")) for i in range(5)]
    for m in ms:
        world.book(m.yes.asset_id, [(0.39, 10)], [(0.41, 10)])
    text, idmap = build_digest([opp(m) for m in ms], world.markets.get, world.books.get)
    lines = text.splitlines()
    assert lines[0] == HEADER and len(lines) == 6 and set(idmap) == {f"m{i}" for i in range(1, 6)}
    assert all(len(line) < 220 for line in lines)


async def test_gate_caches_and_batches(world):
    ms = [world.add(make_market(mid=f"m{i}")) for i in range(3)]
    for m in ms:
        world.book(m.yes.asset_id, [(0.39, 10)], [(0.41, 10)])
    p = FakeProvider()
    g = gate_with(world, p, batch_size=12)
    opps = [opp(m) for m in ms]
    v1 = await g.evaluate(opps)
    assert len(p.calls) == 1 and all(v.enter for v in v1.values())
    v2 = await g.evaluate(opps)
    assert len(p.calls) == 1  # all served from cache
    assert all(v.cached for v in v2.values())
    # material price move → new cache key → re-ask
    world.book(ms[0].yes.asset_id, [(0.49, 10)], [(0.51, 10)])
    await g.evaluate(opps)
    assert len(p.calls) == 2


async def test_gate_low_confidence_is_skip(world):
    m = world.add(make_market())
    world.book(m.yes.asset_id, [(0.39, 10)], [(0.41, 10)])
    g = gate_with(world, FakeProvider("enter", conf=0.3))
    [v] = (await g.evaluate([opp(m)])).values()
    assert not v.enter


async def test_gate_budget_exhaustion_falls_back(world):
    m = world.add(make_market())
    world.book(m.yes.asset_id, [(0.39, 10)], [(0.41, 10)])
    p = FakeProvider(tokens=10_000)
    g = gate_with(world, p, daily_max_tokens=5_000, fallback="skip")
    await g.evaluate([opp(m)])
    m2 = world.add(make_market(mid="m2"))
    world.book(m2.yes.asset_id, [(0.39, 10)], [(0.41, 10)])
    [v] = (await g.evaluate([opp(m2)])).values()
    assert v.provider == "fallback" and not v.enter
    assert len(p.calls) == 1
