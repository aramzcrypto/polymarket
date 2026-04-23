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
        self.seen_trade_ids: set[str] = set()
        self.price_history: list[dict[str, Any]] = []

    async def set_kill_switch(self, enabled: bool, reason: str | None = None) -> None:
        async with self.lock:
            self.kill_switch_enabled = enabled
            self.kill_switch_reason = reason

    async def reset_risk_latches(self) -> None:
        async with self.lock:
            self.kill_switch_enabled = False
            self.kill_switch_reason = None
            self.rejected_order_count = 0
            self.partial_fill_count = 0
            self.last_rejection_at = None

    async def upsert_book(self, book: OrderBook) -> None:
        async with self.lock:
            self.books[book.asset_id] = book
            self.connectivity.last_market_msg_at = utc_now()
            self.connectivity.market_ws_connected = True
            self._recalculate_unrealized_pnl()

    async def upsert_order(self, order: OpenOrder) -> None:
        async with self.lock:
            self.open_orders[order.order_id] = order

    async def remove_order(self, order_id: str) -> None:
        async with self.lock:
            self.open_orders.pop(order_id, None)

    async def record_fill(self, fill: Fill) -> bool:
        async with self.lock:
            if fill.trade_id in self.seen_trade_ids:
                for idx, existing in enumerate(self.fills):
                    if existing.trade_id == fill.trade_id:
                        self.fills[idx] = existing.model_copy(update={"status": fill.status})
                        break
                return False
            self.seen_trade_ids.add(fill.trade_id)
            self.fills.append(fill)
            self.connectivity.last_user_msg_at = utc_now()
            self.connectivity.user_ws_connected = True
            order = self.open_orders.get(fill.order_id or "")
            if order:
                order.filled_size += fill.size
                if order.remaining_size <= ZERO:
                    self.open_orders.pop(order.order_id, None)
            self._apply_fill_to_position(fill)
            return True

    def _apply_fill_to_position(self, fill: Fill) -> None:
        key = fill.token_id
        position = self.positions.get(key) or Position(
            market=fill.market,
            token_id=fill.token_id,
            outcome=fill.outcome,
        )
        if not position.outcome and fill.outcome:
            position = position.model_copy(update={"outcome": fill.outcome})

        if fill.side.value == "BUY":
            cost = position.size * position.avg_price
            new_size = position.size + fill.size
            avg_price = (cost + fill.size * fill.price) / new_size if new_size > ZERO else ZERO
            position = position.model_copy(update={"size": new_size, "avg_price": avg_price})
        else:
            closed_size = min(position.size, fill.size)
            realized_delta = (fill.price - position.avg_price) * closed_size - fill.fee
            realized = position.realized_pnl + realized_delta
            new_size = max(ZERO, position.size - fill.size)
            avg_price = position.avg_price if new_size > ZERO else ZERO
            position = position.model_copy(
                update={"size": new_size, "avg_price": avg_price, "realized_pnl": realized}
            )
            self.realized_pnl += realized_delta

        if position.size > ZERO:
            self.positions[key] = position
        else:
            self.positions.pop(key, None)
        self._recalculate_unrealized_pnl()

    def _recalculate_unrealized_pnl(self) -> None:
        total = ZERO
        for token_id, position in list(self.positions.items()):
            book = self.books.get(token_id)
            mark = position.avg_price
            if book and book.best_bid:
                mark = book.best_bid
            elif book and book.midpoint:
                mark = book.midpoint
            unrealized = (mark - position.avg_price) * position.size
            total += unrealized
            self.positions[token_id] = position.model_copy(update={"unrealized_pnl": unrealized})
        self.unrealized_pnl = total
        self.daily_pnl = self.realized_pnl + self.unrealized_pnl
        self.high_watermark = max(self.high_watermark, self.daily_pnl)

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
                "counters": {
                    **dict(self.raw_counters),
                    "rejected_order_count": self.rejected_order_count,
                    "partial_fill_count": self.partial_fill_count,
                    "last_rejection_at": self.last_rejection_at.isoformat()
                    if self.last_rejection_at
                    else None,
                },
                "price_history": self.price_history[-200:],
            }

    async def upsert_crypto_price(self, snapshot: CryptoPriceSnapshot) -> None:
        async with self.lock:
            self.crypto_prices[snapshot.source] = snapshot
            if snapshot.source == "coinbase":
                self.price_history.append({
                    "time": int(snapshot.timestamp.timestamp()),
                    "value": float(snapshot.price)
                })
                if len(self.price_history) > 1000:
                    self.price_history = self.price_history[-1000:]

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
