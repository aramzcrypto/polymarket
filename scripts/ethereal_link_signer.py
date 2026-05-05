from __future__ import annotations

import json

from app.ethereal.client import EtherealClient
from app.ethereal.config import load_ethereal_settings
from app.ethereal.signing import address_from_private_key, build_link_signer_payload


def main() -> None:
    settings = load_ethereal_settings()
    if not settings.account_address or not settings.subaccount_id:
        raise SystemExit("ETHEREAL_ACCOUNT_ADDRESS and ETHEREAL_SUBACCOUNT_ID are required")
    if not settings.has_signer_key:
        raise SystemExit("ETHEREAL_SIGNER_PRIVATE_KEY is required")
    signer_private_key = settings.signer_private_key.get_secret_value()  # type: ignore[union-attr]
    signer_address = address_from_private_key(signer_private_key)
    if settings.signer_address and settings.signer_address.lower() != signer_address.lower():
        raise SystemExit("ETHEREAL_SIGNER_ADDRESS does not match ETHEREAL_SIGNER_PRIVATE_KEY")

    client = EtherealClient(settings.api_base)
    try:
        existing = client.signers(settings.subaccount_id).get("data", [])
        for signer in existing:
            if signer.get("signer", "").lower() == signer_address.lower():
                print(json.dumps({"status": "already-linked", "signer": signer}, indent=2))
                return
        if not settings.has_owner_key:
            print(
                json.dumps(
                    {
                        "status": "owner-key-missing",
                        "message": "Set ETHEREAL_OWNER_PRIVATE_KEY to complete linking.",
                        "signer_address": signer_address,
                        "subaccount_id": settings.subaccount_id,
                        "subaccount_name": settings.subaccount_name,
                    },
                    indent=2,
                )
            )
            return
        owner_private_key = settings.owner_private_key.get_secret_value()  # type: ignore[union-attr]
        domain = client.rpc_config()["domain"]
        payload = build_link_signer_payload(
            domain=domain,
            subaccount_id=settings.subaccount_id,
            owner_address=settings.account_address,
            subaccount_name=settings.subaccount_name,
            signer_address=signer_address,
            owner_private_key=owner_private_key,
            signer_private_key=signer_private_key,
            signer_name=settings.signer_name,
            signer_category=settings.signer_category,
        )
        response = client.link_signer(payload)
        print(json.dumps(response, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
