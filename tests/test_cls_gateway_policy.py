"""Exercise catalog authorization at the MCP boundary without cloud access."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from app.services.service_catalog import ServiceCatalog
from mcp_servers import cls_gateway


@pytest.fixture
def protected_gateway(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "services": {
                    "orders": {"cls": {"region": "ap-guangzhou", "topic_id": "topic-orders"}},
                    "billing": {"cls": {"region": "ap-guangzhou", "topic_id": "topic-shared"}},
                    "billing-worker": {
                        "cls": {"region": "ap-guangzhou", "topic_id": "topic-shared"}
                    },
                    "missing-cls": {},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cls_gateway, "service_catalog", ServiceCatalog(str(path)), raising=False)
    calls = []
    backend = FastMCP("private-fake-cls")

    @backend.tool(name="SearchLog")
    def search(
        Region: str,
        TopicId: str,
        From: int,
        To: int,
        Query: str,
        Limit: int,
        Topics: list[str] | None = None,
        UseNewAnalysis: bool = False,
    ) -> dict:
        calls.append({"Region": Region, "TopicId": TopicId, "Query": Query, "Limit": Limit})
        return {"Response": {"Results": [{"LogJson": '{"service":"orders","level":"ERROR"}'}]}}

    @backend.tool(name="DescribeWebCallbacks")
    def callbacks() -> dict:
        calls.append("callbacks")
        return {"secret": "private callback"}

    @backend.tool(name="DescribeTopics")
    def topics() -> dict:
        calls.append("inventory")
        return {"topics": ["unrelated"]}

    return cls_gateway.create_gateway(backend=backend, token="c" * 64), calls


def valid_arguments():
    now = int(time.time() * 1000)
    return {
        "Region": "ap-guangzhou",
        "TopicId": "topic-orders",
        "From": now - 900_000,
        "To": now,
        "Limit": 20,
        "Query": 'service:"orders" AND (level:"ERROR" OR level:"FATAL" OR level:"CRITICAL")',
    }


@pytest.mark.asyncio
async def test_gateway_only_lists_catalog_search(protected_gateway):
    gateway, _ = protected_gateway
    async with Client(gateway) as client:
        assert [tool.name for tool in await client.list_tools()] == ["SearchLog"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["DescribeTopics", "DescribeWebCallbacks", "GetAlarmLog", "search_log"]
)
async def test_gateway_blocks_non_search_tools_even_when_called_directly(protected_gateway, name):
    gateway, calls = protected_gateway
    async with Client(gateway) as client:
        result = await client.call_tool(name, {}, raise_on_error=False)
    assert result.is_error
    assert calls == []


@pytest.mark.asyncio
async def test_gateway_allows_exact_catalog_query(protected_gateway):
    gateway, calls = protected_gateway
    async with Client(gateway) as client:
        result = await client.call_tool("SearchLog", valid_arguments())
    assert not result.is_error
    assert (
        result.data["Response"]["Results"][0]["LogJson"] == '{"service":"orders","level":"ERROR"}'
    )
    assert calls[0]["TopicId"] == "topic-orders"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"Region": "ap-beijing"},
        {"TopicId": "unregistered"},
        {"Query": "*"},
        {"Query": 'service:"orders" OR *'},
        {"Query": 'service:"orders" AND level:"INFO"'},
        {"Query": 'service:"billing" AND (level:"ERROR" OR level:"FATAL" OR level:"CRITICAL")'},
        {"Topics": ["topic-orders", "private"]},
        {"Topics": []},
        {"UseNewAnalysis": True},
        {"Context": "cursor"},
        {"Limit": 0},
        {"Limit": 101},
        {"Limit": True},
        {"Limit": "20"},
        {"Limit": 1.5},
        {"From": 0},
        {"From": -1},
        {"From": True},
        {"From": "1800000000000"},
        {"From": 1_800_000_000},
        {"To": 9_999_999_999_999},
        {"To": None},
    ],
)
async def test_gateway_rejects_authorization_and_argument_bypasses(protected_gateway, changes):
    gateway, calls = protected_gateway
    async with Client(gateway) as client:
        result = await client.call_tool(
            "SearchLog", {**valid_arguments(), **changes}, raise_on_error=False
        )
    assert result.is_error
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reversed", "empty", "too_wide", "too_old", "future"])
async def test_gateway_limits_millisecond_time_range(protected_gateway, mode):
    gateway, calls = protected_gateway
    args = valid_arguments()
    if mode == "reversed":
        args["From"] = args["To"] + 1
    elif mode == "empty":
        args["From"] = args["To"]
    elif mode == "too_wide":
        args["From"] = args["To"] - 86_400_001
    elif mode == "too_old":
        args["From"] -= 172_800_000
        args["To"] -= 172_800_000
    else:
        args["From"] += 86_400_000
        args["To"] += 86_400_000
    async with Client(gateway) as client:
        result = await client.call_tool("SearchLog", args, raise_on_error=False)
    assert result.is_error
    assert calls == []


@pytest.mark.asyncio
async def test_shared_topic_still_requires_its_own_catalog_service(protected_gateway):
    gateway, calls = protected_gateway
    args = valid_arguments()
    args["TopicId"] = "topic-shared"
    args["Query"] = (
        'service:"billing-worker" AND (level:"ERROR" OR level:"FATAL" OR level:"CRITICAL")'
    )
    async with Client(gateway) as client:
        result = await client.call_tool("SearchLog", args)
    assert not result.is_error
    assert calls[0]["Query"] == args["Query"]


def test_authenticated_http_call_enforces_policy_and_allows_catalog_search(protected_gateway):
    gateway, calls = protected_gateway
    headers = {
        "Authorization": "Bearer " + "c" * 64,
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(gateway.http_app(path="/mcp")) as client:
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "policy-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
        client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        listed = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
        )
        payload = next(
            json.loads(line[6:]) for line in listed.text.splitlines() if line.startswith("data: ")
        )
        assert [tool["name"] for tool in payload["result"]["tools"]] == ["SearchLog"]
        accepted = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "SearchLog",
                    "arguments": valid_arguments(),
                },
            },
        )
        assert accepted.status_code == 200
        payload = next(
            json.loads(line[6:]) for line in accepted.text.splitlines() if line.startswith("data: ")
        )
        assert not payload["result"]["isError"]
        denied = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "DescribeWebCallbacks",
                    "arguments": {},
                },
            },
        )
        payload = next(
            json.loads(line[6:]) for line in denied.text.splitlines() if line.startswith("data: ")
        )
        assert payload["result"]["isError"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_catalog_denies_all_searches(protected_gateway, monkeypatch):
    gateway, calls = protected_gateway
    monkeypatch.setattr(cls_gateway.service_catalog, "list_services", lambda: {})
    async with Client(gateway) as client:
        result = await client.call_tool("SearchLog", valid_arguments(), raise_on_error=False)
    assert result.is_error
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lookback,limit", [(60_000, 1), (86_400_000, 100)])
async def test_gateway_accepts_wrapper_query_boundaries(protected_gateway, lookback, limit):
    gateway, calls = protected_gateway
    args = valid_arguments()
    args.update({"From": args["To"] - lookback, "Limit": limit})
    async with Client(gateway) as client:
        result = await client.call_tool("SearchLog", args)
    assert not result.is_error
    assert calls[0]["Limit"] == limit
