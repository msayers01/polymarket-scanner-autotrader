"""`pmtrader` command line."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from pmtrader.config import Settings, load_settings
from pmtrader.core.types import Mode
from pmtrader.logging_setup import setup_logging

app = typer.Typer(add_completion=False, help="Polymarket scanner + autotrader")
console = Console()


def _settings(mode: str | None, config: list[Path] | None) -> Settings:
    s = load_settings(extra_files=config)
    if mode:
        s.mode = Mode(mode)
    return s


@app.command()
def run(
    mode: str = typer.Option(None, help="scanner | paper | live (overrides config)"),
    host: str = typer.Option(None, help="bind host (default from config: 127.0.0.1)"),
    port: int = typer.Option(None, help="bind port"),
    config: list[Path] = typer.Option(None, "--config", "-c", help="extra YAML config file(s)"),
) -> None:
    """Run engine + web UI (+ Discord bot if enabled)."""
    import uvicorn

    from pmtrader.api.server import create_app
    from pmtrader.app import Engine

    s = _settings(mode, config)
    setup_logging(Path(s.log_dir), s.log_level)
    if s.mode is Mode.LIVE:
        console.print("[bold red]LIVE MODE — real money. Starting in PAUSED state.[/]")
    engine = Engine(s)
    api = create_app(engine)
    uvicorn.run(api, host=host or s.api.host, port=port or s.api.port, log_level="warning", ws="websockets")


def _scan_table(engine, top: int) -> Table:  # type: ignore[no-untyped-def]
    t = Table(
        title=f"Opportunities · universe {len(engine.catalog.universe)} · books {len(engine.feed.books)} · "
        f"scan {engine.scanner.last_duration_ms:.0f}ms",
        expand=True,
    )
    for col in ("strategy", "market", "legs", "edge¢", "fair", "size", "EV$", "APR", "days"):
        t.add_column(col, overflow="fold")
    for o in engine.opportunities_view(top):
        legs = " + ".join(f"{lg['side'][0]} {lg['outcome'][:12]}@{lg['price']:.3f}" for lg in o["legs"])
        t.add_row(
            o["strategy"],
            o["question"][:70],
            legs,
            f"{o['edge'] * 100:.2f}",
            "" if o["fair"] is None else f"{o['fair']:.3f}",
            f"{o['size']:.0f}",
            f"{o['ev_usd']:.2f}",
            f"{o['score'] * 100:.0f}%",
            "" if o["days"] is None else f"{o['days']:.1f}",
        )
    return t


@app.command()
def scan(
    top: int = typer.Option(20, help="rows to display"),
    seconds: float = typer.Option(0, help="stop after N seconds (0 = run until Ctrl-C)"),
    config: list[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Scanner only (no trading): live table of ranked opportunities in the terminal."""
    from pmtrader.app import Engine

    s = _settings("scanner", config)
    setup_logging(Path(s.log_dir), s.log_level, console=False)

    async def main() -> None:
        engine = Engine(s)
        await engine.start()
        try:
            with Live(_scan_table(engine, top), console=console, refresh_per_second=2) as live:
                elapsed = 0.0
                while seconds <= 0 or elapsed < seconds:
                    await asyncio.sleep(1.0)
                    elapsed += 1.0
                    live.update(_scan_table(engine, top))
        finally:
            await engine.stop()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


@app.command()
def pnl(
    period: str = typer.Option("today", help="today | 7d | all"),
    config: list[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Print a P&L report from the local database (positions marked at cost — engine not running)."""
    from pmtrader.app import Engine
    from pmtrader.portfolio.reports import render_text

    s = _settings(None, config)

    async def main() -> None:
        engine = Engine(s)
        await engine.repo.init()
        await engine.portfolio.load()
        console.print(render_text(await engine.portfolio.build_report(period)))
        await engine.repo.close()
        await engine.http.aclose()

    asyncio.run(main())


@app.command()
def calibrate(
    max_markets: int = typer.Option(1500, help="resolved markets to sample"),
    kappa: float = typer.Option(20.0, help="Beta prior strength toward market price"),
    config: list[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Fit the longshot calibration curve from resolved markets → data/calibration.json."""
    from pmtrader.backtest.calibrate import fit_and_save

    s = _settings(None, config)
    setup_logging(Path(s.log_dir), s.log_level)
    out = Path(s.data_dir) / "calibration.json"
    stats = asyncio.run(fit_and_save(out, s.polymarket.gamma_url, s.polymarket.clob_url, max_markets, kappa))
    console.print(stats)


@app.command()
def record(
    minutes: float = typer.Option(60, help="how long to record"),
    config: list[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Record raw order-book streams for the current universe to data/recordings/."""
    from pmtrader.app import Engine
    from pmtrader.backtest.recorder import Recorder

    s = _settings("scanner", config)
    setup_logging(Path(s.log_dir), s.log_level)
    rec = Recorder(Path(s.data_dir) / "recordings")

    async def main() -> None:
        engine = Engine(s)
        engine.feed._raw_sink = rec
        await engine.start()
        try:
            await asyncio.sleep(minutes * 60)
        finally:
            await engine.stop()
            rec.close()
            console.print(f"recorded {rec.lines} messages")

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


@app.command("check-config")
def check_config(config: list[Path] = typer.Option(None, "--config", "-c")) -> None:
    """Validate and print the effective configuration (secrets masked)."""
    s = _settings(None, config)
    console.print_json(s.model_dump_json(exclude={"secrets"}))
    console.print({k: ("set" if v else "—") for k, v in s.secrets.model_dump().items()})


if __name__ == "__main__":
    app()
