from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.config.settings import Settings
from app.core.state import BotState
from app.core.types import OpenOrder, QuoteIntent, TradingMode, utc_now
from app.data.polymarket import ClobTradingClient
from app.risk.engine import RiskEngine
from app.storage.repositories import EventRepository, NullRepository

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(
        self,
        settings: Settings,
        state: BotState,
        risk: RiskEngine,
        trading_client: ClobTradingClient,
        repository: EventRepository | NullRepository | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.risk = risk
        self.trading_client = trading_client
        self.repository = repository
        self.heartbeat_id = ""

    def client_order_key(self, quote: QuoteIntent) -> str:
        raw = "|".join(
            [
                quote.strategy,
                quote.market,
                quote.token_id,
                quote.side.value,
                str(quote.price),
                str(quote.size),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def submit_quote(self, quote: QuoteIntent) -> dict[str, Any]:
        quote = quote.normalized()
        key = quote.client_order_key or self.client_order_key(quote)
        quote = quote.model_copy(update={"client_order_key": key})
        decision = await self.risk.pre_trade(quote)
        response: dict[str, Any] | None = None
        if not decision.allowed:
            logger.info(
                "order rejected by risk",
                extra={"quote": quote.model_dump(mode="json"), "reasons": decision.reasons},
            )
            if self.repository:
                await self.repository.order_decision(quote, decision, None)
            return {"submitted": False, "decision": decision.model_dump(mode="json")}
        if self.settings.trading.mode == TradingMode.DRY_RUN:
            response = {
                "success": True,
                "dry_run": True,
                "orderID": f"dry-{key[:24]}",
                "status": "simulated",
            }
        else:
            try:
                response = await self.trading_client.create_and_post_limit_order(quote)
            except Exception:
                self.risk.forget_client_order_key(key)
                raise
            if not response.get("success"):
                self.risk.forget_client_order_key(key)
        if response.get("success") or response.get("dry_run"):
            order = OpenOrder(
                order_id=str(response.get("orderID") or response.get("order_id")),
                client_order_key=key,
                market=quote.market,
                token_id=quote.token_id,
                side=quote.side,
                price=quote.price,
                size=quote.size,
                status=str(response.get("status", "live")),
                created_at=utc_now(),
            )
            await self.state.upsert_order(order)
            if self.repository:
                await self.repository.open_order(order)
        if self.repository:
            await self.repository.order_decision(quote, decision, response)
        logger.info("order decision complete", extra={"response": response})
        return {"submitted": True, "response": response}

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        if self.settings.trading.mode == TradingMode.DRY_RUN:
            await self.state.remove_order(order_id)
            return {"success": True, "dry_run": True, "order_id": order_id}
        response = await self.trading_client.cancel(order_id)
        await self.state.remove_order(order_id)
        return response

    async def cancel_all(self, reason: str) -> dict[str, Any]:
        logger.warning("cancel all requested", extra={"reason": reason})
        if self.settings.trading.mode == TradingMode.DRY_RUN:
            async with self.state.lock:
                count = len(self.state.open_orders)
                self.state.open_orders.clear()
            return {"success": True, "dry_run": True, "cancelled": count}
        return await self.trading_client.cancel_all()

    async def reconcile(self) -> None:
        if self.settings.trading.mode == TradingMode.DRY_RUN:
            return
        orders = await self.trading_client.get_open_orders()
        live_ids = {str(order.get("id") or order.get("order_id")) for order in orders}
        async with self.state.lock:
            for order_id in list(self.state.open_orders):
                if order_id not in live_ids:
                    self.state.open_orders.pop(order_id, None)

    async def heartbeat(self) -> None:
        if self.settings.trading.mode == TradingMode.DRY_RUN:
            return
        response = await self.trading_client.heartbeat(self.heartbeat_id)
        next_id = response.get("heartbeat_id")
        if next_id:
            self.heartbeat_id = str(next_id)
