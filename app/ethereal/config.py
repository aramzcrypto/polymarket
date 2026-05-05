from __future__ import annotations

from decimal import Decimal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EtherealSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.ethereal.local", ".env.ethereal", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_base: str = "https://api.ethereal.trade"
    account_address: str = Field(default="", alias="ETHEREAL_ACCOUNT_ADDRESS")
    subaccount_id: str = Field(default="", alias="ETHEREAL_SUBACCOUNT_ID")
    subaccount_name: str = Field(default="primary", alias="ETHEREAL_SUBACCOUNT_NAME")
    owner_private_key: SecretStr | None = Field(default=None, alias="ETHEREAL_OWNER_PRIVATE_KEY")
    signer_private_key: SecretStr | None = Field(
        default=None, alias="ETHEREAL_SIGNER_PRIVATE_KEY"
    )
    signer_address: str = Field(default="", alias="ETHEREAL_SIGNER_ADDRESS")
    signer_name: str = Field(default="Codex API signer", alias="ETHEREAL_SIGNER_NAME")
    signer_category: str = Field(default="API", alias="ETHEREAL_SIGNER_CATEGORY")
    ticker: str = Field(default="BTCUSD", alias="ETHEREAL_TICKER")
    live_trading: bool = Field(default=False, alias="ETHEREAL_LIVE_TRADING")
    dry_run: bool = Field(default=True, alias="ETHEREAL_DRY_RUN")
    target_btc_size: Decimal | None = Field(default=None, alias="ETHEREAL_TARGET_BTC_SIZE")
    target_notional_usd: Decimal = Field(
        default=Decimal("25"), alias="ETHEREAL_TARGET_NOTIONAL_USD"
    )
    max_position_btc_size: Decimal = Field(
        default=Decimal("0.05"), alias="ETHEREAL_MAX_POSITION_BTC_SIZE"
    )
    max_account_leverage: Decimal = Field(
        default=Decimal("8.0"), alias="ETHEREAL_MAX_ACCOUNT_LEVERAGE"
    )
    maintenance_margin_buffer_bps: Decimal = Field(
        default=Decimal("500"), alias="ETHEREAL_MAINTENANCE_MARGIN_BUFFER_BPS"
    )
    min_entry_liquidation_distance_bps: Decimal = Field(
        default=Decimal("1250"), alias="ETHEREAL_MIN_ENTRY_LIQUIDATION_DISTANCE_BPS"
    )
    derisk_liquidation_distance_bps: Decimal = Field(
        default=Decimal("900"), alias="ETHEREAL_DERISK_LIQUIDATION_DISTANCE_BPS"
    )
    max_margin_usage_bps: Decimal = Field(
        default=Decimal("6500"), alias="ETHEREAL_MAX_MARGIN_USAGE_BPS"
    )
    min_available_balance_usd: Decimal = Field(
        default=Decimal("50"), alias="ETHEREAL_MIN_AVAILABLE_BALANCE_USD"
    )
    require_account_risk: bool = Field(
        default=True, alias="ETHEREAL_REQUIRE_ACCOUNT_RISK"
    )
    max_spread_bps: Decimal = Field(default=Decimal("8"), alias="ETHEREAL_MAX_SPREAD_BPS")
    max_projected_funding_rate_1h: Decimal = Field(
        default=Decimal("0.0005"), alias="ETHEREAL_MAX_PROJECTED_FUNDING_RATE_1H"
    )
    entry_basis_bps: Decimal = Field(default=Decimal("1.0"), alias="ETHEREAL_ENTRY_BASIS_BPS")
    volume_mode_enabled: bool = Field(default=False, alias="ETHEREAL_VOLUME_MODE_ENABLED")
    tight_spread_entry_basis_bps: Decimal = Field(
        default=Decimal("0.6"), alias="ETHEREAL_TIGHT_SPREAD_ENTRY_BASIS_BPS"
    )
    tight_spread_max_bps: Decimal = Field(
        default=Decimal("1.25"), alias="ETHEREAL_TIGHT_SPREAD_MAX_BPS"
    )
    funding_entry_threshold_1h: Decimal = Field(
        default=Decimal("0.00001"), alias="ETHEREAL_FUNDING_ENTRY_THRESHOLD_1H"
    )
    min_expected_edge_bps: Decimal = Field(
        default=Decimal("3.0"), alias="ETHEREAL_MIN_EXPECTED_EDGE_BPS"
    )
    expected_hold_minutes: Decimal = Field(
        default=Decimal("90"), alias="ETHEREAL_EXPECTED_HOLD_MINUTES"
    )
    adverse_momentum_lookback_seconds: int = Field(
        default=90, alias="ETHEREAL_ADVERSE_MOMENTUM_LOOKBACK_SECONDS"
    )
    max_adverse_momentum_bps: Decimal = Field(
        default=Decimal("12"), alias="ETHEREAL_MAX_ADVERSE_MOMENTUM_BPS"
    )
    starter_position_fraction: Decimal = Field(
        default=Decimal("0.50"), alias="ETHEREAL_STARTER_POSITION_FRACTION"
    )
    full_size_expected_edge_bps: Decimal = Field(
        default=Decimal("8.0"), alias="ETHEREAL_FULL_SIZE_EXPECTED_EDGE_BPS"
    )
    scale_in_enabled: bool = Field(default=False, alias="ETHEREAL_SCALE_IN_ENABLED")
    min_scale_in_pnl_bps: Decimal = Field(
        default=Decimal("2"), alias="ETHEREAL_MIN_SCALE_IN_PNL_BPS"
    )
    take_profit_bps: Decimal = Field(default=Decimal("30"), alias="ETHEREAL_TAKE_PROFIT_BPS")
    fast_take_profit_bps: Decimal = Field(
        default=Decimal("12"), alias="ETHEREAL_FAST_TAKE_PROFIT_BPS"
    )
    fast_take_profit_min_minutes: int = Field(
        default=65, alias="ETHEREAL_FAST_TAKE_PROFIT_MIN_MINUTES"
    )
    trailing_stop_activation_bps: Decimal = Field(
        default=Decimal("18"), alias="ETHEREAL_TRAILING_STOP_ACTIVATION_BPS"
    )
    trailing_stop_distance_bps: Decimal = Field(
        default=Decimal("10"), alias="ETHEREAL_TRAILING_STOP_DISTANCE_BPS"
    )
    trailing_stop_floor_bps: Decimal = Field(
        default=Decimal("6"), alias="ETHEREAL_TRAILING_STOP_FLOOR_BPS"
    )
    stop_loss_bps: Decimal = Field(default=Decimal("22"), alias="ETHEREAL_STOP_LOSS_BPS")
    min_hold_minutes: int = Field(default=65, alias="ETHEREAL_MIN_HOLD_MINUTES")
    max_hold_minutes: int = Field(default=120, alias="ETHEREAL_MAX_HOLD_MINUTES")
    order_expiry_seconds: int = Field(default=60, alias="ETHEREAL_ORDER_EXPIRY_SECONDS")
    poll_interval_seconds: int = Field(default=5, alias="ETHEREAL_POLL_INTERVAL_SECONDS")
    max_session_drawdown_bps: Decimal = Field(
        default=Decimal("200"), alias="ETHEREAL_MAX_SESSION_DRAWDOWN_BPS"
    )

    # ── Maker mode + book imbalance + signal-reversal exit ───────────
    entry_post_only: bool = Field(default=True, alias="ETHEREAL_ENTRY_POST_ONLY")
    post_only_max_attempts: int = Field(default=4, alias="ETHEREAL_POST_ONLY_MAX_ATTEMPTS")
    imbalance_levels: int = Field(default=5, alias="ETHEREAL_IMBALANCE_LEVELS")
    imbalance_weight: float = Field(default=0.6, alias="ETHEREAL_IMBALANCE_WEIGHT")
    signal_reversal_threshold: float = Field(
        default=0.5, alias="ETHEREAL_SIGNAL_REVERSAL_THRESHOLD"
    )
    maker_recycle_min_minutes: int = Field(
        default=65, alias="ETHEREAL_MAKER_RECYCLE_MIN_MINUTES"
    )
    maker_recycle_profit_bps: Decimal = Field(
        default=Decimal("4"), alias="ETHEREAL_MAKER_RECYCLE_PROFIT_BPS"
    )

    @property
    def has_owner_key(self) -> bool:
        return self.owner_private_key is not None and bool(
            self.owner_private_key.get_secret_value()
        )

    @property
    def has_signer_key(self) -> bool:
        return self.signer_private_key is not None and bool(
            self.signer_private_key.get_secret_value()
        )


def load_ethereal_settings() -> EtherealSettings:
    return EtherealSettings()
