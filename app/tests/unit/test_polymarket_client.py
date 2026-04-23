from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.core.types import OrderSide, OrderType, QuoteIntent
from app.data.polymarket import ClobTradingClient, decimalize_usdc_amount


def test_decimalize_usdc_amount_converts_clob_base_units() -> None:
    assert decimalize_usdc_amount("39600001") == Decimal("39.600001")


def test_decimalize_usdc_amount_keeps_decimal_strings() -> None:
    assert decimalize_usdc_amount("39.600001") == Decimal("39.600001")


class FakeClob:
    def __init__(self) -> None:
        self.options: Any = None
        self.order_type: Any = None

    def create_order(self, _args: Any, options: Any) -> str:
        self.options = options
        return "signed-order"

    def post_order(self, _signed: str, order_type: Any, _post_only: bool) -> dict[str, Any]:
        self.order_type = order_type
        return {"success": True, "orderID": "order-1", "status": "live"}


@pytest.mark.asyncio
async def test_create_order_uses_py_clob_options_object() -> None:
    client = ClobTradingClient.__new__(ClobTradingClient)
    fake = FakeClob()
    client._client = fake
    quote = QuoteIntent(
        strategy="test",
        market="0xmarket",
        token_id="token",
        side=OrderSide.BUY,
        price="0.01",
        size="5",
        order_type=OrderType.FAK,
        tick_size="0.01",
        neg_risk=True,
    )

    response = await client.create_and_post_limit_order(quote)

    assert response["success"] is True
    assert fake.options.tick_size == "0.01"
    assert fake.options.neg_risk is True
    assert fake.order_type == "FAK"
