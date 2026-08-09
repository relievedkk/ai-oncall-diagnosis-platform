"""SuperBizAgent Python 版本

基于 LangChain 的智能业务代理系统
"""

import os

# 系统代理（如 Clash）会拦截 httpx 对本地 MCP 服务的请求（返回 502），
# 需排除本地地址，确保 langchain-mcp-adapters 直连本地 MCP 服务
os.environ.setdefault('NO_PROXY', 'localhost,127.0.0.1,::1')

__version__ = "1.0.0"

from app.utils import logger  # noqa: F401
