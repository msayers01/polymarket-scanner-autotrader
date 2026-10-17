"""PortfolioService: the single writer of the ledger. Marks, snapshots, breakers, settlement, reports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from pmtrader.config import ReportsConfig
from pmtrader.core.bus import EventBus
from pmtrader.core.clock import Clock
from pmtrader.core.events import Alert, FillEvent, MarketResolved, PnLReport, PortfolioUpdated
from pmtrader.core.orderbook import OrderBook
from pmtrader.core.types import Market
from pmtrader.marketdata.gamma import GammaClient
from pmtrader.persistence.repository import Repository
from pmtrader.portfolio.ledger import Ledger
from pmtrader.portfolio.reports import PnLReportData, render_markdown, render_text
from pmtrader.risk.engine import Breach, RiskEngine

log = structlog.get_logger(__name__)


class PortfolioService:
    def __init__(
        self,
        bus: EventBus,
        clock: Clock,
        ledger: Ledger,
        repo: Repository,
        risk: RiskEngine,
        market: Callable[[str], Market | None],
        book: Callable[[str], OrderBook | None],
        cfg: ReportsConfig,
        reports_dir: Path,
        on_breach: Callable[[Breach], object],
        gamma: GammaClient | None = None,
        starting_cash: float = 0.0,
    ) -> None:
        self.bus, self.clock, self.ledger, self.repo, self.risk = bus, clock, ledger, repo, risk
        self._market, self._book = market, book
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.reports_dir = reports_dir
        self._on_breach = on_breach
        self.gamma = gamma
        self.starting_cash = starting_cash
        self.day = ""
        self.day_start_equity: float | None = None
        self.high_water = 0.0
        self.last_marks: dict[str, float] = {}
        self._last_report_day = ""
        self._breached: set[str] = set()

    # ------------------------------------------------------------------ boot
    async def load(self) -> None:
        """Rebuild the ledger by replaying fills + settlements (event-sourced)."""
        cash = await self.repo.kv_get("starting_cash")
        if cash is None:
            cash = self.starting_cash
            await self.repo.kv_set("starting_cash", cash)
        self.ledger.cash = float(cash)
        events: list[tuple[float, int, Any]] = [(f.ts, 0, (f, o, e)) for f, o, e in await self.repo.fills()]
        events += [(ts, 1, (a, p)) for a, _m, p, ts in await self.repo.settlements()]
        for ts, kind, payload in sorted(events, key=lambda x: (x[0], x[1])):
            if kind == 0:
                f, outcome, event_id = payload
                self.ledger.apply_fill(f, outcome, event_id)
            else:
                self.ledger.settle(payload[0], payload[1], ts)
        self.high_water = float(await self.repo.kv_get("high_water", 0.0) or 0.0)
        self._last_report_day = str(await self.repo.kv_get("last_report_day", "") or "")
        await self._roll_day(force=True)
        log.info(
            "portfolio.loaded",
            cash=self.ledger.cash,
            positions=len(self.ledger.positions),
            realized=self.ledger.realized_total,
        )

    # ------------------------------------------------------------------ valuation
    def marks(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for a, p in self.ledger.positions.items():
            b = self._book(a)
            mid = b.mid if b is not None else None
            if mid is None and b is not None:
                mid = b.best_bid
            out[a] = mid if mid is not None else self.last_marks.get(a, p.avg_cost)
        self.last_marks.update(out)
        return out

    def summary(self) -> dict[str, Any]:
        marks = self.marks()
        equity = self.ledger.equity(marks)
        positions = []
        for a, p in self.ledger.positions.items():
            m = self._market(p.market_id)
            mark = marks.get(a, p.avg_cost)
            positions.append(
                {
                    "asset_id": a,
                    "market_id": p.market_id,
                    "question": m.question if m else p.market_id,
                    "outcome": p.outcome,
                    "strategy": p.strategy,
                    "qty": round(p.qty, 2),
                    "avg_cost": round(p.avg_cost, 4),
                    "mark": round(mark, 4),
                    "value": round(p.qty * mark, 2),
                    "unrealized": round(p.qty * (mark - p.avg_cost), 2),
                    "realized": round(p.realized_pnl, 2),
                    "opened_ts": p.opened_ts,
                }
            )
        return {
            "ts": self.clock.now(),
            "equity": round(equity, 2),
            "cash": round(self.ledger.cash, 2),
            "realized": round(self.ledger.realized_total, 2),
            "unrealized": round(self.ledger.unrealized(marks), 2),
            "fees": round(self.ledger.fees_total, 2),
            "gross_exposure": round(self.ledger.gross_exposure(), 2),
            "day_pnl": round(equity - self.day_start_equity, 2) if self.day_start_equity is not None else None,
            "day_start_equity": self.day_start_equity,
            "high_water": round(self.high_water, 2),
            "markets_today": len(self.risk.events_today),
            "positions": sorted(positions, key=lambda x: -abs(x["value"])),
            "by_strategy": {k: round(v, 2) for k, v in self.ledger.realized_by_strategy.items()},
        }

    # ------------------------------------------------------------------ event handling
    async def on_fill(self, ev: FillEvent) -> None:
        f = ev.fill
        m = self._market(f.market_id)
        outcome = m.outcome_for(f.asset_id).name if m and m.outcome_for(f.asset_id) else ""  # type: ignore[union-attr]
        event_id = m.event_id if m else f.market_id
        self.ledger.apply_fill(f, outcome, event_id)
        await self.repo.add_fill(f, outcome, event_id)

    async def settle_market(self, market_id: str, payouts: dict[str, float], ts: float) -> None:
        for asset, payout in payouts.items():
            pos = self.ledger.positions.get(asset)
            if pos is None or not pos.is_open:
                continue
            pnl = self.ledger.settle(asset, payout, ts)
            await self.repo.add_settlement(asset, market_id, payout, ts)
            log.info("fill.settlement", market=market_id, asset=asset, payout=payout, pnl=round(pnl, 4))
            self.bus.publish(
                Alert(
                    "info",
                    "Market resolved",
                    f"{pos.outcome or asset[:8]} paid {payout:g}",
                    {"pnl": round(pnl, 2), "market": market_id},
                )
            )

    async def on_resolved(self, ev: MarketResolved) -> None:
        m = self._market(ev.market_id)
        if m is None:
            # WS sends condition id in some payloads: find by condition id
            for p in self.ledger.positions.values():
                mm = self._market(p.market_id)
                if mm and mm.condition_id == ev.market_id:
                    m = mm
                    break
        if m is None or not ev.winning_asset_id:
            return
        payouts = {a: (1.0 if a == ev.winning_asset_id else 0.0) for a in m.asset_ids}
        await self.settle_market(m.id, payouts, ev.ts)

    async def poll_resolutions(self) -> None:
        """Settle positions whose markets have ended (covers missed WS resolution events)."""
        if self.gamma is None:
            return
        now = self.clock.now()
        for mid in {p.market_id for p in self.ledger.positions.values()}:
            m = self._market(mid)
            if m is not None and m.end_time is not None and m.end_time > now:
                continue
            try:
                payouts = await self.gamma.resolution(mid)
            except Exception as exc:
                log.warning("portfolio.resolution_poll_failed", market=mid, error=repr(exc))
                continue
            if payouts:
                await self.settle_market(mid, payouts, now)

    # ------------------------------------------------------------------ day roll / breakers / reports
    def _day_key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d")

    async def _roll_day(self, force: bool = False) -> None:
        day = self._day_key(self.clock.now())
        if day == self.day and not force:
            return
        row = await self.repo.daily(day)
        equity = self.ledger.equity(self.marks())
        if row.start_equity is None:
            row.start_equity = equity
            await self.repo.save_daily(row)
        self.day, self.day_start_equity = day, row.start_equity
        self.risk.reset_day(row.new_markets or [])
        self._breached.clear()
        log.info("pnl.day_start", day=day, start_equity=row.start_equity)

    async def _persist_markets_today(self) -> None:
        row = await self.repo.daily(self.day)
        if set(row.new_markets or []) != self.risk.events_today:
            row.new_markets = sorted(self.risk.events_today)
            await self.repo.save_daily(row)

    def _check_breakers(self, equity: float) -> None:
        for b in self.risk.breakers(equity, self.day_start_equity, self.high_water):
            if b.code in self._breached:
                continue
            self._breached.add(b.code)
            log.warning("risk.breach", code=b.code, msg=b.message, severity=b.severity)
            self._on_breach(b)

    async def build_report(self, period: str = "today") -> PnLReportData:
        now = self.clock.now()
        local = datetime.fromtimestamp(now, self.tz)
        if period == "today":
            start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif period == "7d":
            start = (local - timedelta(days=7)).timestamp()
        else:
            start = 0.0
        marks = self.marks()
        equity = self.ledger.equity(marks)
        snaps = await self.repo.snapshots(start)
        start_equity = self.day_start_equity if period == "today" else (snaps[0].equity if snaps else None)
        if period == "all":
            start_equity = float(await self.repo.kv_get("starting_cash", self.starting_cash) or 0.0)
        peak, mdd = 0.0, 0.0
        for s in snaps:
            peak = max(peak, s.equity)
            if peak > 0:
                mdd = max(mdd, (peak - s.equity) / peak * 100)
        fills = await self.repo.fills_between(start, now + 1)
        by_strat: dict[str, dict[str, float]] = {}
        for f in fills:
            d = by_strat.setdefault(f.strategy, {"trades": 0, "volume": 0.0, "fees": 0.0, "realized": 0.0})
            d["trades"] += 1
            d["volume"] += f.price * f.size
            d["fees"] += f.fee
        for name, val in self.ledger.realized_by_strategy.items():
            by_strat.setdefault(name, {"trades": 0, "volume": 0.0, "fees": 0.0})["realized"] = val
        closed = [p for p in self.ledger.closed_positions if p.updated_ts >= start]
        return PnLReportData(
            period=period,
            start=start,
            end=now,
            equity=equity,
            start_equity=start_equity,
            cash=self.ledger.cash,
            realized=self.ledger.realized_total,
            unrealized=self.ledger.unrealized(marks),
            fees=self.ledger.fees_total,
            gross_exposure=self.ledger.gross_exposure(),
            open_positions=len(self.ledger.positions),
            trades=len(fills),
            volume_usd=sum(f.price * f.size for f in fills),
            by_strategy=by_strat,
            wins=sum(1 for p in closed if p.realized_pnl > 0),
            losses=sum(1 for p in closed if p.realized_pnl <= 0),
            max_drawdown_pct=mdd,
        )

    async def publish_report(self, period: str = "today") -> PnLReportData:
        r = await self.build_report(period)
        text = render_text(r)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._day_key(r.end)
        (self.reports_dir / f"pnl-{stamp}-{period}.json").write_text(json.dumps(r.to_dict(), indent=1, default=str))
        (self.reports_dir / f"pnl-{stamp}-{period}.md").write_text(render_markdown(r))
        log.info("pnl.report", period=period, **{k: v for k, v in r.to_dict().items() if k != "by_strategy"})
        self.bus.publish(PnLReport(period, r.to_dict(), text))
        return r

    def _report_due(self) -> bool:
        local = datetime.fromtimestamp(self.clock.now(), self.tz)
        hh, mm = (int(x) for x in self.cfg.daily_time.split(":"))
        return local.strftime("%Y-%m-%d") != self._last_report_day and (local.hour, local.minute) >= (hh, mm)

    # ------------------------------------------------------------------ loops
    async def run(self) -> None:
        sub = self.bus.subscribe(FillEvent, MarketResolved, maxsize=100_000, name="portfolio")
        tasks = [asyncio.create_task(self._ticker())]
        try:
            async for ev in sub:
                if isinstance(ev, FillEvent):
                    await self.on_fill(ev)
                elif isinstance(ev, MarketResolved):
                    await self.on_resolved(ev)
                self.bus.publish(PortfolioUpdated(self.summary()))
        finally:
            for t in tasks:
                t.cancel()
            sub.close()

    async def _ticker(self) -> None:
        last_snap = last_resolve = 0.0
        while True:
            await self.clock.sleep(1.0)
            try:
                await self._roll_day()
                s = self.summary()
                equity = s["equity"]
                if equity > self.high_water:
                    self.high_water = equity
                self._check_breakers(equity)
                self.bus.publish(PortfolioUpdated(s))
                now = self.clock.now()
                if now - last_snap >= self.cfg.snapshot_seconds:
                    last_snap = now
                    await self.repo.add_snapshot(
                        ts=now,
                        equity=equity,
                        cash=s["cash"],
                        realized=s["realized"],
                        unrealized=s["unrealized"],
                        fees=s["fees"],
                        gross_exposure=s["gross_exposure"],
                        day_pnl=s["day_pnl"] or 0.0,
                        open_positions=len(s["positions"]),
                    )
                    await self.repo.kv_set("high_water", self.high_water)
                    await self._persist_markets_today()
                if now - last_resolve >= 300:
                    last_resolve = now
                    await self.poll_resolutions()
                if self._report_due():
                    self._last_report_day = self._day_key(now)
                    await self.repo.kv_set("last_report_day", self._last_report_day)
                    await self.publish_report("today")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("portfolio.tick_failed")
