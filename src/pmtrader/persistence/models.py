"""SQLAlchemy models. Fills + settlements are the source of truth for positions (event-sourced ledger)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    exchange_id: Mapped[str | None] = mapped_column(String(100), index=True)
    strategy: Mapped[str] = mapped_column(String(40), index=True)
    market_id: Mapped[str] = mapped_column(String(40), index=True)
    asset_id: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(4))
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    group_id: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="strategy")
    status: Mapped[str] = mapped_column(String(20), index=True)
    filled_size: Mapped[float] = mapped_column(Float, default=0)
    avg_fill_price: Mapped[float] = mapped_column(Float, default=0)
    fees: Mapped[float] = mapped_column(Float, default=0)
    created_ts: Mapped[float] = mapped_column(Float, index=True)
    updated_ts: Mapped[float] = mapped_column(Float)
    reject_reason: Mapped[str] = mapped_column(Text, default="")


class FillRow(Base):
    __tablename__ = "fills"
    trade_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    market_id: Mapped[str] = mapped_column(String(40), index=True)
    asset_id: Mapped[str] = mapped_column(String(100), index=True)
    outcome: Mapped[str] = mapped_column(String(200), default="")
    event_id: Mapped[str] = mapped_column(String(40), default="")
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    ts: Mapped[float] = mapped_column(Float, index=True)
    liquidity: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(40), index=True)


class SettlementRow(Base):
    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(100), index=True)
    market_id: Mapped[str] = mapped_column(String(40))
    payout: Mapped[float] = mapped_column(Float)
    ts: Mapped[float] = mapped_column(Float)


class PnLSnapshotRow(Base):
    __tablename__ = "pnl_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    realized: Mapped[float] = mapped_column(Float)
    unrealized: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    day_pnl: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class DailyStatRow(Base):
    __tablename__ = "daily_stats"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD in report tz
    start_equity: Mapped[float | None] = mapped_column(Float)
    new_markets: Mapped[list[str]] = mapped_column(JSON, default=list)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_usd: Mapped[float] = mapped_column(Float, default=0)


class OpportunityRow(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    strategy: Mapped[str] = mapped_column(String(40))
    market_id: Mapped[str] = mapped_column(String(40))
    question: Mapped[str] = mapped_column(Text)
    edge: Mapped[float] = mapped_column(Float)
    fair: Mapped[float | None] = mapped_column(Float)
    size_hint: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")


class LLMDecisionRow(Base):
    __tablename__ = "llm_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    market_id: Mapped[str] = mapped_column(String(40), index=True)
    opportunity_key: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float)
    fair: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[float] = mapped_column(Float, default=0)
    market_price: Mapped[float | None] = mapped_column(Float)


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(Text, default="")


class RiskEventRow(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    code: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10))
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class KVRow(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
