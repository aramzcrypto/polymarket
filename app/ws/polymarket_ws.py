from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
StatusHandler = Callable[[bool, str | None], Awaitable[None]]


class PolymarketWebSocket:
    def __init__(
        self,
        url: str,
        subscription: dict[str, Any],
        on_message: MessageHandler,
        on_status: StatusHandler,
        *,
        ping_interval_seconds: float = 10.0,
        reconnect_seconds: float = 2.0,
    ) -> None:
        self.url = url
        self.subscription = subscription
        self.on_message = on_message
        self.on_status = on_status
        self.ping_interval_seconds = ping_interval_seconds
        self.reconnect_seconds = reconnect_seconds
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=None) as ws:
                    await ws.send(json.dumps(self.subscription))
                    await self.on_status(True, None)
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for message in ws:
                            if isinstance(message, bytes):
                                message = message.decode("utf-8")
                            if message in {"PONG", "pong"}:
                                continue
                            payload = json.loads(message)
                            if isinstance(payload, list):
                                for item in payload:
                                    await self.on_message(item)
                            else:
                                await self.on_message(payload)
                    finally:
                        ping_task.cancel()
            except Exception as exc:  # pragma: no cover - exercised by integration tests with mocks
                logger.warning("websocket disconnected", extra={"url": self.url, "error": str(exc)})
                await self.on_status(False, str(exc))
                await asyncio.sleep(self.reconnect_seconds)

    async def _ping_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_interval_seconds)
            await ws.send("PING")
