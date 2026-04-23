from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.config.settings import StrategyConfig
from app.core.types import (
    BtcIntervalMarket,
    CryptoPriceSnapshot,
    Fill,
    OrderBook,
    OrderSide,
    PriceLevel,
    utc_now,
)
from app.strategies.base import StrategyContext
from app.strategies.btc_late_convexity import BtcLateConvexityStrategy


def market(
    price_to_beat: str | None = "100000",
    order_min_size: str = "5",
    *,
    start_offset_seconds: int = -30,
) -> BtcIntervalMarket:
    return BtcIntervalMarket(
        market_id="1",
        condition_id="0xmarket",
        question="Bitcoin Up or Down - 5 Minutes",
        start_time=utc_now() + timedelta(seconds=start_offset_seconds),
        end_time=utc_now() + timedelta(seconds=45),
        price_to_beat=price_to_beat,
        up_token_id="up",
        down_token_id="down",
        order_min_size=order_min_size,
    )


def book(token: str, ask: str) -> OrderBook:
    return OrderBook(
        market="0xmarket",
        asset_id=token,
        bids=[PriceLevel(price="0.001", size="100")],
        asks=[PriceLevel(price=ask, size="100")],
    )


async def seed_books(
    strategy: BtcLateConvexityStrategy, *, up_ask: str = "0.90", down_ask: str = "0.10"
) -> None:
    await strategy.on_market_update(book("up", up_ask))
    await strategy.on_market_update(book("down", down_ask))


@pytest.mark.asyncio
async def test_buys_cheaper_late_reversal_side() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", default_volatility_bps="120", max_quote_size="25")
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.90", down_ask="0.10")
    quotes = await strategy.desired_quotes(book("down", "0.10"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].token_id == "down"
    assert quotes[0].post_only is False


@pytest.mark.asyncio
async def test_waits_until_both_outcome_books_are_known() -> None:
    strategy = BtcLateConvexityStrategy(StrategyConfig(default_volatility_bps="120"))
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    quotes = await strategy.desired_quotes(book("down", "0.10"), StrategyContext({}, {}, None))
    assert quotes == []
    assert strategy.last_signals[-1].reason == "waiting for both outcome books"


@pytest.mark.asyncio
async def test_holds_when_longshot_is_too_expensive() -> None:
    strategy = BtcLateConvexityStrategy(StrategyConfig(max_longshot_price="0.05"))
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.92", down_ask="0.08")
    quotes = await strategy.desired_quotes(book("down", "0.08"), StrategyContext({}, {}, None))
    assert quotes == []
    assert strategy.last_signals[-1].reason == "price outside configured band"


@pytest.mark.asyncio
async def test_uses_captured_interval_open_when_market_has_no_price_to_beat() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            min_edge="0.001",
            max_longshot_price="0.50",
            default_volatility_bps="120",
            max_quote_size="25",
        )
    )
    start = utc_now() - timedelta(seconds=5)
    mkt = market(None, start_offset_seconds=-5)
    strategy.update_markets([mkt])
    strategy.update_price(
        CryptoPriceSnapshot(
            symbol="BTC-USD",
            price="100000",
            source="coinbase",
            timestamp=start + timedelta(seconds=1),
        ),
        Decimal("120"),
    )
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.10", down_ask="0.90")
    quotes = await strategy.desired_quotes(book("up", "0.1"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].token_id == "up"
    assert (
        strategy.last_signals[-1].reason
        == "cheaper late-reversal side; model price clears threshold"
    )


@pytest.mark.asyncio
async def test_uses_min_spend_when_interval_open_was_missed() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", max_longshot_price="0.50", max_quote_size="200")
    )
    strategy.update_markets([market(None, start_offset_seconds=-60)])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.10", down_ask="0.90")
    quotes = await strategy.desired_quotes(book("up", "0.1"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].notional == Decimal("2.0")
    assert strategy.last_signals[-1].reason == "cheaper late-reversal side"


@pytest.mark.asyncio
async def test_holds_when_quote_size_is_below_clob_minimum() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(max_longshot_price="1.0", max_spend_per_signal="1")
    )
    strategy.update_markets([market(None, order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.51", down_ask="0.49")
    quotes = await strategy.desired_quotes(book("down", "0.49"), StrategyContext({}, {}, None))
    assert quotes == []


@pytest.mark.asyncio
async def test_bumps_to_market_minimum_when_notional_stays_within_cap() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            max_longshot_price="0.40",
            min_reward_multiple="2.5",
            min_spend_per_signal="1.05",
            max_spend_per_signal="2",
            high_confidence_probability="0.90",
        )
    )
    strategy.update_markets([market("100000", order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="99950", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.35", down_ask="0.65")
    quotes = await strategy.desired_quotes(book("up", "0.35"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].size == Decimal("5")
    assert quotes[0].notional == Decimal("1.75")


@pytest.mark.asyncio
async def test_allows_balanced_round_when_minimum_order_needs_larger_cap() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            max_longshot_price="0.52",
            min_reward_multiple="1.9",
            min_spend_per_signal="1.05",
            max_spend_per_signal="2.60",
            high_confidence_probability="0.90",
        )
    )
    strategy.update_markets([market("100000", order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="99950", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.48", down_ask="0.54")
    quotes = await strategy.desired_quotes(book("up", "0.48"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].size == Decimal("5")
    assert quotes[0].notional == Decimal("2.40")


@pytest.mark.asyncio
async def test_one_usdc_longshot_config_buys_only_when_minimum_order_fits() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            max_longshot_price="0.20",
            min_reward_multiple="5",
            min_spend_per_signal="1.00",
            max_spend_per_signal="1.00",
            bankroll="50",
            kelly_fraction="0.02",
            min_seconds_to_expiry=25,
            max_seconds_to_expiry=50,
        )
    )
    strategy.update_markets([market("100000", order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100250", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.82", down_ask="0.20")
    quotes = await strategy.desired_quotes(book("down", "0.20"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].size == Decimal("5")
    assert quotes[0].notional == Decimal("1.00")


@pytest.mark.asyncio
async def test_one_usdc_longshot_config_skips_above_one_dollar_minimum() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            max_longshot_price="0.20",
            min_reward_multiple="5",
            min_spend_per_signal="1.00",
            max_spend_per_signal="1.00",
            bankroll="50",
            kelly_fraction="0.02",
            min_seconds_to_expiry=25,
            max_seconds_to_expiry=50,
        )
    )
    strategy.update_markets([market("100000", order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100250", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.79", down_ask="0.21")
    quotes = await strategy.desired_quotes(book("down", "0.21"), StrategyContext({}, {}, None))
    assert quotes == []
    assert strategy.last_signals[-1].reason == "price outside configured band"


@pytest.mark.asyncio
async def test_holds_when_tiny_live_cap_makes_marketable_buy_below_min_spend() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", max_longshot_price="0.12", max_spend_per_signal="5")
    )
    strategy.update_markets([market(None, order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.93", down_ask="0.07")
    quotes = await strategy.desired_quotes(book("down", "0.07"), StrategyContext({}, {}, 5))
    assert quotes == []


@pytest.mark.asyncio
async def test_holds_when_notional_is_below_configured_min_spend() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", max_longshot_price="0.50", max_spend_per_signal="5")
    )
    strategy.update_markets([market("100000", order_min_size="5")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.05", down_ask="0.95")
    quotes = await strategy.desired_quotes(book("up", "0.05"), StrategyContext({}, {}, 25))
    assert quotes == []


@pytest.mark.asyncio
async def test_holds_after_market_already_filled() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", default_volatility_bps="120", max_quote_size="25")
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await strategy.on_fill(
        Fill(
            trade_id="trade-1",
            market="0xmarket",
            token_id="up",
            side=OrderSide.BUY,
            price="0.1",
            size="10",
        )
    )
    await seed_books(strategy, up_ask="0.1", down_ask="0.9")
    quotes = await strategy.desired_quotes(book("up", "0.1"), StrategyContext({}, {}, None))
    assert quotes == []


@pytest.mark.asyncio
async def test_holds_when_token_position_is_open() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(min_edge="0.001", default_volatility_bps="120", max_quote_size="25")
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.1", down_ask="0.9")
    quotes = await strategy.desired_quotes(book("up", "0.1"), StrategyContext({"up": 1}, {}, None))
    assert quotes == []


@pytest.mark.asyncio
async def test_uses_min_spend_for_lower_confidence_entries() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            min_edge="0.001",
            default_volatility_bps="120",
            high_confidence_probability="0.90",
            min_spend_per_signal="1.05",
            max_spend_per_signal="2",
            max_quote_size="200",
        )
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.10", down_ask="0.90")
    quotes = await strategy.desired_quotes(book("up", "0.10"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].notional == Decimal("1.05")


@pytest.mark.asyncio
async def test_uses_max_spend_for_higher_confidence_entries() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            min_edge="0.001",
            default_volatility_bps="120",
            high_confidence_probability="0.10",
            min_spend_per_signal="1.05",
            max_spend_per_signal="2",
            max_quote_size="200",
        )
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.10", down_ask="0.90")
    quotes = await strategy.desired_quotes(book("up", "0.10"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].notional == Decimal("2.0")


@pytest.mark.asyncio
async def test_entry_price_buffer_crosses_one_tick_above_best_ask() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            default_volatility_bps="120",
            entry_price_buffer_ticks=1,
            max_entry_price_buffer_bps="2500",
            high_confidence_probability="0.90",
            max_longshot_price="0.35",
            min_reward_multiple="2.75",
            min_spend_per_signal="1.10",
            max_spend_per_signal="2",
            max_quote_size="200",
        )
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.80", down_ask="0.20")
    quotes = await strategy.desired_quotes(book("down", "0.20"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].price == Decimal("0.21")
    assert quotes[0].reason.endswith("ask=0.20")


@pytest.mark.asyncio
async def test_entry_price_buffer_stays_at_ask_when_tick_exceeds_bps_cap() -> None:
    strategy = BtcLateConvexityStrategy(
        StrategyConfig(
            default_volatility_bps="120",
            entry_price_buffer_ticks=1,
            max_entry_price_buffer_bps="2500",
            high_confidence_probability="0.90",
            max_longshot_price="0.35",
            min_reward_multiple="2.75",
            min_spend_per_signal="1.10",
            max_spend_per_signal="2",
            max_quote_size="200",
        )
    )
    strategy.update_markets([market("100000")])
    strategy.update_price(
        CryptoPriceSnapshot(symbol="BTC-USD", price="100050", source="coinbase"),
        Decimal("120"),
    )
    await seed_books(strategy, up_ask="0.90", down_ask="0.03")
    quotes = await strategy.desired_quotes(book("down", "0.03"), StrategyContext({}, {}, None))
    assert len(quotes) == 1
    assert quotes[0].price == Decimal("0.03")
