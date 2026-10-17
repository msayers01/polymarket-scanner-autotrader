"""Async repository over SQLite (WAL). Keeps SQL out of the domain (ARCHITECTURE.md §3)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from pmtrader.core.types import Fill, Liquidity, Opportunity, Order, Side
from pmtrader.persistence.models import (
    AuditRow,
    Base,
    DailyStatRow,
    FillRow,
    KVRow,
    LLMDecisionRow,
    OpportunityRow,
    OrderRow,
    PnLSnapshotRow,
    RiskEventRow,
    SettlementRow,
)


class Repository:
    def __init__(self, url: str) -> None:
        if url.startswith("sqlite") and ":///" in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragma(dbapi_conn: Any, _rec: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------ kv
    async def kv_get(self, key: str, default: Any = None) -> Any:
        async with self.session() as s:
            row = await s.get(KVRow, key)
            return row.value if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        async with self.session() as s, s.begin():
            await s.merge(KVRow(key=key, value=value))

    # ------------------------------------------------------------------ orders / fills
    async def upsert_order(self, o: Order) -> None:
        i = o.intent
        async with self.session() as s, s.begin():
            await s.merge(
                OrderRow(
                    id=o.id,
                    exchange_id=o.exchange_id,
                    strategy=i.strategy,
                    market_id=i.market_id,
                    asset_id=i.asset_id,
                    side=i.side.value,
                    price=i.price,
                    size=i.size,
                    order_type=i.order_type.value,
                    post_only=i.post_only,
                    reduce_only=i.reduce_only,
                    group_id=i.group_id,
                    reason=i.reason,
                    source=i.source,
                    status=o.status.value,
                    filled_size=o.filled_size,
                    avg_fill_price=o.avg_fill_price,
                    fees=o.fees_paid,
                    created_ts=o.created_ts,
                    updated_ts=o.updated_ts,
                    reject_reason=o.reject_reason,
                )
            )

    async def add_fill(self, f: Fill, outcome: str, event_id: str) -> None:
        async with self.session() as s, s.begin():
            await s.merge(
                FillRow(
                    trade_id=f.trade_id,
                    order_id=f.order_id,
                    market_id=f.market_id,
                    asset_id=f.asset_id,
                    outcome=outcome,
                    event_id=event_id,
                    side=f.side.value,
                    price=f.price,
                    size=f.size,
                    fee=f.fee,
                    ts=f.ts,
                    liquidity=f.liquidity.value,
                    strategy=f.strategy,
                )
            )

    async def fills(self, since: float = 0.0) -> list[tuple[Fill, str, str]]:
        async with self.session() as s:
            rows = (await s.execute(select(FillRow).where(FillRow.ts >= since).order_by(FillRow.ts))).scalars()
            return [
                (
                    Fill(
                        r.order_id,
                        r.market_id,
                        r.asset_id,
                        Side(r.side),
                        r.price,
                        r.size,
                        r.fee,
                        r.ts,
                        Liquidity(r.liquidity),
                        r.strategy,
                        r.trade_id,
                    ),
                    r.outcome,
                    r.event_id,
                )
                for r in rows
            ]

    async def add_settlement(self, asset_id: str, market_id: str, payout: float, ts: float) -> None:
        async with self.session() as s, s.begin():
            s.add(SettlementRow(asset_id=asset_id, market_id=market_id, payout=payout, ts=ts))

    async def settlements(self) -> list[tuple[str, str, float, float]]:
        async with self.session() as s:
            rows = (await s.execute(select(SettlementRow).order_by(SettlementRow.ts))).scalars()
            return [(r.asset_id, r.market_id, r.payout, r.ts) for r in rows]

    async def open_orders(self) -> list[OrderRow]:
        terminal = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")
        async with self.session() as s:
            return list((await s.execute(select(OrderRow).where(OrderRow.status.not_in(terminal)))).scalars())

    async def recent_orders(self, limit: int = 100) -> list[OrderRow]:
        async with self.session() as s:
            q = select(OrderRow).order_by(OrderRow.created_ts.desc()).limit(limit)
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------------ P&L
    async def add_snapshot(self, **kw: float) -> None:
        async with self.session() as s, s.begin():
            s.add(PnLSnapshotRow(**kw))

    async def snapshots(self, since: float, limit: int = 5000) -> list[PnLSnapshotRow]:
        async with self.session() as s:
            q = select(PnLSnapshotRow).where(PnLSnapshotRow.ts >= since).order_by(PnLSnapshotRow.ts).limit(limit)
            return list((await s.execute(q)).scalars())

    async def prune_snapshots(self, before: float) -> None:
        async with self.session() as s, s.begin():
            await s.execute(delete(PnLSnapshotRow).where(PnLSnapshotRow.ts < before))

    async def daily(self, day: str) -> DailyStatRow:
        async with self.session() as s, s.begin():
            row = await s.get(DailyStatRow, day)
            if row is None:
                row = DailyStatRow(day=day, new_markets=[], llm_calls=0, llm_tokens=0, llm_usd=0.0)
                s.add(row)
            return row

    async def save_daily(self, row: DailyStatRow) -> None:
        async with self.session() as s, s.begin():
            await s.merge(row)

    async def fills_between(self, start: float, end: float) -> list[FillRow]:
        async with self.session() as s:
            q = select(FillRow).where(FillRow.ts >= start, FillRow.ts < end).order_by(FillRow.ts)
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------------ logs
    async def add_opportunities(self, ts: float, opps: list[Opportunity]) -> None:
        async with self.session() as s, s.begin():
            s.add_all(
                OpportunityRow(
                    ts=ts,
                    strategy=o.strategy,
                    market_id=o.market_id,
                    question=o.question[:500],
                    edge=o.edge,
                    fair=o.fair,
                    size_hint=o.size_hint,
                    score=o.score,
                    note=o.note,
                )
                for o in opps
            )

    async def add_llm_decision(self, **kw: Any) -> None:
        async with self.session() as s, s.begin():
            s.add(LLMDecisionRow(**kw))

    async def audit(self, ts: float, actor: str, action: str, params: dict[str, Any], result: str) -> None:
        async with self.session() as s, s.begin():
            s.add(AuditRow(ts=ts, actor=actor, action=action, params=params, result=result))

    async def recent_audit(self, limit: int = 50) -> list[AuditRow]:
        async with self.session() as s:
            return list((await s.execute(select(AuditRow).order_by(AuditRow.ts.desc()).limit(limit))).scalars())

    async def risk_event(self, ts: float, code: str, message: str, severity: str, context: Any = None) -> None:
        ctx = asdict(context) if context is not None and hasattr(context, "__dataclass_fields__") else (context or {})
        safe = {k: (v.value if hasattr(v, "value") else v) for k, v in dict(ctx).items()}
        async with self.session() as s, s.begin():
            s.add(RiskEventRow(ts=ts, code=code, message=message, severity=severity, context=safe))
