from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.config.settings import RiskLimits, TradingSettings
from app.core.state import BotState
from app.core.types import (
    ZERO,
    OrderBook,
    OrderSide,
    QuoteIntent,
    RejectReason,
    RiskDecision,
    utc_now,
)


class RiskEngine:
    def __init__(self, state: BotState, limits: RiskLimits, trading: TradingSettings) -> None:
        self.state = state
        self.limits = limits
        self.trading = trading
        self.seen_client_order_keys: set[str] = set()

    def _book_is_stale(self, book: OrderBook | None) -> bool:
        if book is None:
            return True
        age = (utc_now() - book.timestamp).total_seconds()
        return age > self.limits.stale_book_seconds

    def _slippage_rejected(self, quote: QuoteIntent, book: OrderBook | None) -> bool:
        if book is None:
            return True
        if quote.side == OrderSide.BUY and book.best_ask is not None:
            max_price = book.best_ask * (
                Decimal("1") + self.limits.max_slippage_bps / Decimal("10000")
            )
            return quote.price > max_price
        if quote.side == OrderSide.SELL and book.best_bid is not None:
            min_price = book.best_bid * (
                Decimal("1") - self.limits.max_slippage_bps / Decimal("10000")
            )
            return quote.price < min_price
        return False

    def _spread_floor_rejected(self, quote: QuoteIntent, book: OrderBook | None) -> bool:
        if not quote.post_only:
            return False
        if book is None or book.spread is None:
            return True
        return book.spread < self.limits.min_spread

    def _should_count_rejection(self, reasons: list[RejectReason]) -> bool:
        operational_reasons = {
            RejectReason.WS_DISCONNECTED,
            RejectReason.BALANCE_UNVERIFIED,
            RejectReason.INSUFFICIENT_BALANCE,
            RejectReason.AUTH_INVALID,
            RejectReason.RATE_LIMIT_PRESSURE,
            RejectReason.COMPLIANCE_BLOCKED,
            RejectReason.STALE_BOOK,
            RejectReason.DUPLICATE_ORDER,
        }
        return any(reason not in operational_reasons for reason in reasons)

    def forget_client_order_key(self, key: str | None) -> None:
        if key:
            self.seen_client_order_keys.discard(key)

    async def pre_trade(self, quote: QuoteIntent) -> RiskDecision:
        async with self.state.lock:
            book = self.state.books.get(quote.token_id)
            reasons: list[RejectReason] = []
            if self.state.kill_switch_enabled:
                reasons.append(RejectReason.KILL_SWITCH)
            if self.trading.require_ws_connected and (
                not self.state.connectivity.market_ws_connected
                or not self.state.connectivity.user_ws_connected
            ):
                reasons.append(RejectReason.WS_DISCONNECTED)
            if self.trading.require_balance_verified and not self.state.balances.verified:
                reasons.append(RejectReason.BALANCE_UNVERIFIED)
            if (
                self.trading.live_enabled
                and self.trading.require_balance_verified
                and (
                    self.state.balances.collateral <= ZERO
                    or self.state.balances.allowance <= ZERO
                )
            ):
                reasons.append(RejectReason.INSUFFICIENT_BALANCE)
            if not self.state.connectivity.auth_valid:
                reasons.append(RejectReason.AUTH_INVALID)
            if self.trading.require_geoblock_ok and not self.state.connectivity.compliance_ok:
                reasons.append(RejectReason.COMPLIANCE_BLOCKED)
            if self.state.connectivity.rate_limit_pressure:
                reasons.append(RejectReason.RATE_LIMIT_PRESSURE)
            if self._book_is_stale(book):
                reasons.append(RejectReason.STALE_BOOK)
            if quote.size > self.limits.max_order_size:
                reasons.append(RejectReason.MAX_ORDER_SIZE)
            if self.trading.tiny_live_mode and quote.size > self.trading.tiny_live_max_order_size:
                reasons.append(RejectReason.MAX_ORDER_SIZE)
            if (
                self.state.market_exposure(quote.market) + quote.notional
                > self.limits.max_notional_exposure_per_market
            ):
                reasons.append(RejectReason.MARKET_EXPOSURE)
            if (
                self.state.total_exposure() + quote.notional
                > self.limits.max_total_account_exposure
            ):
                reasons.append(RejectReason.TOTAL_EXPOSURE)
            if (
                abs(self.state.daily_pnl) > self.limits.max_daily_loss
                and self.state.daily_pnl < ZERO
            ):
                reasons.append(RejectReason.DAILY_LOSS)
            drawdown = self.state.high_watermark - (
                self.state.realized_pnl + self.state.unrealized_pnl
            )
            if drawdown > self.limits.max_drawdown:
                reasons.append(RejectReason.DRAWDOWN)
            if (
                self.state.open_orders_for_market(quote.market)
                >= self.limits.max_open_orders_per_market
            ):
                reasons.append(RejectReason.OPEN_ORDERS)
            if quote.client_order_key and quote.client_order_key in self.seen_client_order_keys:
                reasons.append(RejectReason.DUPLICATE_ORDER)
            if self._slippage_rejected(quote, book):
                reasons.append(RejectReason.SLIPPAGE)
            if self._spread_floor_rejected(quote, book):
                reasons.append(RejectReason.SPREAD_FLOOR)
            if quote.price <= ZERO or quote.price >= Decimal("1") or quote.size <= ZERO:
                reasons.append(RejectReason.INVALID_QUOTE)
            if reasons:
                if self._should_count_rejection(reasons):
                    self.state.rejected_order_count += 1
                self.state.last_rejection_at = datetime.now(tz=UTC)
                return RiskDecision(
                    allowed=False, reasons=reasons, message="pre-trade risk rejection"
                )
            if quote.client_order_key:
                self.seen_client_order_keys.add(quote.client_order_key)
            return RiskDecision.allow()

    async def post_trade_fill(self, fill_size: Decimal) -> RiskDecision:
        async with self.state.lock:
            if fill_size >= self.limits.unusually_large_fill:
                self.state.kill_switch_enabled = True
                self.state.kill_switch_reason = "unusually_large_fill"
                return RiskDecision.reject(RejectReason.KILL_SWITCH, message="unusually large fill")
            return RiskDecision.allow()

    async def circuit_breaker_check(self) -> RiskDecision:
        async with self.state.lock:
            if self.state.rejected_order_count >= self.limits.rejected_order_circuit_breaker:
                self.state.kill_switch_enabled = True
                self.state.kill_switch_reason = "repeated_rejected_orders"
                return RiskDecision.reject(
                    RejectReason.KILL_SWITCH, message="repeated rejected orders"
                )
            if self.state.partial_fill_count >= self.limits.partial_fill_circuit_breaker:
                self.state.kill_switch_enabled = True
                self.state.kill_switch_reason = "repeated_partial_fills"
                return RiskDecision.reject(
                    RejectReason.KILL_SWITCH, message="repeated partial fills"
                )
            return RiskDecision.allow()
