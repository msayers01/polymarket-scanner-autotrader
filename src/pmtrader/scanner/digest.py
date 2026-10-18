"""Compact, token-frugal digest of opportunities for the LLM gate (docs/LLM_GATE.md §2)."""

from __future__ import annotations

from collections.abc import Callable

from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Market, Opportunity

HEADER = "id|question|cat|bid|ask|fair|edge_c|ttc_h|vol24k|d1h_c|d24h_c|strat|side|note"


def _p(x: float | None) -> str:
    return "" if x is None else f"{x:.3f}".lstrip("0") if x < 1 else f"{x:.2f}"


def _clean(s: str, n: int) -> str:
    s = " ".join(s.replace("|", "/").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_digest(
    opps: list[Opportunity],
    market: Callable[[str], Market | None],
    book: Callable[[str], OrderBook | None],
) -> tuple[str, dict[str, Opportunity]]:
    """Returns (digest text, short-id → opportunity)."""
    rows = [HEADER]
    idmap: dict[str, Opportunity] = {}
    for i, o in enumerate(opps, 1):
        sid = f"m{i}"
        idmap[sid] = o
        m = market(o.market_id)
        asset = o.legs[0][0] if o.legs else ""
        b = book(asset)
        side = m.outcome_for(asset).name if m and m.outcome_for(asset) else "?"  # type: ignore[union-attr]
        ttc = f"{o.days_to_close * 24:.1f}" if o.days_to_close is not None else ""
        d1h = f"{(m.one_hour_change or 0) * 100:+.0f}" if m and m.one_hour_change is not None else ""
        d24 = f"{(m.one_day_change or 0) * 100:+.0f}" if m and m.one_day_change is not None else ""
        rows.append(
            "|".join(
                [
                    sid,
                    _clean(o.question, 120),
                    _clean(o.category or (m.category if m else ""), 12),
                    _p(b.best_bid if b else None),
                    _p(b.best_ask if b else None),
                    _p(o.fair),
                    f"{o.edge * 100:.1f}",
                    ttc,
                    f"{(m.volume_24h_usd if m else 0) / 1000:.0f}",
                    d1h,
                    d24,
                    o.strategy,
                    _clean(side, 20),
                    _clean(o.note, 40),
                ]
            )
        )
    return "\n".join(rows), idmap
