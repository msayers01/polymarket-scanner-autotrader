"""FastAPI app: REST commands + WebSocket stream + static web UI. Every command goes through ControlPlane."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pmtrader.api.ws import WebSocketHub, _sanitize
from pmtrader.app import Engine
from pmtrader.control.plane import CommandError
from pmtrader.core.types import OrderIntent, OrderType, Side, round_price, round_size

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


class PauseBody(BaseModel):
    reason: str = "manual pause (web)"


class ResumeBody(BaseModel):
    confirm: str | None = None


class KillBody(BaseModel):
    reason: str = "kill switch (web)"
    flatten: bool = False


class OrderBody(BaseModel):
    market_id: str
    asset_id: str
    side: Literal["BUY", "SELL"]
    price: float = Field(gt=0, lt=1)
    size: float = Field(gt=0)
    order_type: Literal["GTC", "FAK", "FOK", "GTD"] = "GTC"
    post_only: bool = False


class LimitsBody(BaseModel):
    changes: dict[str, Any]


class StrategyBody(BaseModel):
    auto: bool


class CloseBody(BaseModel):
    price: float | None = None
    fraction: float = Field(default=1.0, gt=0, le=1)


def create_app(engine: Engine, start_engine: bool = True) -> FastAPI:
    hub = WebSocketHub(engine, engine.settings.api.max_fps)
    token = engine.settings.secrets.web_token

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_engine:
            await engine.start()
        hub_task = asyncio.create_task(hub.run())
        try:
            yield
        finally:
            hub_task.cancel()
            if start_engine:
                await engine.stop()

    app = FastAPI(title="pmtrader", version="0.1.0", lifespan=lifespan)

    def _check(supplied: str | None) -> None:
        if token is None:
            return
        if not supplied or not hmac.compare_digest(supplied, token.get_secret_value()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")

    def auth(request: Request) -> str:
        hdr = request.headers.get("authorization", "")
        supplied = hdr.removeprefix("Bearer ").strip() if hdr else request.query_params.get("token")
        _check(supplied)
        host = request.client.host if request.client else "?"
        return f"web:{host}"

    @app.exception_handler(CommandError)
    async def _command_error(_req: Request, exc: CommandError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    # ------------------------------------------------------------------ reads
    @app.get("/api/snapshot")
    async def snapshot(_actor: str = Depends(auth)) -> JSONResponse:
        return JSONResponse(_sanitize(engine.snapshot()))

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(_sanitize(engine.health()))

    @app.get("/api/pnl")
    async def pnl(period: Literal["today", "7d", "all"] = "today", _actor: str = Depends(auth)) -> JSONResponse:
        r = await engine.portfolio.build_report(period)
        return JSONResponse(_sanitize(r.to_dict()))

    @app.get("/api/equity")
    async def equity(hours: float = Query(24, gt=0, le=24 * 90), _actor: str = Depends(auth)) -> list[list[float]]:
        rows = await engine.repo.snapshots(engine.clock.now() - hours * 3600)
        step = max(1, len(rows) // 500)
        return [[r.ts, r.equity, r.day_pnl] for r in rows[::step]]

    @app.get("/api/orders/recent")
    async def recent_orders(limit: int = 100, _actor: str = Depends(auth)) -> list[dict[str, Any]]:
        rows = await engine.repo.recent_orders(min(limit, 500))
        return [
            {
                c: getattr(r, c)
                for c in (
                    "id",
                    "strategy",
                    "market_id",
                    "side",
                    "price",
                    "size",
                    "status",
                    "filled_size",
                    "avg_fill_price",
                    "fees",
                    "created_ts",
                    "reject_reason",
                )
            }
            for r in rows
        ]

    @app.get("/api/audit")
    async def audit(_actor: str = Depends(auth)) -> list[dict[str, Any]]:
        return [
            {"ts": r.ts, "actor": r.actor, "action": r.action, "params": r.params, "result": r.result}
            for r in await engine.repo.recent_audit()
        ]

    @app.get("/api/markets")
    async def search_markets(
        q: str = "", limit: int = Query(20, le=100), _actor: str = Depends(auth)
    ) -> list[dict[str, Any]]:
        """Search the catalog by question text (universe markets first, then by 24h volume)."""
        needle = q.lower().strip()
        universe = set(engine.catalog.universe)
        hits = [m for m in engine.catalog.markets.values() if not needle or needle in m.question.lower()]
        hits.sort(key=lambda m: (m.id not in universe, -m.volume_24h_usd))
        return [
            {
                "id": m.id,
                "question": m.question,
                "volume_24h": round(m.volume_24h_usd),
                "live": m.id in universe,
                "end_time": m.end_time,
                "outcomes": [o.name for o in m.outcomes],
            }
            for m in hits[:limit]
        ]

    @app.get("/api/market/{market_id}")
    async def market(market_id: str, _actor: str = Depends(auth)) -> JSONResponse:
        m = engine.catalog.get(market_id)
        if m is None:
            raise HTTPException(404, "unknown market")
        if market_id not in engine.catalog.universe:
            await engine.catalog.watch(market_id)
            await asyncio.sleep(1.0)  # give the WS a moment to deliver the snapshot
        books = {}
        for o in m.outcomes:
            b = engine.feed.book(o.asset_id)
            books[o.asset_id] = {"outcome": o.name, **(b.snapshot(8) if b else {"bids": [], "asks": []})}
        return JSONResponse(
            _sanitize(
                {
                    "id": m.id,
                    "question": m.question,
                    "slug": m.slug,
                    "end_time": m.end_time,
                    "tick": m.tick_size,
                    "min_size": m.min_order_size,
                    "fees": m.fees.__dict__
                    if hasattr(m.fees, "__dict__")
                    else {"rate": m.fees.rate, "exponent": m.fees.exponent},
                    "books": books,
                    "outcomes": [{"name": o.name, "asset_id": o.asset_id} for o in m.outcomes],
                    "url": f"https://polymarket.com/event/{m.event.slug if m.event else m.slug}",
                }
            )
        )

    # ------------------------------------------------------------------ control
    @app.post("/api/control/pause")
    async def pause(body: PauseBody, actor: str = Depends(auth)) -> dict[str, str]:
        return {"result": await engine.control.pause(actor, body.reason)}

    @app.post("/api/control/resume")
    async def resume(body: ResumeBody, actor: str = Depends(auth)) -> dict[str, str]:
        return {"result": await engine.control.resume(actor, body.confirm)}

    @app.post("/api/control/kill")
    async def kill(body: KillBody, actor: str = Depends(auth)) -> dict[str, str]:
        return {"result": await engine.control.kill(actor, body.reason, body.flatten)}

    @app.post("/api/control/flatten")
    async def flatten(actor: str = Depends(auth)) -> dict[str, str]:
        return {"result": await engine.control.flatten(actor)}

    @app.post("/api/control/cancel_all")
    async def cancel_all(actor: str = Depends(auth)) -> dict[str, int]:
        return {"cancelled": await engine.control.cancel_all(actor)}

    @app.put("/api/limits")
    async def set_limits(body: LimitsBody, actor: str = Depends(auth)) -> dict[str, Any]:
        return {"applied": await engine.control.set_limits(actor, body.changes)}

    @app.put("/api/strategies/{name}")
    async def set_strategy(name: str, body: StrategyBody, actor: str = Depends(auth)) -> dict[str, bool]:
        await engine.control.set_strategy_auto(actor, name, body.auto)
        return {"auto": body.auto}

    # ------------------------------------------------------------------ trading
    @app.post("/api/orders")
    async def place_order(body: OrderBody, actor: str = Depends(auth)) -> dict[str, Any]:
        m = engine.catalog.get(body.market_id)
        if m is None or m.outcome_for(body.asset_id) is None:
            raise HTTPException(400, "unknown market/asset")
        side = Side(body.side)
        intent = OrderIntent(
            strategy="manual",
            market_id=m.id,
            asset_id=body.asset_id,
            side=side,
            price=round_price(body.price, m.tick_size, side),
            size=round_size(body.size),
            order_type=OrderType(body.order_type),
            post_only=body.post_only,
            reduce_only=side is Side.SELL,
            reason="manual (web)",
            source="manual",
        )
        return await engine.control.place_manual(actor, intent)

    @app.delete("/api/orders/{order_id}")
    async def cancel_order(order_id: str, actor: str = Depends(auth)) -> dict[str, bool]:
        return {"cancelled": await engine.control.cancel_order(actor, order_id)}

    @app.post("/api/positions/{asset_id}/close")
    async def close_position(asset_id: str, body: CloseBody, actor: str = Depends(auth)) -> dict[str, Any]:
        pos = engine.ledger.positions.get(asset_id)
        m = engine.catalog.get(pos.market_id) if pos else None
        book = engine.feed.book(asset_id)
        if pos is None or m is None:
            raise HTTPException(404, "no such position")
        px = body.price if body.price is not None else (book.best_bid if book and book.best_bid else None)
        if px is None:
            raise HTTPException(409, "no bid to close into; pass an explicit price")
        intent = OrderIntent(
            "manual",
            m.id,
            asset_id,
            Side.SELL,
            round_price(px, m.tick_size, Side.SELL),
            round_size(pos.qty * body.fraction),
            OrderType.FAK if body.price is None else OrderType.GTC,
            reduce_only=True,
            reason="close (web)",
            source="manual",
        )
        return await engine.control.place_manual(actor, intent)

    @app.post("/api/opportunities/execute")
    async def execute_opportunity(key: str = Query(...), actor: str = Depends(auth)) -> dict[str, Any]:
        opp = engine.scanner.latest_by_key.get(key)
        if opp is None:
            raise HTTPException(404, "opportunity no longer present")
        strat = engine.strategies[opp.strategy]
        intents = strat.plan_entry(opp, engine.scanner.context(), strat.cfg.max_usd_per_trade)
        if not intents:
            raise HTTPException(409, "nothing to execute at current size/limits")
        for it in intents:
            it.source = "manual"
        if len(intents) > 1:
            orders, d = await engine.oms.submit_group(intents)
            await engine.repo.audit(engine.clock.now(), actor, "execute_opportunity", {"key": key}, d.code or "ok")
            return {"ok": d.ok, "code": d.code, "message": d.message, "orders": [o.id for o in orders]}
        order, d = await engine.oms.submit(intents[0])
        await engine.repo.audit(engine.clock.now(), actor, "execute_opportunity", {"key": key}, d.code or "ok")
        return {"ok": d.ok, "code": d.code, "message": d.message, "orders": [order.id] if order else []}

    @app.post("/api/reports/publish")
    async def publish_report(period: Literal["today", "7d", "all"] = "today", _actor: str = Depends(auth)) -> dict:
        r = await engine.portfolio.publish_report(period)
        return _sanitize(r.to_dict())

    # ------------------------------------------------------------------ websocket + static
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if token is not None:
            supplied = ws.query_params.get("token")
            if not supplied or not hmac.compare_digest(supplied, token.get_secret_value()):
                await ws.close(code=4401)
                return
        await hub.handle(ws)

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            f = WEB_DIST / path
            if path and f.is_file() and WEB_DIST in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def no_ui() -> Response:
            return Response("Web UI not built. Run: cd web && npm install && npm run build", media_type="text/plain")

    app.state.engine = engine
    app.state.hub = hub
    return app
