from __future__ import annotations

from app.config.settings import Settings
from app.strategies.base import Strategy
from app.strategies.btc_late_convexity import BtcLateConvexityStrategy
from app.strategies.conservative_mm import ConservativeMarketMakerStrategy


def build_strategies(settings: Settings) -> dict[str, Strategy]:
    strategies: dict[str, Strategy] = {}
    config = settings.strategies.get("conservative_mm")
    if config:
        strategies["conservative_mm"] = ConservativeMarketMakerStrategy(config)
    btc_config = settings.strategies.get("btc_5m_late_convexity")
    if btc_config:
        strategies["btc_5m_late_convexity"] = BtcLateConvexityStrategy(btc_config)
    return strategies
