from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.config.settings import RiskLimits, TradingSettings
from app.core.state import BotState
from app.core.types import OrderBook, OrderSide, PriceLevel, QuoteIntent, RejectReason, utc_now
from app.risk.engine import RiskEngine


def quote(size: str = "1", price: str = "0.49") -> QuoteIntent:
    return QuoteIntent(
        strategy="test",
        market="0xmarket",
        token_id="token",
        side=OrderSide.BUY,
        price=price,
        size=size,
        client_order_key=f"key-{size}-{price}",
    )


async def ready_state() -> BotState:
    state = BotState()
    state.books["token"] = OrderBook(
        market="0xmarket",
        asset_id="token",
        bids=[PriceLevel(price="0.48", size="100")],
        asks=[PriceLevel(price="0.52", size="100")],
        timestamp=utc_now(),
    )
    state.balances.verified = True
    state.connectivity.market_ws_connected = True
    state.connectivity.user_ws_connected = True
    state.connectivity.auth_valid = True
    state.connectivity.compliance_ok = True
    return state


@pytest.mark.asyncio
async def test_allows_valid_quote() -> None:
    state = await ready_state()
    engine = RiskEngine(state, RiskLimits(), TradingSettings())
    decision = await engine.pre_trade(quote())
    assert decision.allowed


@pytest.mark.asyncio
async def test_rejects_stale_book() -> None:
    state = await ready_state()
    state.books["token"].timestamp = utc_now() - timedelta(seconds=10)
    engine = RiskEngine(state, RiskLimits(stale_book_seconds=1), TradingSettings())
    decision = await engine.pre_trade(quote())
    assert RejectReason.STALE_BOOK in decision.reasons


@pytest.mark.asyncio
async def test_rejects_oversized_order() -> None:
    state = await ready_state()
    engine = RiskEngine(state, RiskLimits(max_order_size="1"), TradingSettings())
    decision = await engine.pre_trade(quote(size="2"))
    assert RejectReason.MAX_ORDER_SIZE in decision.reasons


@pytest.mark.asyncio
async def test_duplicate_order_prevention() -> None:
    state = await ready_state()
    engine = RiskEngine(state, RiskLimits(), TradingSettings())
    first = await engine.pre_trade(quote())
    second = await engine.pre_trade(quote())
    assert first.allowed
    assert RejectReason.DUPLICATE_ORDER in second.reasons


@pytest.mark.asyncio
async def test_kill_switch_rejects() -> None:
    state = await ready_state()
    await state.set_kill_switch(True, "test")
    engine = RiskEngine(state, RiskLimits(), TradingSettings())
    decision = await engine.pre_trade(quote())
    assert RejectReason.KILL_SWITCH in decision.reasons


@pytest.mark.asyncio
async def test_tiny_live_cap_rejects() -> None:
    state = await ready_state()
    trading = TradingSettings(
        live_trading=True,
        live_trading_acknowledged=True,
        tiny_live_mode=True,
        tiny_live_max_order_size=Decimal("1"),
    )
    engine = RiskEngine(state, RiskLimits(max_order_size="10"), trading)
    decision = await engine.pre_trade(quote(size="2"))
    assert RejectReason.MAX_ORDER_SIZE in decision.reasons
