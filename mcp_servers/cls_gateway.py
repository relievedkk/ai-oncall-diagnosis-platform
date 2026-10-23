"""Authenticated HTTP gateway to the official CLS server over private stdio.

The official child never opens a TCP listener. Only this gateway is exposed to
Docker clients, using a dedicated credential independent from the Web API key.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import AccessToken, TokenVerifier

from app.config import config
from app.core.credentials import secrets_equal


class CLSTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets_equal(token, self._token):
            return None
        return AccessToken(token=token, client_id="oncall-cls", scopes=[])


def official_transport() -> StdioTransport:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "NODE_EXTRA_CA_CERTS",
            "TENCENTCLOUD_SECRET_ID",
            "TENCENTCLOUD_SECRET_KEY",
            "TENCENTCLOUD_REGION",
        }
    }
    # Native entrypoints load .env through Settings, not into os.environ.
    from dotenv import dotenv_values

    for key in ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_REGION"):
        value = os.environ.get(key) or dotenv_values(".env").get(key)
        if value:
            environment[key] = value
    if not all(
        environment.get(key) for key in ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY")
    ):
        raise ValueError("Official CLS requires Tencent Cloud credentials")
    environment["TRANSPORT"] = "stdio"
    environment["TZ"] = "Asia/Shanghai"
    preload = Path(__file__).with_name("cls_stdio_logging.cjs").resolve().as_posix()
    environment["NODE_OPTIONS"] = f'--require="{preload}"'
    if sys.platform == "win32":
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is required for the official CLS server")
        npx = Path(node).parent / "node_modules/npm/bin/npx-cli.js"
        if not npx.is_file():
            raise RuntimeError("The Node.js installation must include npm/npx")
        command, args = node, [str(npx), "-y", "cls-mcp-server@1.2.1"]
    else:
        command, args = "npx", ["-y", "cls-mcp-server@1.2.1"]
    return StdioTransport(command=command, args=args, env=environment, keep_alive=True)


def create_gateway(*, backend=None, token: str | None = None) -> FastMCP:
    token = config.mcp_cls_token if token is None else token
    if len(token) < 32 or token.startswith("replace-with-"):
        raise ValueError(
            "MCP_CLS_TOKEN must be an independent random secret of at least 32 characters"
        )
    return create_proxy(
        backend if backend is not None else official_transport(),
        name="Authenticated CLS",
        auth=CLSTokenVerifier(token),
    )


def main() -> None:
    create_gateway().run(
        transport="streamable-http", host=config.mcp_cls_gateway_host, port=3000, path="/mcp"
    )


if __name__ == "__main__":
    main()
