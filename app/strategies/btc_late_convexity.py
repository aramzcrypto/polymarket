from __future__ import annotations

import logging
import math
from datetime import timedelta
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

logger = logging.getLogger(__name__)


class BtcLateConvexityStrategy(Strategy):
    name = "btc_5m_late_convexity"

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.markets_by_token: dict[str, BtcIntervalMarket] = {}
        self.markets_by_condition: dict[str, BtcIntervalMarket] = {}
        self.books_by_token: dict[str, OrderBook] = {}
        self.open_prices_by_market: dict[str, Decimal] = {}
        self.filled_markets: set[str] = set()
        self.price: CryptoPriceSnapshot | None = None
        self.volatility_bps = config.default_volatility_bps
        self.last_signals: list[StrategySignal] = []

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
        return None

    async def on_fill(self, fill: Fill) -> None:
        if fill.side == OrderSide.BUY and fill.market:
            self.filled_markets.add(fill.market)
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

    def _capture_open_prices(self, price: CryptoPriceSnapshot) -> None:
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
    ) -> tuple[Decimal | None, str]:
        if self.price is None:
            return None, "missing BTC price feed"
        reference = market.price_to_beat or self.open_prices_by_market.get(market.condition_id)
        if reference is None:
            return None, "missing interval open price"
        if reference <= ZERO or self.price.price <= ZERO:
            return None, "invalid BTC reference price"
        distance_bps = (self.price.price - reference) / self.price.price * Decimal("10000")
        z = distance_bps / self._remaining_sigma_bps(seconds_to_expiry)
        up_probability = self._normal_cdf(z)
        if outcome == "UP":
            return up_probability, "model price clears threshold"
        return Decimal("1") - up_probability, "model price clears threshold"

    def _cheapest_outcome_token(self, market: BtcIntervalMarket) -> str | None:
        candidates: list[tuple[Decimal, str]] = []
        for token_id in (market.up_token_id, market.down_token_id):
            book = self.books_by_token.get(token_id)
            if book is None or book.best_ask is None:
                return None
            candidates.append((book.best_ask, token_id))
        return min(candidates, key=lambda item: (item[0], item[1]))[1]

    def _entry_price(self, ask: Decimal, tick_size: Decimal) -> Decimal:
        if self.config.entry_price_buffer_ticks <= 0:
            return ask
        buffered = ask + tick_size * Decimal(self.config.entry_price_buffer_ticks)
        if self.config.max_entry_price_buffer_bps > ZERO:
            max_buffered = ask * (
                Decimal("1") + self.config.max_entry_price_buffer_bps / Decimal("10000")
            )
            if buffered > max_buffered:
                return ask
        return min(Decimal("0.99"), buffered)

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
        if seconds_to_expiry > self.config.max_seconds_to_expiry:
            logger.debug("STRATEGY: %s | too early (%ds)", market.slug, int(seconds_to_expiry))
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                action="hold",
                btc_price=self.price.price if self.price is not None else None,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="too early for late-convexity entry",
            )
        if seconds_to_expiry < self.config.min_seconds_to_expiry:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                action="hold",
                btc_price=self.price.price if self.price is not None else None,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="too close to expiry",
            )
        if book.asset_id == market.up_token_id:
            outcome = "UP"
        elif book.asset_id == market.down_token_id:
            outcome = "DOWN"
        else:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                action="hold",
                btc_price=self.price.price if self.price is not None else None,
                price_to_beat=market.price_to_beat,
                seconds_to_expiry=seconds_to_expiry,
                reason="book token is not an UP/DOWN outcome token",
            )
        ask = book.best_ask
        if ask is None:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                outcome=outcome,
                action="hold",
                reason="missing ask liquidity",
                seconds_to_expiry=seconds_to_expiry,
            )
        cheapest_token = self._cheapest_outcome_token(market)
        if cheapest_token is None:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                outcome=outcome,
                action="hold",
                reason="waiting for both outcome books",
                seconds_to_expiry=seconds_to_expiry,
            )
        if book.asset_id != cheapest_token:
            return StrategySignal(
                strategy=self.name,
                market=market.condition_id,
                token_id=book.asset_id,
                outcome=outcome,
                action="hold",
                reason="not the cheaper late-reversal side",
                seconds_to_expiry=seconds_to_expiry,
            )
        reward = implied_reward_multiple(ask)
        probability, model_reason = self._outcome_probability(market, outcome, seconds_to_expiry)
        edge = (probability - ask) if probability is not None else ZERO
        action = "buy_late_reversal"
        reason = (
            "cheaper late-reversal side"
            if probability is None
            else f"cheaper late-reversal side; {model_reason}"
        )
        if ask < self.config.min_longshot_price or ask > self.config.max_longshot_price:
            action = "hold"
            reason = "price outside configured band"
        if action != "hold" and reward < self.config.min_reward_multiple:
            action = "hold"
            reason = "reward multiple too low"
        logger.info(
            "STRATEGY TICK: %s | outcome=%s | ask=%s | prob=%.4f | edge=%.4f | action=%s",
            market.slug,
            outcome,
            ask,
            probability or ZERO,
            edge,
            action,
        )
        return StrategySignal(
            strategy=self.name,
            market=market.condition_id,
            token_id=book.asset_id,
            outcome=outcome,
            action=action,
            confidence=probability or ZERO,
            edge=edge,
            model_probability=probability,
            offered_price=ask,
            expected_reward_multiple=reward,
            btc_price=self.price.price if self.price is not None else None,
            price_to_beat=market.price_to_beat,
            seconds_to_expiry=seconds_to_expiry,
            reason=reason,
        )

    async def desired_quotes(self, book: OrderBook, context: StrategyContext) -> list[QuoteIntent]:
        self.books_by_token[book.asset_id] = book
        signal = self._signal_for_book(book)
        self.last_signals.append(signal)
        self.last_signals = self.last_signals[-100:]
        if signal.action != "buy_late_reversal" or signal.offered_price is None:
            return []
        market = self.markets_by_token[book.asset_id]
        if market.condition_id in self.filled_markets:
            return []
        current_position = Decimal(str(context.inventory_by_token.get(book.asset_id, 0)))
        if current_position > ZERO:
            return []
        already_live = context.live_orders_by_token.get(book.asset_id, 0)
        if already_live > 0:
            return []
        quote_price = self._entry_price(signal.offered_price, market.tick_size)
        target_spend = (
            self.config.max_spend_per_signal
            if (
                signal.model_probability is not None
                and signal.model_probability >= self.config.high_confidence_probability
            )
            else self.config.min_spend_per_signal
        )
        spend = min(target_spend, self.config.bankroll * self.config.kelly_fraction)
        if spend < self.config.min_spend_per_signal:
            return []
        size = min(self.config.max_quote_size, spend / quote_price)
        if context.tiny_live_cap is not None:
            size = min(size, Decimal(str(context.tiny_live_cap)))
        if size <= ZERO:
            return []
        if size < market.order_min_size:
            size = market.order_min_size
        notional = size * quote_price
        if (
            notional < self.config.min_spend_per_signal
            or notional > self.config.max_spend_per_signal
        ):
            return []
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
                reason=(
                    f"{signal.reason}; edge={signal.edge}; prob={signal.model_probability}; "
                    f"ask={signal.offered_price}"
                ),
            )
        ]
