"""Identity-first native readiness; optional protocol libraries load lazily."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_local(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('Native health requires a local HTTP(S) URL without embedded credentials')


def open_http(request, timeout=3):
    """Refuse redirects and environment proxies before sending any credentials."""
    _validate_local(request.full_url)
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def protocol_http_client(**options):
    import httpx

    options.update(follow_redirects=False, trust_env=False)
    return httpx.AsyncClient(**options)


def _mcp_protocol_ready_in_process(url, token, timeout, client_factory):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def check():
        transport = StreamableHttpTransport(url, auth=token,
                                           httpx_client_factory=protocol_http_client)
        async with (client_factory or Client)(transport, timeout=timeout, init_timeout=timeout) as client:
            await client.list_tools()
        return True

    async def bounded():
        return await asyncio.wait_for(check(), timeout=timeout)

    return asyncio.run(bounded())


def mcp_protocol_ready(url, token=None, *, timeout=3, client_factory=None):
    """Initialize and list metadata only, with a bound around the full session."""
    _validate_local(url)
    if timeout <= 0:
        return False
    try:
        if client_factory is not None:
            return _mcp_protocol_ready_in_process(url, token, timeout, client_factory)
        environment = {**os.environ, 'ONCALL_MCP_PROBE_URL': url,
                       'ONCALL_MCP_PROBE_TIMEOUT': str(timeout)}
        if token:
            environment['ONCALL_MCP_PROBE_TOKEN'] = token
        options = {'env': environment, 'capture_output': True, 'text': True,
                   'timeout': timeout, 'check': False}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [sys.executable, '-m', 'oncall_ops.native_health', '--probe-mcp'], **options)
        return result.returncode == 0
    except Exception:
        # Protocol failures can wrap private transport errors in exception groups.
        # Return only a public boolean and never stringify those errors.
        return False


def _probe_main():
    if sys.argv[1:] != ['--probe-mcp']:
        return None
    url = os.environ.pop('ONCALL_MCP_PROBE_URL', '')
    token = os.environ.pop('ONCALL_MCP_PROBE_TOKEN', None)
    try:
        timeout = float(os.environ.pop('ONCALL_MCP_PROBE_TIMEOUT', '3'))
        _validate_local(url)
        return 0 if _mcp_protocol_ready_in_process(url, token, timeout, None) else 1
    except Exception:
        return 1


def _request_json(url, opener, timeout, key=None):
    _validate_local(url)
    request = Request(url, headers={'X-API-Key': key} if key else {}, method='GET')
    with opener(request, timeout=timeout) as response:
        if response.status != 200:
            return None
        return json.loads(response.read(65536))


def native_ready(status, environment, api_url, *, expected_run_id=None,
                 opener=None, protocol_check=None, timeout=3, deadline=None,
                 clock=None):
    """Require all owned children and identities before sending any credential.

    One call is finite: three public identity checks, one authenticated API
    readiness read, and two initialize/list-tools sessions (each bounded).
    Release smoke may retain maintenance admission while calling this helper.
    """
    opener = opener or open_http
    protocol_check = protocol_check or mcp_protocol_ready
    clock = clock or time.monotonic

    def remaining_timeout():
        if deadline is None:
            return timeout
        return min(timeout, max(0.0, deadline - clock()))

    try:
        run_id = status['run_id']
        if not run_id or (expected_run_id is not None and run_id != expected_run_id):
            return False
        if status.get('stopping') or status.get('paused'):
            return False
        for name in ('api', 'cls', 'monitor'):
            service = status['services'][name]
            if service['status'] != 'running' or not isinstance(service.get('pid'), int) or service['pid'] <= 0:
                return False
        official = bool(environment.get('TENCENTCLOUD_SECRET_ID') and environment.get('TENCENTCLOUD_SECRET_KEY'))
        cls_url = f'http://127.0.0.1:{3000 if official else 8003}'
        monitor_url = 'http://127.0.0.1:8004'
        identities = [(api_url.rstrip('/') + '/live', None),
                      (cls_url + '/ops/live', 'cls'), (monitor_url + '/ops/live', 'monitor')]
        for url, name in identities:
            operation_timeout = remaining_timeout()
            if operation_timeout <= 0:
                return False
            value = _request_json(url, opener, operation_timeout)
            if not isinstance(value, dict) or value.get('operations_run_id') != run_id:
                return False
            if name and value.get('service') != name:
                return False
        key = environment.get('API_KEY')
        operation_timeout = remaining_timeout()
        if (not key or operation_timeout <= 0
                or _request_json(api_url.rstrip('/') + '/ready', opener, operation_timeout, key) is None):
            return False
        operation_timeout = remaining_timeout()
        if operation_timeout <= 0 or not protocol_check(
                cls_url + '/mcp', environment.get('MCP_CLS_TOKEN'), timeout=operation_timeout):
            return False
        operation_timeout = remaining_timeout()
        if operation_timeout <= 0 or not protocol_check(
                monitor_url + '/mcp', None, timeout=operation_timeout):
            return False
        return remaining_timeout() > 0
    except Exception:
        return False


if __name__ == '__main__':
    raise SystemExit(_probe_main() or 0)
