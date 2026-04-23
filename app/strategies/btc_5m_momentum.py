from __future__ import annotations

import logging
import math
from decimal import Decimal

from app.config.settings import StrategyConfig
from app.core.types import (
    ZERO,
    BtcIntervalMarket,
    CryptoPriceSnapshot,
    Fill,
    OrderBook,
    OrderSide,
    OrderType,
    QuoteIntent,
    RiskDecision,
    StrategySignal,
    utc_now,
    quantize_to_tick,
)
from app.strategies.base import Strategy, StrategyContext

logger = logging.getLogger(__name__)


class BtcMomentumStrategy(Strategy):
    name = "btc_5m_momentum"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.markets_by_token: dict[str, BtcIntervalMarket] = {}
        self.markets_by_condition: dict[str, BtcIntervalMarket] = {}
        self.books_by_token: dict[str, OrderBook] = {}
        self.open_prices_by_market: dict[str, Decimal] = {}
        self.price: CryptoPriceSnapshot | None = None
        self.volatility_bps = config.default_volatility_bps
        self.filled_markets: set[str] = set()

    def update_price(self, price: CryptoPriceSnapshot, volatility_bps: Decimal) -> None:
        self.price = price
        self.volatility_bps = max(self.config.default_volatility_bps, volatility_bps)
        self._capture_open_prices(price)

    def update_markets(self, markets: list[BtcIntervalMarket]) -> None:
        self.markets_by_token = {}
        self.markets_by_condition = {}
        for market in markets:
            self.markets_by_token[market.up_token_id] = market
            self.markets_by_token[market.down_token_id] = market
            self.markets_by_condition[market.condition_id] = market
        if self.price is not None:
            self._capture_open_prices(self.price)

    async def on_market_update(self, book: OrderBook) -> None:
        self.books_by_token[book.asset_id] = book

    async def on_fill(self, fill: Fill) -> None:
        if fill.side == OrderSide.BUY and fill.market:
            self.filled_markets.add(fill.market)

    async def on_risk_state(self, decision: RiskDecision) -> None:
        return None

    def _capture_open_prices(self, price: CryptoPriceSnapshot) -> None:
        from datetime import timedelta
        capture_grace = timedelta(seconds=12)
        for market in self.markets_by_condition.values():
            if market.condition_id in self.open_prices_by_market:
                continue
            if market.price_to_beat is not None:
                self.open_prices_by_market[market.condition_id] = market.price_to_beat
                continue
            start_time = market.start_time
            if start_time is None:
                continue
            if start_time <= price.timestamp <= start_time + capture_grace:
                self.open_prices_by_market[market.condition_id] = price.price

    def _normal_cdf(self, z: Decimal) -> Decimal:
        value = Decimal(str(0.5 * (1 + math.erf(float(z) / math.sqrt(2)))))
        return max(Decimal("0.01"), min(Decimal("0.99"), value))

    def _remaining_sigma_bps(self, seconds_to_expiry: float) -> Decimal:
        scale = Decimal(
            str(math.sqrt(max(seconds_to_expiry, 1) / self.config.volatility_window_seconds))
        )
        return max(Decimal("1"), self.volatility_bps * scale)

    def _outcome_probability(
        self, market: BtcIntervalMarket, outcome: str, seconds_to_expiry: float
    ) -> Decimal | None:
        if self.price is None:
            return None
        reference = market.price_to_beat or self.open_prices_by_market.get(market.condition_id)
        if reference is None or reference <= ZERO or self.price.price <= ZERO:
            return None
        distance_bps = (self.price.price - reference) / self.price.price * Decimal("10000")
        z = distance_bps / self._remaining_sigma_bps(seconds_to_expiry)
        up_probability = self._normal_cdf(z)
        if outcome == "UP":
            return up_probability
        return Decimal("1") - up_probability

    async def desired_quotes(self, book: OrderBook, context: StrategyContext) -> list[QuoteIntent]:
        self.books_by_token[book.asset_id] = book
        market = self.markets_by_token.get(book.asset_id)
        if market is None:
            return []
            
        if market.condition_id in self.filled_markets:
            return []
            
        seconds_to_expiry = (market.end_time - utc_now()).total_seconds()
        if seconds_to_expiry < self.config.min_seconds_to_expiry or seconds_to_expiry > self.config.max_seconds_to_expiry:
            return []
            
        outcome = "UP" if book.asset_id == market.up_token_id else "DOWN"
        prob = self._outcome_probability(market, outcome, seconds_to_expiry)
        
        if prob is None:
            return []
            
        ask = book.best_ask
        if ask is None:
            return []
            
        edge = prob - ask
        if edge < self.config.min_edge:
            return []
            
        # We found a mispriced token according to our model. Fire FAK!
        current_position = Decimal(str(context.inventory_by_token.get(book.asset_id, 0)))
        if current_position > ZERO:
            return []
            
        already_live = context.live_orders_by_token.get(book.asset_id, 0)
        if already_live > 0:
            return []
            
        # Entry price: be slightly aggressive to cross the spread
        quote_price = min(Decimal("0.99"), ask + market.tick_size * Decimal(self.config.entry_price_buffer_ticks))
        
        spend = min(self.config.max_spend_per_signal, self.config.bankroll * self.config.kelly_fraction)
        if spend < self.config.min_spend_per_signal:
            return []
            
        size = min(self.config.max_quote_size, spend / quote_price)
        if context.tiny_live_cap is not None:
            size = min(size, Decimal(str(context.tiny_live_cap)))
        if size < market.order_min_size:
            size = market.order_min_size
            
        notional = size * quote_price
        if notional < self.config.min_spend_per_signal:
            return []
            
        logger.info(f"MOMENTUM SPOTTED: {market.slug} | outcome={outcome} | prob={prob:.4f} | ask={ask:.4f} | edge={edge:.4f}")
        
        return [
            QuoteIntent(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                side=OrderSide.BUY,
                price=quote_price,
                size=size,
                order_type=OrderType.FAK,
                post_only=False,
                tick_size=market.tick_size,
                neg_risk=market.neg_risk,
                reason=f"momentum_shock; prob={prob:.4f}; edge={edge:.4f}; ask={ask:.4f}",
            )
        ]
