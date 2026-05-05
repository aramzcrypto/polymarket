# Perps Trading Strategies

Last updated: 2026-05-05

This note documents the current Ethereal and Nado perp bots as implemented in:

- `scripts/ethereal_points_bot.py`
- `scripts/nado_points_bot.py`
- `app/ethereal/config.py`
- `app/nado/config.py`

It is strategy documentation, not a profitability guarantee. Both bots can lose money, especially when basis/funding signals are overwhelmed by directional price movement.

## Shared Concepts

Both bots trade BTC perpetuals and poll the venue every `*_POLL_INTERVAL_SECONDS`.

Core market fields:

- `mid`: midpoint of best bid and best ask.
- `oracle_price` / index price: reference price used by the venue.
- `basis_bps`: `(mid - oracle_price) / oracle_price * 10000`.
- `funding_rate_1h`: projected or current hourly funding signal.
- `spread_bps`: bid/ask spread in basis points.

The main shared idea is basis/funding carry:

- Long setup: perp trades below oracle by at least the entry threshold, and funding is negative enough.
- Short setup: perp trades above oracle by at least the entry threshold, and funding is positive enough.

In plain English:

- If perp is cheap versus oracle and longs are being paid or funding is favorable, buy.
- If perp is expensive versus oracle and shorts are being paid or funding is favorable, sell.

Shared entry filters:

- Skip if spread is wider than `*_MAX_SPREAD_BPS`.
- Skip if absolute funding is above `*_MAX_PROJECTED_FUNDING_RATE_1H`.
- Skip if expected edge is below `*_MIN_EXPECTED_EDGE_BPS`.
- Skip if recent momentum is strongly adverse.
- Skip if account risk is unavailable when `*_REQUIRE_ACCOUNT_RISK=true`.
- Skip if available balance is below `*_MIN_AVAILABLE_BALANCE_USD`.
- Skip if margin usage is above `*_MAX_MARGIN_USAGE_BPS`.
- Skip if estimated liquidation distance is below `*_MIN_ENTRY_LIQUIDATION_DISTANCE_BPS`.

Sizing:

- If `*_TARGET_BTC_SIZE` is set, that is the desired BTC size.
- Otherwise size comes from `*_TARGET_NOTIONAL_USD / mid`.
- Size is capped by `*_MAX_POSITION_BTC_SIZE`.
- Size is also capped by account leverage and liquidation-distance risk checks.
- If expected edge is below `*_FULL_SIZE_EXPECTED_EDGE_BPS`, the bot uses `*_STARTER_POSITION_FRACTION` of the risk-adjusted size.

## Ethereal

Venue/ticker:

- Default ticker: `BTCUSD`.
- Uses Ethereal account/subaccount and API signer.
- Live trading requires `ETHEREAL_LIVE_TRADING=true`; dry-run mode uses Ethereal dry-run order submission.

Entry strategy:

- Uses basis/funding carry only.
- Long when:
  - `basis_bps <= -ETHEREAL_ENTRY_BASIS_BPS`, and
  - `funding_rate_1h <= -ETHEREAL_FUNDING_ENTRY_THRESHOLD_1H`.
- Short when:
  - `basis_bps >= ETHEREAL_ENTRY_BASIS_BPS`, and
  - `funding_rate_1h >= ETHEREAL_FUNDING_ENTRY_THRESHOLD_1H`.
- If `ETHEREAL_VOLUME_MODE_ENABLED=true` and spread is tight enough, the entry threshold can tighten to `ETHEREAL_TIGHT_SPREAD_ENTRY_BASIS_BPS`.

Entry quality:

- Expected edge is `abs(basis_bps) + funding_carry_bps`.
- Funding carry assumes `ETHEREAL_EXPECTED_HOLD_MINUTES`.
- A trade is rejected if expected edge is below `ETHEREAL_MIN_EXPECTED_EDGE_BPS`.
- A long is rejected during adverse down momentum.
- A short is rejected during adverse up momentum.

Position management:

- Ethereal can scale into an existing position when:
  - `ETHEREAL_SCALE_IN_ENABLED=true`,
  - no working order exists,
  - current position is smaller than desired size,
  - current PnL is at least `ETHEREAL_MIN_SCALE_IN_PNL_BPS`,
  - the current entry signal matches the existing position side,
  - quality and risk checks still pass.
- Scaling is disabled by default. The points bot should recycle maker fills rather than
  average down into a losing directional position.
- Scaling is paused if session drawdown reaches `ETHEREAL_MAX_SESSION_DRAWDOWN_BPS`.
- Position size is calculated from absolute exposure, so short positions are not accidentally treated as negative capacity.

Exit strategy:

- Stop loss: exit if PnL <= `-ETHEREAL_STOP_LOSS_BPS`.
- Risk deleverage: exit if liquidation distance falls below `ETHEREAL_DERISK_LIQUIDATION_DISTANCE_BPS`.
- Margin deleverage: exit if margin usage exceeds `ETHEREAL_MAX_MARGIN_USAGE_BPS`.
- Fast take profit: in volume mode, exit after `ETHEREAL_FAST_TAKE_PROFIT_MIN_MINUTES` if `ETHEREAL_FAST_TAKE_PROFIT_BPS > 0` and PnL >= `ETHEREAL_FAST_TAKE_PROFIT_BPS`.
- Maker recycle profit: in volume mode, exit after `ETHEREAL_MAKER_RECYCLE_MIN_MINUTES` if PnL >= `ETHEREAL_MAKER_RECYCLE_PROFIT_BPS`.
- Trailing profit stop: after peak PnL reaches `ETHEREAL_TRAILING_STOP_ACTIVATION_BPS`, exit if PnL gives back `ETHEREAL_TRAILING_STOP_DISTANCE_BPS`, with a floor of `ETHEREAL_TRAILING_STOP_FLOOR_BPS`.
- Profitable carry flip: in volume mode, exit a profitable trade if funding flips against the position and basis reverts.
- Max hold: exit after `ETHEREAL_MAX_HOLD_MINUTES`.
- Take profit: after `ETHEREAL_MIN_HOLD_MINUTES`, exit if PnL >= `ETHEREAL_TAKE_PROFIT_BPS`.
- Carry flip: after `ETHEREAL_MIN_HOLD_MINUTES`, exit if funding flips and basis reverts.

Order behavior:

- Entries are post-only limit orders at bid for buys and ask for sells.
- Normal exits are post-only GTD reduce-only orders.
- Emergency exits for stop/risk/margin use IOC reduce-only orders crossing the book.
- Emergency exits are allowed even when a working maker order exists, so stop/risk exits are not blocked by stale open orders.

Current Ethereal points-farming settings:

- `ETHEREAL_TARGET_BTC_SIZE=0.02`
- `ETHEREAL_MAX_POSITION_BTC_SIZE=0.02`
- `ETHEREAL_MAX_ACCOUNT_LEVERAGE=2.0`
- `ETHEREAL_MAX_SPREAD_BPS=3`
- `ETHEREAL_MAX_PROJECTED_FUNDING_RATE_1H=0.0002`
- `ETHEREAL_ENTRY_BASIS_BPS=1.0`
- `ETHEREAL_VOLUME_MODE_ENABLED=true`
- `ETHEREAL_TIGHT_SPREAD_ENTRY_BASIS_BPS=0.6`
- `ETHEREAL_TIGHT_SPREAD_MAX_BPS=1.25`
- `ETHEREAL_TAKE_PROFIT_BPS=30`
- `ETHEREAL_FAST_TAKE_PROFIT_BPS=0`
- `ETHEREAL_FAST_TAKE_PROFIT_MIN_MINUTES=2`
- `ETHEREAL_SCALE_IN_ENABLED=false`
- `ETHEREAL_MAKER_RECYCLE_MIN_MINUTES=2`
- `ETHEREAL_MAKER_RECYCLE_PROFIT_BPS=4`
- `ETHEREAL_TRAILING_STOP_ACTIVATION_BPS=18`
- `ETHEREAL_TRAILING_STOP_DISTANCE_BPS=10`
- `ETHEREAL_TRAILING_STOP_FLOOR_BPS=6`
- `ETHEREAL_STOP_LOSS_BPS=22`
- `ETHEREAL_MIN_HOLD_MINUTES=0`
- `ETHEREAL_MAX_HOLD_MINUTES=45`
- `ETHEREAL_MAX_SESSION_DRAWDOWN_BPS=200`
- `ETHEREAL_ENTRY_POST_ONLY=true`
- `ETHEREAL_ORDER_EXPIRY_SECONDS=15`
- `ETHEREAL_POLL_INTERVAL_SECONDS=5`

Ethereal points-farming intent:

- The bot prioritizes maker volume, low fees, and account safety over directional PnL.
- It should enter with post-only maker orders, avoid averaging down, and recycle positions after small positive PnL.
- Profit target is intentionally modest: near breakeven or slightly positive after spread/funding/fees.
- Safety priority is avoiding liquidation: staged size, moderate leverage cap, tight stop loss, session drawdown pause, and IOC emergency reduce-only exits.
- This mode can still lose money during sharp moves, thin liquidity, failed API submissions, or venue outages.

## Nado

Venue/ticker:

- Default ticker: `BTC-PERP_USDT0`.
- Uses Nado account/subaccount and 1CT private key.
- Live trading requires `NADO_LIVE_TRADING=true` and `NADO_DRY_RUN=false`.

Entry strategy 1: basis/funding carry

- Long when:
  - `basis_bps <= -NADO_ENTRY_BASIS_BPS`, and
  - `funding_rate_1h <= -NADO_FUNDING_ENTRY_THRESHOLD_1H`.
- Short when:
  - `basis_bps >= NADO_ENTRY_BASIS_BPS`, and
  - `funding_rate_1h >= NADO_FUNDING_ENTRY_THRESHOLD_1H`.
- Signal name in logs: `basis_carry`.
- If `NADO_VOLUME_MODE_ENABLED=true` and spread is tight enough, the entry threshold can tighten to `NADO_TIGHT_SPREAD_ENTRY_BASIS_BPS`.

Entry strategy 2: optional trend long

- Enabled only when `NADO_TREND_MODE_ENABLED=true`.
- Long only; it does not open trend shorts.
- Conditions:
  - recent return over `NADO_TREND_LOOKBACK_SECONDS` is at least `NADO_TREND_ENTRY_RETURN_BPS`,
  - basis premium is no higher than `NADO_TREND_MAX_PREMIUM_BPS`,
  - funding is no higher than `NADO_TREND_MAX_FUNDING_RATE_1H`.
- Signal name in logs: `trend_long`.

Entry quality:

- Basis/funding carry trades are rejected when expected edge is below `NADO_MIN_EXPECTED_EDGE_BPS`.
- Basis/funding carry trades are rejected when recent momentum is adverse.
- Trend-long trades currently bypass the extra `entry_quality_rejection_reason` check after their trend conditions pass.

Position management:

- Nado can scale into an existing position when:
  - no working order exists,
  - current size is smaller than the target size,
  - the current signal is the same side as the open position,
  - quality and risk checks pass.
- It will not scale if the current signal side disagrees with the current position side.

Exit strategy:

- Stop loss: exit if estimated position PnL <= `-NADO_STOP_LOSS_BPS`.
- Take profit: exit if estimated position PnL >= `NADO_TAKE_PROFIT_BPS`.
- Nado currently uses a simpler exit stack than Ethereal. Although config fields exist for fast TP, trailing stop, min hold, max hold, and derisking, the current Nado bot exit logic only uses stop loss and take profit.

Order behavior:

- Entries are post-only limit orders at bid for buys and ask for sells.
- Exits are IOC reduce-only orders crossing the book.
- If Nado rejects a post-only order because it crosses the book, the bot logs the failed response and tries again on the next loop if the signal still exists.

Useful current/example settings to document:

- `NADO_TARGET_BTC_SIZE=0.03`
- `NADO_MAX_POSITION_BTC_SIZE=0.1`
- `NADO_MAX_ACCOUNT_LEVERAGE=5.5`
- `NADO_STARTER_POSITION_FRACTION=1.0`
- `NADO_ENTRY_BASIS_BPS=1.0`
- `NADO_FUNDING_ENTRY_THRESHOLD_1H=0.00001`
- `NADO_TREND_MODE_ENABLED=false` in the example file; the VPS may override this.
- `NADO_TREND_LOOKBACK_SECONDS=900`
- `NADO_TREND_ENTRY_RETURN_BPS=12`
- `NADO_TREND_MAX_PREMIUM_BPS=8`
- `NADO_TREND_MAX_FUNDING_RATE_1H=0.0002`
- `NADO_TAKE_PROFIT_BPS=25`
- `NADO_STOP_LOSS_BPS=80`

## Key Differences

Ethereal:

- Basis/funding carry only.
- More mature exit logic.
- Has min-hold, max-hold, fast take-profit, trailing stop, carry-flip exits, risk deleverage, and margin deleverage.
- Normal exits are post-only unless emergency.

Nado:

- Basis/funding carry plus optional trend-long mode.
- Simpler exits: take profit or stop loss.
- Can scale into same-side positions.
- Exits are IOC reduce-only.

## Open Improvement Ideas

- Add Ethereal-style trailing stop, max-hold, carry-flip, and risk-deleverage exits to Nado.
- Require stronger confirmation before Nado opens or scales to full size.
- Add a dashboard field showing the exact latest signal: `basis_carry`, `trend_long`, or `none`.
- Add a per-venue strategy state endpoint with current basis, funding, expected edge, recent return, and rejection reason.
- Backtest threshold changes before increasing leverage or size.
