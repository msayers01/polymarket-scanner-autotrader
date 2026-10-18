"""Fit the longshot-fade calibration curve from resolved Polymarket markets (docs/STRATEGIES.md §6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from pmtrader.marketdata.clob_rest import ClobRestClient
from pmtrader.marketdata.gamma import GammaClient, _jlist, _ts
from pmtrader.quant.calibration import CalibrationCurve, brier

log = structlog.get_logger(__name__)


async def _price_at(clob: ClobRestClient, token: str, ts: float) -> float | None:
    try:
        hist = await clob.price_history(token, fidelity=60, start_ts=int(ts - 6 * 3600), end_ts=int(ts + 3600))
    except httpx.HTTPError:
        return None
    before = [p for t, p in hist if t <= ts]
    return before[-1] if before else None


async def collect(
    gamma_url: str,
    clob_url: str,
    max_markets: int = 1500,
    horizons_days: tuple[float, ...] = (1.0, 7.0),
    concurrency: int = 8,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as http:
        gamma, clob = GammaClient(gamma_url, http), ClobRestClient(clob_url, http)
        raw: list[dict[str, Any]] = []
        offset = 0
        while len(raw) < max_markets:
            page = await gamma.closed_markets(limit=500, offset=offset, order="volumeNum", ascending="false")
            if not page:
                break
            raw.extend(page)
            offset += 500
        rows: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(concurrency)

        async def one(m: dict[str, Any]) -> None:
            outcomes, prices, tokens = (
                _jlist(m.get("outcomes")),
                _jlist(m.get("outcomePrices")),
                _jlist(m.get("clobTokenIds")),
            )
            if len(outcomes) != 2 or len(tokens) != 2 or len(prices) != 2:
                return
            try:
                final = [float(p) for p in prices]
            except (TypeError, ValueError):
                return
            if sorted(final) != [0.0, 1.0]:
                return  # unresolved or 50-50
            end = _ts(m.get("closedTime")) or _ts(m.get("endDate"))
            if end is None:
                return
            tags = [t.get("slug", "") for t in ((m.get("events") or [{}])[0].get("tags") or []) if isinstance(t, dict)]
            for h in horizons_days:
                async with sem:
                    p = await _price_at(clob, str(tokens[0]), end - h * 86400)
                if p is not None and 0.0 < p < 1.0:
                    rows.append(
                        {
                            "p": p,
                            "y": int(final[0] == 1.0),
                            "h": h,
                            "cat": tags[0] if tags else "",
                            "id": str(m.get("id")),
                        }
                    )

        await asyncio.gather(*(one(m) for m in raw[:max_markets]))
        return rows


async def fit_and_save(
    out: Path,
    gamma_url: str,
    clob_url: str,
    max_markets: int = 1500,
    kappa: float = 20.0,
) -> dict[str, Any]:
    rows = await collect(gamma_url, clob_url, max_markets)
    if len(rows) < 100:
        raise RuntimeError(f"only {len(rows)} samples collected — not enough to fit a calibration curve")
    prices, ys = [r["p"] for r in rows], [r["y"] for r in rows]
    curve = CalibrationCurve.fit(prices, ys, kappa=kappa, samples=len(rows), source="gamma+clob prices-history")
    curve.save(out)
    (out.parent / "calibration_samples.json").write_text(json.dumps(rows))
    fitted = [curve.predict(p)[0] for p in prices]
    stats = {"samples": len(rows), "brier_market": brier(prices, ys), "brier_calibrated": brier(fitted, ys)}
    log.info("calibration.saved", path=str(out), **stats)
    return stats
