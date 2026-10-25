import asyncio
import io
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from oncall_ops import native_health


def status():
    return {'run_id': 'owned', 'services': {
        name: {'status': 'running', 'pid': 12000 + index}
        for index, name in enumerate(('api', 'cls', 'monitor'))}}


def identity_opener(calls, foreign=None):
    def open_request(request, timeout):
        calls.append(request)
        service = 'api' if ':9900/' in request.full_url else ('cls' if ':8003/' in request.full_url else 'monitor')
        response = io.BytesIO(json.dumps({
            'service': service, 'operations_run_id': 'foreign' if service == foreign else 'owned',
        }).encode())
        response.status = 200
        return response
    return open_request


@pytest.mark.parametrize('service', ['cls', 'monitor'])
def test_foreign_identity_receives_no_credentials_and_no_protocol_request(service):
    calls, protocols = [], []
    assert not native_health.native_ready(status(), {'API_KEY': 'api-secret', 'MCP_CLS_TOKEN': 'cls-secret'},
        'http://127.0.0.1:9900', opener=identity_opener(calls, foreign=service),
        protocol_check=lambda *args, **kwargs: protocols.append(args) or True)
    assert calls and all(not request.get_header('X-api-key') and not request.get_header('Authorization') for request in calls)
    assert protocols == []


@pytest.mark.parametrize('state', ['backoff', 'degraded', 'stopped'])
def test_native_ready_requires_all_owned_services_running_before_network(state):
    current = status()
    current['services']['monitor']['status'] = state
    calls = []
    assert not native_health.native_ready(current, {'API_KEY': 'test'}, 'http://127.0.0.1:9900',
        opener=identity_opener(calls), protocol_check=lambda *args, **kwargs: True)
    assert calls == []


def test_native_ready_checks_both_mcp_protocols_and_rejects_failed_protocol():
    calls, protocols = [], []
    def protocol(url, token=None, **kwargs):
        protocols.append((url, token, kwargs))
        return ':8004/' not in url
    assert not native_health.native_ready(status(), {'API_KEY': 'api-secret', 'MCP_CLS_TOKEN': 'cls-secret'},
        'http://127.0.0.1:9900', opener=identity_opener(calls), protocol_check=protocol)
    assert [item[0] for item in protocols] == ['http://127.0.0.1:8003/mcp', 'http://127.0.0.1:8004/mcp']
    assert calls[-1].get_header('X-api-key') == 'api-secret'
    assert protocols[0][1] == 'cls-secret'


def test_protocol_check_initializes_lists_only_and_bounds_slow_initialization():
    events = []
    class Client:
        def __init__(self, transport, **options):
            events.append(options)
        async def __aenter__(self):
            events.append('initialize')
            return self
        async def __aexit__(self, *args):
            events.append('close')
        async def list_tools(self):
            events.append('list_tools')
            return []
    assert native_health.mcp_protocol_ready('http://127.0.0.1:8003/mcp', 'secret', client_factory=Client)
    assert events[1:] == ['initialize', 'list_tools', 'close']
    class Slow(Client):
        async def __aenter__(self):
            await asyncio.sleep(2)
    before = time.monotonic()
    assert not native_health.mcp_protocol_ready('http://127.0.0.1:8003/mcp', timeout=0.03, client_factory=Slow)
    assert time.monotonic() - before < 0.5


def test_production_protocol_probe_has_an_outer_process_timeout(monkeypatch):
    calls = []

    def timeout(command, **options):
        calls.append((command, options))
        raise subprocess.TimeoutExpired(command, options['timeout'])

    monkeypatch.setattr(native_health.subprocess, 'run', timeout)
    assert not native_health.mcp_protocol_ready(
        'http://127.0.0.1:8003/mcp', 'private-secret', timeout=0.1)
    command, options = calls[0]
    assert options['timeout'] <= 0.1
    assert 'private-secret' not in command


def test_native_ready_caps_each_operation_by_absolute_deadline():
    calls, protocols = [], []
    now = [0.0]

    def opener(request, timeout):
        calls.append(timeout)
        now[0] += 3
        service = 'api' if ':9900/' in request.full_url else ('cls' if ':8003/' in request.full_url else 'monitor')
        response = io.BytesIO(json.dumps({'service': service, 'operations_run_id': 'owned'}).encode())
        response.status = 200
        return response

    def protocol(*_args, timeout, **_kwargs):
        protocols.append(timeout)
        now[0] += 3
        return True

    assert not native_health.native_ready(
        status(),
        {'API_KEY': 'api-secret', 'MCP_CLS_TOKEN': 'cls-secret'},
        'http://127.0.0.1:9900',
        opener=opener,
        protocol_check=protocol,
        timeout=15,
        deadline=10,
        clock=lambda: now[0],
    )
    assert calls == [10, 7, 4, 1]
    assert protocols == []


@pytest.mark.parametrize('module,service', [('cls_server', 'cls'), ('monitor_server', 'monitor'), ('cls_gateway', 'cls')])
def test_real_fastmcp_operations_identity_route_is_public_without_tools(module, service, monkeypatch):
    import importlib
    monkeypatch.setenv('ONCALL_OPERATIONS_RUN_ID', 'route-run')
    loaded = importlib.import_module('mcp_servers.' + module)
    server = loaded.create_gateway(backend=FastMCP('isolated-backend'), token='c' * 64) if module == 'cls_gateway' else loaded.mcp
    with TestClient(server.http_app(path='/mcp')) as client:
        response = client.get('/ops/live')
        assert response.status_code == 200
        assert response.json() == {'operations_run_id': 'route-run', 'service': service}


def test_protocol_http_factory_refuses_redirects_even_when_transport_requests_them():
    client = native_health.protocol_http_client(follow_redirects=True)
    assert client.follow_redirects is False
    asyncio.run(client.aclose())


def test_real_mcp_initialization_and_tool_listing_never_invokes_tools():
    from fastmcp import Client
    backend = FastMCP('isolated-protocol-health')
    @backend.tool()
    def must_not_execute() -> str:
        raise AssertionError('Health checks must never execute tools')
    def local_client(_transport, **options):
        return Client(backend, **options)
    assert native_health.mcp_protocol_ready('http://127.0.0.1:8003/mcp', client_factory=local_client)


def test_real_http_redirect_never_forwards_machine_credential():
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/capture-secret')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f'http://127.0.0.1:{server.server_port}/redirect', headers={'X-API-Key': 'private-test-key'})
        with pytest.raises(HTTPError) as failure:
            native_health.open_http(request, timeout=1)
        assert failure.value.code == 302
        failure.value.close()
        assert paths == ['/redirect']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
