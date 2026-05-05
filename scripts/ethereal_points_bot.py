from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, cast

from app.ethereal.client import EtherealClient
from app.ethereal.config import EtherealSettings, load_ethereal_settings
from app.ethereal.signing import (
    address_from_private_key,
    build_limit_order_data,
    build_limit_order_payload,
)

BUY = 0
SELL = 1


def bps(spread: Decimal, mid: Decimal) -> Decimal:
    if mid <= 0:
        return Decimal("999999")
    return spread / mid * Decimal("10000")


def signed_bps(price: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        return Decimal("0")
    return (price - reference) / reference * Decimal("10000")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def decimal_field(row: dict[str, Any], key: str, default: str = "0") -> Decimal:
    return Decimal(str(row.get(key, default) or default))


@dataclass(frozen=True)
class MarketSnapshot:
    product_id: str
    onchain_id: int
    ticker: str
    lot_size: Decimal
    min_quantity: Decimal
    best_bid: Decimal
    best_ask: Decimal
    oracle_price: Decimal
    funding_rate_1h: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return bps(self.best_ask - self.best_bid, self.mid)

    @property
    def basis_bps(self) -> Decimal:
        return signed_bps(self.mid, self.oracle_price)


@dataclass(frozen=True)
class AccountRisk:
    total_balance_usd: Decimal
    available_balance_usd: Decimal
    used_margin_usd: Decimal

    @property
    def margin_usage_bps(self) -> Decimal:
        if self.total_balance_usd <= 0:
            return Decimal("999999")
        return self.used_margin_usd / self.total_balance_usd * Decimal("10000")


@dataclass(frozen=True)
class PriceSample:
    ts: float
    mid: Decimal


class TrendState:
    def __init__(self, max_samples: int = 720) -> None:
        self.samples: deque[PriceSample] = deque(maxlen=max_samples)

    def record(self, snapshot: MarketSnapshot) -> None:
        self.samples.append(PriceSample(ts=time.time(), mid=snapshot.mid))

    def return_bps(self, lookback_seconds: int) -> Decimal | None:
        if len(self.samples) < 2:
            return None
        cutoff = time.time() - lookback_seconds
        start = self.samples[0]
        for sample in self.samples:
            if sample.ts >= cutoff:
                start = sample
                break
        end = self.samples[-1]
        if start is end or start.mid <= 0:
            return None
        return signed_bps(end.mid, start.mid)


TREND_STATE = TrendState()
POSITION_PEAK_PNL_BPS: dict[str, Decimal] = {}


@dataclass
class SessionRiskState:
    start_equity_usd: Decimal | None = None
    entry_pause_reason: str | None = None


SESSION_RISK_STATE = SessionRiskState()


def side_label(side: int) -> str:
    return "BUY" if side == BUY else "SELL"


def log_event(event: str, **fields: object) -> None:
    payload: dict[str, object] = {"event": event, "ts": int(time.time())}
    payload.update(fields)
    print(json.dumps(payload), flush=True)


def fetch_snapshot(client: EtherealClient, ticker: str) -> MarketSnapshot:
    products = client.products(ticker=ticker).get("data", [])
    if not products:
        raise SystemExit(f"ticker not found: {ticker}")
    product = products[0]
    product_id = product["id"]
    prices = client.market_prices([product_id]).get("data", [])
    if not prices:
        raise SystemExit(f"market prices unavailable for: {ticker}")
    projected = client.projected_funding_rates([product_id]).get("data", [])
    price_row = prices[0]
    funding_row = projected[0] if projected else {}
    return MarketSnapshot(
        product_id=product_id,
        onchain_id=int(product["onchainId"]),
        ticker=product["ticker"],
        lot_size=Decimal(product["lotSize"]),
        min_quantity=Decimal(product["minQuantity"]),
        best_bid=Decimal(price_row["bestBidPrice"]),
        best_ask=Decimal(price_row["bestAskPrice"]),
        oracle_price=Decimal(price_row["oraclePrice"]),
        funding_rate_1h=Decimal(str(funding_row.get("fundingRate1h", "0"))),
    )


def choose_entry_side(snapshot: MarketSnapshot, settings: EtherealSettings) -> int | None:
    entry_basis_bps = settings.entry_basis_bps
    if (
        settings.volume_mode_enabled
        and snapshot.spread_bps <= settings.tight_spread_max_bps
    ):
        entry_basis_bps = min(entry_basis_bps, settings.tight_spread_entry_basis_bps)
    if (
        snapshot.basis_bps <= -entry_basis_bps
        and snapshot.funding_rate_1h <= -settings.funding_entry_threshold_1h
    ):
        return BUY
    if (
        snapshot.basis_bps >= entry_basis_bps
        and snapshot.funding_rate_1h >= settings.funding_entry_threshold_1h
    ):
        return SELL
    return None


def expected_edge_bps(snapshot: MarketSnapshot, settings: EtherealSettings) -> Decimal:
    expected_hold_hours = settings.expected_hold_minutes / Decimal("60")
    carry_bps = abs(snapshot.funding_rate_1h) * Decimal("10000") * expected_hold_hours
    return abs(snapshot.basis_bps) + carry_bps


def momentum_rejection_reason(
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    side: int,
    trend_state: TrendState,
) -> str | None:
    recent_return_bps = trend_state.return_bps(settings.adverse_momentum_lookback_seconds)
    if recent_return_bps is None:
        return None
    if side == BUY and recent_return_bps <= -settings.max_adverse_momentum_bps:
        return "adverse_down_momentum"
    if side == SELL and recent_return_bps >= settings.max_adverse_momentum_bps:
        return "adverse_up_momentum"
    return None


def entry_quality_rejection_reason(
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    side: int,
    trend_state: TrendState,
) -> str | None:
    if expected_edge_bps(snapshot, settings) < settings.min_expected_edge_bps:
        return "expected_edge_low"
    return momentum_rejection_reason(snapshot, settings, side, trend_state)


def entry_threshold_bps(snapshot: MarketSnapshot, settings: EtherealSettings) -> Decimal:
    if (
        settings.volume_mode_enabled
        and snapshot.spread_bps <= settings.tight_spread_max_bps
    ):
        return min(settings.entry_basis_bps, settings.tight_spread_entry_basis_bps)
    return settings.entry_basis_bps


def target_quantity(snapshot: MarketSnapshot, target_notional_usd: Decimal) -> Decimal:
    return floor_to_step(target_notional_usd / snapshot.mid, snapshot.lot_size)


def desired_quantity(snapshot: MarketSnapshot, settings: EtherealSettings) -> Decimal:
    if settings.target_btc_size is not None:
        quantity = settings.target_btc_size
    else:
        quantity = target_quantity(snapshot, settings.target_notional_usd)
    return floor_to_step(min(quantity, settings.max_position_btc_size), snapshot.lot_size)


def risk_adjusted_quantity(
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    account_risk: AccountRisk | None,
) -> Decimal:
    quantity = desired_quantity(snapshot, settings)
    if account_risk is None or account_risk.total_balance_usd <= 0:
        return quantity
    leverage_cap = account_risk.total_balance_usd * settings.max_account_leverage / snapshot.mid
    required_buffer = (
        settings.min_entry_liquidation_distance_bps + settings.maintenance_margin_buffer_bps
    ) / Decimal("10000")
    liq_cap = account_risk.total_balance_usd / required_buffer / snapshot.mid
    return floor_to_step(min(quantity, leverage_cap, liq_cap), snapshot.lot_size)


def conviction_adjusted_quantity(
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    account_risk: AccountRisk | None,
) -> Decimal:
    quantity = risk_adjusted_quantity(snapshot, settings, account_risk)
    if expected_edge_bps(snapshot, settings) >= settings.full_size_expected_edge_bps:
        return quantity
    fraction = max(Decimal("0"), min(Decimal("1"), settings.starter_position_fraction))
    return floor_to_step(quantity * fraction, snapshot.lot_size)


def estimated_liquidation_distance_bps(
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    account_risk: AccountRisk | None,
    position_size: Decimal,
) -> Decimal | None:
    if account_risk is None:
        return None
    notional = abs(position_size) * snapshot.mid
    if notional <= 0:
        return None
    equity_buffer_bps = account_risk.total_balance_usd / notional * Decimal("10000")
    return max(Decimal("0"), equity_buffer_bps - settings.maintenance_margin_buffer_bps)


def fetch_account_risk(client: EtherealClient, subaccount_id: str) -> AccountRisk | None:
    rows = client.balances(subaccount_id).get("data", [])
    if not rows:
        return None
    row = next((item for item in rows if item.get("tokenName") == "USD"), rows[0])
    total = decimal_field(row, "amount")
    available = decimal_field(row, "available")
    used = decimal_field(row, "totalUsed")
    return AccountRisk(
        total_balance_usd=total,
        available_balance_usd=available,
        used_margin_usd=used,
    )


def risk_rejection_reason(
    *,
    snapshot: MarketSnapshot,
    settings: EtherealSettings,
    account_risk: AccountRisk | None,
    projected_position_size: Decimal,
) -> str | None:
    if account_risk is None:
        return "account_risk_unavailable" if settings.require_account_risk else None
    if account_risk.available_balance_usd < settings.min_available_balance_usd:
        return "available_balance_low"
    if account_risk.margin_usage_bps > settings.max_margin_usage_bps:
        return "margin_usage_high"
    liq_distance = estimated_liquidation_distance_bps(
        snapshot, settings, account_risk, projected_position_size
    )
    if liq_distance is not None and liq_distance < settings.min_entry_liquidation_distance_bps:
        return "liquidation_distance_low"
    return None


def update_session_risk(
    settings: EtherealSettings,
    account_risk: AccountRisk | None,
    state: SessionRiskState = SESSION_RISK_STATE,
) -> str | None:
    if account_risk is None or account_risk.total_balance_usd <= 0:
        return None
    if state.start_equity_usd is None:
        state.start_equity_usd = account_risk.total_balance_usd
    if state.start_equity_usd <= 0:
        return None
    drawdown_bps = (
        (state.start_equity_usd - account_risk.total_balance_usd)
        / state.start_equity_usd
        * Decimal("10000")
    )
    if drawdown_bps >= settings.max_session_drawdown_bps:
        state.entry_pause_reason = f"session_drawdown_{drawdown_bps:.2f}bps"
    return state.entry_pause_reason


def pnl_bps(position: dict[str, Any]) -> Decimal:
    cost = Decimal(str(position.get("cost", "0")))
    if cost <= 0:
        return Decimal("0")
    unrealized = Decimal(str(position.get("unrealizedPnl", "0")))
    return unrealized / cost * Decimal("10000")


def held_minutes(position: dict[str, Any]) -> Decimal:
    created_at = int(position.get("createdAt", 0))
    if created_at <= 0:
        return Decimal("0")
    age_ms = max(0, int(time.time() * 1000) - created_at)
    return Decimal(age_ms) / Decimal("60000")


def position_memory_key(snapshot: MarketSnapshot, position: dict[str, Any]) -> str:
    return ":".join(
        [
            snapshot.product_id,
            str(position.get("side", "")),
            str(position.get("createdAt", "")),
        ]
    )


def update_peak_pnl_bps(snapshot: MarketSnapshot, position: dict[str, Any]) -> Decimal:
    key = position_memory_key(snapshot, position)
    current = pnl_bps(position)
    peak = max(current, POSITION_PEAK_PNL_BPS.get(key, current))
    POSITION_PEAK_PNL_BPS[key] = peak
    return peak


def working_orders_for_product(
    client: EtherealClient, subaccount_id: str, product_id: str
) -> list[dict[str, Any]]:
    orders = client.orders(subaccount_id, is_working=True).get("data", [])
    return [order for order in orders if order.get("productId") == product_id]


def active_position_for_product(
    client: EtherealClient, subaccount_id: str, product_id: str
) -> dict[str, Any] | None:
    positions = client.positions(subaccount_id, open_only=True).get("data", [])
    for position in positions:
        if position.get("productId") == product_id:
            return cast(dict[str, Any], position)
    return None


def order_price_for_side(snapshot: MarketSnapshot, side: int, *, post_only: bool = True) -> Decimal:
    if not post_only:
        return snapshot.best_ask if side == BUY else snapshot.best_bid
    return snapshot.best_bid if side == BUY else snapshot.best_ask


def place_order(
    *,
    client: EtherealClient,
    settings: EtherealSettings,
    domain: dict[str, Any],
    signer_address: str,
    signer_private_key: str,
    onchain_id: int,
    side: int,
    quantity: Decimal,
    price: Decimal,
    reduce_only: bool,
    tag: str,
    post_only: bool = True,
    time_in_force: str = "GTD",
) -> dict[str, Any]:
    signed_at = int(time.time())
    expires_at = signed_at + settings.order_expiry_seconds
    client_order_id = f"{tag}{time.time_ns()}"
    if settings.live_trading:
        payload = build_limit_order_payload(
            domain=domain,
            signer_address=signer_address,
            signer_private_key=signer_private_key,
            subaccount_name=settings.subaccount_name,
            quantity=quantity,
            price=price,
            side=side,
            onchain_id=onchain_id,
            reduce_only=reduce_only,
            post_only=post_only,
            time_in_force=time_in_force,
            expires_at=expires_at,
            client_order_id=client_order_id,
            signed_at=signed_at,
        )
        return client.submit_order(payload)
    payload = {
        "data": build_limit_order_data(
            sender_address=settings.account_address,
            subaccount_name=settings.subaccount_name,
            quantity=quantity,
            price=price,
            side=side,
            onchain_id=onchain_id,
            reduce_only=reduce_only,
            post_only=post_only,
            time_in_force=time_in_force,
            expires_at=expires_at,
            client_order_id=client_order_id,
            signed_at=signed_at,
        )
    }
    return client.dry_run_order(payload)


def maybe_manage_position(
    *,
    client: EtherealClient,
    settings: EtherealSettings,
    domain: dict[str, Any],
    signer_address: str,
    signer_private_key: str,
    snapshot: MarketSnapshot,
    position: dict[str, Any],
    working_orders: list[dict[str, Any]],
    account_risk: AccountRisk | None,
    trend_state: TrendState,
    entry_pause_reason: str | None = None,
) -> None:
    minutes_held = held_minutes(position)
    current_pnl_bps = pnl_bps(position)
    peak_pnl_bps = update_peak_pnl_bps(snapshot, position)
    position_side = int(position["side"])
    signed_size = Decimal(str(position["size"]))
    current_size = abs(signed_size)
    exit_side = SELL if position_side == BUY else BUY
    desired_size = conviction_adjusted_quantity(snapshot, settings, account_risk)
    liq_distance_bps = estimated_liquidation_distance_bps(
        snapshot, settings, account_risk, current_size
    )
    funding_flipped = (
        snapshot.funding_rate_1h >= settings.funding_entry_threshold_1h
        if position_side == BUY
        else snapshot.funding_rate_1h <= -settings.funding_entry_threshold_1h
    )
    basis_reverted = (
        snapshot.basis_bps >= Decimal("0")
        if position_side == BUY
        else snapshot.basis_bps <= Decimal("0")
    )
    should_exit = False
    reason = ""
    if current_pnl_bps <= -settings.stop_loss_bps:
        should_exit = True
        reason = "stop_loss"
    elif (
        liq_distance_bps is not None
        and liq_distance_bps < settings.derisk_liquidation_distance_bps
    ):
        should_exit = True
        reason = "risk_deleverage"
    elif (
        account_risk is not None
        and account_risk.margin_usage_bps > settings.max_margin_usage_bps
    ):
        should_exit = True
        reason = "margin_deleverage"
    elif (
        settings.volume_mode_enabled
        and settings.fast_take_profit_bps > 0
        and minutes_held >= settings.fast_take_profit_min_minutes
        and current_pnl_bps >= settings.fast_take_profit_bps
    ):
        should_exit = True
        reason = "fast_take_profit"
    elif (
        settings.volume_mode_enabled
        and minutes_held >= settings.maker_recycle_min_minutes
        and current_pnl_bps >= settings.maker_recycle_profit_bps
    ):
        should_exit = True
        reason = "maker_recycle_profit"
    elif (
        minutes_held >= settings.min_hold_minutes
        and peak_pnl_bps >= settings.trailing_stop_activation_bps
        and current_pnl_bps >= Decimal("0")
        and current_pnl_bps
        <= max(
            settings.trailing_stop_floor_bps,
            peak_pnl_bps - settings.trailing_stop_distance_bps,
        )
    ):
        should_exit = True
        reason = "trailing_profit_stop"
    elif (
        settings.volume_mode_enabled
        and minutes_held >= settings.min_hold_minutes
        and current_pnl_bps > 0
        and funding_flipped
        and basis_reverted
    ):
        should_exit = True
        reason = "profitable_carry_flip"
    elif minutes_held >= settings.max_hold_minutes:
        should_exit = True
        reason = "max_hold"
    elif minutes_held >= settings.min_hold_minutes and current_pnl_bps >= settings.take_profit_bps:
        should_exit = True
        reason = "take_profit"
    elif minutes_held >= settings.min_hold_minutes and funding_flipped and basis_reverted:
        should_exit = True
        reason = "carry_flip"

    log_event(
        "position_state",
        ticker=snapshot.ticker,
        side=side_label(position_side),
        size=str(signed_size),
        desired_size=str(desired_size),
        cost=position["cost"],
        pnl_bps=f"{current_pnl_bps:.2f}",
        peak_pnl_bps=f"{peak_pnl_bps:.2f}",
        held_minutes=f"{minutes_held:.2f}",
        expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
        recent_return_bps=(
            f"{trend_state.return_bps(settings.adverse_momentum_lookback_seconds):.2f}"
            if trend_state.return_bps(settings.adverse_momentum_lookback_seconds) is not None
            else None
        ),
        liq_distance_bps=f"{liq_distance_bps:.2f}" if liq_distance_bps is not None else None,
        margin_usage_bps=(
            f"{account_risk.margin_usage_bps:.2f}" if account_risk is not None else None
        ),
        entry_pause_reason=entry_pause_reason,
        should_exit=should_exit,
        reason=reason,
        working_orders=len(working_orders),
    )
    entry_signal = choose_entry_side(snapshot, settings)
    if (
        not should_exit
        and settings.scale_in_enabled
        and not working_orders
        and entry_pause_reason is None
        and current_size < desired_size
        and current_pnl_bps >= settings.min_scale_in_pnl_bps
        and entry_signal == position_side
    ):
        quality_reason = entry_quality_rejection_reason(
            snapshot, settings, position_side, trend_state
        )
        if quality_reason is not None:
            log_event(
                "skip_scale_quality",
                ticker=snapshot.ticker,
                reason=quality_reason,
                expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
                recent_return_bps=(
                    f"{trend_state.return_bps(settings.adverse_momentum_lookback_seconds):.2f}"
                    if trend_state.return_bps(settings.adverse_momentum_lookback_seconds)
                    is not None
                    else None
                ),
            )
            return
        add_quantity = floor_to_step(desired_size - current_size, snapshot.lot_size)
        risk_reason = risk_rejection_reason(
            snapshot=snapshot,
            settings=settings,
            account_risk=account_risk,
            projected_position_size=current_size + add_quantity,
        )
        if risk_reason is not None:
            log_event(
                "skip_scale_risk",
                ticker=snapshot.ticker,
                reason=risk_reason,
                current_size=str(current_size),
                projected_size=str(current_size + add_quantity),
                desired_size=str(desired_size),
            )
        elif add_quantity >= snapshot.min_quantity:
            response = place_order(
                client=client,
                settings=settings,
                domain=domain,
                signer_address=signer_address,
                signer_private_key=signer_private_key,
                onchain_id=snapshot.onchain_id,
                side=position_side,
                quantity=add_quantity,
                price=order_price_for_side(snapshot, position_side),
                reduce_only=False,
                tag="scale",
                post_only=settings.entry_post_only,
            )
            log_event(
                "scale_in_order_submitted",
                ticker=snapshot.ticker,
                side=side_label(position_side),
                add_quantity=str(add_quantity),
                current_size=str(current_size),
                target_size=str(desired_size),
                price=str(order_price_for_side(snapshot, position_side)),
                response=response,
            )
            return
    if (
        not should_exit
        and settings.scale_in_enabled
        and not working_orders
        and entry_pause_reason is not None
        and current_size < desired_size
        and entry_signal == position_side
    ):
        log_event(
            "skip_scale_session_risk",
            ticker=snapshot.ticker,
            reason=entry_pause_reason,
            current_size=str(current_size),
            desired_size=str(desired_size),
        )
        return
    if not should_exit:
        return

    quantity = floor_to_step(current_size, snapshot.lot_size)
    if quantity < snapshot.min_quantity:
        log_event(
            "skip_exit_too_small",
            ticker=snapshot.ticker,
            quantity=str(quantity),
            min_quantity=str(snapshot.min_quantity),
        )
        return
    emergency_exit = reason in {"stop_loss", "risk_deleverage", "margin_deleverage"}
    if working_orders and not emergency_exit:
        log_event(
            "skip_exit_working_order",
            ticker=snapshot.ticker,
            reason=reason,
            working_orders=len(working_orders),
        )
        return
    exit_price = order_price_for_side(snapshot, exit_side, post_only=not emergency_exit)
    response = place_order(
        client=client,
        settings=settings,
        domain=domain,
        signer_address=signer_address,
        signer_private_key=signer_private_key,
        onchain_id=snapshot.onchain_id,
        side=exit_side,
        quantity=quantity,
        price=exit_price,
        reduce_only=True,
        tag="exit",
        post_only=not emergency_exit,
        time_in_force="IOC" if emergency_exit else "GTD",
    )
    log_event(
        "exit_order_submitted",
        ticker=snapshot.ticker,
        side=side_label(exit_side),
        quantity=str(quantity),
        price=str(exit_price),
        reason=reason,
        emergency=emergency_exit,
        response=response,
    )


def maybe_enter_position(
    *,
    client: EtherealClient,
    settings: EtherealSettings,
    domain: dict[str, Any],
    signer_address: str,
    signer_private_key: str,
    snapshot: MarketSnapshot,
    working_orders: list[dict[str, Any]],
    account_risk: AccountRisk | None,
    trend_state: TrendState,
    entry_pause_reason: str | None = None,
) -> None:
    if working_orders:
        log_event("working_order_present", ticker=snapshot.ticker, count=len(working_orders))
        return
    if entry_pause_reason is not None:
        log_event("skip_entry_session_risk", ticker=snapshot.ticker, reason=entry_pause_reason)
        return
    if snapshot.spread_bps > settings.max_spread_bps:
        log_event(
            "skip_spread",
            ticker=snapshot.ticker,
            spread_bps=f"{snapshot.spread_bps:.4f}",
            limit=str(settings.max_spread_bps),
        )
        return
    if abs(snapshot.funding_rate_1h) > settings.max_projected_funding_rate_1h:
        log_event(
            "skip_funding_limit",
            ticker=snapshot.ticker,
            funding_rate_1h=str(snapshot.funding_rate_1h),
            limit=str(settings.max_projected_funding_rate_1h),
        )
        return
    side = choose_entry_side(snapshot, settings)
    if side is None:
        log_event(
            "no_entry_signal",
            ticker=snapshot.ticker,
            basis_bps=f"{snapshot.basis_bps:.4f}",
            entry_basis_bps=str(entry_threshold_bps(snapshot, settings)),
            funding_rate_1h=str(snapshot.funding_rate_1h),
            expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
            volume_mode=settings.volume_mode_enabled,
        )
        return
    quality_reason = entry_quality_rejection_reason(snapshot, settings, side, trend_state)
    if quality_reason is not None:
        log_event(
            "skip_entry_quality",
            ticker=snapshot.ticker,
            side=side_label(side),
            reason=quality_reason,
            basis_bps=f"{snapshot.basis_bps:.4f}",
            expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
            min_expected_edge_bps=str(settings.min_expected_edge_bps),
            recent_return_bps=(
                f"{trend_state.return_bps(settings.adverse_momentum_lookback_seconds):.2f}"
                if trend_state.return_bps(settings.adverse_momentum_lookback_seconds) is not None
                else None
            ),
        )
        return
    quantity = conviction_adjusted_quantity(snapshot, settings, account_risk)
    if quantity < snapshot.min_quantity:
        log_event(
            "skip_min_quantity",
            ticker=snapshot.ticker,
            quantity=str(quantity),
            min_quantity=str(snapshot.min_quantity),
        )
        return
    risk_reason = risk_rejection_reason(
        snapshot=snapshot,
        settings=settings,
        account_risk=account_risk,
        projected_position_size=quantity,
    )
    if risk_reason is not None:
        log_event(
            "skip_entry_risk",
            ticker=snapshot.ticker,
            reason=risk_reason,
            quantity=str(quantity),
            total_balance_usd=(
                str(account_risk.total_balance_usd) if account_risk is not None else None
            ),
            available_balance_usd=(
                str(account_risk.available_balance_usd) if account_risk is not None else None
            ),
            margin_usage_bps=(
                f"{account_risk.margin_usage_bps:.2f}" if account_risk is not None else None
            ),
        )
        return
    response = place_order(
        client=client,
        settings=settings,
        domain=domain,
        signer_address=signer_address,
        signer_private_key=signer_private_key,
        onchain_id=snapshot.onchain_id,
        side=side,
        quantity=quantity,
        price=order_price_for_side(snapshot, side, post_only=settings.entry_post_only),
        reduce_only=False,
        tag="entry",
        post_only=settings.entry_post_only,
    )
    log_event(
        "entry_order_submitted",
        ticker=snapshot.ticker,
        side=side_label(side),
        quantity=str(quantity),
        price=str(order_price_for_side(snapshot, side, post_only=settings.entry_post_only)),
        basis_bps=f"{snapshot.basis_bps:.4f}",
        entry_basis_bps=str(entry_threshold_bps(snapshot, settings)),
        expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
        full_size_expected_edge_bps=str(settings.full_size_expected_edge_bps),
        funding_rate_1h=str(snapshot.funding_rate_1h),
        response=response,
    )


def run_once(
    client: EtherealClient,
    settings: EtherealSettings,
    domain: dict[str, Any],
    signer_address: str,
    signer_private_key: str,
    trend_state: TrendState = TREND_STATE,
) -> None:
    snapshot = fetch_snapshot(client, settings.ticker)
    trend_state.record(snapshot)
    account_risk: AccountRisk | None = None
    try:
        account_risk = fetch_account_risk(client, settings.subaccount_id)
    except Exception as exc:  # noqa: BLE001
        log_event("account_risk_error", error=type(exc).__name__, detail=str(exc))
    entry_pause_reason = update_session_risk(settings, account_risk)
    log_event(
        "market_snapshot",
        ticker=snapshot.ticker,
        best_bid=str(snapshot.best_bid),
        best_ask=str(snapshot.best_ask),
        oracle=str(snapshot.oracle_price),
        basis_bps=f"{snapshot.basis_bps:.4f}",
        spread_bps=f"{snapshot.spread_bps:.4f}",
        expected_edge_bps=f"{expected_edge_bps(snapshot, settings):.2f}",
        recent_return_bps=(
            f"{trend_state.return_bps(settings.adverse_momentum_lookback_seconds):.2f}"
            if trend_state.return_bps(settings.adverse_momentum_lookback_seconds) is not None
            else None
        ),
        funding_rate_1h=str(snapshot.funding_rate_1h),
        total_balance_usd=str(account_risk.total_balance_usd) if account_risk else None,
        available_balance_usd=str(account_risk.available_balance_usd) if account_risk else None,
        margin_usage_bps=f"{account_risk.margin_usage_bps:.2f}" if account_risk else None,
        session_start_equity_usd=(
            str(SESSION_RISK_STATE.start_equity_usd)
            if SESSION_RISK_STATE.start_equity_usd is not None
            else None
        ),
        entry_pause_reason=entry_pause_reason,
    )
    working_orders = working_orders_for_product(client, settings.subaccount_id, snapshot.product_id)
    position = active_position_for_product(client, settings.subaccount_id, snapshot.product_id)
    if position is not None:
        maybe_manage_position(
            client=client,
            settings=settings,
            domain=domain,
            signer_address=signer_address,
            signer_private_key=signer_private_key,
            snapshot=snapshot,
            position=position,
            working_orders=working_orders,
            account_risk=account_risk,
            trend_state=trend_state,
            entry_pause_reason=entry_pause_reason,
        )
        return
    maybe_enter_position(
        client=client,
        settings=settings,
        domain=domain,
        signer_address=signer_address,
        signer_private_key=signer_private_key,
        snapshot=snapshot,
        working_orders=working_orders,
        account_risk=account_risk,
        trend_state=trend_state,
        entry_pause_reason=entry_pause_reason,
    )


def main() -> None:
    settings = load_ethereal_settings()
    if not settings.account_address or not settings.subaccount_id:
        raise SystemExit("ETHEREAL_ACCOUNT_ADDRESS and ETHEREAL_SUBACCOUNT_ID are required")
    if not settings.has_signer_key:
        raise SystemExit("ETHEREAL_SIGNER_PRIVATE_KEY is required")
    if not settings.live_trading and not settings.dry_run:
        raise SystemExit("Either ETHEREAL_LIVE_TRADING or ETHEREAL_DRY_RUN must be enabled")

    signer_private_key = settings.signer_private_key.get_secret_value()  # type: ignore[union-attr]
    signer_address = address_from_private_key(signer_private_key)
    client = EtherealClient(settings.api_base)
    try:
        domain = client.rpc_config()["domain"]
        if settings.live_trading:
            log_event(
                "live_loop_started",
                ticker=settings.ticker,
                target_notional_usd=str(settings.target_notional_usd),
                target_btc_size=str(settings.target_btc_size) if settings.target_btc_size else None,
                max_position_btc_size=str(settings.max_position_btc_size),
                max_account_leverage=str(settings.max_account_leverage),
                min_entry_liquidation_distance_bps=str(
                    settings.min_entry_liquidation_distance_bps
                ),
                derisk_liquidation_distance_bps=str(settings.derisk_liquidation_distance_bps),
                volume_mode=settings.volume_mode_enabled,
                min_expected_edge_bps=str(settings.min_expected_edge_bps),
                full_size_expected_edge_bps=str(settings.full_size_expected_edge_bps),
                max_adverse_momentum_bps=str(settings.max_adverse_momentum_bps),
                scale_in_enabled=settings.scale_in_enabled,
                min_scale_in_pnl_bps=str(settings.min_scale_in_pnl_bps),
                min_hold_minutes=settings.min_hold_minutes,
                fast_take_profit_bps=str(settings.fast_take_profit_bps),
                fast_take_profit_min_minutes=settings.fast_take_profit_min_minutes,
                trailing_stop_activation_bps=str(settings.trailing_stop_activation_bps),
                trailing_stop_distance_bps=str(settings.trailing_stop_distance_bps),
                trailing_stop_floor_bps=str(settings.trailing_stop_floor_bps),
                max_hold_minutes=settings.max_hold_minutes,
                maker_recycle_min_minutes=settings.maker_recycle_min_minutes,
                maker_recycle_profit_bps=str(settings.maker_recycle_profit_bps),
                max_session_drawdown_bps=str(settings.max_session_drawdown_bps),
                entry_post_only=settings.entry_post_only,
                poll_interval_seconds=settings.poll_interval_seconds,
            )
            while True:
                try:
                    run_once(client, settings, domain, signer_address, signer_private_key)
                except Exception as exc:  # noqa: BLE001
                    log_event("loop_error", error=type(exc).__name__, detail=str(exc))
                time.sleep(settings.poll_interval_seconds)
            return
        run_once(client, settings, domain, signer_address, signer_private_key)
    finally:
        client.close()


if __name__ == "__main__":
    main()
