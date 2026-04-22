from __future__ import annotations

from app.config.settings import Settings, TradingSettings
from app.core.types import TradingMode


def test_live_requires_double_ack() -> None:
    settings = Settings(trading=TradingSettings(live_trading=True, live_trading_acknowledged=False))
    assert settings.trading.mode == TradingMode.DRY_RUN


def test_tiny_live_mode() -> None:
    settings = Settings(
        trading=TradingSettings(
            live_trading=True,
            live_trading_acknowledged=True,
            tiny_live_mode=True,
        )
    )
    assert settings.trading.mode == TradingMode.TINY_LIVE
