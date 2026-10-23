"""The cloud MCP backend must never be reachable without its own credential."""

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP


def test_gateway_rejects_missing_wrong_and_web_api_credentials():
    from mcp_servers.cls_gateway import create_gateway

    backend = FastMCP("private-test-backend")
    gateway = create_gateway(backend=backend, token="c" * 64)
    with TestClient(gateway.http_app(path="/mcp")) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "c" * 64}):
            response = client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert response.status_code == 401
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer " + "c" * 64,
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert "serverInfo" in response.text


@pytest.mark.parametrize("token", ["", "short", "replace-with-random-64-character-token"])
def test_gateway_refuses_weak_configuration(token):
    from mcp_servers.cls_gateway import create_gateway

    with pytest.raises(ValueError, match="MCP_CLS_TOKEN"):
        create_gateway(backend=FastMCP("test"), token=token)


def test_clients_send_the_dedicated_mcp_credential(monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "mcp_cls_token", "c" * 64, raising=False)
    assert config.mcp_servers["cls"].get("headers") == {"Authorization": "Bearer " + "c" * 64}


def test_official_child_keeps_startup_logs_out_of_protocol_stdout(monkeypatch):
    import os
    import shutil
    import subprocess

    from mcp_servers.cls_gateway import official_transport

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for the official backend")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_ID", "test-id")
    monkeypatch.setenv("TENCENTCLOUD_SECRET_KEY", "test-key")
    transport = official_transport()
    result = subprocess.run(
        [
            node,
            "-e",
            'console.log("startup log"); process.stdout.write("{\\"jsonrpc\\":\\"2.0\\"}\\n")',
        ],
        env={**os.environ, **transport.env},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == '{"jsonrpc":"2.0"}\n'
    assert "startup log" in result.stderr


@pytest.mark.asyncio
async def test_gateway_rejects_non_ascii_token_without_exception():
    from mcp_servers.cls_gateway import CLSTokenVerifier

    assert await CLSTokenVerifier("c" * 64).verify_token("é") is None
