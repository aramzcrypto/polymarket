from __future__ import annotations

from typing import Any

import pytest

from app.config.settings import Settings
from app.core.runtime import BotRuntime


class NoopRepository:
    async def raw_event(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def fill(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_market_book_message_updates_state() -> None:
    runtime = BotRuntime(Settings())
    runtime.repository = NoopRepository()  # type: ignore[assignment]
    await runtime.handle_market_message(
        {
            "event_type": "book",
            "market": "0xmarket",
            "asset_id": "token",
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [{"price": "0.52", "size": "11"}],
        }
    )
    assert runtime.state.books["token"].best_bid is not None
    assert runtime.state.connectivity.market_ws_connected is True


@pytest.mark.asyncio
async def test_user_trade_message_records_fill() -> None:
    runtime = BotRuntime(Settings())
    runtime.repository = NoopRepository()  # type: ignore[assignment]
    await runtime.handle_user_message(
        {
            "event_type": "trade",
            "id": "trade-1",
            "taker_order_id": "order-1",
            "market": "0xmarket",
            "asset_id": "token",
            "side": "BUY",
            "price": "0.51",
            "size": "1",
            "status": "MATCHED",
        }
    )
    assert len(runtime.state.fills) == 1
    assert runtime.state.connectivity.user_ws_connected is True
