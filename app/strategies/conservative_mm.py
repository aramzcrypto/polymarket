from __future__ import annotations

from decimal import Decimal

from app.config.settings import StrategyConfig
from app.core.types import (
    ZERO,
    Fill,
    OrderBook,
    OrderSide,
    QuoteIntent,
    RiskDecision,
    quantize_to_tick,
)
from app.strategies.base import Strategy, StrategyContext


class ConservativeMarketMakerStrategy(Strategy):
    name = "conservative_mm"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.last_fair_values: dict[str, Decimal] = {}

    async def on_market_update(self, book: OrderBook) -> None:
        fair = self.estimate_fair_value(book)
        if fair is not None:
            self.last_fair_values[book.asset_id] = fair

    async def on_fill(self, fill: Fill) -> None:
        return None

    async def on_risk_state(self, decision: RiskDecision) -> None:
        return None

    def estimate_fair_value(self, book: OrderBook) -> Decimal | None:
        midpoint = book.midpoint
        if midpoint is None:
            return None
        if book.top_liquidity <= ZERO:
            return midpoint
        best_bid_size = next(
            (level.size for level in sorted(book.bids, key=lambda x: x.price, reverse=True)),
            ZERO,
        )
        best_ask_size = next(
            (level.size for level in sorted(book.asks, key=lambda x: x.price)), ZERO
        )
        if best_bid_size + best_ask_size <= ZERO:
            return midpoint
        imbalance = (best_bid_size - best_ask_size) / (best_bid_size + best_ask_size)
        return max(Decimal("0.01"), min(Decimal("0.99"), midpoint + imbalance * Decimal("0.005")))

    def _quote_size(self, book: OrderBook, inventory: Decimal, side: OrderSide) -> Decimal:
        liquidity_size = book.top_liquidity * self.config.liquidity_fraction
        base = min(
            self.config.quote_size,
            self.config.max_quote_size,
            max(self.config.min_quote_size, liquidity_size),
        )
        if side == OrderSide.BUY and inventory > ZERO:
            base *= max(Decimal("0.10"), Decimal("1") - self.config.inventory_skew_factor)
        if side == OrderSide.SELL and inventory < ZERO:
            base *= max(Decimal("0.10"), Decimal("1") - self.config.inventory_skew_factor)
        worsens_inventory = (side == OrderSide.BUY and inventory > ZERO) or (
            side == OrderSide.SELL and inventory < ZERO
        )
        if abs(inventory) >= self.config.max_inventory_per_side and worsens_inventory:
            return ZERO
        return max(ZERO, min(base, self.config.max_quote_size))

    async def desired_quotes(self, book: OrderBook, context: StrategyContext) -> list[QuoteIntent]:
        fair = self.estimate_fair_value(book)
        if fair is None:
            return []
        spread = max(self.config.min_spread, book.spread or self.config.min_spread)
        half = spread / Decimal("2")
        tick = Decimal("0.01")
        bid_price = quantize_to_tick(fair - half, tick, side=OrderSide.BUY)
        ask_price = quantize_to_tick(fair + half, tick, side=OrderSide.SELL)
        inventory = Decimal(str(context.inventory_by_token.get(book.asset_id, 0)))
        buy_size = self._quote_size(book, inventory, OrderSide.BUY)
        sell_size = self._quote_size(book, inventory, OrderSide.SELL)
        if context.tiny_live_cap is not None:
            cap = Decimal(str(context.tiny_live_cap))
            buy_size = min(buy_size, cap)
            sell_size = min(sell_size, cap)
        quotes: list[QuoteIntent] = []
        if buy_size > ZERO and Decimal("0") < bid_price < Decimal("1"):
            quotes.append(
                QuoteIntent(
                    strategy=self.name,
                    market=book.market,
                    token_id=book.asset_id,
                    side=OrderSide.BUY,
                    price=bid_price,
                    size=buy_size,
                    tick_size=tick,
                    reason="conservative_bid",
                )
            )
        if sell_size > ZERO and Decimal("0") < ask_price < Decimal("1"):
            quotes.append(
                QuoteIntent(
                    strategy=self.name,
                    market=book.market,
                    token_id=book.asset_id,
                    side=OrderSide.SELL,
                    price=ask_price,
                    size=sell_size,
                    tick_size=tick,
                    reason="conservative_ask",
                )
            )
        return quotes
