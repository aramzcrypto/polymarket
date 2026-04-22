from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.config.settings import StrategyConfig
from app.core.types import (
    BtcIntervalMarket,
    CryptoPriceSnapshot,
    OrderBook,
    PriceLevel,
    utc_now,
)
from app.strategies.base import StrategyContext
from app.strategies.btc_late_convexity import BtcLateConvexityStrategy


def market(price_to_beat: str = "100000") -> BtcIntervalMarket:
    return BtcIntervalMarket(
        market_id="1",
        condition_id="0xmarket",
        question="Bitcoin Up or Down - 5 Minutes",
        end_time=utc_now() + timedelta(seconds=45),
        price_to_beat=price_to_beat,
        up_token_id="up",
        down_token_id="down",
    )


def book(token: str, ask: str) -> OrderBook:
    return OrderBook(
        market="0xmarket",
        asset_id=token,
        bids=[PriceLevel(price="0.001", size="100")],
        asks=[PriceLevel(price=ask, size="100")],
    )


@pytest.mark.asyncio
async def test_buys_losing_down_when_price_above_threshold_and_edge_exists() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            min_edge="0.001",
            default_volatility_bps="120",
            max_required_move_sigma="2.5",
            max_longshot_price="0.12",
            max_quote_size="25",
        )
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    quotes = await strategy.desired_quotes(book("down", "0.03"), StrategyContext({}, {}, 1.0))
    assert len(quotes) == 1
    assert quotes[0].token_id == "down"
    assert quotes[0].post_only is False


@pytest.mark.asyncio
async def test_holds_when_longshot_is_too_expensive() -> None:
    strategy = BtcLateConvexityStrategy(StrategyConfig(max_longshot_price="0.05"))
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    quotes = await strategy.desired_quotes(book("down", "0.08"), StrategyContext({}, {}, None))
    assert quotes == []
    assert strategy.last_signals[-1].reason == "longshot price outside configured band"


@pytest.mark.asyncio
async def test_holds_when_market_has_no_price_to_beat() -> None:
    strategy = BtcLateConvexityStrategy(StrategyConfig())
    mkt = market()
    mkt.price_to_beat = None
    strategy.update_markets([mkt])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    quotes = await strategy.desired_quotes(book("down", "0.03"), StrategyContext({}, {}, None))
    assert quotes == []
    assert strategy.last_signals[-1].reason == "missing price_to_beat in market metadata"
