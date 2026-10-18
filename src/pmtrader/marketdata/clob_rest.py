"""Public CLOB REST: book snapshots and price history."""

from __future__ import annotations

from typing import Any

import httpx

Level = tuple[float, float]


def parse_levels(raw: list[dict[str, Any]]) -> list[Level]:
    return [(float(x["price"]), float(x["size"])) for x in raw or []]


class ClobRestClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = client or httpx.AsyncClient(timeout=15.0, http2=True)

    async def close(self) -> None:
        await self._http.aclose()

    async def book(self, asset_id: str) -> dict[str, Any]:
        r = await self._http.get(f"{self._base}/book", params={"token_id": asset_id})
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data

    async def books(self, asset_ids: list[str]) -> list[dict[str, Any]]:
        r = await self._http.post(f"{self._base}/books", json=[{"token_id": a} for a in asset_ids])
        r.raise_for_status()
        data: list[dict[str, Any]] = r.json()
        return data

    async def price_history(
        self,
        asset_id: str,
        interval: str = "1d",
        fidelity: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[tuple[float, float]]:
        """[(ts, price)] — used for calibration and charts."""
        params: dict[str, Any] = {"market": asset_id, "fidelity": fidelity}
        if start_ts is not None and end_ts is not None:
            params.update(startTs=start_ts, endTs=end_ts)
        else:
            params["interval"] = interval
        r = await self._http.get(f"{self._base}/prices-history", params=params)
        r.raise_for_status()
        return [(float(p["t"]), float(p["p"])) for p in r.json().get("history", [])]
