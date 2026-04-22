# ruff: noqa: E501
from __future__ import annotations

from typing import Any, cast

from fastapi import Depends, FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from starlette.responses import HTMLResponse, Response

from app.api.dependencies import require_admin, runtime
from app.config.settings import Settings, load_settings
from app.core.logging import configure_logging
from app.core.runtime import BotRuntime

open_orders_gauge = Gauge("polymarket_open_orders", "Current open orders")
kill_switch_gauge = Gauge("polymarket_kill_switch_enabled", "Kill switch enabled")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    configure_logging(resolved.log_level)
    app = FastAPI(title="Polymarket Live Trading Bot", version="0.1.0")
    app.state.runtime = BotRuntime(resolved)

    @app.on_event("startup")
    async def on_startup() -> None:
        await app.state.runtime.startup()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await app.state.runtime.shutdown()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/ready")
    async def ready(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        snap = await rt.state.snapshot()
        return {
            "ready": snap["connectivity"]["clob_ok"]
            and snap["connectivity"]["auth_valid"]
            and snap["connectivity"]["compliance_ok"],
            "mode": rt.settings.trading.mode.value,
            "connectivity": snap["connectivity"],
            "kill_switch": snap["kill_switch"],
        }

    @app.get("/metrics")
    async def metrics(rt: BotRuntime = Depends(runtime)) -> Response:
        snap = await rt.state.snapshot()
        open_orders_gauge.set(len(snap["open_orders"]))
        kill_switch_gauge.set(1 if snap["kill_switch"]["enabled"] else 0)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/markets")
    async def markets(rt: BotRuntime = Depends(runtime)) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], (await rt.state.snapshot())["markets"])

    @app.get("/positions")
    async def positions(rt: BotRuntime = Depends(runtime)) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], (await rt.state.snapshot())["positions"])

    @app.get("/orders/open")
    async def open_orders(rt: BotRuntime = Depends(runtime)) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], (await rt.state.snapshot())["open_orders"])

    @app.get("/balances")
    async def balances(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        return cast(dict[str, Any], (await rt.state.snapshot())["balances"])

    @app.get("/pnl")
    async def pnl(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        return cast(dict[str, Any], (await rt.state.snapshot())["pnl"])

    @app.get("/risk")
    async def risk(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        snap = await rt.state.snapshot()
        return {
            "kill_switch": snap["kill_switch"],
            "limits": rt.settings.risk.model_dump(mode="json"),
            "counters": snap["counters"],
        }

    @app.get("/strategies")
    async def strategies(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        snap = await rt.state.snapshot()
        return {"enabled": snap["strategy_enabled"], "configured": list(rt.strategies)}

    @app.get("/connectivity")
    async def connectivity(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        return cast(dict[str, Any], (await rt.state.snapshot())["connectivity"])

    @app.get("/fills")
    async def fills(rt: BotRuntime = Depends(runtime)) -> list[dict[str, Any]]:
        async with rt.state.lock:
            return [fill.model_dump(mode="json") for fill in rt.state.fills]

    @app.get("/dashboard/state")
    async def dashboard_state(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        snap = await rt.state.snapshot()
        return {
            "mode": rt.settings.trading.mode.value,
            "ready": snap["connectivity"]["clob_ok"]
            and snap["connectivity"]["auth_valid"]
            and snap["connectivity"]["compliance_ok"],
            "connectivity": snap["connectivity"],
            "kill_switch": snap["kill_switch"],
            "pnl": snap["pnl"],
            "balances": snap["balances"],
            "open_orders": snap["open_orders"],
            "fills": [fill.model_dump(mode="json") for fill in rt.state.fills[-50:]],
            "btc": {
                "prices": snap["crypto_prices"],
                "markets": snap["btc_interval_markets"],
                "signals": snap["strategy_signals"],
            },
            "risk": {
                "limits": rt.settings.risk.model_dump(mode="json"),
                "counters": snap["counters"],
            },
            "strategies": snap["strategy_enabled"],
        }

    @app.post("/admin/kill-switch", dependencies=[Depends(require_admin)])
    async def kill_switch(
        payload: dict[str, Any] | None = None, rt: BotRuntime = Depends(runtime)
    ) -> dict[str, Any]:
        reason = str((payload or {}).get("reason", "manual_admin"))
        await rt.state.set_kill_switch(True, reason)
        result = await rt.order_manager.cancel_all(reason)
        await rt.repository.admin_audit("kill_switch", {"reason": reason, "cancel": result})
        await rt.notifier.send("Kill switch triggered", reason)
        return {"enabled": True, "reason": reason, "cancel": result}

    @app.post("/admin/kill-switch/reset", dependencies=[Depends(require_admin)])
    async def reset_kill_switch(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        await rt.state.set_kill_switch(False, None)
        await rt.repository.admin_audit("kill_switch_reset", {})
        return {"enabled": False}

    @app.post("/admin/strategies/{name}/enable", dependencies=[Depends(require_admin)])
    async def enable_strategy(name: str, rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        async with rt.state.lock:
            rt.state.strategy_enabled[name] = True
        await rt.repository.admin_audit("strategy_enable", {"name": name})
        return {"name": name, "enabled": True}

    @app.post("/admin/strategies/{name}/disable", dependencies=[Depends(require_admin)])
    async def disable_strategy(name: str, rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        async with rt.state.lock:
            rt.state.strategy_enabled[name] = False
        await rt.repository.admin_audit("strategy_disable", {"name": name})
        return {"name": name, "enabled": False}

    @app.post("/admin/orders/cancel-all", dependencies=[Depends(require_admin)])
    async def cancel_all(rt: BotRuntime = Depends(runtime)) -> dict[str, Any]:
        result = await rt.order_manager.cancel_all("manual_admin")
        await rt.repository.admin_audit("cancel_all", result)
        return result

    return app


app = create_app()


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Polymarket BTC 5m Bot</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101214;
      --panel: #181c20;
      --panel-2: #20262c;
      --line: #303840;
      --text: #f3f5f7;
      --muted: #9ca7b3;
      --good: #64d68a;
      --bad: #ff7474;
      --warn: #ffd166;
      --accent: #64b5f6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #14181c;
      position: sticky;
      top: 0;
      z-index: 3;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 720; }
    main { padding: 20px; display: grid; gap: 16px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .wide { grid-column: span 2; }
    .full { grid-column: 1 / -1; }
    section, .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    .metric .label { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    .metric .value { margin-top: 8px; font-size: 24px; font-weight: 750; }
    .muted { color: var(--muted); }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .pill {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      background: var(--panel-2);
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 13px;
      color: var(--muted);
      white-space: nowrap;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    tr:last-child td { border-bottom: 0; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    button {
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      cursor: pointer;
    }
    button.danger { border-color: #793838; background: #2b1818; color: #ffd3d3; }
    @media (max-width: 900px) {
      .grid { grid-template-columns: 1fr; }
      .wide { grid-column: auto; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Polymarket BTC 5m Bot</h1>
      <div class="muted" id="subtitle">Loading...</div>
    </div>
    <div class="toolbar">
      <span class="pill" id="mode">mode</span>
      <span class="pill" id="ready">ready</span>
      <span class="pill" id="kill">kill</span>
      <button class="danger" onclick="killSwitch()">Kill switch</button>
    </div>
  </header>
  <main>
    <div class="grid">
      <div class="metric"><div class="label">BTC Coinbase</div><div class="value" id="btc">-</div></div>
      <div class="metric"><div class="label">Daily PnL</div><div class="value" id="daily">-</div></div>
      <div class="metric"><div class="label">Open Orders</div><div class="value" id="orders">-</div></div>
      <div class="metric"><div class="label">Balance Verified</div><div class="value" id="balance">-</div></div>
      <section class="wide">
        <h2>Latest Signals</h2>
        <table>
          <thead><tr><th>Action</th><th>Outcome</th><th>Ask</th><th>Model</th><th>Edge</th><th>Expiry</th><th>Reason</th></tr></thead>
          <tbody id="signals"></tbody>
        </table>
      </section>
      <section class="wide">
        <h2>Active BTC Markets</h2>
        <table>
          <thead><tr><th>Question</th><th>Beat</th><th>Ends</th><th>Up Token</th><th>Down Token</th></tr></thead>
          <tbody id="markets"></tbody>
        </table>
      </section>
      <section class="wide">
        <h2>Open Orders</h2>
        <table>
          <thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Status</th><th>Token</th></tr></thead>
          <tbody id="openOrders"></tbody>
        </table>
      </section>
      <section class="wide">
        <h2>Recent Fills</h2>
        <table>
          <thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Status</th><th>Market</th></tr></thead>
          <tbody id="fills"></tbody>
        </table>
      </section>
    </div>
  </main>
  <script>
    const fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
    const money = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
    function cls(ok) { return ok ? 'good' : 'bad'; }
    function cell(v) { return v === null || v === undefined ? '-' : String(v); }
    function pct(v) { return v === null || v === undefined ? '-' : (Number(v) * 100).toFixed(2) + '%'; }
    function rows(items, render) {
      return items.length ? items.map(render).join('') : '<tr><td colspan="8" class="muted">No data yet</td></tr>';
    }
    async function refresh() {
      const r = await fetch('/dashboard/state');
      const s = await r.json();
      const coinbase = s.btc.prices.coinbase;
      document.getElementById('subtitle').textContent = new Date().toLocaleString();
      document.getElementById('mode').textContent = s.mode;
      document.getElementById('ready').textContent = s.ready ? 'ready' : 'not ready';
      document.getElementById('ready').className = 'pill ' + cls(s.ready);
      document.getElementById('kill').textContent = s.kill_switch.enabled ? 'kill on' : 'kill off';
      document.getElementById('kill').className = 'pill ' + (s.kill_switch.enabled ? 'bad' : 'good');
      document.getElementById('btc').textContent = coinbase ? money.format(Number(coinbase.price)) : '-';
      document.getElementById('daily').textContent = s.pnl.daily;
      document.getElementById('orders').textContent = s.open_orders.length;
      document.getElementById('balance').textContent = s.balances.verified ? 'yes' : 'no';
      document.getElementById('signals').innerHTML = rows([...s.btc.signals].reverse().slice(0, 12), x => `
        <tr>
          <td class="${x.action === 'buy_losing_side' ? 'good' : 'muted'}">${cell(x.action)}</td>
          <td>${cell(x.outcome)}</td>
          <td>${cell(x.offered_price)}</td>
          <td>${pct(x.model_probability)}</td>
          <td>${pct(x.edge)}</td>
          <td>${x.seconds_to_expiry === null ? '-' : Math.round(x.seconds_to_expiry) + 's'}</td>
          <td class="muted">${cell(x.reason)}</td>
        </tr>`);
      document.getElementById('markets').innerHTML = rows(s.btc.markets.slice(0, 10), x => `
        <tr>
          <td>${cell(x.question)}</td>
          <td>${cell(x.price_to_beat)}</td>
          <td>${new Date(x.end_time).toLocaleTimeString()}</td>
          <td class="muted">${x.up_token_id.slice(0, 10)}...</td>
          <td class="muted">${x.down_token_id.slice(0, 10)}...</td>
        </tr>`);
      document.getElementById('openOrders').innerHTML = rows(s.open_orders, x => `
        <tr><td>${x.side}</td><td>${x.price}</td><td>${fmt.format(Number(x.size))}</td><td>${x.status}</td><td class="muted">${x.token_id.slice(0, 10)}...</td></tr>`);
      document.getElementById('fills').innerHTML = rows(s.fills.slice(-20).reverse(), x => `
        <tr><td>${x.side}</td><td>${x.price}</td><td>${fmt.format(Number(x.size))}</td><td>${x.status}</td><td class="muted">${x.market.slice(0, 12)}...</td></tr>`);
    }
    async function killSwitch() {
      const token = prompt('Admin bearer token');
      if (!token) return;
      await fetch('/admin/kill-switch', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + token, 'content-type': 'application/json' },
        body: JSON.stringify({ reason: 'dashboard' })
      });
      await refresh();
    }
    refresh();
    setInterval(refresh, 1500);
  </script>
</body>
</html>
"""
