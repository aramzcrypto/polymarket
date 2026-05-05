from __future__ import annotations

from pathlib import Path

from app.ethereal.signing import generate_signer


def main() -> None:
    signer = generate_signer()
    path = Path(".env.ethereal.local")
    if path.exists():
        raise SystemExit(f"{path} already exists; refusing to overwrite secrets")
    path.write_text(
        "\n".join(
            [
                "ETHEREAL_API_BASE=https://api.ethereal.trade",
                "ETHEREAL_ACCOUNT_ADDRESS=0x4Bb12cC382E36B4b6faF7BDcA7708969Aed258eF",
                "ETHEREAL_SUBACCOUNT_ID=5993d170-25f7-414a-b8e5-d2e3a256947b",
                "ETHEREAL_SUBACCOUNT_NAME=primary",
                f"ETHEREAL_SIGNER_PRIVATE_KEY={signer.private_key}",
                f"ETHEREAL_SIGNER_ADDRESS={signer.address}",
                "ETHEREAL_SIGNER_NAME=Codex API signer",
                "ETHEREAL_SIGNER_CATEGORY=API",
                "ETHEREAL_TICKER=BTCUSD",
                "ETHEREAL_DRY_RUN=true",
                "ETHEREAL_LIVE_TRADING=false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    print(f"signer address: {signer.address}")


if __name__ == "__main__":
    main()
