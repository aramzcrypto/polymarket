# Polymarket Live-Trading Bot

Safety-first Python 3.11+ live trading bot for the non-US Polymarket CLOB using the official `py-clob-client`, REST APIs, WebSocket market/user channels, FastAPI admin controls, Postgres persistence, and Docker Compose.

This project is designed for controlled real-money deployment, but it does not guarantee profitability. Trading is disabled by default and remains disabled unless both `TRADING__LIVE_TRADING=true` and `TRADING__LIVE_TRADING_ACKNOWLEDGED=true` are set.

## Safety Defaults

- Default mode is dry-run.
- Live mode requires explicit double acknowledgement.
- Tiny live mode should be used first and caps order size aggressively.
- Geoblock/compliance checks fail closed.
- If the deployment IP is blocked or close-only, the bot will not open orders.
- Kill switch cancels open orders when possible and blocks new trading.
- State-changing admin endpoints require a bearer token.
- Market orders are not treated as a separate native primitive; aggressive execution must use marketable limit/FOK/FAK semantics with slippage limits.

## Quick Start

```bash
cp .env.example .env
make install
make test
docker compose up --build postgres migrate bot
```

Admin API:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl -H "Authorization: Bearer $ADMIN__TOKEN" -X POST http://localhost:8000/admin/kill-switch \
  -H "content-type: application/json" -d '{"reason":"manual test"}'
```

## Credential Flow

Configure the Polymarket proxy/funder wallet flow:

- `POLYMARKET__PRIVATE_KEY`: signing key, never commit it.
- `POLYMARKET__FUNDER_ADDRESS`: proxy wallet/funder shown by Polymarket.
- `POLYMARKET__SIGNATURE_TYPE`: default `1` for proxy/Magic; set `2` for Gnosis Safe when appropriate.
- `POLYMARKET__API_KEY`, `POLYMARKET__SECRET`, `POLYMARKET__PASSPHRASE`: L2 credentials.
- Alternatively set `POLYMARKET__DERIVE_API_CREDS=true` to call `create_or_derive_api_creds()` at startup.

The CLOB client handles L1/L2 signing and order payload signing. Authenticated trading methods require L2 credentials, and order creation still requires signing with the private key.

## Strategy Configuration

Copy `configs/strategy.example.yaml` into the active config and set:

- `markets`: condition IDs for the user WebSocket.
- `token_ids`: CLOB token/asset IDs for market WebSocket.
- `min_spread`, `quote_size`, `max_quote_size`, `max_inventory_per_side`.

The included `conservative_mm` strategy estimates fair value from midpoint plus small top-book imbalance, quotes both sides around fair value, reduces size when inventory is one-sided, and stops quoting the side that worsens inventory beyond limits.

## BTC 5-Minute Late Convexity Strategy

The `btc_5m_late_convexity` strategy is based on buying a tiny amount of the losing side near expiry when the payout is large but the required BTC move is still statistically plausible.

It does not buy every cheap longshot. It requires:

- Fresh BTC spot data from Coinbase, with optional Binance agreement check.
- A discovered active "Bitcoin Up or Down - 5 Minutes" market.
- A parsed price-to-beat from market metadata.
- Time remaining inside `min_seconds_to_expiry` and `max_seconds_to_expiry`.
- Offered ask price inside the configured longshot band.
- Reward multiple above `min_reward_multiple`.
- Estimated probability minus offered price above `min_edge`.
- Risk checks still pass before any order is submitted.

The dashboard is available at:

```bash
open http://localhost:8000/
```

For a $50 tiny-live test, start with:

```yaml
strategies:
  btc_5m_late_convexity:
    enabled: true
    bankroll: "50"
    max_spend_per_signal: "1"
    max_spend_per_market: "3"
    max_quote_size: "25"
```

## Live Deployment Checklist

1. Confirm the deployment region is eligible using `/ready` and Polymarket geoblock response.
2. Fund the correct proxy/funder wallet and verify token allowances/balances.
3. Set tiny live mode: `TRADING__TINY_LIVE_MODE=true`.
4. Set very small limits in `configs/production.yaml`.
5. Start with one market and one token pair.
6. Watch `/connectivity`, `/orders/open`, `/balances`, `/risk`, `/fills`, and alerts.
7. Enable live only with both:
   - `TRADING__LIVE_TRADING=true`
   - `TRADING__LIVE_TRADING_ACKNOWLEDGED=true`
8. Run for a short observation window, then widen limits only after reviewing fills, rejects, and PnL.

## Rollback Procedure

1. Trigger kill switch:
   ```bash
   curl -H "Authorization: Bearer $ADMIN__TOKEN" \
     -X POST http://localhost:8000/admin/kill-switch \
     -H "content-type: application/json" \
     -d '{"reason":"rollback"}'
   ```
2. Verify `/orders/open` is empty or manually cancel any residual orders from Polymarket.
3. Set `TRADING__LIVE_TRADING=false`.
4. Restart the bot.
5. Review `raw_events`, `order_decisions`, `fills`, `risk_events`, and `admin_audit_log`.

## Useful Commands

```bash
make test
make lint
make typecheck
make migrate
make run
make docker-up
make docker-down
```

## Compliance Note

Polymarket restricts trading in certain countries and regions. The bot checks `https://polymarket.com/api/geoblock` before live trading and refuses to trade when blocked. Do not use this software to bypass geographic, legal, or platform restrictions.
