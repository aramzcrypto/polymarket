from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.types import Fill, OrderBook, QuoteIntent, RiskDecision


@dataclass(frozen=True)
class StrategyContext:
    inventory_by_token: dict[str, float]
    live_orders_by_token: dict[str, int]
    tiny_live_cap: float | None = None


class Strategy(ABC):
    name: str

    @abstractmethod
    async def on_market_update(self, book: OrderBook) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_fill(self, fill: Fill) -> None:
        raise NotImplementedError

    @abstractmethod
    async def on_risk_state(self, decision: RiskDecision) -> None:
        raise NotImplementedError

    @abstractmethod
    async def desired_quotes(self, book: OrderBook, context: StrategyContext) -> list[QuoteIntent]:
        raise NotImplementedError
