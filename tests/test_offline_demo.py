"""Exercise the offline HTTP boundary, rather than production application startup."""

import asyncio
import contextlib
import http.client
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest


@contextlib.contextmanager
def serving(**kwargs):
    from app.demo.server import build_server

    server = build_server(port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, path, method="GET", headers=None, body=None):
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


CASES = [{"id": "cpu-load", "title": "CPU 合成负载", "source": "synthetic"}]


async def replay(case_id, *, strategy, mode, max_calls, timeout_seconds):
    assert (case_id, strategy, mode) == ("cpu-load", "baseline", "replay")
    assert 0 < max_calls <= 24 and 0 < timeout_seconds <= 60
    return {
        "mode": mode,
        "report": "<img src=x onerror=alert(1)>",
        "diagnosis_result": {"outcome": "insufficient_evidence", "summary": "待人工复核"},
        "trace": [{"tool": "metrics", "evidence_ref": "fixture-cpu"}],
        "semantic_accuracy_measured": False,
    }


def test_http_serves_cases_and_explicit_replay_result():
    # Removing route dispatch or passing live mode must break this boundary contract.
    with serving(cases=CASES, replay=replay) as server:
        status, headers, body = request(server, "/")
        assert status == 200 and b"/demo/app.js" in body
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        status, _, body = request(server, "/api/cases")
        assert status == 200
        assert json.loads(body)["cases"][0]["id"] == "cpu-load"
        status, _, body = request(server, "/api/replay?case_id=cpu-load&strategy=baseline")
        data = json.loads(body)
        assert status == 200
        assert data["diagnosis_result"]["outcome"] == "insufficient_evidence"
        assert data["trace"][0]["evidence_ref"] == "fixture-cpu"
        assert data["report"] == "<img src=x onerror=alert(1)>"
        assert data["mode"] == "replay" and data["semantic_accuracy_measured"] is False


def test_scenario_case_id_is_mapped_without_exposing_tools():
    scenario = {
        "case_id": "cpu-load",
        "title": "CPU load",
        "source": "synthetic",
        "target_service": "checkout-api",
        "tools": {"private_fixture": "not-for-list"},
    }
    with serving(cases=[scenario], replay=replay) as server:
        status, _, body = request(server, "/api/cases")
        case = json.loads(body)["cases"][0]
        assert status == 200
        assert case["id"] == "cpu-load"
        assert case["service"] == "checkout-api"
        assert "tools" not in case
        assert request(server, "/api/replay?case_id=cpu-load&strategy=baseline")[0] == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.1", "example.com", "localhost", "::"])
def test_refuses_non_loopback_or_hostname_binding(host):
    from app.demo.server import build_server

    with pytest.raises(ValueError, match="loopback"):
        build_server(host=host, port=0, cases=CASES, replay=replay)


@pytest.mark.parametrize(
    "path,status",
    [
        ("/../.env", 404),
        ("/%2e%2e/.env", 404),
        ("/demo/../../README.md", 404),
        ("/api/replay?case_id=../../.env", 400),
        ("/api/replay?case_id=https://example.com", 400),
        ("/api/replay?case_id=cpu-load&mode=live", 400),
        ("/api/replay?case_id=cpu-load&strategy=live", 400),
        ("/api/replay?case_id=cpu-load&case_id=cpu-load", 400),
        ("/api/replay", 400),
        ("/api/replay?case_id=unknown", 404),
    ],
)
def test_rejects_non_builtin_routes_and_replay_parameters(path, status):
    with serving(cases=CASES, replay=replay) as server:
        assert request(server, path)[0] == status


def test_rejects_remote_host_origin_and_http_bodies():
    with serving(cases=CASES, replay=replay) as server:
        assert request(server, "/api/cases", headers={"Host": "attacker.example"})[0] == 403
        assert (
            request(server, "/api/cases", headers={"Origin": "https://attacker.example"})[0] == 403
        )
        assert request(server, "/api/replay?case_id=cpu-load", body="x")[0] == 413
        assert request(server, "/api/replay?case_id=cpu-load", method="POST", body="x")[0] == 405
        assert request(server, "/api/cases?" + "x" * 2100)[0] == 414


def test_replay_timeout_returns_bounded_failure():
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    with serving(cases=CASES, replay=slow, timeout_seconds=0.03) as server:
        status, _, body = request(server, "/api/replay?case_id=cpu-load")
        assert status == 504
        assert json.loads(body)["error"] == "Replay timed out"


def test_concurrent_replay_is_rejected_without_starting_second_run():
    started, release = threading.Event(), threading.Event()

    async def waiting(*args, **kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"mode": "replay", "trace": [], "report": "", "diagnosis_result": {}}

    with serving(cases=CASES, replay=waiting) as server:
        first = threading.Thread(target=request, args=(server, "/api/replay?case_id=cpu-load"))
        first.start()
        try:
            assert started.wait(2)
            assert request(server, "/api/replay?case_id=cpu-load")[0] == 429
        finally:
            release.set()
            first.join(timeout=2)


def test_real_replay_runs_without_env_or_production_initialization(tmp_path):
    # Observe side effects in a clean process; importing ordinary Settings is allowed
    # once the real graph runs, but production startup/network acquisition is not.
    project = Path(__file__).resolve().parents[1]
    script = """
import asyncio, importlib.abc, ipaddress, socket, sys
forbidden = []
class RejectProduction(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'app.main', 'app.services.vector_embedding_service', 'app.services.vector_store_manager', 'app.db.database'}:
            forbidden.append('import:' + fullname)
            raise AssertionError('production import: ' + fullname)
sys.meta_path.insert(0, RejectProduction())
from app.demo.server import build_server
server = build_server(port=0)
assert 'app.config' not in sys.modules, 'demo startup loaded production configuration'
connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
def guard(original):
    def checked(sock, address):
        try:
            allowed = ipaddress.ip_address(address[0]).is_loopback
        except (ValueError, TypeError, IndexError):
            allowed = False
        if not allowed:
            forbidden.append('network:' + str(address))
            raise AssertionError('external network in replay')
        return original(sock, address)
    return checked
socket.socket.connect = guard(connect)
socket.socket.connect_ex = guard(connect_ex)
import app.agent.mcp_client as mcp
def reject_mcp(*args, **kwargs):
    forbidden.append('real MCP initialization')
    raise AssertionError('real MCP initialization in replay')
mcp._create_mcp_client = reject_mcp
try:
    assert len(server.cases) == 12
    result = asyncio.run(server.replay(server.cases[0]['id'], strategy='agent', mode='replay', max_calls=24, timeout_seconds=20))
    assert result['mode'] == 'replay'
    assert result['diagnosis_result']['outcome']
    assert isinstance(result['trace'], list)
    assert result['semantic_accuracy_measured'] is False
    assert forbidden == [], forbidden
    print('offline-real-replay-ok')
finally:
    server.server_close()
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(
            word in key.upper() for word in ("API_KEY", "SECRET", "TOKEN", "DATABASE_URL", "DSN")
        )
    }
    environment["PYTHONPATH"] = str(project)
    process = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "offline-real-replay-ok" in process.stdout


def test_real_cases_http_replay_exposes_no_private_expectations():
    with serving() as server:
        status, _, body = request(server, "/api/cases")
        data = json.loads(body)
        assert status == 200 and len(data["cases"]) == 12
        assert all(
            set(case)
            <= {"id", "title", "description", "source", "review_status", "task", "service"}
            for case in data["cases"]
        )
        query = urlencode({"case_id": data["cases"][0]["id"], "strategy": "agent"})
        status, _, body = request(server, "/api/replay?" + query)
        result = json.loads(body)
        assert status == 200
        assert result["mode"] == "replay"
        assert result["diagnosis_result"]["outcome"]
        assert result["semantic_accuracy_measured"] is False
