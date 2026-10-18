"""Typed configuration.

Precedence (highest first): environment (``PMT_`` prefix, ``__`` for nesting) → ``.env`` →
``config/local.yaml`` → ``config/default.yaml``. Secrets live only in env/.env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from pmtrader.core.types import Mode

ROOT = Path(__file__).resolve().parents[2]


class UniverseConfig(BaseModel):
    max_markets: int = 300
    min_liquidity_usd: float = 1000.0
    min_volume_24h_usd: float = 500.0
    max_days_to_close: float = 120.0
    min_minutes_to_close: float = 5.0
    include_tags: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    catalog_refresh_seconds: float = 60.0
    catalog_max_pages: int = 20


class ScannerConfig(BaseModel):
    interval_ms: int = 1000
    top_k: int = 25
    min_score: float = 0.0
    record_opportunities_every_s: float = 60.0


class StrategyConfig(BaseModel):
    """Common knobs; strategy-specific extras go in ``params``."""

    enabled: bool = True
    auto_trade: bool = True  # False = scanner shows it but never trades it
    min_edge: float = 0.005  # probability points net of fees
    max_usd_per_trade: float = 50.0
    kelly_fraction: float = 0.25
    params: dict[str, Any] = Field(default_factory=dict)


def _default_strategies() -> dict[str, StrategyConfig]:
    return {
        "complete_set_arb": StrategyConfig(min_edge=0.003, max_usd_per_trade=100),
        "negrisk_basket_arb": StrategyConfig(min_edge=0.005, max_usd_per_trade=100),
        "ladder_arb": StrategyConfig(min_edge=0.005, max_usd_per_trade=100),
        "crypto_fair_value": StrategyConfig(min_edge=0.02, max_usd_per_trade=40),
        "market_maker": StrategyConfig(enabled=False, auto_trade=False, max_usd_per_trade=50),
        "longshot_fade": StrategyConfig(enabled=False, auto_trade=False, min_edge=0.02),
    }


class RiskLimits(BaseModel):
    max_new_markets_per_day: int = 10
    max_daily_loss_usd: float = 50.0
    max_drawdown_pct: float = 15.0
    max_order_usd: float = 50.0
    max_position_usd_per_market: float = 100.0
    max_exposure_per_event_usd: float = 150.0
    max_gross_exposure_usd: float = 500.0
    max_open_orders: int = 40
    max_orders_per_minute: int = 30
    max_price_deviation: float = 0.05
    min_time_to_close_minutes: float = 10.0
    stale_book_seconds: float = 15.0
    min_market_liquidity_usd: float = 1000.0
    kelly_fraction: float = 0.25
    max_consecutive_rejects: int = 5
    flatten_max_slippage: float = 0.05
    allowed_strategies: list[str] = Field(
        default_factory=lambda: ["complete_set_arb", "negrisk_basket_arb", "ladder_arb", "crypto_fair_value"]
    )
    blocked_keywords: list[str] = Field(default_factory=list)


class RiskCeilings(BaseModel):
    """Hard upper bounds that runtime overrides (UI/Discord) can never exceed."""

    max_new_markets_per_day: int = 100
    max_daily_loss_usd: float = 1000.0
    max_drawdown_pct: float = 50.0
    max_order_usd: float = 1000.0
    max_position_usd_per_market: float = 2000.0
    max_exposure_per_event_usd: float = 3000.0
    max_gross_exposure_usd: float = 10000.0
    max_open_orders: int = 200
    max_orders_per_minute: int = 120
    kelly_fraction: float = 0.5


class PaperConfig(BaseModel):
    starting_cash_usd: float = 1000.0
    latency_ms: int = 150
    maker_fill_model: Literal["cross", "touch"] = "cross"


class LiveConfig(BaseModel):
    reconcile_seconds: float = 15.0
    builder_code: str | None = None


class ExecutionConfig(BaseModel):
    paper: PaperConfig = Field(default_factory=PaperConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)


class LLMProviderConfig(BaseModel):
    name: str
    kind: Literal["anthropic", "openai_compat"]
    model: str
    base_url: str | None = None
    api_key_env: str | None = None  # env var holding the key (never put keys in YAML)
    effort: Literal["low", "medium", "high"] | None = "low"
    timeout_s: float = 60.0
    input_usd_per_mtok: float = 0.0  # for budget accounting
    output_usd_per_mtok: float = 0.0


class LLMConfig(BaseModel):
    gate_mode: Literal["off", "advisory", "required"] = "off"
    fallback: Literal["skip", "deterministic"] = "deterministic"
    providers: list[LLMProviderConfig] = Field(default_factory=list)
    batch_size: int = 12
    min_confidence: float = 0.6
    cache_ttl_s: float = 6 * 3600
    price_bucket: float = 0.02
    daily_max_calls: int = 100
    daily_max_tokens: int = 200_000
    daily_max_usd: float = 1.0
    escalate_notional_usd: float = 200.0
    borderline: tuple[float, float] = (0.45, 0.65)
    min_interval_s: float = 30.0


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    max_fps: float = 10.0


class DiscordConfig(BaseModel):
    enabled: bool = False
    guild_id: int | None = None
    channel_id: int | None = None  # notifications + control panel
    owner_user_ids: list[int] = Field(default_factory=list)
    notify_fills: bool = True
    notify_opportunities_min_score: float | None = None


class ReportsConfig(BaseModel):
    timezone: str = "America/New_York"
    daily_time: str = "23:55"
    snapshot_seconds: float = 60.0
    hourly_discord: bool = False


class ExternalConfig(BaseModel):
    binance_base_url: str = "https://data-api.binance.vision"
    deribit_base_url: str = "https://www.deribit.com"
    refresh_seconds: float = 2.0
    vol_refresh_seconds: float = 300.0
    symbols: dict[str, str] = Field(
        default_factory=lambda: {
            "bitcoin": "BTCUSDT",
            "ethereum": "ETHUSDT",
            "solana": "SOLUSDT",
            "xrp": "XRPUSDT",
            "dogecoin": "DOGEUSDT",
            "bnb": "BNBUSDT",
        }
    )


class PolymarketConfig(BaseModel):
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_ping_seconds: float = 10.0
    ws_max_assets_per_conn: int = 400


class Secrets(BaseModel):
    polymarket_private_key: SecretStr | None = None
    polymarket_wallet: str | None = None
    polymarket_relayer_api_key: SecretStr | None = None
    polymarket_relayer_api_key_address: str | None = None
    discord_token: SecretStr | None = None
    web_token: SecretStr | None = None
    live_confirm: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: Any) -> Any:
        # ``KEY=`` lines copied from .env.example arrive as "", which must mean "not configured": an empty
        # web_token would otherwise enable auth with a token nobody can supply and lock the UI out.
        if isinstance(v, str) and not v.strip():
            return None
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PMT_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    mode: Mode = Mode.PAPER
    data_dir: Path = ROOT / "data"
    log_dir: Path = ROOT / "logs"
    db_url: str | None = None
    log_level: str = "INFO"

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    strategies: dict[str, StrategyConfig] = Field(default_factory=_default_strategies)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    risk_ceilings: RiskCeilings = Field(default_factory=RiskCeilings)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    external: ExternalConfig = Field(default_factory=ExternalConfig)
    secrets: Secrets = Field(default_factory=Secrets)

    @field_validator("strategies", mode="before")
    @classmethod
    def _merge_strategy_defaults(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        merged: dict[str, Any] = {k: s.model_dump() for k, s in _default_strategies().items()}
        for name, cfg in v.items():
            base = merged.get(name, {})
            merged[name] = {**base, **(cfg.model_dump() if isinstance(cfg, BaseModel) else cfg or {})}
        return merged

    @property
    def database_url(self) -> str:
        return self.db_url or f"sqlite+aiosqlite:///{self.data_dir / 'pmtrader.db'}"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env beats YAML (passed as init kwargs)
        return env_settings, dotenv_settings, init_settings, file_secret_settings


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(config_dir: Path | None = None, extra_files: list[Path] | None = None) -> Settings:
    config_dir = config_dir or ROOT / "config"
    data: dict[str, Any] = {}
    for path in [config_dir / "default.yaml", config_dir / "local.yaml", *(extra_files or [])]:
        if path.exists():
            loaded = yaml.safe_load(path.read_text()) or {}
            data = _deep_merge(data, loaded)
    return Settings(**data)
