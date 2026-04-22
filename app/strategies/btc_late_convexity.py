from __future__ import annotations

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
)
from app.data.crypto_prices import implied_reward_multiple
from app.strategies.base import Strategy, StrategyContext


class BtcLateConvexityStrategy(Strategy):
    name = "btc_5m_late_convexity"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.markets_by_token: dict[str, BtcIntervalMarket] = {}
        self.price: CryptoPriceSnapshot | None = None
        self.volatility_bps = config.default_volatility_bps
        self.last_signals: list[StrategySignal] = []

    def update_price(self, price: CryptoPriceSnapshot, volatility_bps: Decimal) -> None:
        self.price = price
        self.volatility_bps = max(self.config.default_volatility_bps, volatility_bps)

    def update_markets(self, markets: list[BtcIntervalMarket]) -> None:
        self.markets_by_token = {}
        for market in markets:
            self.markets_by_token[market.up_token_id] = market
            self.markets_by_token[market.down_token_id] = market

    async def on_market_update(self, book: OrderBook) -> None:
        return None

    async def on_fill(self, fill: Fill) -> None:
        return None

    async def on_risk_state(self, decision: RiskDecision) -> None:
        return None

    def _tail_probability(self, distance_bps: Decimal, seconds_to_expiry: float) -> Decimal:
        if seconds_to_expiry <= 0:
            return ZERO
        scale = Decimal(str(math.sqrt(seconds_to_expiry / self.config.volatility_window_seconds)))
        sigma = max(Decimal("0.01"), self.volatility_bps * scale)
        z = distance_bps / sigma
        if z > self.config.max_required_move_sigma:
            return ZERO
        probability = Decimal(
            str(Decimal("0.5") * Decimal(str(math.erfc(float(z) / math.sqrt(2)))))
        )
        return max(ZERO, min(Decimal("0.99"), probability))

    def _signal_for_book(self, book: OrderBook) -> StrategySignal:
        market = self.markets_by_token.get(book.asset_id)
        if market is None:
            return StrategySignal(
                strategy=self.name,
                market=book.market,
                token_id=book.asset_id,
                action="hold",
                reason="book is not a discovered BTC 5m token",
            )
        seconds_to_expiry = (market.end_time - utc_now()).total_seconds()
        if self.price is None:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                action="hold",
                reason="missing BTC price feed",
                seconds_to_expiry=seconds_to_expiry,
            )
        if market.price_to_beat is None:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                action="hold",
                btc_price=self.price.price,
                seconds_to_expiry=seconds_to_expiry,
                reason="missing price_to_beat in market metadata",
            )
        if seconds_to_expiry > self.config.max_seconds_to_expiry:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                action="hold",
                btc_price=self.price.price,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="too early for late-convexity entry",
            )
        if seconds_to_expiry < self.config.min_seconds_to_expiry:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                action="hold",
                btc_price=self.price.price,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="too close to expiry",
            )
        losing_outcome = "DOWN" if self.price.price > market.price_to_beat else "UP"
        token_id = market.token_for_outcome(losing_outcome)
        if book.asset_id != token_id:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                action="hold",
                outcome=losing_outcome,
                btc_price=self.price.price,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="not the currently losing side",
            )
        ask = book.best_ask
        if ask is None:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                outcome=losing_outcome,
                action="hold",
                reason="missing ask liquidity",
                seconds_to_expiry=seconds_to_expiry,
            )
        reward = implied_reward_multiple(ask)
        distance_bps = (
            abs(self.price.price - market.price_to_beat) / self.price.price * Decimal("10000")
        )
        probability = self._tail_probability(distance_bps, seconds_to_expiry)
        edge = probability - ask
        action = "buy_losing_side" if edge >= self.config.min_edge else "hold"
        reason = "edge clears threshold" if action != "hold" else "edge below threshold"
        if ask < self.config.min_longshot_price or ask > self.config.max_longshot_price:
            action = "hold"
            reason = "longshot price outside configured band"
        if reward < self.config.min_reward_multiple:
            action = "hold"
            reason = "reward multiple too low"
        return StrategySignal(
            strategy=self.name,
            market=market.condition_id,
            token_id=book.asset_id,
            outcome=losing_outcome,
            action=action,
            confidence=probability,
            edge=edge,
            model_probability=probability,
            offered_price=ask,
            expected_reward_multiple=reward,
            btc_price=self.price.price,
            price_to_beat=market.price_to_beat,
            seconds_to_expiry=seconds_to_expiry,
            reason=reason,
        )

    async def desired_quotes(self, book: OrderBook, context: StrategyContext) -> list[QuoteIntent]:
        signal = self._signal_for_book(book)
        self.last_signals.append(signal)
        self.last_signals = self.last_signals[-100:]
        if signal.action != "buy_losing_side" or signal.offered_price is None:
            return []
        market = self.markets_by_token[book.asset_id]
        already_live = context.live_orders_by_token.get(book.asset_id, 0)
        if already_live > 0:
            return []
        spend = min(
            self.config.max_spend_per_signal, self.config.bankroll * self.config.kelly_fraction
        )
        if context.tiny_live_cap is not None:
            spend = min(spend, Decimal(str(context.tiny_live_cap)))
        size = min(self.config.max_quote_size, spend / signal.offered_price)
        if size <= ZERO:
            return []
        return [
            QuoteIntent(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                side=OrderSide.BUY,
                price=signal.offered_price,
                size=size,
                order_type=OrderType.FAK,
                post_only=False,
                tick_size=market.tick_size,
                neg_risk=market.neg_risk,
                reason=f"{signal.reason}; edge={signal.edge}",
            )
        ]
