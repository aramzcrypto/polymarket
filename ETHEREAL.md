## Ethereal Bot Notes

This repository now includes additive Ethereal utilities under `app/ethereal/` and standalone scripts under `scripts/`.

Useful entry points:

- `scripts/ethereal_generate_signer.py`
- `scripts/ethereal_account_overview.py`
- `scripts/ethereal_link_signer.py`
- `scripts/ethereal_points_bot.py`

The linked signer flow uses the live Ethereal signing domain from `/v1/rpc/config`.

Setup order:

1. Generate a dedicated signer: `python scripts/ethereal_generate_signer.py`
2. Add `ETHEREAL_OWNER_PRIVATE_KEY` to `.env.ethereal.local`
3. Link the signer: `python scripts/ethereal_link_signer.py`
4. Inspect account state: `python scripts/ethereal_account_overview.py`
5. Test order signing with a dry run: `python scripts/ethereal_points_bot.py`
6. Flip to live trading by setting `ETHEREAL_LIVE_TRADING=true` and `ETHEREAL_DRY_RUN=false`

Safety notes:

- Use a dedicated linked signer, not your seed phrase.
- The live loop is maker-first and points-oriented: BTCUSD size can be set by `ETHEREAL_TARGET_BTC_SIZE`, and exits happen on take-profit, stop-loss, carry flip, risk de-leveraging, or max hold.
- `ETHEREAL_VOLUME_MODE_ENABLED=true` increases turnover by accepting tighter-spread basis signals and taking profit earlier once a position is already green. Keep `ETHEREAL_MIN_EXPECTED_EDGE_BPS` above the tight-spread threshold if profit quality matters more than raw volume.
- Entries now require enough basis-plus-carry edge, avoid buying into sharp short-term downside or shorting into sharp upside, and use `ETHEREAL_STARTER_POSITION_FRACTION` until `ETHEREAL_FULL_SIZE_EXPECTED_EDGE_BPS` is reached.
- Winner protection is controlled by `ETHEREAL_TRAILING_STOP_ACTIVATION_BPS`, `ETHEREAL_TRAILING_STOP_DISTANCE_BPS`, and `ETHEREAL_TRAILING_STOP_FLOOR_BPS`; the bot tracks the peak open PnL in memory and can exit before a good trade round-trips.
- Higher-volume live settings are clamped by account risk controls: `ETHEREAL_MAX_POSITION_BTC_SIZE`, `ETHEREAL_MAX_ACCOUNT_LEVERAGE`, `ETHEREAL_MIN_ENTRY_LIQUIDATION_DISTANCE_BPS`, `ETHEREAL_DERISK_LIQUIDATION_DISTANCE_BPS`, and `ETHEREAL_MAX_MARGIN_USAGE_BPS`.
- Entries and normal profit exits stay post-only maker orders. Stop-loss and liquidation/margin de-risk exits use reduce-only IOC so the bot can prioritize getting smaller over earning maker volume.
- Existing signers can be queried via `scripts/ethereal_account_overview.py`.
