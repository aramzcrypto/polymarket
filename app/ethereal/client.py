from __future__ import annotations

from typing import Any

import httpx


class EtherealClient:
    def __init__(self, api_base: str = "https://api.ethereal.trade", timeout: float = 15.0) -> None:
        self.api_base = api_base.rstrip("/")
        self.session = httpx.Client(base_url=self.api_base, timeout=timeout)

    def close(self) -> None:
        self.session.close()

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(path, params=params)
        response.raise_for_status()
        return dict(response.json())

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(path, json=payload)
        response.raise_for_status()
        return dict(response.json())

    def _delete(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.request("DELETE", path, json=payload)
        response.raise_for_status()
        return dict(response.json())

    def rpc_config(self) -> dict[str, Any]:
        return self._get("/v1/rpc/config")

    def subaccounts(self, sender: str, *, name: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"sender": sender}
        if name:
            params["name"] = name
        return self._get("/v1/subaccount", params=params)

    def subaccount(self, subaccount_id: str) -> dict[str, Any]:
        return self._get(f"/v1/subaccount/{subaccount_id}")

    def balances(self, subaccount_id: str) -> dict[str, Any]:
        return self._get("/v1/subaccount/balance", params={"subaccountId": subaccount_id})

    def products(self, *, ticker: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/v1/product", params=params)

    def market_prices(self, product_ids: list[str]) -> dict[str, Any]:
        return self._get("/v1/product/market-price", params={"productIds": product_ids})

    def market_liquidity(self, product_id: str) -> dict[str, Any]:
        return self._get("/v1/product/market-liquidity", params={"productId": product_id})

    def projected_funding_rates(self, product_ids: list[str]) -> dict[str, Any]:
        return self._get("/v1/funding/projected-rate", params={"productIds": product_ids})

    def funding_history(self, product_id: str, *, range_name: str = "DAY") -> dict[str, Any]:
        return self._get("/v1/funding", params={"productId": product_id, "range": range_name})

    def positions(self, subaccount_id: str, *, open_only: bool = True) -> dict[str, Any]:
        return self._get("/v1/position", params={"subaccountId": subaccount_id, "open": open_only})

    def orders(
        self,
        subaccount_id: str,
        *,
        is_working: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"subaccountId": subaccount_id, "limit": limit}
        if is_working:
            params["isWorking"] = True
        return self._get("/v1/order", params=params)

    def signers(self, subaccount_id: str) -> dict[str, Any]:
        return self._get("/v1/linked-signer", params={"subaccountId": subaccount_id})

    def signer_by_address(self, address: str) -> dict[str, Any]:
        return self._get(f"/v1/linked-signer/address/{address}")

    def signer_quota(self, subaccount_id: str) -> dict[str, Any]:
        return self._get("/v1/linked-signer/quota", params={"subaccountId": subaccount_id})

    def link_signer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/linked-signer/link", payload)

    def revoke_signer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._delete("/v1/linked-signer/revoke", payload)

    def refresh_signer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/linked-signer/refresh", payload)

    def dry_run_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/order/dry-run", payload)

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/order", payload)
