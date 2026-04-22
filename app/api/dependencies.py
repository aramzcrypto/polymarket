from __future__ import annotations

from typing import cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.runtime import BotRuntime

bearer = HTTPBearer(auto_error=False)


def runtime(request: Request) -> BotRuntime:
    return cast(BotRuntime, request.app.state.runtime)


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    token = request.app.state.runtime.settings.admin.token
    expected = token.get_secret_value() if token else None
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="admin token not configured"
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or credentials.credentials != expected
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")
