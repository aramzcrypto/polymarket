from __future__ import annotations

from decimal import Decimal

from eth_account import Account

from app.ethereal.signing import (
    build_limit_order_data,
    build_limit_order_payload,
    build_link_signer_payload,
    bytes32_from_text,
    decimal_to_d9,
)


def test_bytes32_from_text_encodes_primary() -> None:
    assert (
        bytes32_from_text("primary")
        == "0x7072696d61727900000000000000000000000000000000000000000000000000"
    )


def test_decimal_to_d9_scales_precision() -> None:
    assert decimal_to_d9(Decimal("1.23456789")) == 1234567890


def test_build_link_signer_payload_contains_two_signatures() -> None:
    owner = Account.create()
    signer = Account.create()
    payload = build_link_signer_payload(
        domain={
            "name": "Ethereal",
            "version": "1",
            "chainId": 5064014,
            "verifyingContract": "0xB3cDC82035C495c484C9fF11eD5f3Ff6d342e3cc",
        },
        subaccount_id="5993d170-25f7-414a-b8e5-d2e3a256947b",
        owner_address=owner.address,
        subaccount_name="primary",
        signer_address=signer.address,
        owner_private_key=owner.key.hex(),
        signer_private_key=signer.key.hex(),
        signer_name="Codex API signer",
        signer_category="API",
        nonce="123456789",
        signed_at=1712019600,
    )
    assert payload["data"]["subaccountId"] == "5993d170-25f7-414a-b8e5-d2e3a256947b"
    assert payload["signature"].startswith("0x")
    assert payload["signerSignature"].startswith("0x")


def test_build_limit_order_payload_signs_trade_order() -> None:
    signer = Account.create()
    payload = build_limit_order_payload(
        domain={
            "name": "Ethereal",
            "version": "1",
            "chainId": 5064014,
            "verifyingContract": "0xB3cDC82035C495c484C9fF11eD5f3Ff6d342e3cc",
        },
        signer_address=signer.address,
        signer_private_key=signer.key.hex(),
        subaccount_name="primary",
        quantity="0.01",
        price="100000",
        side=0,
        onchain_id=1,
        nonce="123456789",
        signed_at=1712019600,
        client_order_id="codex-test",
    )
    assert payload["signature"].startswith("0x")
    assert payload["data"]["subaccount"] == bytes32_from_text("primary")
    assert payload["data"]["clientOrderId"] == "codex-test"


def test_build_limit_order_data_uses_explicit_sender() -> None:
    data = build_limit_order_data(
        sender_address="0x1111111111111111111111111111111111111111",
        subaccount_name="primary",
        quantity="0.01",
        price="100000",
        side=0,
        onchain_id=1,
        expires_at=1712023200,
        nonce="123456789",
        signed_at=1712019600,
        client_order_id="codextest",
    )
    assert data["sender"] == "0x1111111111111111111111111111111111111111"
    assert data["subaccount"] == bytes32_from_text("primary")
    assert data["clientOrderId"] == "codextest"
    assert data["expiresAt"] == 1712023200
