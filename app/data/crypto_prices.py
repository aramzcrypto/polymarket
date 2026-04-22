from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any, cast

import httpx

from app.core.types import CryptoPriceSnapshot, decimalize, utc_now


class CryptoPriceClient:
    def __init__(self, timeout: float = 5.0) -> None:
        self.http = httpx.AsyncClient(timeout=timeout)
        self.history: deque[CryptoPriceSnapshot] = deque(maxlen=1000)

    async def close(self) -> None:
        await self.http.aclose()

    async def coinbase_btc_usd(self) -> CryptoPriceSnapshot:
        response = await self.http.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        snapshot = CryptoPriceSnapshot(
            symbol="BTC-USD",
            price=payload["price"],
            source="coinbase",
            timestamp=utc_now(),
        )
        self.history.append(snapshot)
        return snapshot

    async def binance_btc_usdt(self) -> CryptoPriceSnapshot:
        response = await self.http.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        snapshot = CryptoPriceSnapshot(
            symbol="BTC-USDT",
            price=payload["price"],
            source="binance",
            timestamp=utc_now(),
        )
        self.history.append(snapshot)
        return snapshot

    def realized_volatility_bps(self, window_seconds: float, fallback: Decimal) -> Decimal:
        now = utc_now()
        prices = [
            item.price
            for item in self.history
            if (now - item.timestamp).total_seconds() <= window_seconds and item.price > 0
        ]
        if len(prices) < 2:
            return fallback
        start = prices[0]
        end = prices[-1]
        if start <= 0:
            return fallback
        realized = abs(end - start) / start * Decimal("10000")
        return max(fallback, realized)

    @staticmethod
    def feed_agreement_bps(a: CryptoPriceSnapshot, b: CryptoPriceSnapshot) -> Decimal:
        mid = (a.price + b.price) / Decimal("2")
        if mid <= 0:
            return Decimal("100000")
        return abs(a.price - b.price) / mid * Decimal("10000")


def implied_reward_multiple(price: Decimal) -> Decimal:
    if price <= 0:
        return Decimal("0")
    return (Decimal("1") / decimalize(price)).quantize(Decimal("0.01"))
