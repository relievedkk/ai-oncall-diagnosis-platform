from types import SimpleNamespace

import pytest

from app.agent.mcp_client import load_mcp_tools_safe


class PartiallyAvailableMCPClient:
    connections = {"cls": {}, "monitor": {}}

    async def get_tools(self, *, server_name: str | None = None):
        if server_name is None:
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ConnectionError("CLS endpoint refused the connection")],
            )
        if server_name == "cls":
            raise ConnectionError("CLS endpoint refused the connection")
        return [SimpleNamespace(name="query_cpu_metrics")]


@pytest.mark.asyncio
async def test_load_mcp_tools_safe_keeps_monitor_without_loading_private_cls() -> None:
    tools, warning = await load_mcp_tools_safe(PartiallyAvailableMCPClient())  # type: ignore[arg-type]

    assert [tool.name for tool in tools] == ["query_cpu_metrics"]
    assert warning is None


@pytest.mark.asyncio
async def test_agent_tool_loading_rejects_raw_cls_and_unapproved_monitor_tools():
    class Client:
        connections = {"cls": {}, "monitor": {}, "other": {}}

        async def get_tools(self, *, server_name=None):
            if server_name != "monitor":
                raise AssertionError("Private or untrusted servers must not be discovered")
            return [
                SimpleNamespace(name=name)
                for name in (
                    "query_service_metric",
                    "query_cpu_metrics",
                    "query_memory_metrics",
                    "SearchLog",
                    "DescribeWebCallbacks",
                    "delete_metric",
                    "search_log",
                )
            ]

    tools, warning = await load_mcp_tools_safe(Client())
    assert [tool.name for tool in tools] == [
        "query_service_metric",
        "query_cpu_metrics",
        "query_memory_metrics",
    ]
    assert warning is None


@pytest.mark.asyncio
async def test_agent_tool_loading_fails_closed_without_server_provenance():
    class Client:
        async def get_tools(self):
            return [SimpleNamespace(name="query_cpu_metrics"), SimpleNamespace(name="SearchLog")]

    tools, warning = await load_mcp_tools_safe(Client())
    assert tools == []
    assert warning


@pytest.mark.asyncio
async def test_agent_tool_loading_reports_monitor_failure():
    class Client:
        connections = {"monitor": {}}

        async def get_tools(self, *, server_name):
            raise ConnectionError("monitor unavailable")

    tools, warning = await load_mcp_tools_safe(Client())
    assert tools == []
    assert "[monitor] ConnectionError: monitor unavailable" in warning


@pytest.mark.asyncio
async def test_agent_tool_loading_propagates_cancellation():
    import asyncio

    class Client:
        connections = {"monitor": {}}

        async def get_tools(self, *, server_name):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await load_mcp_tools_safe(Client())
