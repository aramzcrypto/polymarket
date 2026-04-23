from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import DatabaseSettings

logger = logging.getLogger(__name__)

_SENTINEL_URL = "none"


def _is_configured(url: str) -> bool:
    return bool(url) and url.lower() not in (_SENTINEL_URL, "", "none", "null")


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._configured = _is_configured(settings.url)
        if self._configured:
            self.engine: AsyncEngine | None = create_async_engine(settings.url, echo=settings.echo)
            self.session_factory: async_sessionmaker[AsyncSession] | None = async_sessionmaker(
                self.engine, expire_on_commit=False
            )
        else:
            logger.warning(
                "No database URL configured — running without persistence. "
                "Set DATABASE__URL in .env to enable trade/fill logging."
            )
            self.engine = None
            self.session_factory = None

    @property
    def is_configured(self) -> bool:
        return self._configured

    async def session(self) -> AsyncIterator[AsyncSession]:
        if self.session_factory is None:
            raise RuntimeError("Database not configured")
        async with self.session_factory() as session:
            yield session

    async def check(self) -> bool:
        if not self._configured or self.session_factory is None:
            return True  # skip check when DB is not configured
        from sqlalchemy import text
        async with self.session_factory() as session:
            await session.execute(text("select 1"))
        return True

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
