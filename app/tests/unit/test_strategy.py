from __future__ import annotations

from decimal import Decimal

import pytest

from app.config.settings import StrategyConfig
from app.core.types import OrderBook, OrderSide, PriceLevel, quantize_to_tick
from app.strategies.base import StrategyContext
from app.strategies.conservative_mm import ConservativeMarketMakerStrategy


def book() -> OrderBook:
    return OrderBook(
        market="0xmarket",
        asset_id="token",
        bids=[PriceLevel(price="0.48", size="100")],
        asks=[PriceLevel(price="0.52", size="100")],
    )


def test_tick_rounding_buy_down_sell_up() -> None:
    assert quantize_to_tick(Decimal("0.514"), Decimal("0.01"), side=OrderSide.BUY) == Decimal(
        "0.51"
    )
    assert quantize_to_tick(Decimal("0.514"), Decimal("0.01"), side=OrderSide.SELL) == Decimal(
        "0.52"
    )


def test_fair_value_uses_midpoint_for_balanced_book() -> None:
    strategy = ConservativeMarketMakerStrategy(StrategyConfig())
    assert strategy.estimate_fair_value(book()) == Decimal("0.50")


@pytest.mark.asyncio
async def test_quote_generation_respects_spread_and_size() -> None:
    strategy = ConservativeMarketMakerStrategy(
        StrategyConfig(min_spread="0.04", quote_size="5", max_quote_size="10")
    )
    quotes = await strategy.desired_quotes(book(), StrategyContext({}, {}, None))
    assert len(quotes) == 2
    assert {quote.side for quote in quotes} == {OrderSide.BUY, OrderSide.SELL}
    bid = next(quote for quote in quotes if quote.side == OrderSide.BUY)
    ask = next(quote for quote in quotes if quote.side == OrderSide.SELL)
    assert ask.price - bid.price >= Decimal("0.04")
    assert bid.size == Decimal("5")


@pytest.mark.asyncio
async def test_inventory_adjustment_withdraws_worsening_side() -> None:
    strategy = ConservativeMarketMakerStrategy(StrategyConfig(max_inventory_per_side="1"))
    quotes = await strategy.desired_quotes(book(), StrategyContext({"token": 2.0}, {}, None))
    assert all(quote.side != OrderSide.BUY for quote in quotes)


@pytest.mark.asyncio
async def test_tiny_live_cap_limits_quote_size() -> None:
    strategy = ConservativeMarketMakerStrategy(StrategyConfig(quote_size="5", max_quote_size="10"))
    quotes = await strategy.desired_quotes(book(), StrategyContext({}, {}, 1.0))
    assert all(quote.size <= Decimal("1") for quote in quotes)
