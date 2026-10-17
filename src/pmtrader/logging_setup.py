"""structlog configuration: pretty console + JSON-lines files (all events, and trades only)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

TRADE_EVENT_PREFIXES = ("order.", "fill", "risk.", "control.", "pnl.")


class _TradeFilter(logging.Filter):
    """Routes order/fill/risk/control/P&L events to trades.jsonl."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg
        event = msg.get("event", "") if isinstance(msg, dict) else str(msg)
        return str(event).startswith(TRADE_EVENT_PREFIXES)


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    json_fmt = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared,
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    all_h = logging.handlers.TimedRotatingFileHandler(log_dir / "pmtrader.jsonl", when="midnight", backupCount=30)
    all_h.setFormatter(json_fmt)
    root.addHandler(all_h)

    trades_h = logging.handlers.TimedRotatingFileHandler(log_dir / "trades.jsonl", when="midnight", backupCount=365)
    trades_h.setFormatter(json_fmt)
    trades_h.addFilter(_TradeFilter())
    root.addHandler(trades_h)

    if console:
        con = logging.StreamHandler(sys.stderr)
        con.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                ],
                foreign_pre_chain=shared,
            )
        )
        root.addHandler(con)

    for noisy in ("httpx", "httpcore", "websockets", "discord", "uvicorn.access", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
