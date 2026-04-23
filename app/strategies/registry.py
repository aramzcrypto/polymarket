from __future__ import annotations

from app.config.settings import Settings
from app.strategies.base import Strategy
from app.strategies.btc_late_convexity import BtcLateConvexityStrategy
from app.strategies.conservative_mm import ConservativeMarketMakerStrategy
from app.strategies.btc_5m_mean_reversion import BtcMeanReversionStrategy
from app.strategies.btc_5m_momentum import BtcMomentumStrategy


def build_strategies(settings: Settings) -> dict[str, Strategy]:
    strategies: dict[str, Strategy] = {}
    config = settings.strategies.get("conservative_mm")
    if config:
        strategies["conservative_mm"] = ConservativeMarketMakerStrategy(config)
    btc_config = settings.strategies.get("btc_5m_late_convexity")
    if btc_config:
        strategies["btc_5m_late_convexity"] = BtcLateConvexityStrategy(btc_config)
    mr_config = settings.strategies.get("btc_5m_mean_reversion")
    if mr_config:
        strategies["btc_5m_mean_reversion"] = BtcMeanReversionStrategy(mr_config)
    mom_config = settings.strategies.get("btc_5m_momentum")
    if mom_config:
        strategies["btc_5m_momentum"] = BtcMomentumStrategy(mom_config)
    return strategies
