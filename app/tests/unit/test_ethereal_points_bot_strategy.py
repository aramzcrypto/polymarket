from __future__ import annotations

import time
from decimal import Decimal

from scripts.ethereal_points_bot import (
    BUY,
    POSITION_PEAK_PNL_BPS,
    SESSION_RISK_STATE,
    SELL,
    MarketSnapshot,
    PriceSample,
    SessionRiskState,
    TrendState,
    choose_entry_side,
    conviction_adjusted_quantity,
    entry_quality_rejection_reason,
    entry_threshold_bps,
    estimated_liquidation_distance_bps,
    expected_edge_bps,
    maybe_manage_position,
    risk_adjusted_quantity,
    update_session_risk,
)


class SettingsStub:
    entry_basis_bps = Decimal("1.0")
    volume_mode_enabled = False
    tight_spread_entry_basis_bps = Decimal("0.6")
    tight_spread_max_bps = Decimal("1.25")
    funding_entry_threshold_1h = Decimal("0.00001")
    min_expected_edge_bps = Decimal("0")
    expected_hold_minutes = Decimal("60")
    adverse_momentum_lookback_seconds = 90
    max_adverse_momentum_bps = Decimal("12")
    starter_position_fraction = Decimal("0.50")
    full_size_expected_edge_bps = Decimal("8")
    scale_in_enabled = False
    min_scale_in_pnl_bps = Decimal("2")
    target_btc_size = Decimal("0.03")
    target_notional_usd = Decimal("25")
    max_position_btc_size = Decimal("0.06")
    max_account_leverage = Decimal("5.5")
    maintenance_margin_buffer_bps = Decimal("500")
    min_entry_liquidation_distance_bps = Decimal("1250")
    derisk_liquidation_distance_bps = Decimal("900")
    max_margin_usage_bps = Decimal("6500")
    min_available_balance_usd = Decimal("50")
    require_account_risk = True
    fast_take_profit_min_minutes = 20
    fast_take_profit_bps = Decimal("25")
    trailing_stop_activation_bps = Decimal("35")
    trailing_stop_distance_bps = Decimal("18")
    trailing_stop_floor_bps = Decimal("8")
    min_hold_minutes = 65
    max_hold_minutes = 360
    stop_loss_bps = Decimal("50")
    take_profit_bps = Decimal("35")
    maker_recycle_min_minutes = 3
    maker_recycle_profit_bps = Decimal("4")
    max_session_drawdown_bps = Decimal("250")
    entry_post_only = True
    live_trading = False
    order_expiry_seconds = 60
    account_address = "0x0000000000000000000000000000000000000001"
    subaccount_name = "test"


class FakeClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def dry_run_order(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"ok": True}


def snapshot(
    *,
    bid: str = "99999",
    ask: str = "100000",
    oracle: str = "99990",
    funding: str = "0.000011",
) -> MarketSnapshot:
    return MarketSnapshot(
        product_id="btc",
        onchain_id=1,
        ticker="BTCUSD",
        lot_size=Decimal("0.0001"),
        min_quantity=Decimal("0.0001"),
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        oracle_price=Decimal(oracle),
        funding_rate_1h=Decimal(funding),
    )


def test_default_entry_requires_full_basis_threshold() -> None:
    settings = SettingsStub()
    snap = snapshot(bid="99999", ask="100000", oracle="99992")

    assert snap.basis_bps < settings.entry_basis_bps
    assert choose_entry_side(snap, settings) is None


def test_volume_mode_relaxes_basis_only_when_spread_is_tight() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True
    snap = snapshot(bid="99999", ask="100000", oracle="99992")

    assert entry_threshold_bps(snap, settings) == Decimal("0.6")
    assert choose_entry_side(snap, settings) == SELL


def test_volume_mode_keeps_normal_threshold_when_spread_is_wide() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True
    snap = snapshot(bid="99980", ask="100000", oracle="99992")

    assert snap.spread_bps > settings.tight_spread_max_bps
    assert entry_threshold_bps(snap, settings) == Decimal("1.0")
    assert choose_entry_side(snap, settings) is None


def test_volume_mode_respects_funding_direction() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True

    assert choose_entry_side(snapshot(oracle="100008", funding="-0.000011"), settings) == BUY
    assert choose_entry_side(snapshot(oracle="100008", funding="0.000011"), settings) is None


def test_entry_quality_blocks_low_expected_edge() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True
    settings.min_expected_edge_bps = Decimal("3")
    snap = snapshot(bid="99999", ask="100000", oracle="99992")
    side = choose_entry_side(snap, settings)

    assert side == SELL
    assert expected_edge_bps(snap, settings) < settings.min_expected_edge_bps
    assert (
        entry_quality_rejection_reason(snap, settings, side, TrendState())
        == "expected_edge_low"
    )


def test_entry_quality_blocks_adverse_momentum() -> None:
    settings = SettingsStub()
    settings.min_expected_edge_bps = Decimal("1")
    trend = TrendState()
    now = time.time()
    trend.samples.append(PriceSample(ts=now - 80, mid=Decimal("100000")))
    trend.samples.append(PriceSample(ts=now, mid=Decimal("99800")))
    snap = snapshot(bid="99799", ask="99800", oracle="99825", funding="-0.000011")
    side = choose_entry_side(snap, settings)

    assert side == BUY
    assert (
        entry_quality_rejection_reason(snap, settings, side, trend)
        == "adverse_down_momentum"
    )


def test_conviction_quantity_uses_starter_size_until_edge_is_strong() -> None:
    settings = SettingsStub()
    settings.min_expected_edge_bps = Decimal("3")
    settings.full_size_expected_edge_bps = Decimal("8")
    snap = snapshot(bid="99999", ask="100000", oracle="99960")

    assert expected_edge_bps(snap, settings) < settings.full_size_expected_edge_bps
    assert conviction_adjusted_quantity(snap, settings, None) == Decimal("0.015")


def test_short_exit_uses_absolute_position_size() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True
    client = FakeClient()
    position = {
        "side": SELL,
        "size": "-0.09",
        "cost": "6859.62",
        "unrealizedPnl": "55",
        "createdAt": int((time.time() - 60 * 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(),
        position=position,
        working_orders=[],
        account_risk=None,
        trend_state=TrendState(),
    )

    assert client.payloads
    data = client.payloads[0]["data"]
    assert data["reduceOnly"] is True
    assert data["side"] == BUY
    assert data["quantity"] == "0.09"
    assert data["postOnly"] is True
    assert data["timeInForce"] == "GTD"


def test_trailing_stop_exits_winner_that_falls_below_floor() -> None:
    settings = SettingsStub()
    client = FakeClient()
    snap = snapshot()
    created_at = int((time.time() - 70 * 60) * 1000)
    position = {
        "side": BUY,
        "size": "0.03",
        "cost": "3000",
        "unrealizedPnl": "1.5",
        "createdAt": created_at,
    }
    POSITION_PEAK_PNL_BPS.clear()
    POSITION_PEAK_PNL_BPS[f"{snap.product_id}:{BUY}:{created_at}"] = Decimal("45")

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snap,
        position=position,
        working_orders=[],
        account_risk=None,
        trend_state=TrendState(),
    )

    assert client.payloads
    data = client.payloads[0]["data"]
    assert data["reduceOnly"] is True
    assert data["side"] == SELL
    assert data["postOnly"] is True


def test_trailing_stop_waits_for_min_hold() -> None:
    settings = SettingsStub()
    client = FakeClient()
    snap = snapshot()
    created_at = int((time.time() - 10 * 60) * 1000)
    position = {
        "side": BUY,
        "size": "0.03",
        "cost": "3000",
        "unrealizedPnl": "1.5",
        "createdAt": created_at,
    }
    POSITION_PEAK_PNL_BPS.clear()
    POSITION_PEAK_PNL_BPS[f"{snap.product_id}:{BUY}:{created_at}"] = Decimal("45")

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snap,
        position=position,
        working_orders=[],
        account_risk=None,
        trend_state=TrendState(),
    )

    assert client.payloads == []


class RiskStub:
    total_balance_usd = Decimal("641.5")
    available_balance_usd = Decimal("527")
    used_margin_usd = Decimal("114.5")

    @property
    def margin_usage_bps(self) -> Decimal:
        return self.used_margin_usd / self.total_balance_usd * Decimal("10000")


def test_risk_adjusted_quantity_clamps_higher_target_to_account_buffer() -> None:
    settings = SettingsStub()
    settings.target_btc_size = Decimal("0.06")
    snap = snapshot(bid="75600", ask="75601", oracle="75600")

    quantity = risk_adjusted_quantity(snap, settings, RiskStub())  # type: ignore[arg-type]
    distance = estimated_liquidation_distance_bps(
        snap, settings, RiskStub(), quantity  # type: ignore[arg-type]
    )

    assert quantity == Decimal("0.0466")
    assert distance is not None
    assert distance >= settings.min_entry_liquidation_distance_bps


def test_risk_deleverage_exit_uses_reduce_only_ioc() -> None:
    settings = SettingsStub()
    client = FakeClient()
    thin_risk = RiskStub()
    thin_risk.total_balance_usd = Decimal("400")
    position = {
        "side": BUY,
        "size": "0.06",
        "cost": "4536",
        "unrealizedPnl": "0",
        "createdAt": int((time.time() - 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(bid="75600", ask="75601", oracle="75600"),
        position=position,
        working_orders=[],
        account_risk=thin_risk,  # type: ignore[arg-type]
        trend_state=TrendState(),
    )

    data = client.payloads[0]["data"]
    assert data["reduceOnly"] is True
    assert data["side"] == SELL
    assert data["postOnly"] is False
    assert data["timeInForce"] == "IOC"


def test_emergency_stop_loss_ignores_working_orders() -> None:
    settings = SettingsStub()
    client = FakeClient()
    position = {
        "side": SELL,
        "size": "-0.03",
        "cost": "3000",
        "unrealizedPnl": "-30",
        "createdAt": int((time.time() - 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(),
        position=position,
        working_orders=[{"id": "resting-maker-order"}],
        account_risk=None,
        trend_state=TrendState(),
    )

    data = client.payloads[0]["data"]
    assert data["reduceOnly"] is True
    assert data["side"] == BUY
    assert data["quantity"] == "0.03"
    assert data["postOnly"] is False
    assert data["timeInForce"] == "IOC"


def test_maker_recycle_exits_small_profitable_volume_trade() -> None:
    settings = SettingsStub()
    settings.volume_mode_enabled = True
    settings.maker_recycle_min_minutes = 3
    settings.maker_recycle_profit_bps = Decimal("4")
    client = FakeClient()
    position = {
        "side": BUY,
        "size": "0.03",
        "cost": "3000",
        "unrealizedPnl": "1.5",
        "createdAt": int((time.time() - 5 * 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(),
        position=position,
        working_orders=[],
        account_risk=None,
        trend_state=TrendState(),
    )

    data = client.payloads[0]["data"]
    assert data["reduceOnly"] is True
    assert data["side"] == SELL
    assert data["postOnly"] is True
    assert data["timeInForce"] == "GTD"


def test_scale_in_disabled_by_default_even_when_signal_matches() -> None:
    settings = SettingsStub()
    settings.target_btc_size = Decimal("0.06")
    settings.full_size_expected_edge_bps = Decimal("0")
    client = FakeClient()
    position = {
        "side": SELL,
        "size": "-0.03",
        "cost": "3000",
        "unrealizedPnl": "3",
        "createdAt": int((time.time() - 5 * 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(bid="100000", ask="100001", oracle="99980", funding="0.000011"),
        position=position,
        working_orders=[],
        account_risk=RiskStub(),  # type: ignore[arg-type]
        trend_state=TrendState(),
    )

    assert client.payloads == []


def test_scale_in_requires_profit_when_enabled() -> None:
    settings = SettingsStub()
    settings.scale_in_enabled = True
    settings.target_btc_size = Decimal("0.06")
    settings.full_size_expected_edge_bps = Decimal("0")
    client = FakeClient()
    position = {
        "side": SELL,
        "size": "-0.03",
        "cost": "3000",
        "unrealizedPnl": "-3",
        "createdAt": int((time.time() - 5 * 60) * 1000),
    }

    maybe_manage_position(
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        domain={},
        signer_address="",
        signer_private_key="",
        snapshot=snapshot(bid="100000", ask="100001", oracle="99980", funding="0.000011"),
        position=position,
        working_orders=[],
        account_risk=RiskStub(),  # type: ignore[arg-type]
        trend_state=TrendState(),
    )

    assert client.payloads == []


def test_session_drawdown_pauses_new_entries() -> None:
    settings = SettingsStub()
    state = SessionRiskState()
    risk = RiskStub()
    risk.total_balance_usd = Decimal("1000")

    assert update_session_risk(settings, risk, state) is None  # type: ignore[arg-type]

    risk.total_balance_usd = Decimal("974")
    assert update_session_risk(settings, risk, state) == "session_drawdown_260.00bps"  # type: ignore[arg-type]
    assert state.entry_pause_reason == "session_drawdown_260.00bps"
    SESSION_RISK_STATE.entry_pause_reason = None
