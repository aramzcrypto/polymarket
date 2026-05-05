"""
Ethereal perp bot entry point.

Usage:
    python -m app.ethereal.bot

Configuration is loaded from .env.ethereal.local / .env.ethereal / .env.
Key variables:
    ETHEREAL_ACCOUNT_ADDRESS    Your wallet address
    ETHEREAL_SUBACCOUNT_ID      Numeric subaccount ID
    ETHEREAL_SUBACCOUNT_NAME    Subaccount name string (default: "primary")
    ETHEREAL_SIGNER_ADDRESS     Linked signer address
    ETHEREAL_SIGNER_PRIVATE_KEY Signer private key (hex)
    ETHEREAL_TICKER             Product ticker (default: BTCUSD)
    ETHEREAL_DRY_RUN            Set to false for live trading
    ETHEREAL_LIVE_TRADING       Set to true to actually submit orders
    ETHEREAL_POLL_INTERVAL_SECONDS  Seconds between ticks (default: 15)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from app.ethereal.client import EtherealClient
from app.ethereal.config import load_ethereal_settings
from app.ethereal.strategy import EtherealMomentumStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)


async def run_bot() -> None:
    config = load_ethereal_settings()
    client = EtherealClient(api_base=config.api_base)
    strategy = EtherealMomentumStrategy(config=config, client=client)
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    logger.info(
        "Ethereal bot starting | ticker=%s dry_run=%s live=%s poll=%ds",
        config.ticker,
        config.dry_run,
        config.live_trading,
        config.poll_interval_seconds,
    )

    try:
        strategy.initialize()
    except Exception as exc:
        logger.error("Initialisation failed: %s", exc)
        sys.exit(1)

    logger.info("Initialised — entering polling loop")

    while not stop_event.is_set():
        t0 = time.monotonic()
        try:
            strategy.tick()
        except Exception as exc:
            logger.error("Tick error (continuing): %s", exc, exc_info=True)

        elapsed = time.monotonic() - t0
        sleep_for = max(0.0, config.poll_interval_seconds - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass

    logger.info("Stop signal received — shutting down")
    client.close()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
