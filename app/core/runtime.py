from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from app.alerts.notifier import AlertNotifier
from app.config.settings import Settings, load_settings
from app.core.state import BotState
from app.core.types import Fill, OrderBook, OrderSide, PriceLevel, utc_now
from app.data.crypto_prices import CryptoPriceClient
from app.data.polymarket import ClobTradingClient, PolymarketREST
from app.execution.order_manager import OrderManager
from app.risk.engine import RiskEngine
from app.storage.database import Database
from app.storage.repositories import EventRepository
from app.strategies.base import StrategyContext
from app.strategies.registry import build_strategies
from app.ws.polymarket_ws import PolymarketWebSocket

logger = logging.getLogger(__name__)


class BotRuntime:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.state = BotState()
        self.db = Database(self.settings.database)
        self.repository = EventRepository(self.db.session_factory)
        self.rest = PolymarketREST(self.settings.polymarket)
        self.crypto_prices = CryptoPriceClient()
        self.trading_client = ClobTradingClient(self.settings.polymarket)
        self.risk = RiskEngine(self.state, self.settings.risk, self.settings.trading)
        self.order_manager = OrderManager(
            self.settings,
            self.state,
            self.risk,
            self.trading_client,
            self.repository,
        )
        self.notifier = AlertNotifier(self.settings.alerts)
        self.strategies = build_strategies(self.settings)
        self.tasks: list[asyncio.Task[Any]] = []
        self.market_ws: PolymarketWebSocket | None = None
        self.user_ws: PolymarketWebSocket | None = None
        for name, strategy_config in self.settings.strategies.items():
            self.state.strategy_enabled[name] = strategy_config.enabled

    async def startup(self) -> None:
        await self._startup_checks()
        if self.settings.trading.cancel_all_on_startup:
            await self.order_manager.cancel_all("startup_configured")
        await self.notifier.send(
            "Polymarket bot started", f"mode={self.settings.trading.mode.value}"
        )
        self._start_websockets()
        self.tasks.append(asyncio.create_task(self._strategy_loop(), name="strategy-loop"))
        self.tasks.append(asyncio.create_task(self._reconcile_loop(), name="reconcile-loop"))
        self.tasks.append(asyncio.create_task(self._heartbeat_loop(), name="heartbeat-loop"))
        if self._btc_config() is not None:
            self.tasks.append(asyncio.create_task(self._btc_price_loop(), name="btc-price-loop"))
            self.tasks.append(
                asyncio.create_task(self._btc_discovery_loop(), name="btc-discovery-loop")
            )
            self.tasks.append(asyncio.create_task(self._btc_book_loop(), name="btc-book-loop"))

    async def shutdown(self) -> None:
        if self.settings.trading.cancel_all_on_shutdown:
            with suppress(Exception):
                await self.order_manager.cancel_all("shutdown")
        if self.market_ws:
            await self.market_ws.stop()
        if self.user_ws:
            await self.user_ws.stop()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.rest.close()
        await self.crypto_prices.close()
        await self.db.close()
        await self.notifier.send("Polymarket bot stopped", "shutdown complete")

    def _btc_config(self) -> Any | None:
        config = self.settings.strategies.get("btc_5m_late_convexity")
        if config and config.enabled:
            return config
        return None

    async def _startup_checks(self) -> None:
        await self.db.check()
        async with self.state.lock:
            self.state.connectivity.clob_ok = await self.rest.clob_ok()
        if self.settings.trading.require_geoblock_ok:
            geo = await self.rest.geoblock()
            blocked = bool(geo.get("blocked"))
            async with self.state.lock:
                self.state.connectivity.compliance_ok = not blocked
                if blocked:
                    self.state.kill_switch_enabled = True
                    self.state.kill_switch_reason = (
                        f"geoblocked:{geo.get('country')}:{geo.get('region')}"
                    )
            if blocked and self.settings.trading.live_enabled:
                await self.notifier.send("Polymarket geoblock rejection", str(geo))
                raise RuntimeError(f"Geoblock check failed: {geo}")
        else:
            async with self.state.lock:
                self.state.connectivity.compliance_ok = True
        if self.settings.trading.live_enabled:
            try:
                self.trading_client.build()
                balance = await self.trading_client.get_balance_allowance()
                async with self.state.lock:
                    self.state.balances = balance.model_copy(update={"updated_at": utc_now()})
                    self.state.connectivity.auth_valid = True
            except Exception as exc:
                async with self.state.lock:
                    self.state.connectivity.auth_valid = False
                    self.state.connectivity.last_error = str(exc)
                raise
        else:
            async with self.state.lock:
                self.state.balances.verified = True
                self.state.connectivity.auth_valid = True

    def _start_websockets(self) -> None:
        token_ids = [
            token_id
            for strategy in self.settings.strategies.values()
            for token_id in strategy.token_ids
        ]
        market_ids = [
            market for strategy in self.settings.strategies.values() for market in strategy.markets
        ]
        if token_ids:
            self.market_ws = PolymarketWebSocket(
                self.settings.polymarket.market_ws_url,
                {"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True},
                self.handle_market_message,
                self.handle_market_status,
            )
            self.tasks.append(asyncio.create_task(self.market_ws.run(), name="market-ws"))
        if market_ids:
            creds = {
                "apiKey": self.settings.polymarket.api_key.get_secret_value()
                if self.settings.polymarket.api_key
                else "",
                "secret": self.settings.polymarket.secret.get_secret_value()
                if self.settings.polymarket.secret
                else "",
                "passphrase": self.settings.polymarket.passphrase.get_secret_value()
                if self.settings.polymarket.passphrase
                else "",
            }
            self.user_ws = PolymarketWebSocket(
                self.settings.polymarket.user_ws_url,
                {"auth": creds, "markets": market_ids, "type": "user"},
                self.handle_user_message,
                self.handle_user_status,
            )
            self.tasks.append(asyncio.create_task(self.user_ws.run(), name="user-ws"))
        if not token_ids:
            logger.warning("no token_ids configured; market websocket not started")
        if not market_ids:
            logger.warning("no markets configured; user websocket not started")

    async def handle_market_status(self, connected: bool, error: str | None) -> None:
        async with self.state.lock:
            self.state.connectivity.market_ws_connected = connected
            self.state.connectivity.last_error = error

    async def handle_user_status(self, connected: bool, error: str | None) -> None:
        async with self.state.lock:
            self.state.connectivity.user_ws_connected = connected
            self.state.connectivity.last_error = error

    async def handle_market_message(self, payload: dict[str, Any]) -> None:
        await self.repository.raw_event(
            "market_ws", str(payload.get("event_type", "unknown")), payload
        )
        event_type = payload.get("event_type")
        if event_type == "book":
            book = OrderBook(
                market=str(payload["market"]),
                asset_id=str(payload["asset_id"]),
                bids=[PriceLevel.model_validate(level) for level in payload.get("bids", [])],
                asks=[PriceLevel.model_validate(level) for level in payload.get("asks", [])],
                timestamp=utc_now(),
            )
            await self.state.upsert_book(book)
            for strategy in self.strategies.values():
                await strategy.on_market_update(book)
        elif event_type in {"price_change", "best_bid_ask"}:
            async with self.state.lock:
                self.state.connectivity.last_market_msg_at = utc_now()
                self.state.connectivity.market_ws_connected = True

    async def handle_user_message(self, payload: dict[str, Any]) -> None:
        await self.repository.raw_event(
            "user_ws", str(payload.get("event_type", "unknown")), payload
        )
        event_type = payload.get("event_type")
        if event_type == "trade":
            fill = Fill(
                trade_id=str(payload.get("id") or payload.get("trade_id")),
                order_id=payload.get("taker_order_id"),
                market=str(payload["market"]),
                token_id=str(payload["asset_id"]),
                side=OrderSide(str(payload["side"]).upper()),
                price=payload.get("price", "0"),
                size=payload.get("size", "0"),
                status=str(payload.get("status", "MATCHED")),
            )
            await self.state.record_fill(fill)
            await self.repository.fill(fill)
            decision = await self.risk.post_trade_fill(fill.size)
            if not decision.allowed:
                await self.order_manager.cancel_all("post_trade_risk")
                await self.notifier.send("Kill switch triggered", decision.message)

    async def _strategy_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            decision = await self.risk.circuit_breaker_check()
            if not decision.allowed:
                await self.order_manager.cancel_all("circuit_breaker")
                continue
            async with self.state.lock:
                books = list(self.state.books.values())
                inventory = {pos.token_id: float(pos.size) for pos in self.state.positions.values()}
                live: dict[str, int] = {}
                for order in self.state.open_orders.values():
                    live[order.token_id] = live.get(order.token_id, 0) + 1
                tiny_cap = (
                    float(self.settings.trading.tiny_live_max_order_size)
                    if self.settings.trading.tiny_live_mode
                    else None
                )
                enabled = dict(self.state.strategy_enabled)
                killed = self.state.kill_switch_enabled
            context = StrategyContext(inventory, live, tiny_cap)
            for book in books:
                for name, strategy in self.strategies.items():
                    if not enabled.get(name, True):
                        continue
                    quotes = await strategy.desired_quotes(book, context)
                    latest_signal = getattr(strategy, "last_signals", [])[-1:]
                    for signal in latest_signal:
                        await self.state.record_strategy_signal(signal)
                    if killed:
                        continue
                    for quote in quotes:
                        await self.order_manager.submit_quote(quote)

    async def _btc_price_loop(self) -> None:
        config = self._btc_config()
        if config is None:
            return
        while True:
            try:
                coinbase = await self.crypto_prices.coinbase_btc_usd()
                await self.state.upsert_crypto_price(coinbase)
                price = coinbase
                with suppress(Exception):
                    binance = await self.crypto_prices.binance_btc_usdt()
                    await self.state.upsert_crypto_price(binance)
                    agreement = self.crypto_prices.feed_agreement_bps(coinbase, binance)
                    if agreement > config.min_price_feed_agreement_bps:
                        async with self.state.lock:
                            self.state.connectivity.last_error = (
                                f"BTC feeds diverged by {agreement} bps"
                            )
                        await asyncio.sleep(1)
                        continue
                    price = coinbase
                volatility = self.crypto_prices.realized_volatility_bps(
                    config.volatility_window_seconds, config.default_volatility_bps
                )
                strategy = self.strategies.get("btc_5m_late_convexity")
                if strategy and hasattr(strategy, "update_price"):
                    strategy.update_price(price, volatility)
            except Exception as exc:
                async with self.state.lock:
                    self.state.connectivity.last_error = f"BTC price feed error: {exc}"
            await asyncio.sleep(1)

    async def _btc_discovery_loop(self) -> None:
        config = self._btc_config()
        if config is None:
            return
        while True:
            try:
                markets = await self.rest.discover_btc_5m_markets(config.market_query)
                for market in markets:
                    await self.state.upsert_btc_interval_market(market)
                strategy = self.strategies.get("btc_5m_late_convexity")
                if strategy and hasattr(strategy, "update_markets"):
                    strategy.update_markets(markets)
            except Exception as exc:
                async with self.state.lock:
                    self.state.connectivity.last_error = f"BTC market discovery error: {exc}"
            await asyncio.sleep(config.discovery_interval_seconds)

    async def _btc_book_loop(self) -> None:
        config = self._btc_config()
        if config is None:
            return
        while True:
            async with self.state.lock:
                token_ids = [
                    token
                    for market in self.state.btc_interval_markets.values()
                    for token in (market.up_token_id, market.down_token_id)
                ]
            for token_id in token_ids:
                try:
                    book = await self.rest.order_book(token_id)
                    await self.state.upsert_book(book)
                except Exception as exc:
                    async with self.state.lock:
                        self.state.connectivity.last_error = f"BTC book poll error: {exc}"
            await asyncio.sleep(config.book_poll_interval_seconds)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.trading.reconcile_interval_seconds)
            with suppress(Exception):
                await self.order_manager.reconcile()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.polymarket.heartbeat_interval_seconds)
            with suppress(Exception):
                await self.order_manager.heartbeat()
