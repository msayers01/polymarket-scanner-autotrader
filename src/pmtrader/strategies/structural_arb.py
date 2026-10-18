"""Structural arbitrage strategies (docs/STRATEGIES.md §1–3). No judgment needed → never uses the LLM."""

from __future__ import annotations

from collections import defaultdict

from pmtrader.core.types import Market, Opportunity, OrderIntent, Side, new_id
from pmtrader.marketdata.question_parser import parse_crypto_question
from pmtrader.quant.arbitrage import Leg, basket_buy
from pmtrader.strategies.base import ScanContext, Strategy


def _opportunity(
    strategy: Strategy,
    ctx: ScanContext,
    anchor: Market,
    legs: list[tuple[str, float]],
    res_sets: float,
    res_cost: float,
    res_payoff: float,
    note: str,
    question: str | None = None,
) -> Opportunity | None:
    if res_sets <= 0:
        return None
    edge = (res_payoff - res_cost) / res_sets
    if edge < strategy.cfg.min_edge:
        return None
    capital = res_cost / res_sets
    max_sets = strategy.cfg.max_usd_per_trade / capital if capital > 0 else 0.0
    return Opportunity(
        strategy=strategy.name,
        market_id=anchor.id,
        question=question or anchor.question,
        legs=tuple((a, Side.BUY, p) for a, p in legs),
        fair=None,
        edge=edge,
        size_hint=min(res_sets, max_sets),
        capital_per_unit=capital,
        days_to_close=ctx.days_to_close(anchor),
        needs_judgment=False,
        event_id=anchor.event_id,
        category=anchor.category,
        features={"sets_available": round(res_sets, 2), "set_cost": round(capital, 4)},
        note=note,
    )


class CompleteSetArb(Strategy):
    """Buy YES + NO of a binary market for < $1 (after fees)."""

    name = "complete_set_arb"

    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        eps = self.param("epsilon", 0.003)
        out: list[Opportunity] = []
        for m in ctx.markets.values():
            if not m.is_binary or not m.accepting_orders:
                continue
            by, bn = ctx.book(m.yes.asset_id), ctx.book(m.no.asset_id)
            if by is None or bn is None or not by.asks() or not bn.asks():
                continue
            # cheap pre-check before walking depth
            if by.asks()[0][0] + bn.asks()[0][0] >= 1.0 - eps:
                continue
            r = basket_buy([Leg(by.asks(), m.fees), Leg(bn.asks(), m.fees)], 1.0, eps)
            opp = _opportunity(
                self,
                ctx,
                m,
                [(m.yes.asset_id, r.limit_prices[0]), (m.no.asset_id, r.limit_prices[1])],
                r.sets,
                r.cost,
                r.payoff,
                "YES+NO < $1",
            )
            if opp:
                out.append(opp)
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        sets = min(opp.size_hint, budget_usd / opp.capital_per_unit)
        return self.taker_intents(opp, ctx, sets, new_id("g_"))


class NegRiskBasketArb(Strategy):
    """Buy one YES of every outcome of an exhaustive neg-risk event for < $1 (or all NOs for < N−1)."""

    name = "negrisk_basket_arb"

    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        eps = self.param("epsilon", 0.005)
        events: dict[str, list[Market]] = defaultdict(list)
        for m in ctx.markets.values():
            if m.event and m.event.neg_risk and m.is_binary:
                events[m.event.id].append(m)
        out: list[Opportunity] = []
        for markets in events.values():
            ev = markets[0].event
            assert ev is not None
            if ev.neg_risk_augmented or not ev.basket_complete or len(markets) != ev.n_open_markets:
                continue  # not provably exhaustive → an "arb" here could lose 100%
            if not all(m.accepting_orders for m in markets):
                continue
            yes_books = [ctx.book(m.yes.asset_id) for m in markets]
            no_books = [ctx.book(m.no.asset_id) for m in markets]
            anchor = max(markets, key=lambda m: m.volume_24h_usd)
            if all(b is not None and b.asks() for b in yes_books):
                legs = [Leg(b.asks(), m.fees, m.yes.asset_id) for b, m in zip(yes_books, markets, strict=True)]  # type: ignore[union-attr]
                r = basket_buy(legs, 1.0, eps)
                opp = _opportunity(
                    self,
                    ctx,
                    anchor,
                    [(lg.asset_id, p) for lg, p in zip(legs, r.limit_prices, strict=True)],
                    r.sets,
                    r.cost,
                    r.payoff,
                    f"Σ YES asks < $1 over {len(markets)} outcomes",
                    ev.title,
                )
                if opp:
                    out.append(opp)
            n = len(markets)
            if n >= 3 and all(b is not None and b.asks() for b in no_books):
                legs = [Leg(b.asks(), m.fees, m.no.asset_id) for b, m in zip(no_books, markets, strict=True)]  # type: ignore[union-attr]
                r = basket_buy(legs, float(n - 1), eps)
                opp = _opportunity(
                    self,
                    ctx,
                    anchor,
                    [(lg.asset_id, p) for lg, p in zip(legs, r.limit_prices, strict=True)],
                    r.sets,
                    r.cost,
                    r.payoff,
                    f"Σ NO asks < {n - 1} over {n} outcomes",
                    ev.title,
                )
                if opp:
                    out.append(opp)
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        sets = min(opp.size_hint, budget_usd / opp.capital_per_unit)
        return self.taker_intents(opp, ctx, sets, new_id("g_"))


class LadderArb(Strategy):
    """Monotonicity violations across strikes of the same underlying/date (same Gamma event)."""

    name = "ladder_arb"

    def detect(self, ctx: ScanContext) -> list[Opportunity]:
        eps = self.param("epsilon", 0.005)
        groups: dict[tuple[str, str, str], list[tuple[float, Market]]] = defaultdict(list)
        for m in ctx.markets.values():
            if not m.is_binary or not m.event:
                continue
            c = parse_crypto_question(m.question)
            if c is None or c.strike is None or c.kind not in ("above", "touch_up", "touch_down"):
                continue
            groups[(m.event.id, c.asset, c.kind)].append((c.strike, m))
        out: list[Opportunity] = []
        for (_ev, _asset, kind), items in groups.items():
            items.sort(key=lambda x: x[0])
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    (k1, m1), (k2, m2) = items[i], items[j]
                    if k1 == k2 or m1.end_time != m2.end_time:
                        continue
                    # above/touch_up: P non-increasing in K → YES(K1)+NO(K2) ≥ 1
                    # touch_down: P non-decreasing in L → YES(L2)+NO(L1) ≥ 1
                    yes_m, no_m = (m1, m2) if kind in ("above", "touch_up") else (m2, m1)
                    by, bn = ctx.book(yes_m.yes.asset_id), ctx.book(no_m.no.asset_id)
                    if by is None or bn is None or not by.asks() or not bn.asks():
                        continue
                    if by.asks()[0][0] + bn.asks()[0][0] >= 1.0 - eps:
                        continue
                    r = basket_buy([Leg(by.asks(), yes_m.fees), Leg(bn.asks(), no_m.fees)], 1.0, eps)
                    opp = _opportunity(
                        self,
                        ctx,
                        yes_m,
                        [(yes_m.yes.asset_id, r.limit_prices[0]), (no_m.no.asset_id, r.limit_prices[1])],
                        r.sets,
                        r.cost,
                        r.payoff,
                        f"ladder {kind}: YES({yes_m.question[:40]}…) + NO(…)",
                        f"{yes_m.question} ⟂ NO: {no_m.question}",
                    )
                    if opp:
                        out.append(opp)
        return out

    def plan_entry(self, opp: Opportunity, ctx: ScanContext, budget_usd: float) -> list[OrderIntent]:
        sets = min(opp.size_hint, budget_usd / opp.capital_per_unit)
        return self.taker_intents(opp, ctx, sets, new_id("g_"))
