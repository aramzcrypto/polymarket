from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.types import TradingMode, decimalize


class PolymarketSettings(BaseModel):
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    user_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    chain_id: int = 137
    signature_type: int = 1
    private_key: SecretStr | None = None
    api_key: SecretStr | None = None
    secret: SecretStr | None = None
    passphrase: SecretStr | None = None
    funder_address: str | None = None
    derive_api_creds: bool = False
    heartbeat_interval_seconds: float = 5.0


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://polymarket:polymarket@postgres:5432/polymarket"
    echo: bool = False

    @model_validator(mode="after")
    def normalize_asyncpg_url(self) -> DatabaseSettings:
        if self.url.startswith("postgresql://"):
            self.url = self.url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return self


class RedisSettings(BaseModel):
    enabled: bool = False
    url: str = "redis://redis:6379/0"


class RiskLimits(BaseModel):
    max_notional_exposure_per_market: Decimal = Decimal("50")
    max_total_account_exposure: Decimal = Decimal("150")
    max_order_size: Decimal = Decimal("10")
    max_daily_loss: Decimal = Decimal("25")
    max_drawdown: Decimal = Decimal("50")
    max_inventory_imbalance: Decimal = Decimal("20")
    max_open_orders_per_market: int = 4
    stale_book_seconds: float = 3.0
    max_slippage_bps: Decimal = Decimal("150")
    min_spread: Decimal = Decimal("0.02")
    high_rate_limit_pressure_ratio: Decimal = Decimal("0.80")
    rejected_order_circuit_breaker: int = 5
    partial_fill_circuit_breaker: int = 10
    volatility_bps_circuit_breaker: Decimal = Decimal("500")
    spread_collapse_seconds: float = 10.0
    unusually_large_fill: Decimal = Decimal("25")

    @field_validator(
        "max_notional_exposure_per_market",
        "max_total_account_exposure",
        "max_order_size",
        "max_daily_loss",
        "max_drawdown",
        "max_inventory_imbalance",
        "max_slippage_bps",
        "min_spread",
        "high_rate_limit_pressure_ratio",
        "volatility_bps_circuit_breaker",
        "unusually_large_fill",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class StrategyConfig(BaseModel):
    enabled: bool = True
    markets: list[str] = Field(default_factory=list)
    token_ids: list[str] = Field(default_factory=list)
    min_spread: Decimal = Decimal("0.03")
    quote_size: Decimal = Decimal("5")
    max_quote_size: Decimal = Decimal("10")
    min_quote_size: Decimal = Decimal("1")
    max_inventory_per_side: Decimal = Decimal("20")
    quote_ttl_seconds: float = 10.0
    replace_threshold_ticks: int = 1
    liquidity_fraction: Decimal = Decimal("0.05")
    inventory_skew_factor: Decimal = Decimal("0.40")
    volatility_widening_enabled: bool = False
    cross_market_enabled: bool = False
    event_signal_enabled: bool = False
    market_query: str = "Bitcoin Up or Down - 5 Minutes"
    discovery_interval_seconds: float = 20.0
    book_poll_interval_seconds: float = 1.0
    max_seconds_to_expiry: float = 75.0
    min_seconds_to_expiry: float = 8.0
    min_longshot_price: Decimal = Decimal("0.005")
    max_longshot_price: Decimal = Decimal("0.12")
    min_reward_multiple: Decimal = Decimal("8")
    min_edge: Decimal = Decimal("0.015")
    max_spend_per_signal: Decimal = Decimal("1")
    max_spend_per_market: Decimal = Decimal("3")
    bankroll: Decimal = Decimal("50")
    kelly_fraction: Decimal = Decimal("0.05")
    volatility_window_seconds: float = 180.0
    default_volatility_bps: Decimal = Decimal("18")
    max_required_move_sigma: Decimal = Decimal("1.35")
    min_price_feed_agreement_bps: Decimal = Decimal("25")
    price_feed_stale_seconds: float = 3.0
    exit_when_edge_below: Decimal = Decimal("-0.01")

    @field_validator(
        "min_spread",
        "quote_size",
        "max_quote_size",
        "min_quote_size",
        "max_inventory_per_side",
        "liquidity_fraction",
        "inventory_skew_factor",
        "min_longshot_price",
        "max_longshot_price",
        "min_reward_multiple",
        "min_edge",
        "max_spend_per_signal",
        "max_spend_per_market",
        "bankroll",
        "kelly_fraction",
        "default_volatility_bps",
        "max_required_move_sigma",
        "min_price_feed_agreement_bps",
        "exit_when_edge_below",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class TradingSettings(BaseModel):
    live_trading: bool = False
    live_trading_acknowledged: bool = False
    tiny_live_mode: bool = False
    tiny_live_max_order_size: Decimal = Decimal("1")
    require_geoblock_ok: bool = True
    require_balance_verified: bool = True
    require_ws_connected: bool = True
    cancel_all_on_startup: bool = False
    cancel_all_on_shutdown: bool = True
    post_only_quotes: bool = True
    reconcile_interval_seconds: float = 15.0

    @field_validator("tiny_live_max_order_size", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)

    @property
    def live_enabled(self) -> bool:
        return self.live_trading and self.live_trading_acknowledged

    @property
    def mode(self) -> TradingMode:
        if not self.live_enabled:
            return TradingMode.DRY_RUN
        if self.tiny_live_mode:
            return TradingMode.TINY_LIVE
        return TradingMode.LIVE


class AdminSettings(BaseModel):
    token: SecretStr | None = None
    host: str = "0.0.0.0"
    port: int = 8000


class AlertSettings(BaseModel):
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    email_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["dev", "staging", "production"] = "dev"
    config_file: str | None = None
    log_level: str = "INFO"
    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    strategies: dict[str, StrategyConfig] = Field(
        default_factory=lambda: {"conservative_mm": StrategyConfig()}
    )

    @classmethod
    def from_yaml_and_env(cls, path: str | None = None) -> Settings:
        config_path = path or os.getenv("CONFIG_FILE")
        data: dict[str, Any] = {}
        if config_path and Path(config_path).exists():
            with Path(config_path).open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                if not isinstance(loaded, dict):
                    raise ValueError("Config YAML must contain an object")
                data = loaded
        return cls(**data)


def load_settings() -> Settings:
    return Settings.from_yaml_and_env()
