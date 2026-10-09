"""AI 智能运维与 OnCall 诊断平台

基于 LangChain 的智能业务代理系统
"""

import asyncio
import os
import sys

# 系统代理（如 Clash）会拦截 httpx 对本地 MCP 服务的请求（返回 502），
# 需排除本地地址，确保 langchain-mcp-adapters 直连本地 MCP 服务
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

# psycopg's async driver cannot run on Windows' default ProactorEventLoop.
# Set the compatible policy before uvicorn, tests, or CLI entrypoints create a loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

__version__ = "1.0.0"

from app.utils import logger  # noqa: F401
