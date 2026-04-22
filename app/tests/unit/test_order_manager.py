from __future__ import annotations

import pytest

from app.config.settings import Settings, TradingSettings
from app.core.state import BotState
from app.core.types import OrderBook, OrderSide, PriceLevel, QuoteIntent, utc_now
from app.data.polymarket import ClobTradingClient
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine


class DummyClient(ClobTradingClient):
    def __init__(self) -> None:
        pass


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
async def test_default_startup_mode_is_dry_run_and_simulates_order() -> None:
    settings = Settings(
        trading=TradingSettings(live_trading=False, live_trading_acknowledged=False)
    )
    state = await ready_state()
    risk = RiskEngine(state, settings.risk, settings.trading)
    manager = OrderManager(settings, state, risk, DummyClient())
    result = await manager.submit_quote(
        QuoteIntent(
            strategy="test",
            market="0xmarket",
            token_id="token",
            side=OrderSide.BUY,
            price="0.49",
            size="1",
        )
    )
    assert result["submitted"] is True
    assert result["response"]["dry_run"] is True
    assert len(state.open_orders) == 1


@pytest.mark.asyncio
async def test_cancel_all_in_dry_run_clears_state() -> None:
    settings = Settings()
    state = await ready_state()
    risk = RiskEngine(state, settings.risk, settings.trading)
    manager = OrderManager(settings, state, risk, DummyClient())
    await manager.submit_quote(
        QuoteIntent(
            strategy="test",
            market="0xmarket",
            token_id="token",
            side=OrderSide.BUY,
            price="0.49",
            size="1",
        )
    )
    result = await manager.cancel_all("test")
    assert result["dry_run"] is True
    assert not state.open_orders
