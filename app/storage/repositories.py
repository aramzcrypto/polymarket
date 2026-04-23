from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.types import Fill, OpenOrder, QuoteIntent, RiskDecision

logger = logging.getLogger(__name__)


class NullRepository:
    """No-op repository used when no database is configured."""

    async def raw_event(self, source: str, event_type: str, payload: dict[str, Any]) -> None:
        return

    async def order_decision(
        self, quote: QuoteIntent, decision: RiskDecision, response: dict[str, Any] | None
    ) -> None:
        return

    async def open_order(self, order: OpenOrder) -> None:
        return

    async def fill(self, fill: Fill) -> None:
        return

    async def recent_fills(self, limit: int = 500) -> list[Fill]:
        return []

    async def admin_audit(self, action: str, payload: dict[str, Any]) -> None:
        return


class EventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def raw_event(self, source: str, event_type: str, payload: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "insert into raw_events (id, source, event_type, payload) "
                    "values (:id, :source, :event_type, cast(:payload as jsonb))"
                ),
                {
                    "id": str(uuid4()),
                    "source": source,
                    "event_type": event_type,
                    "payload": __import__("json").dumps(payload, default=str),
                },
            )
            await session.commit()

    async def order_decision(
        self, quote: QuoteIntent, decision: RiskDecision, response: dict[str, Any] | None
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    """
                    insert into order_decisions (
                        id, client_order_key, strategy, market, token_id, side,
                        price, size, allowed, reasons, response
                    )
                    values (
                        :id, :client_order_key, :strategy, :market, :token_id,
                        :side, :price, :size, :allowed, cast(:reasons as jsonb),
                        cast(:response as jsonb)
                    )
                    """
                ),
                {
                    "id": str(uuid4()),
                    "client_order_key": quote.client_order_key,
                    "strategy": quote.strategy,
                    "market": quote.market,
                    "token_id": quote.token_id,
                    "side": quote.side.value,
                    "price": str(quote.price),
                    "size": str(quote.size),
                    "allowed": decision.allowed,
                    "reasons": __import__("json").dumps(
                        [reason.value for reason in decision.reasons]
                    ),
                    "response": __import__("json").dumps(response or {}, default=str),
                },
            )
            await session.commit()

    async def open_order(self, order: OpenOrder) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    """
                    insert into orders (
                        order_id, client_order_key, market, token_id, side,
                        price, size, filled_size, status
                    )
                    values (
                        :order_id, :client_order_key, :market, :token_id, :side,
                        :price, :size, :filled_size, :status
                    )
                    on conflict (order_id) do update set
                        filled_size = excluded.filled_size,
                        status = excluded.status
                    """
                ),
                order.model_dump(mode="json"),
            )
            await session.commit()

    async def fill(self, fill: Fill) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    """
                    insert into fills (
                        trade_id, order_id, market, token_id, side, price,
                        size, fee, status
                    )
                    values (
                        :trade_id, :order_id, :market, :token_id, :side,
                        :price, :size, :fee, :status
                    )
                    on conflict (trade_id) do nothing
                    """
                ),
                fill.model_dump(mode="json"),
            )
            await session.commit()

    async def recent_fills(self, limit: int = 500) -> list[Fill]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        select *
                        from (
                            select trade_id, order_id, market, token_id, side, price,
                                   size, fee, status, created_at
                            from fills
                            order by created_at desc
                            limit :limit
                        ) recent
                        order by created_at asc
                        """
                    ),
                    {"limit": limit},
                )
            ).mappings()
            return [
                Fill(
                    trade_id=str(row["trade_id"]),
                    order_id=row["order_id"],
                    market=str(row["market"]),
                    token_id=str(row["token_id"]),
                    side=str(row["side"]),
                    price=row["price"],
                    size=row["size"],
                    fee=row["fee"],
                    status=str(row["status"]),
                    timestamp=row["created_at"],
                )
                for row in rows
            ]

    async def admin_audit(self, action: str, payload: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "insert into admin_audit_log (id, action, payload) "
                    "values (:id, :action, cast(:payload as jsonb))"
                ),
                {
                    "id": str(uuid4()),
                    "action": action,
                    "payload": __import__("json").dumps(payload, default=str),
                },
            )
            await session.commit()
