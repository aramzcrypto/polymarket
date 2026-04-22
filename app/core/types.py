from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

ZERO = Decimal("0")
ONE = Decimal("1")


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    FAK = "FAK"


class TradingMode(StrEnum):
    DRY_RUN = "dry_run"
    TINY_LIVE = "tiny_live"
    LIVE = "live"


class RejectReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    DRY_RUN = "dry_run"
    STALE_BOOK = "stale_book"
    WS_DISCONNECTED = "ws_disconnected"
    BALANCE_UNVERIFIED = "balance_unverified"
    AUTH_INVALID = "auth_invalid"
    RATE_LIMIT_PRESSURE = "rate_limit_pressure"
    COMPLIANCE_BLOCKED = "compliance_blocked"
    MAX_ORDER_SIZE = "max_order_size"
    MARKET_EXPOSURE = "market_exposure"
    TOTAL_EXPOSURE = "total_exposure"
    DAILY_LOSS = "daily_loss"
    DRAWDOWN = "drawdown"
    INVENTORY_IMBALANCE = "inventory_imbalance"
    OPEN_ORDERS = "open_orders"
    DUPLICATE_ORDER = "duplicate_order"
    SLIPPAGE = "slippage"
    SPREAD_FLOOR = "spread_floor"
    INVALID_QUOTE = "invalid_quote"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def decimalize(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_to_tick(
    value: Decimal | int | float | str,
    tick_size: Decimal | int | float | str,
    *,
    side: OrderSide | None = None,
) -> Decimal:
    price = decimalize(value)
    tick = decimalize(tick_size)
    if tick <= ZERO:
        raise ValueError("tick_size must be positive")
    units = price / tick
    rounding = ROUND_DOWN if side == OrderSide.BUY else ROUND_HALF_UP
    if side == OrderSide.SELL:
        units = units.to_integral_value(rounding=ROUND_HALF_UP)
        if units * tick < price:
            units += 1
        return max(ZERO, min(ONE, units * tick))
    units = units.to_integral_value(rounding=rounding)
    return max(ZERO, min(ONE, units * tick))


class PriceLevel(BaseModel):
    price: Decimal
    size: Decimal

    @field_validator("price", "size", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class MarketToken(BaseModel):
    token_id: str
    outcome: str
    price: Decimal | None = None

    @field_validator("price", mode="before")
    @classmethod
    def parse_optional_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else decimalize(value)


class Market(BaseModel):
    condition_id: str
    question: str = ""
    slug: str | None = None
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    neg_risk: bool = False
    minimum_tick_size: Decimal = Decimal("0.01")
    tokens: list[MarketToken] = Field(default_factory=list)

    @field_validator("minimum_tick_size", mode="before")
    @classmethod
    def parse_tick(cls, value: Any) -> Decimal:
        return decimalize(value or "0.01")


class OrderBook(BaseModel):
    market: str
    asset_id: str
    bids: list[PriceLevel] = Field(default_factory=list)
    asks: list[PriceLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utc_now)

    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids if level.size > ZERO), default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks if level.size > ZERO), default=None)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def top_liquidity(self) -> Decimal:
        bid = next(
            (level.size for level in sorted(self.bids, key=lambda x: x.price, reverse=True)), ZERO
        )
        ask = next((level.size for level in sorted(self.asks, key=lambda x: x.price)), ZERO)
        return bid + ask


class QuoteIntent(BaseModel):
    strategy: str
    market: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    post_only: bool = True
    tick_size: Decimal = Decimal("0.01")
    neg_risk: bool = False
    client_order_key: str | None = None
    reason: str = ""

    @field_validator("price", "size", "tick_size", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    def normalized(self) -> QuoteIntent:
        return self.model_copy(
            update={"price": quantize_to_tick(self.price, self.tick_size, side=self.side)}
        )


class OpenOrder(BaseModel):
    order_id: str
    client_order_key: str
    market: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    filled_size: Decimal = ZERO
    status: str = "live"
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("price", "size", "filled_size", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)

    @property
    def remaining_size(self) -> Decimal:
        return max(ZERO, self.size - self.filled_size)

    @property
    def remaining_notional(self) -> Decimal:
        return self.remaining_size * self.price


class Fill(BaseModel):
    trade_id: str
    order_id: str | None = None
    market: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    fee: Decimal = ZERO
    status: str = "MATCHED"
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("price", "size", "fee", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class Position(BaseModel):
    market: str
    token_id: str
    outcome: str = ""
    size: Decimal = ZERO
    avg_price: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO

    @field_validator("size", "avg_price", "realized_pnl", "unrealized_pnl", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)

    @property
    def notional(self) -> Decimal:
        return abs(self.size * self.avg_price)


class BalanceSnapshot(BaseModel):
    collateral: Decimal = ZERO
    allowance: Decimal = ZERO
    verified: bool = False
    updated_at: datetime | None = None

    @field_validator("collateral", "allowance", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class ConnectivityState(BaseModel):
    market_ws_connected: bool = False
    user_ws_connected: bool = False
    clob_ok: bool = False
    auth_valid: bool = False
    compliance_ok: bool = False
    rate_limit_pressure: bool = False
    last_market_msg_at: datetime | None = None
    last_user_msg_at: datetime | None = None
    last_error: str | None = None


class CryptoPriceSnapshot(BaseModel):
    symbol: str
    price: Decimal
    source: str
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("price", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        return decimalize(value)


class BtcIntervalMarket(BaseModel):
    market_id: str
    condition_id: str
    question: str
    slug: str | None = None
    start_time: datetime | None = None
    end_time: datetime
    price_to_beat: Decimal | None = None
    up_token_id: str
    down_token_id: str
    tick_size: Decimal = Decimal("0.01")
    neg_risk: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("price_to_beat", "tick_size", mode="before")
    @classmethod
    def parse_optional_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else decimalize(value)

    def token_for_outcome(self, outcome: str) -> str:
        return self.up_token_id if outcome.upper() == "UP" else self.down_token_id


class StrategySignal(BaseModel):
    strategy: str
    market: str
    token_id: str | None = None
    outcome: str | None = None
    action: str = "hold"
    confidence: Decimal = ZERO
    edge: Decimal = ZERO
    model_probability: Decimal | None = None
    offered_price: Decimal | None = None
    expected_reward_multiple: Decimal | None = None
    btc_price: Decimal | None = None
    price_to_beat: Decimal | None = None
    seconds_to_expiry: float | None = None
    reason: str = ""
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator(
        "confidence",
        "edge",
        "model_probability",
        "offered_price",
        "expected_reward_multiple",
        "btc_price",
        "price_to_beat",
        mode="before",
    )
    @classmethod
    def parse_optional_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else decimalize(value)


class RiskDecision(BaseModel):
    allowed: bool
    reasons: list[RejectReason] = Field(default_factory=list)
    message: str = ""

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(allowed=True)

    @classmethod
    def reject(cls, *reasons: RejectReason, message: str = "") -> RiskDecision:
        return cls(allowed=False, reasons=list(reasons), message=message)
