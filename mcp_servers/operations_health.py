"""Public process identity only; this route never invokes an MCP tool."""

import os

from starlette.responses import JSONResponse


def register_operations_health(server, service: str):
    @server.custom_route('/ops/live', methods=['GET'], include_in_schema=False)
    async def operations_live(request):
        return JSONResponse({
            'operations_run_id': os.environ.get('ONCALL_OPERATIONS_RUN_ID'),
            'service': service,
        })
