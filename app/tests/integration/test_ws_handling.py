from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.config.settings import Settings
from app.core.runtime import BotRuntime
from app.core.types import OrderBook, PriceLevel


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
async def test_record_market_book_forwards_polled_books_to_strategies() -> None:
    seen: list[str] = []

    class SpyStrategy:
        async def on_market_update(self, book: OrderBook) -> None:
            seen.append(book.asset_id)

    runtime = BotRuntime(Settings())
    runtime.strategies = {"spy": SpyStrategy()}  # type: ignore[dict-item]
    await runtime._record_market_book(
        OrderBook(
            market="0xmarket",
            asset_id="token",
            bids=[PriceLevel(price="0.20", size="10")],
            asks=[PriceLevel(price="0.21", size="10")],
        )
    )

    assert runtime.state.books["token"].best_ask == Decimal("0.21")
    assert seen == ["token"]


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
    assert len(runtime.state.positions) == 1
    position = runtime.state.positions["token"]
    assert position.size == Decimal("1")
    assert position.avg_price == Decimal("0.51")
    assert runtime.state.connectivity.user_ws_connected is True
    await runtime.handle_market_message(
        {
            "event_type": "book",
            "market": "0xmarket",
            "asset_id": "token",
            "bids": [{"price": "0.60", "size": "10"}],
            "asks": [{"price": "0.62", "size": "11"}],
        }
    )
    assert runtime.state.positions["token"].unrealized_pnl == Decimal("0.09")
    assert runtime.state.daily_pnl == Decimal("0.09")


@pytest.mark.asyncio
async def test_repeated_trade_status_does_not_double_count_position() -> None:
    runtime = BotRuntime(Settings())
    runtime.repository = NoopRepository()  # type: ignore[assignment]
    payload = {
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
    await runtime.handle_user_message(payload)
    await runtime.handle_user_message({**payload, "status": "CONFIRMED"})

    assert len(runtime.state.fills) == 1
    assert runtime.state.fills[0].status == "CONFIRMED"
    assert runtime.state.positions["token"].size == Decimal("1")


@pytest.mark.asyncio
async def test_dynamic_btc_markets_start_user_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[Any] = []

    class FakeWebSocket:
        def __init__(
            self,
            _url: str,
            subscription: dict[str, Any],
            _on_message: Any,
            on_status: Any,
        ) -> None:
            self.subscription = subscription
            self.on_status = on_status
            self.stopped = False
            instances.append(self)

        async def run(self) -> None:
            await self.on_status(True, None)
            await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stopped = True

    import asyncio

    monkeypatch.setattr("app.core.runtime.PolymarketWebSocket", FakeWebSocket)
    runtime = BotRuntime(Settings())

    await runtime._ensure_user_websocket_markets(["0xbtc"])
    await asyncio.sleep(0)

    assert instances[-1].subscription["markets"] == ["0xbtc"]
    assert runtime.state.connectivity.user_ws_connected is True

    for task in runtime.tasks:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
