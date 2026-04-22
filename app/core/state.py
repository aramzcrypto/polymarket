from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.types import (
    ZERO,
    BalanceSnapshot,
    BtcIntervalMarket,
    ConnectivityState,
    CryptoPriceSnapshot,
    Fill,
    Market,
    OpenOrder,
    OrderBook,
    Position,
    StrategySignal,
    utc_now,
)


class BotState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.markets: dict[str, Market] = {}
        self.books: dict[str, OrderBook] = {}
        self.open_orders: dict[str, OpenOrder] = {}
        self.positions: dict[str, Position] = {}
        self.balances = BalanceSnapshot()
        self.connectivity = ConnectivityState()
        self.crypto_prices: dict[str, CryptoPriceSnapshot] = {}
        self.btc_interval_markets: dict[str, BtcIntervalMarket] = {}
        self.strategy_signals: list[StrategySignal] = []
        self.strategy_enabled: dict[str, bool] = defaultdict(lambda: True)
        self.kill_switch_enabled = False
        self.kill_switch_reason: str | None = None
        self.realized_pnl = ZERO
        self.unrealized_pnl = ZERO
        self.daily_pnl = ZERO
        self.high_watermark = ZERO
        self.rejected_order_count = 0
        self.partial_fill_count = 0
        self.last_rejection_at: datetime | None = None
        self.raw_counters: dict[str, int] = defaultdict(int)
        self.last_alerts: list[dict[str, Any]] = []
        self.fills: list[Fill] = []

    async def set_kill_switch(self, enabled: bool, reason: str | None = None) -> None:
        async with self.lock:
            self.kill_switch_enabled = enabled
            self.kill_switch_reason = reason

    async def upsert_book(self, book: OrderBook) -> None:
        async with self.lock:
            self.books[book.asset_id] = book
            self.connectivity.last_market_msg_at = utc_now()
            self.connectivity.market_ws_connected = True

    async def upsert_order(self, order: OpenOrder) -> None:
        async with self.lock:
            self.open_orders[order.order_id] = order

    async def remove_order(self, order_id: str) -> None:
        async with self.lock:
            self.open_orders.pop(order_id, None)

    async def record_fill(self, fill: Fill) -> None:
        async with self.lock:
            self.fills.append(fill)
            self.connectivity.last_user_msg_at = utc_now()
            self.connectivity.user_ws_connected = True
            order = self.open_orders.get(fill.order_id or "")
            if order:
                order.filled_size += fill.size
                if order.remaining_size <= ZERO:
                    self.open_orders.pop(order.order_id, None)

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return {
                "markets": [market.model_dump(mode="json") for market in self.markets.values()],
                "books": [book.model_dump(mode="json") for book in self.books.values()],
                "open_orders": [
                    order.model_dump(mode="json") for order in self.open_orders.values()
                ],
                "positions": [pos.model_dump(mode="json") for pos in self.positions.values()],
                "balances": self.balances.model_dump(mode="json"),
                "connectivity": self.connectivity.model_dump(mode="json"),
                "crypto_prices": {
                    symbol: price.model_dump(mode="json")
                    for symbol, price in self.crypto_prices.items()
                },
                "btc_interval_markets": [
                    market.model_dump(mode="json") for market in self.btc_interval_markets.values()
                ],
                "strategy_signals": [
                    signal.model_dump(mode="json") for signal in self.strategy_signals[-100:]
                ],
                "kill_switch": {
                    "enabled": self.kill_switch_enabled,
                    "reason": self.kill_switch_reason,
                },
                "pnl": {
                    "realized": str(self.realized_pnl),
                    "unrealized": str(self.unrealized_pnl),
                    "daily": str(self.daily_pnl),
                    "high_watermark": str(self.high_watermark),
                },
                "strategy_enabled": dict(self.strategy_enabled),
                "counters": dict(self.raw_counters),
            }

    async def upsert_crypto_price(self, snapshot: CryptoPriceSnapshot) -> None:
        async with self.lock:
            self.crypto_prices[snapshot.source] = snapshot

    async def upsert_btc_interval_market(self, market: BtcIntervalMarket) -> None:
        async with self.lock:
            self.btc_interval_markets[market.condition_id] = market

    async def record_strategy_signal(self, signal: StrategySignal) -> None:
        async with self.lock:
            self.strategy_signals.append(signal)
            if len(self.strategy_signals) > 500:
                self.strategy_signals = self.strategy_signals[-500:]

    def market_exposure(self, market: str) -> Decimal:
        exposure = sum(
            (
                position.notional
                for position in self.positions.values()
                if position.market == market
            ),
            ZERO,
        )
        exposure += sum(
            (
                order.remaining_notional
                for order in self.open_orders.values()
                if order.market == market
            ),
            ZERO,
        )
        return exposure

    def total_exposure(self) -> Decimal:
        return sum(
            (position.notional for position in self.positions.values()),
            ZERO,
        ) + sum(
            (order.remaining_notional for order in self.open_orders.values()),
            ZERO,
        )

    def open_orders_for_market(self, market: str) -> int:
        return sum(1 for order in self.open_orders.values() if order.market == market)
