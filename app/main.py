from __future__ import annotations

import os

import uvicorn

from app.config.settings import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.api.app:app",
        host=settings.admin.host,
        port=int(os.getenv("PORT", str(settings.admin.port))),
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
