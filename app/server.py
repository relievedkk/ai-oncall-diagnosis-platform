"""Cross-platform ASGI entrypoint with psycopg-compatible Windows loop."""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.config import config
from app.core.maintenance import watch_operational_control


async def serve_managed(server) -> None:
    watcher = asyncio.create_task(watch_operational_control(server))
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def main() -> None:
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host=config.host,
            port=config.port,
            log_level="info",
            server_header=False,
            proxy_headers=False,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=30,
            limit_concurrency=100,
            # Native entrypoints have no supervising parent to respawn workers.
            # Keep serving until an explicit shutdown instead of exiting on traffic.
        )
    )
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(serve_managed(server))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
