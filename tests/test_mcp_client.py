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
async def test_load_mcp_tools_safe_keeps_tools_from_healthy_servers() -> None:
    tools, warning = await load_mcp_tools_safe(PartiallyAvailableMCPClient())  # type: ignore[arg-type]

    assert [tool.name for tool in tools] == ["query_cpu_metrics"]
    assert warning is not None
    assert "cls" in warning
    assert "ConnectionError: CLS endpoint refused the connection" in warning
    assert "monitor" not in warning
