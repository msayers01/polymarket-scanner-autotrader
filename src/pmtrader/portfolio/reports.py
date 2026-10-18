"""P&L report construction (pure) and rendering for Discord/markdown/logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PnLReportData:
    period: str
    start: float
    end: float
    equity: float
    start_equity: float | None
    cash: float
    realized: float
    unrealized: float
    fees: float
    gross_exposure: float
    open_positions: int
    trades: int
    volume_usd: float
    by_strategy: dict[str, dict[str, float]] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    max_drawdown_pct: float = 0.0

    @property
    def period_pnl(self) -> float | None:
        return None if self.start_equity is None else self.equity - self.start_equity

    @property
    def win_rate(self) -> float | None:
        n = self.wins + self.losses
        return None if n == 0 else self.wins / n

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["period_pnl"] = self.period_pnl
        d["win_rate"] = self.win_rate
        return d


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):,.2f}"


def render_text(r: PnLReportData) -> str:
    lines = [
        f"**P&L — {r.period}**",
        f"Equity: ${r.equity:,.2f}  ({_money(r.period_pnl)} this period)",
        f"Realized: {_money(r.realized)} · Unrealized: {_money(r.unrealized)} · Fees: ${r.fees:,.2f}",
        f"Trades: {r.trades} · Volume: ${r.volume_usd:,.0f} · Win rate: "
        + (f"{r.win_rate:.0%}" if r.win_rate is not None else "n/a"),
        f"Open positions: {r.open_positions} · Exposure: ${r.gross_exposure:,.2f} · Cash: ${r.cash:,.2f}",
        f"Max drawdown: {r.max_drawdown_pct:.1f}%",
    ]
    if r.by_strategy:
        lines.append("By strategy:")
        for name, s in sorted(r.by_strategy.items(), key=lambda kv: -kv[1].get("realized", 0)):
            lines.append(f"  • {name}: {_money(s.get('realized'))} realized, {int(s.get('trades', 0))} fills")
    return "\n".join(lines)


def render_markdown(r: PnLReportData) -> str:
    return "# " + render_text(r).replace("**", "").replace("  • ", "- ")
