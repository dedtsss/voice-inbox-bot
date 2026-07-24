from __future__ import annotations

import logging

import uvicorn

from app.config import get_settings
from app.dashboard.app import create_dashboard_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    uvicorn.run(
        create_dashboard_app(settings),
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
