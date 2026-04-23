from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config.settings import PolymarketSettings
from app.core.types import (
    BalanceSnapshot,
    BtcIntervalMarket,
    Market,
    OrderBook,
    PriceLevel,
    decimalize,
)

logger = logging.getLogger(__name__)
USDC_BASE_UNITS = Decimal("1000000")


def decimalize_usdc_amount(value: Any) -> Decimal:
    amount = decimalize(value or "0")
    if isinstance(value, str) and "." in value:
        return amount
    return amount / USDC_BASE_UNITS


def _loads_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _btc_slug_interval(slug: Any) -> tuple[datetime, datetime] | None:
    match = re.search(r"btc-updown-5m-(\d+)", str(slug or ""))
    if not match:
        return None
    end_time = datetime.fromtimestamp(int(match.group(1)), tz=UTC)
    return end_time - timedelta(minutes=5), end_time


def _extract_price_to_beat(item: dict[str, Any]) -> Decimal | None:
    candidates = [
        item.get("line"),
        item.get("priceToBeat"),
        item.get("price_to_beat"),
        item.get("xAxisValue"),
    ]
    event_metadata = item.get("eventMetadata")
    if isinstance(event_metadata, dict):
        candidates.append(event_metadata.get("priceToBeat"))
    text = " ".join(
        str(item.get(key, "")) for key in ("question", "description", "groupItemTitle", "slug")
    )
    candidates.extend(re.findall(r"\$?\b([1-9][0-9]{3,6}(?:\.[0-9]+)?)\b", text.replace(",", "")))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            value = decimalize(str(candidate).replace("$", "").replace(",", ""))
        except Exception:
            continue
        if Decimal("1000") <= value <= Decimal("1000000"):
            return value
    return None


def parse_btc_interval_market(item: dict[str, Any]) -> BtcIntervalMarket | None:
    outcomes = [str(outcome).lower() for outcome in _loads_array(item.get("outcomes"))]
    token_ids = [str(token) for token in _loads_array(item.get("clobTokenIds"))]
    if len(outcomes) < 2 or len(token_ids) < 2:
        return None
    try:
        up_idx = outcomes.index("up")
        down_idx = outcomes.index("down")
    except ValueError:
        try:
            up_idx = outcomes.index("yes")
            down_idx = outcomes.index("no")
        except ValueError:
            return None
    slug_interval = _btc_slug_interval(item.get("slug"))
    end_time = (
        slug_interval[1]
        if slug_interval
        else _parse_dt(item.get("endDate") or item.get("end_date"))
    )
    if end_time is None:
        return None
    start_time = (
        slug_interval[0]
        if slug_interval
        else _parse_dt(item.get("startDate") or item.get("start_date"))
    )
    return BtcIntervalMarket(
        market_id=str(item.get("id", "")),
        condition_id=str(item.get("conditionId") or item.get("condition_id") or ""),
        question=str(item.get("question", "")),
        slug=item.get("slug"),
        start_time=start_time,
        end_time=end_time,
        price_to_beat=_extract_price_to_beat(item),
        up_token_id=token_ids[up_idx],
        down_token_id=token_ids[down_idx],
        tick_size=item.get("orderPriceMinTickSize")
        or item.get("minimum_tick_size")
        or item.get("minTickSize")
        or "0.01",
        order_min_size=item.get("orderMinSize") or item.get("order_min_size") or "5",
        neg_risk=bool(item.get("negRisk") or item.get("neg_risk") or False),
        raw=item,
    )


@dataclass(frozen=True)
class ApiCreds:
    api_key: str
    secret: str
    passphrase: str


class PolymarketREST:
    def __init__(self, settings: PolymarketSettings, timeout: float = 10.0) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def geoblock(self) -> dict[str, Any]:
        response = await self.http.get(self.settings.geoblock_url)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def clob_ok(self) -> bool:
        response = await self.http.get(f"{self.settings.clob_host}/ok")
        return response.status_code == 200

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def simplified_markets(self) -> list[Market]:
        response = await self.http.get(f"{self.settings.clob_host}/simplified-markets")
        response.raise_for_status()
        payload = response.json()
        return [Market.model_validate(item) for item in payload.get("data", [])]

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def gamma_markets(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.settings.gamma_host}/markets", params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return cast(list[dict[str, Any]], payload)
        return cast(list[dict[str, Any]], payload.get("data", []))

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def gamma_events_by_slug(self, slug: str) -> list[dict[str, Any]]:
        response = await self.http.get(f"{self.settings.gamma_host}/events", params={"slug": slug})
        response.raise_for_status()
        payload = response.json()
        return cast(list[dict[str, Any]], payload) if isinstance(payload, list) else []

    @retry(wait=wait_exponential(multiplier=0.25, min=0.25, max=5), stop=stop_after_attempt(3))
    async def order_book(self, token_id: str) -> OrderBook:
        response = await self.http.get(
            f"{self.settings.clob_host}/book", params={"token_id": token_id}
        )
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return OrderBook(
            market=str(payload.get("market", "")),
            asset_id=str(payload.get("asset_id") or token_id),
            bids=[PriceLevel.model_validate(level) for level in payload.get("bids", [])],
            asks=[PriceLevel.model_validate(level) for level in payload.get("asks", [])],
        )

    async def discover_btc_5m_markets(self, query: str) -> list[BtcIntervalMarket]:
        # BTC 5m market slugs follow the pattern: btc-updown-5m-{unix_end_timestamp}
        now_ts = int(time.time())
        current_boundary = math.floor(now_ts / 300) * 300
        # Check from -1 boundary to +10 boundaries to be safe
        slugs = [f"btc-updown-5m-{current_boundary + i * 300}" for i in range(-1, 11)]

        async def validate_market(
            slug: str, event: dict[str, Any], mkt: dict[str, Any]
        ) -> BtcIntervalMarket | None:
            if not mkt.get("enableOrderBook"):
                return None
            merged = {**event, **mkt}
            parsed = parse_btc_interval_market(merged)
            if not parsed:
                return None

            try:
                # Validate against CLOB
                res = await self.http.get(
                    f"{self.settings.clob_host}/book",
                    params={"token_id": parsed.up_token_id},
                )
                if res.status_code == 200:
                    logger.info(f"Discovery: accepted market {parsed.slug} (Verified on CLOB)")
                    return parsed
                else:
                    logger.info(
                        "Discovery: slug %s tokens 404 on CLOB (%s), skipping",
                        slug,
                        parsed.up_token_id,
                    )
                    return None
            except Exception as e:
                logger.error(f"Discovery: error verifying {slug} on CLOB: {e}")
                return None

        # Fetch all slugs concurrently
        tasks = [self.gamma_events_by_slug(slug) for slug in slugs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        markets: list[BtcIntervalMarket] = []
        seen: set[str] = set()

        validation_tasks: list[Any] = []
        for i, slug in enumerate(slugs):
            events = results[i]
            if isinstance(events, BaseException) or not events:
                continue
            for event in events:
                for mkt in event.get("markets", []):
                    validation_tasks.append(validate_market(slug, event, mkt))

        validated = await asyncio.gather(*validation_tasks)
        for m in validated:
            if m and m.condition_id not in seen:
                seen.add(m.condition_id)
                markets.append(m)

        return markets



class ClobTradingClient:
    def __init__(self, settings: PolymarketSettings) -> None:
        self.settings = settings
        self._client: Any | None = None

    def _secret(self, value: Any) -> str | None:
        return None if value is None else value.get_secret_value()

    def build(self) -> Any:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds as PyApiCreds

        private_key = self._secret(self.settings.private_key)
        if not private_key:
            raise RuntimeError("PRIVATE_KEY is required for authenticated CLOB client")
        client = ClobClient(
            self.settings.clob_host,
            key=private_key,
            chain_id=self.settings.chain_id,
            signature_type=self.settings.signature_type,
            funder=self.settings.funder_address,
        )
        if self.settings.derive_api_creds:
            client.set_api_creds(client.create_or_derive_api_creds())
        else:
            api_key = self._secret(self.settings.api_key)
            secret = self._secret(self.settings.secret)
            passphrase = self._secret(self.settings.passphrase)
            if not (api_key and secret and passphrase):
                raise RuntimeError(
                    "API key, secret and passphrase are required when derive_api_creds=false"
                )
            try:
                creds = PyApiCreds(
                    api_key=api_key,
                    api_secret=secret,
                    api_passphrase=passphrase,
                )
            except TypeError:
                creds = PyApiCreds(api_key=api_key, secret=secret, passphrase=passphrase)
            client.set_api_creds(creds)
        self._client = client
        return client

    @property
    def client(self) -> Any:
        return self._client or self.build()

    def websocket_api_creds(self) -> dict[str, str]:
        client = self._client
        if client is None:
            return {}
        creds = getattr(client, "creds", None)
        if creds is None:
            return {}
        return {
            "apiKey": str(getattr(creds, "api_key", "")),
            "secret": str(getattr(creds, "api_secret", getattr(creds, "secret", ""))),
            "passphrase": str(
                getattr(creds, "api_passphrase", getattr(creds, "passphrase", ""))
            ),
        }

    async def get_balance_allowance(self) -> BalanceSnapshot:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        def call() -> Any:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            return self.client.get_balance_allowance(params)

        payload = await asyncio.to_thread(call)
        
        # Allowance can be a single string or a dict of contract allowances (for proxy wallets)
        allowance_raw = payload.get("allowance")
        if allowance_raw is None and "allowances" in payload:
            # For proxy wallets, take the max allowance across known exchange/proxy contracts
            allowances = payload.get("allowances", {})
            if isinstance(allowances, dict) and allowances:
                allowance_raw = max(allowances.values(), key=lambda x: int(x))
            else:
                allowance_raw = "0"

        return BalanceSnapshot(
            collateral=decimalize_usdc_amount(payload.get("balance", "0")),
            allowance=decimalize_usdc_amount(allowance_raw or "0"),
            verified=True,
        )

    async def get_open_orders(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.client.get_orders)

    async def create_and_post_limit_order(self, quote: Any) -> dict[str, Any]:
        from py_clob_client.clob_types import CreateOrderOptions, OrderArgs, OrderType

        def call() -> Any:
            order_args = OrderArgs(
                token_id=quote.token_id,
                price=float(quote.price),
                size=float(quote.size),
                side=quote.side.value,
            )
            signed = self.client.create_order(
                order_args,
                CreateOrderOptions(tick_size=str(quote.tick_size), neg_risk=quote.neg_risk),
            )
            order_type = getattr(OrderType, quote.order_type.value, quote.order_type.value)
            try:
                return self.client.post_order(signed, order_type, quote.post_only)
            except TypeError:
                return self.client.post_order(signed, order_type)

        return await asyncio.to_thread(call)

    async def cancel(self, order_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.client.cancel, order_id)

    async def cancel_all(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.client.cancel_all)

    async def heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.client.post_heartbeat, heartbeat_id)
