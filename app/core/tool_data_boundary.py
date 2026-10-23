"""Apply the same result boundary to tools invoked by the chat Agent."""

from langchain.agents.middleware.types import AgentMiddleware

from app.core.data_safety import sanitize_tool_message


class ToolDataBoundaryMiddleware(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        return sanitize_tool_message(await handler(request))

    def wrap_tool_call(self, request, handler):
        return sanitize_tool_message(handler(request))
