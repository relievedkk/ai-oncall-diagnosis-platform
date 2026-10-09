"""Cross-platform ASGI entrypoint with psycopg-compatible Windows loop."""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.config import config


def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=config.host,
            port=config.port,
            log_level="info",
        )
    )
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(server.serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
