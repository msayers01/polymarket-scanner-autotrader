"""Strategy registry: name → class. Add new strategies here (and to config/default.yaml)."""

from __future__ import annotations

from pathlib import Path

from pmtrader.config import Settings
from pmtrader.strategies.base import Strategy
from pmtrader.strategies.crypto_fair_value import CryptoFairValue
from pmtrader.strategies.longshot_fade import LongshotFade
from pmtrader.strategies.market_maker import MarketMaker
from pmtrader.strategies.structural_arb import CompleteSetArb, LadderArb, NegRiskBasketArb

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (CompleteSetArb, NegRiskBasketArb, LadderArb, CryptoFairValue, MarketMaker, LongshotFade)
}


def build_strategies(settings: Settings) -> dict[str, Strategy]:
    out: dict[str, Strategy] = {}
    for name, cfg in settings.strategies.items():
        if not cfg.enabled:
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"unknown strategy in config: {name}")
        if cls is LongshotFade:
            out[name] = LongshotFade(cfg, calibration_path=Path(settings.data_dir) / "calibration.json")
        else:
            out[name] = cls(cfg)
    return out
