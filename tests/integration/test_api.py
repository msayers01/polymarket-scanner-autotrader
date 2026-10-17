import httpx
import pytest
from fastapi.testclient import TestClient

from pmtrader.api.server import create_app
from pmtrader.app import Engine
from pmtrader.config import Settings
from pmtrader.core.types import Mode


@pytest.fixture
async def engine(tmp_path):
    s = Settings(mode=Mode.PAPER, data_dir=tmp_path, log_dir=tmp_path)
    e = Engine(s)
    await e.repo.init()
    await e.portfolio.load()
    await e.control.load()
    yield e
    await e.repo.close()
    await e.http.aclose()


async def test_snapshot_and_controls(engine):
    app = create_app(engine, start_engine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        snap = (await c.get("/api/snapshot")).json()
        assert snap["control"]["state"] == "RUNNING"
        assert snap["portfolio"]["equity"] == pytest.approx(1000)
        assert (await c.post("/api/control/pause", json={})).status_code == 200
        assert (await c.get("/api/snapshot")).json()["control"]["state"] == "PAUSED"
        r = await c.post("/api/control/kill", json={"reason": "test"})
        assert r.status_code == 200 and "HALTED" in r.json()["result"]
        r = await c.post("/api/control/resume", json={})
        assert r.status_code == 409  # needs confirm
        r = await c.post("/api/control/resume", json={"confirm": "RESUME"})
        assert r.status_code == 200
        r = await c.put("/api/limits", json={"changes": {"max_new_markets_per_day": 3}})
        assert r.json()["applied"] == {"max_new_markets_per_day": 3}
        r = await c.put("/api/limits", json={"changes": {"max_daily_loss_usd": 10**9}})
        assert r.status_code == 409  # above hard ceiling
        audit = (await c.get("/api/audit")).json()
        assert {a["action"] for a in audit} >= {"pause", "kill", "resume", "set_limits"}


async def test_token_auth(engine):
    from pydantic import SecretStr

    engine.settings.secrets.web_token = SecretStr("s3cret")
    app = create_app(engine, start_engine=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/snapshot")).status_code == 401
        ok = await c.get("/api/snapshot", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_websocket_snapshot(engine):
    app = create_app(engine, start_engine=False)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert "portfolio" in msg["data"] and "control" in msg["data"]
