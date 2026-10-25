import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oncall_ops import probe


@pytest.fixture
def http_endpoint():
    class Handler(BaseHTTPRequestHandler):
        status = 503
        received_key = None

        def do_GET(self):
            Handler.received_key = self.headers.get("X-API-Key")
            if self.path == "/slow":
                time.sleep(0.3)
            self.send_response(Handler.status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", Handler
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_probe_failure_recovery_hysteresis_and_transition_events(tmp_path, http_endpoint):
    url, handler = http_endpoint
    config = {"checks": [{"name": "api-ready", "url": url + "/ready"}]}
    outputs = [probe.run_once(config, tmp_path) for _ in range(3)]
    assert [len(item["events"]) for item in outputs] == [0, 0, 1]
    assert outputs[-1]["events"][0]["status"] == "degraded"
    assert outputs[-1]["exit_code"] == 1
    assert probe.run_once(config, tmp_path)["events"] == []
    handler.status = 200
    assert probe.run_once(config, tmp_path)["events"] == []
    recovery = probe.run_once(config, tmp_path)
    assert recovery["healthy"] is True
    assert recovery["events"][0]["status"] == "healthy"
    assert recovery["exit_code"] == 0
    assert probe.run_once(config, tmp_path)["events"] == []
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["status"] for event in events] == ["degraded", "healthy"]
    persisted = json.loads((tmp_path / "probe.json").read_text())
    assert persisted["last_run"]
    assert persisted["checks"]["api-ready"]["consecutive_successes"] == 3
    assert not list(tmp_path.glob("*.tmp"))


def test_401_is_unhealthy_credentials_from_env_never_in_state(tmp_path, http_endpoint, monkeypatch):
    url, handler = http_endpoint
    handler.status = 401
    monkeypatch.setenv("PROBE_SECRET", "super-private-key")
    config = {"checks": [{"name": "ready", "url": url, "api_key_env": "PROBE_SECRET"}]}
    result = probe.run_once(config, tmp_path)
    assert not result["healthy"]
    assert result["checks"]["ready"]["error"] == "http_401"
    assert handler.received_key == "super-private-key"
    assert "super-private-key" not in json.dumps(result)
    assert "super-private-key" not in (tmp_path / "probe.json").read_text()


def test_probe_timeout_is_bounded(tmp_path, http_endpoint):
    url, _ = http_endpoint
    before = time.monotonic()
    result = probe.run_once({"timeout": 0.05, "checks": [{"name": "api", "url": url + "/slow"}]}, tmp_path)
    assert time.monotonic() - before < 0.25
    assert result["checks"]["api"]["ok"] is False
    assert result["checks"]["api"]["error"] in {"timeout", "connection_failed"}


def test_error_never_echoes_url_or_exception_secrets(tmp_path):
    result = probe.run_once({"checks": [{"name": "bad", "url": "http://user:private@127.0.0.1:1/?token=private"}]}, tmp_path)
    assert "private" not in json.dumps(result)
    assert result["checks"]["bad"]["error"] == "invalid_url"


def test_missing_auth_key_fails_closed_and_file_env_used(tmp_path, http_endpoint):
    url, handler = http_endpoint
    handler.status = 200
    config = {"checks": [{"name": "ready", "url": url, "api_key_env": "ABSENT_OPS_TEST_KEY"}]}
    assert probe.run_once(config, tmp_path / "state")["checks"]["ready"]["error"] == "missing_credential"
    env = tmp_path / "probe.env"
    env.write_text("ABSENT_OPS_TEST_KEY=file-secret\n")
    config["env_file"] = str(env)
    assert probe.run_once(config, tmp_path / "state")["checks"]["ready"]["ok"]
    assert handler.received_key == "file-secret"


def test_invalid_or_empty_configuration_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="check"):
        probe.run_once({"checks": []}, tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        probe.run_once({"timeout": 0, "checks": [{"name": "api", "url": "http://localhost"}]}, tmp_path)


def test_unavailable_http_service_faults_then_recovers_when_listener_starts(tmp_path, http_endpoint):
    _, handler = http_endpoint
    handler.status = 200
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = {"timeout": 0.1, "checks": [{"name": "api", "url": f"http://127.0.0.1:{port}/live"}]}
    for _ in range(2):
        assert probe.run_once(config, tmp_path)["events"] == []
    failed = probe.run_once(config, tmp_path)
    assert failed["events"][0]["status"] == "degraded"
    assert failed["checks"]["api"]["error"] in {"connection_failed", "timeout"}
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert probe.run_once(config, tmp_path)["events"] == []
        assert probe.run_once(config, tmp_path)["events"][0]["status"] == "healthy"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
