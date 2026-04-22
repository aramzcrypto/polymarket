from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.core.runtime import BotRuntime


class FakeDB:
    async def check(self) -> bool:
        return True


class FakeREST:
    def __init__(self, blocked: bool) -> None:
        self.blocked = blocked

    async def clob_ok(self) -> bool:
        return True

    async def geoblock(self) -> dict[str, object]:
        return {"blocked": self.blocked, "country": "US", "region": "NY"}


@pytest.mark.asyncio
async def test_blocked_geoblock_fails_closed_for_live_mode() -> None:
    settings = Settings()
    settings.trading.live_trading = True
    settings.trading.live_trading_acknowledged = True
    runtime = BotRuntime(settings)
    runtime.db = FakeDB()  # type: ignore[assignment]
    runtime.rest = FakeREST(blocked=True)  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await runtime._startup_checks()
    assert runtime.state.kill_switch_enabled is True


@pytest.mark.asyncio
async def test_blocked_geoblock_sets_kill_switch_in_dry_run_without_raise() -> None:
    runtime = BotRuntime(Settings())
    runtime.db = FakeDB()  # type: ignore[assignment]
    runtime.rest = FakeREST(blocked=True)  # type: ignore[assignment]
    await runtime._startup_checks()
    assert runtime.state.kill_switch_enabled is True
