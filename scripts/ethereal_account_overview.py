from __future__ import annotations

import json

from app.ethereal.client import EtherealClient
from app.ethereal.config import load_ethereal_settings


def main() -> None:
    settings = load_ethereal_settings()
    if not settings.account_address or not settings.subaccount_id:
        raise SystemExit("ETHEREAL_ACCOUNT_ADDRESS and ETHEREAL_SUBACCOUNT_ID are required")
    client = EtherealClient(settings.api_base)
    try:
        subaccounts = client.subaccounts(settings.account_address)
        balances = client.balances(settings.subaccount_id)
        signers = client.signers(settings.subaccount_id)
        quota = client.signer_quota(settings.subaccount_id)
        print(
            json.dumps(
                {
                    "account": settings.account_address,
                    "subaccount_id": settings.subaccount_id,
                    "subaccounts": subaccounts.get("data", []),
                    "balances": balances.get("data", []),
                    "signers": signers.get("data", []),
                    "quota": quota,
                },
                indent=2,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
