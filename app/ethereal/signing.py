from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from secrets import randbelow
from typing import Any, cast

from eth_account import Account
from eth_account.messages import encode_typed_data

D9 = Decimal("1000000000")

EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

LINK_SIGNER_TYPES = [
    {"name": "sender", "type": "address"},
    {"name": "signer", "type": "address"},
    {"name": "subaccount", "type": "bytes32"},
    {"name": "nonce", "type": "uint64"},
    {"name": "signedAt", "type": "uint64"},
]

TRADE_ORDER_TYPES = [
    {"name": "sender", "type": "address"},
    {"name": "subaccount", "type": "bytes32"},
    {"name": "quantity", "type": "uint128"},
    {"name": "price", "type": "uint128"},
    {"name": "reduceOnly", "type": "bool"},
    {"name": "side", "type": "uint8"},
    {"name": "engineType", "type": "uint8"},
    {"name": "productId", "type": "uint32"},
    {"name": "nonce", "type": "uint64"},
    {"name": "signedAt", "type": "uint64"},
]


@dataclass(frozen=True)
class GeneratedSigner:
    address: str
    private_key: str


def bytes32_from_text(value: str) -> str:
    encoded = value.encode("ascii")
    if len(encoded) > 32:
        raise ValueError("subaccount name must fit in 32 bytes")
    return "0x" + encoded.hex().ljust(64, "0")


def now_signed_at() -> int:
    return int(time.time())


def new_nonce() -> str:
    base = time.time_ns()
    return str(base + randbelow(1000))


def decimal_to_d9(value: Decimal | str) -> int:
    decimal_value = Decimal(str(value))
    return int((decimal_value * D9).to_integral_value())


def generate_signer() -> GeneratedSigner:
    account = Account.create()
    return GeneratedSigner(address=account.address, private_key=cast(str, account.key.hex()))


def address_from_private_key(private_key: str) -> str:
    return cast(str, Account.from_key(private_key).address)


def _sign_typed(
    *,
    domain: dict[str, Any],
    primary_type: str,
    message_types: list[dict[str, str]],
    message_data: dict[str, Any],
    private_key: str,
) -> str:
    signable = encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": EIP712_DOMAIN_TYPES,
                primary_type: message_types,
            },
            "primaryType": primary_type,
            "domain": domain,
            "message": message_data,
        }
    )
    signed = Account.sign_message(signable, private_key).signature.hex()
    normalized = signed if str(signed).startswith("0x") else f"0x{signed}"
    return cast(str, normalized)


def build_link_signer_payload(
    *,
    domain: dict[str, Any],
    subaccount_id: str,
    owner_address: str,
    subaccount_name: str,
    signer_address: str,
    owner_private_key: str,
    signer_private_key: str,
    signer_name: str,
    signer_category: str,
    nonce: str | None = None,
    signed_at: int | None = None,
) -> dict[str, Any]:
    nonce_value = nonce or new_nonce()
    signed_at_value = signed_at or now_signed_at()
    data = {
        "subaccountId": subaccount_id,
        "sender": owner_address,
        "subaccount": bytes32_from_text(subaccount_name),
        "signer": signer_address,
        "nonce": nonce_value,
        "signedAt": signed_at_value,
        "name": signer_name,
        "category": signer_category,
    }
    message = {
        "sender": owner_address,
        "signer": signer_address,
        "subaccount": data["subaccount"],
        "nonce": int(nonce_value),
        "signedAt": signed_at_value,
    }
    signature = _sign_typed(
        domain=domain,
        primary_type="LinkSigner",
        message_types=LINK_SIGNER_TYPES,
        message_data=message,
        private_key=owner_private_key,
    )
    signer_signature = _sign_typed(
        domain=domain,
        primary_type="LinkSigner",
        message_types=LINK_SIGNER_TYPES,
        message_data=message,
        private_key=signer_private_key,
    )
    return {"signature": signature, "signerSignature": signer_signature, "data": data}


def build_limit_order_payload(
    *,
    domain: dict[str, Any],
    signer_address: str,
    signer_private_key: str,
    subaccount_name: str,
    quantity: Decimal | str,
    price: Decimal | str,
    side: int,
    onchain_id: int,
    engine_type: int = 0,
    reduce_only: bool = False,
    time_in_force: str = "IOC",
    post_only: bool = False,
    expires_at: int | None = None,
    client_order_id: str | None = None,
    nonce: str | None = None,
    signed_at: int | None = None,
) -> dict[str, Any]:
    nonce_value = nonce or new_nonce()
    signed_at_value = signed_at or now_signed_at()
    data = build_limit_order_data(
        sender_address=signer_address,
        subaccount_name=subaccount_name,
        quantity=quantity,
        price=price,
        side=side,
        onchain_id=onchain_id,
        engine_type=engine_type,
        reduce_only=reduce_only,
        time_in_force=time_in_force,
        post_only=post_only,
        expires_at=expires_at,
        client_order_id=client_order_id,
        nonce=nonce_value,
        signed_at=signed_at_value,
    )
    message = {
        "sender": signer_address,
        "subaccount": data["subaccount"],
        "quantity": decimal_to_d9(Decimal(str(quantity))),
        "price": decimal_to_d9(Decimal(str(price))),
        "reduceOnly": reduce_only,
        "side": side,
        "engineType": engine_type,
        "productId": onchain_id,
        "nonce": int(nonce_value),
        "signedAt": signed_at_value,
    }
    signature = _sign_typed(
        domain=domain,
        primary_type="TradeOrder",
        message_types=TRADE_ORDER_TYPES,
        message_data=message,
        private_key=signer_private_key,
    )
    return {"signature": signature, "data": data}


def build_limit_order_data(
    *,
    sender_address: str,
    subaccount_name: str,
    quantity: Decimal | str,
    price: Decimal | str,
    side: int,
    onchain_id: int,
    engine_type: int = 0,
    reduce_only: bool = False,
    time_in_force: str = "IOC",
    post_only: bool = False,
    expires_at: int | None = None,
    client_order_id: str | None = None,
    nonce: str | None = None,
    signed_at: int | None = None,
) -> dict[str, Any]:
    nonce_value = nonce or new_nonce()
    signed_at_value = signed_at or now_signed_at()
    data = {
        "subaccount": bytes32_from_text(subaccount_name),
        "sender": sender_address,
        "nonce": nonce_value,
        "type": "LIMIT",
        "quantity": str(quantity),
        "price": str(price),
        "side": side,
        "onchainId": onchain_id,
        "engineType": engine_type,
        "reduceOnly": reduce_only,
        "signedAt": signed_at_value,
        "timeInForce": time_in_force,
        "postOnly": post_only,
    }
    if expires_at is not None:
        data["expiresAt"] = expires_at
    if client_order_id:
        data["clientOrderId"] = client_order_id
    return data
