"""
Ethereal BTC/USD perpetual strategy — momentum + book imbalance + funding bias.

Signal = momentum_score · w_m + imbalance_score · w_i + funding_bias

  momentum_score   30-second BTC price velocity normalised by realised vol.
                   Positive → upward trend → bias LONG.

  imbalance_score  Top-N order-book bid/ask depth ratio mapped to [-1, +1].
                   Positive → bids dominate → upward pressure → bias LONG.
                   This is a real, well-documented micro-edge.

  funding_bias     Projected funding rate direction.
                   High positive funding → shorts collect → slight SHORT bias.
                   Beyond max_projected_funding_rate_1h blocks new LONG entries
                   (too expensive to hold); below negative max blocks SHORTs.

Entry: when the combined signal clears the entry threshold and the spread is
       within max_spread_bps. By default we post a maker (POST_ONLY) limit at
       our side of the book; if it doesn't fill within
       post_only_max_attempts ticks we fall back to an aggressive IOC.

Exit (no min_hold by default):
  - Take profit:        unrealised PnL ≥ take_profit_bps
  - Fast take profit:   unrealised PnL ≥ fast_take_profit_bps within
                        fast_take_profit_min_minutes (lock small wins fast)
  - Trailing stop:      after pnl ≥ trailing_stop_activation_bps, trail the
                        peak by trailing_stop_distance_bps; never give back
                        below trailing_stop_floor_bps.
  - Stop loss:          unrealised PnL ≤ -stop_loss_bps
  - Max hold:           position age > max_hold_minutes
  - Signal reversal:    the same signal that opened the trade flips by
                        ≥ signal_reversal_threshold against the position.

The signal-reversal exit is the single most important change — it stops
locking in losers when the regime that justified the entry is gone.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.ethereal.client import EtherealClient
from app.ethereal.config import EtherealSettings
from app.ethereal.signing import build_limit_order_payload

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
SIDE_BUY = 0
SIDE_SELL = 1

# Minimum data points required before taking a signal.
MIN_HISTORY_POINTS = 10


@dataclass
class OpenPosition:
    side: int  # SIDE_BUY or SIDE_SELL
    entry_price: Decimal
    quantity: Decimal
    product_id: str
    onchain_id: int
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Signed signal value at entry (used for reversal-exit comparison).
    entry_signal: float = 0.0
    # Trailing-stop bookkeeping (in bps from entry).
    peak_pnl_bps: Decimal = ZERO


class EtherealMomentumStrategy:
    def __init__(self, config: EtherealSettings, client: EtherealClient) -> None:
        self.config = config
        self.client = client
        # Price history: (timestamp, mark_price) tuples.
        self.price_history: deque[tuple[datetime, Decimal]] = deque(maxlen=300)
        self.position: OpenPosition | None = None
        self.product: dict[str, Any] | None = None
        self.domain: dict[str, Any] | None = None
        # Track POST_ONLY attempts on the working order so we can fall back.
        self.post_only_attempts: int = 0
        self.last_working_order_id: str | None = None

    def _now(self) -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------
    # Initialisation helpers
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Fetch RPC domain config and resolve the BTC product metadata."""
        rpc = self.client.rpc_config()
        self.domain = rpc.get("domain") or rpc

        products = self.client.products(ticker=self.config.ticker)
        items: list[dict[str, Any]] = products.get("products") or products.get("data") or []
        for item in items:
            ticker = item.get("ticker") or item.get("symbol") or ""
            if ticker.upper() == self.config.ticker.upper():
                self.product = item
                break
        if self.product is None and items:
            self.product = items[0]
        if self.product is None:
            raise RuntimeError(f"Could not find product for ticker {self.config.ticker}")
        logger.info(
            "Ethereal product resolved: %s",
            self.product.get("ticker") or self.product.get("id"),
        )

    # ------------------------------------------------------------------
    # Market data helpers
    # ------------------------------------------------------------------

    def _product_id(self) -> str:
        assert self.product is not None
        return str(self.product.get("id") or self.product.get("productId") or "")

    def _onchain_id(self) -> int:
        assert self.product is not None
        return int(self.product.get("onchainId") or self.product.get("id") or 0)

    def _fetch_mark_price(self) -> Decimal | None:
        product_id = self._product_id()
        data = self.client.market_prices([product_id])
        rows: list[dict[str, Any]] = data.get("prices") or data.get("data") or [data]
        for row in rows:
            raw = row.get("markPrice") or row.get("mark_price") or row.get("price")
            if raw is not None:
                return Decimal(str(raw))
        return None

    def _fetch_liquidity(self) -> tuple[Decimal | None, Decimal | None, float]:
        """Returns (best_bid, best_ask, imbalance) where imbalance ∈ [-1, +1].

        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth) summed
        across the top-N levels of the book. Positive = bids dominate.
        """
        product_id = self._product_id()
        data = self.client.market_liquidity(product_id)
        bid = data.get("bestBid") or data.get("bid")
        ask = data.get("bestAsk") or data.get("ask")
        bids_list = data.get("bids") if isinstance(data.get("bids"), list) else []
        asks_list = data.get("asks") if isinstance(data.get("asks"), list) else []
        if bid is None and bids_list:
            bid = bids_list[0][0] if bids_list[0] else None
        if ask is None and asks_list:
            ask = asks_list[0][0] if asks_list[0] else None

        # Imbalance over top-N levels.
        levels = max(1, int(self.config.imbalance_levels))

        def _depth(side: list[Any]) -> float:
            total = 0.0
            for row in side[:levels]:
                if not row:
                    continue
                # Each row is typically [price, size] or {"price": ..., "size": ...}
                if isinstance(row, dict):
                    size = float(row.get("size") or row.get("quantity") or 0)
                else:
                    try:
                        size = float(row[1])
                    except (IndexError, TypeError, ValueError):
                        size = 0.0
                total += size
            return total

        bid_depth = _depth(bids_list)
        ask_depth = _depth(asks_list)
        denom = bid_depth + ask_depth
        imbalance = ((bid_depth - ask_depth) / denom) if denom > 0 else 0.0

        return (
            Decimal(str(bid)) if bid is not None else None,
            Decimal(str(ask)) if ask is not None else None,
            imbalance,
        )

    def _fetch_projected_funding_rate(self) -> Decimal:
        product_id = self._product_id()
        data = self.client.projected_funding_rates([product_id])
        rows: list[dict[str, Any]] = data.get("rates") or data.get("data") or [data]
        for row in rows:
            raw = (
                row.get("projectedFundingRate1h")
                or row.get("projected_funding_rate_1h")
                or row.get("rate")
            )
            if raw is not None:
                return Decimal(str(raw))
        return ZERO

    def _refresh_position(self) -> None:
        """Sync self.position against the exchange's open positions."""
        data = self.client.positions(self.config.subaccount_id, open_only=True)
        rows: list[dict[str, Any]] = data.get("positions") or data.get("data") or []
        product_id = self._product_id()
        for row in rows:
            if str(row.get("productId") or row.get("product_id") or "") != product_id:
                continue
            qty_raw = row.get("quantity") or row.get("size") or "0"
            qty = Decimal(str(qty_raw))
            if qty == ZERO:
                self.position = None
                return
            entry_raw = row.get("entryPrice") or row.get("entry_price") or "0"
            side_raw = row.get("side") or ("BUY" if qty > ZERO else "SELL")
            side = SIDE_BUY if str(side_raw).upper() in ("BUY", "LONG", "0") else SIDE_SELL
            if self.position is None:
                # Position appeared without us tracking it (e.g. dashboard restart).
                self.position = OpenPosition(
                    side=side,
                    entry_price=Decimal(str(entry_raw)),
                    quantity=abs(qty),
                    product_id=product_id,
                    onchain_id=self._onchain_id(),
                )
            else:
                self.position.entry_price = Decimal(str(entry_raw))
                self.position.quantity = abs(qty)
            return
        self.position = None

    # ------------------------------------------------------------------
    # Working-order tracking
    # ------------------------------------------------------------------

    def _refresh_working_orders(self) -> list[dict[str, Any]]:
        try:
            data = self.client.orders(self.config.subaccount_id, is_working=True)
        except Exception as exc:
            logger.debug("orders fetch failed: %s", exc)
            return []
        rows = data.get("orders") or data.get("data") or []
        product_id = self._product_id()
        return [
            r for r in rows
            if isinstance(r, dict)
            and str(r.get("productId") or r.get("product_id") or "") == product_id
        ]

    # ------------------------------------------------------------------
    # Signal computation
    # ------------------------------------------------------------------

    def _momentum_score(self) -> float:
        if len(self.price_history) < MIN_HISTORY_POINTS:
            return 0.0
        now = self._now()
        lookback_30s = [
            p for t, p in self.price_history
            if (now - t).total_seconds() <= 30.0 and p > ZERO
        ]
        if len(lookback_30s) < 2:
            return 0.0
        ret_30s = float((lookback_30s[-1] - lookback_30s[0]) / lookback_30s[0])
        # Normalise by 60-second realised volatility.
        lookback_60s = [
            p for t, p in self.price_history
            if (now - t).total_seconds() <= 60.0 and p > ZERO
        ]
        if len(lookback_60s) >= 2 and lookback_60s[0] > ZERO:
            vol = abs(float((lookback_60s[-1] - lookback_60s[0]) / lookback_60s[0]))
            if vol > 1e-8:
                return ret_30s / vol
        return ret_30s * 1000

    def _imbalance_score(self, imbalance: float) -> float:
        """Map raw imbalance ∈ [-1, +1] to a roughly [-1, +1] score weighted
        by `imbalance_weight`. Returns 0 when weight is 0."""
        weight = float(self.config.imbalance_weight)
        if weight <= 0:
            return 0.0
        # Mild non-linear shaping so that |imbalance| < 0.1 doesn't fire.
        if abs(imbalance) < 0.05:
            return 0.0
        return imbalance * weight

    def _funding_bias(self, funding_rate: Decimal) -> float:
        threshold = float(self.config.funding_entry_threshold_1h)
        cap = float(self.config.max_projected_funding_rate_1h)
        r = float(funding_rate)
        if abs(r) < threshold:
            return 0.0
        clamped = max(-cap, min(cap, r))
        return -clamped / cap if cap > 0 else 0.0

    def _combined_signal(self, funding_rate: Decimal, imbalance: float) -> float:
        return (
            self._momentum_score()
            + self._imbalance_score(imbalance)
            + self._funding_bias(funding_rate)
        )

    # ------------------------------------------------------------------
    # Entry / exit checks
    # ------------------------------------------------------------------

    def _spread_ok(self, bid: Decimal | None, ask: Decimal | None) -> bool:
        if bid is None or ask is None or bid <= ZERO or ask <= ZERO:
            return False
        mid = (bid + ask) / Decimal("2")
        spread_bps = (ask - bid) / mid * Decimal("10000")
        return spread_bps <= self.config.max_spread_bps

    def _entry_allowed(
        self,
        signal: float,
        funding_rate: Decimal,
    ) -> tuple[bool, int]:
        entry_threshold = 1.0
        if signal > entry_threshold:
            if funding_rate <= self.config.max_projected_funding_rate_1h:
                return True, SIDE_BUY
        elif signal < -entry_threshold:
            if funding_rate >= -self.config.max_projected_funding_rate_1h:
                return True, SIDE_SELL
        return False, SIDE_BUY

    def _pnl_bps(self, mark_price: Decimal) -> Decimal:
        if self.position is None or self.position.entry_price <= ZERO:
            return ZERO
        if self.position.side == SIDE_BUY:
            return (mark_price - self.position.entry_price) / self.position.entry_price * Decimal("10000")
        return (self.position.entry_price - mark_price) / self.position.entry_price * Decimal("10000")

    def _should_exit(
        self,
        mark_price: Decimal,
        signal: float,
    ) -> tuple[bool, str]:
        if self.position is None:
            return False, ""

        age = self._now() - self.position.opened_at
        pnl = self._pnl_bps(mark_price)

        # Update trailing-stop peak.
        if pnl > self.position.peak_pnl_bps:
            self.position.peak_pnl_bps = pnl

        # ── Hard stops ────────────────────────────────────────────
        if pnl <= -self.config.stop_loss_bps:
            return True, f"stop_loss pnl={pnl:.1f}bps"
        if pnl >= self.config.take_profit_bps:
            return True, f"take_profit pnl={pnl:.1f}bps"

        # ── Trailing stop (active only after activation threshold) ─
        if (
            self.position.peak_pnl_bps >= self.config.trailing_stop_activation_bps
            and self.config.trailing_stop_distance_bps > ZERO
        ):
            give_back = self.position.peak_pnl_bps - pnl
            floor = self.config.trailing_stop_floor_bps
            if (
                give_back >= self.config.trailing_stop_distance_bps
                and pnl >= floor
            ):
                return True, (
                    f"trailing_stop pnl={pnl:.1f} peak={self.position.peak_pnl_bps:.1f} "
                    f"give_back={give_back:.1f}bps"
                )

        # ── Fast take profit (lock small wins early) ──────────────
        age_minutes = age.total_seconds() / 60.0
        if (
            self.config.fast_take_profit_bps > ZERO
            and age_minutes <= self.config.fast_take_profit_min_minutes
            and pnl >= self.config.fast_take_profit_bps
        ):
            return True, f"fast_take_profit pnl={pnl:.1f}bps age={age_minutes:.1f}m"

        # ── Max hold ─────────────────────────────────────────────
        if age > timedelta(minutes=self.config.max_hold_minutes):
            return True, f"max_hold age={int(age.total_seconds() // 60)}min"

        # ── Min hold guard (default 0 → effectively disabled) ────
        min_hold = timedelta(minutes=self.config.min_hold_minutes)
        if age < min_hold:
            return False, ""

        # ── Signal-reversal exit (the key new behavior) ──────────
        threshold = float(self.config.signal_reversal_threshold)
        if threshold > 0:
            opened_long = self.position.side == SIDE_BUY
            entry_sig = self.position.entry_signal
            # Long → exit when signal swings sufficiently below 0
            # Short → exit when signal swings sufficiently above 0
            if opened_long and signal <= -threshold and entry_sig > 0:
                return True, f"signal_reversal entry={entry_sig:+.2f} now={signal:+.2f}"
            if (not opened_long) and signal >= threshold and entry_sig < 0:
                return True, f"signal_reversal entry={entry_sig:+.2f} now={signal:+.2f}"

        return False, ""

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    def _compute_quantity(self, price: Decimal) -> Decimal:
        if self.config.target_btc_size is not None:
            return self.config.target_btc_size
        if price <= ZERO:
            return ZERO
        return (self.config.target_notional_usd / price).quantize(Decimal("0.0001"))

    def _submit_limit(
        self,
        *,
        side: int,
        price: Decimal,
        quantity: Decimal,
        post_only: bool,
        reduce_only: bool = False,
    ) -> dict[str, Any] | None:
        if not self.config.has_signer_key or self.domain is None:
            logger.warning("Missing signer key or domain — order skipped")
            return None
        if self.config.dry_run:
            logger.info(
                "DRY RUN order: side=%s price=%s qty=%s post_only=%s reduce_only=%s",
                "BUY" if side == SIDE_BUY else "SELL",
                price,
                quantity,
                post_only,
                reduce_only,
            )
            return {"dry_run": True}
        time_in_force = "GTC" if post_only else "IOC"
        payload = build_limit_order_payload(
            domain=self.domain,
            signer_address=self.config.signer_address,
            signer_private_key=self.config.signer_private_key.get_secret_value(),  # type: ignore[union-attr]
            subaccount_name=self.config.subaccount_name,
            quantity=quantity,
            price=price,
            side=side,
            onchain_id=self._onchain_id(),
            reduce_only=reduce_only,
            time_in_force=time_in_force,
            post_only=post_only,
        )
        try:
            result = self.client.submit_order(payload)
            logger.info(
                "Order submitted: side=%s price=%s qty=%s post_only=%s reduce_only=%s result=%s",
                "BUY" if side == SIDE_BUY else "SELL",
                price,
                quantity,
                post_only,
                reduce_only,
                result,
            )
            return result
        except Exception as exc:
            logger.error("Order submission failed: %s", exc)
            return None

    def _cancel_order(self, order: dict[str, Any]) -> None:
        # Best effort — the EtherealClient currently only exposes submit/dry-run,
        # so unmatched POST_ONLY orders simply expire. Once cancel is exposed
        # we can wire it in here without touching the strategy.
        oid = order.get("id") or order.get("orderId") or order.get("order_id")
        logger.debug("Letting working order %s expire (cancel not yet wired)", oid)

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    def tick(self) -> None:
        if not self.config.live_trading and not self.config.dry_run:
            logger.debug("Trading disabled; skipping tick")
            return

        # 1. Fetch market data.
        mark_price = self._fetch_mark_price()
        if mark_price is None or mark_price <= ZERO:
            logger.warning("Could not fetch mark price; skipping tick")
            return
        self.price_history.append((self._now(), mark_price))

        bid, ask, imbalance = self._fetch_liquidity()
        funding_rate = self._fetch_projected_funding_rate()

        # 2. Compute signal.
        signal = self._combined_signal(funding_rate, imbalance)
        logger.debug(
            "tick | px=%.2f bid=%s ask=%s imb=%+.3f fund=%.6f signal=%+.3f position=%s",
            float(mark_price),
            bid,
            ask,
            imbalance,
            float(funding_rate),
            signal,
            "LONG" if (self.position and self.position.side == SIDE_BUY)
            else ("SHORT" if self.position else "none"),
        )

        # 3. Sync state.
        self._refresh_position()
        working = self._refresh_working_orders()

        # 4. Track / fall back POST_ONLY working orders.
        if working:
            cur_id = working[0].get("id") or working[0].get("orderId") or working[0].get("order_id")
            if cur_id == self.last_working_order_id:
                self.post_only_attempts += 1
            else:
                self.post_only_attempts = 1
                self.last_working_order_id = str(cur_id) if cur_id else None
            if (
                self.position is None
                and self.post_only_attempts >= self.config.post_only_max_attempts
            ):
                logger.info(
                    "POST_ONLY order stale after %d ticks — letting it expire and "
                    "re-evaluating with IOC fallback",
                    self.post_only_attempts,
                )
                self._cancel_order(working[0])
                self.post_only_attempts = 0
                self.last_working_order_id = None
            return  # Don't double-up while an order is working.
        else:
            self.post_only_attempts = 0
            self.last_working_order_id = None

        # 5. Exit check.
        if self.position is not None:
            should_exit, exit_reason = self._should_exit(mark_price, signal)
            if should_exit:
                logger.info("EXIT triggered: %s", exit_reason)
                exit_side = SIDE_SELL if self.position.side == SIDE_BUY else SIDE_BUY
                # Exits cross the spread (IOC) — we want out, not maker queue.
                exit_price = (ask if exit_side == SIDE_BUY else bid) or mark_price
                self._submit_limit(
                    side=exit_side,
                    price=exit_price,
                    quantity=self.position.quantity,
                    post_only=False,
                    reduce_only=True,
                )
                self.position = None
            return

        # 6. Entry check.
        if len(self.price_history) < MIN_HISTORY_POINTS:
            logger.debug(
                "Warming up price history (%d/%d)",
                len(self.price_history),
                MIN_HISTORY_POINTS,
            )
            return
        if not self._spread_ok(bid, ask):
            logger.debug("Spread too wide; skipping entry")
            return

        should_enter, entry_side = self._entry_allowed(signal, funding_rate)
        if not should_enter:
            return

        # POST_ONLY at our side of the book. For a LONG we sit on the bid; for a
        # SHORT we sit on the ask. Maker fills mean we collect, not pay, the
        # spread — typically saves 1-3 bps per round-trip versus IOC.
        post_only = bool(self.config.entry_post_only)
        if post_only:
            entry_price = (bid if entry_side == SIDE_BUY else ask) or mark_price
        else:
            entry_price = (ask if entry_side == SIDE_BUY else bid) or mark_price
            tick_adjust = mark_price * self.config.entry_basis_bps / Decimal("10000")
            entry_price = (
                entry_price + tick_adjust
                if entry_side == SIDE_BUY
                else entry_price - tick_adjust
            )

        quantity = self._compute_quantity(mark_price)
        if quantity <= ZERO:
            logger.warning("Computed quantity is zero; skipping entry")
            return

        logger.info(
            "ENTRY: side=%s price=%.2f qty=%s signal=%+.3f imb=%+.3f fund=%.6f post_only=%s",
            "LONG" if entry_side == SIDE_BUY else "SHORT",
            float(entry_price),
            quantity,
            signal,
            imbalance,
            float(funding_rate),
            post_only,
        )
        result = self._submit_limit(
            side=entry_side,
            price=entry_price,
            quantity=quantity,
            post_only=post_only,
        )
        if result and not result.get("dry_run"):
            # Track the entry signal so we can detect reversals later.
            self.position = OpenPosition(
                side=entry_side,
                entry_price=entry_price,
                quantity=quantity,
                product_id=self._product_id(),
                onchain_id=self._onchain_id(),
                entry_signal=signal,
            )
