"""Bounded standard-library HTTP surface for built-in replay cases only."""

import asyncio
import ipaddress
import json
import re
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ASSETS = Path(__file__).resolve().parents[2] / "static" / "demo"
ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/demo/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/demo/style.css": ("style.css", "text/css; charset=utf-8"),
}
CASE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class ReplayServer(ThreadingHTTPServer):
    """Limit HTTP workers and replay concurrency separately."""

    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, *, cases, replay, timeout_seconds):
        self.cases = [
            {
                "id": case.get("case_id", case.get("id", "")),
                "title": case.get("title", ""),
                "description": case.get("description", ""),
                "task": case.get("task", ""),
                "service": case.get("target_service", case.get("service", "")),
                "source": "synthetic",
                "review_status": "unreviewed",
            }
            for case in cases
        ]
        self.replay = replay
        self.timeout_seconds = timeout_seconds
        self.replay_slot = threading.BoundedSemaphore(1)
        self.worker_slots = threading.BoundedSemaphore(8)
        super().__init__(address, ReplayHandler)

    def process_request(self, request, client_address):
        request.settimeout(5)
        if not self.worker_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_slots.release()


class ReplayHandler(BaseHTTPRequestHandler):
    server_version = "OfflineReplay/1"

    def log_message(self, format, *args):
        # Case/model content and raw request URLs need not enter application logs.
        pass

    def send_payload(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > 1024 * 1024:
            status, body = 500, b'{"error":"Replay output exceeded limit"}'
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.close_connection = True
        self.send_payload(status, {"error": message})

    def do_POST(self):
        self.fail(405, "Only GET is supported")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
    do_HEAD = do_POST

    def do_GET(self):
        if len(self.path) > 2048:
            self.fail(414, "Request URL is too long")
            return
        if sum(len(key) + len(value) for key, value in self.headers.items()) > 8192:
            self.fail(431, "Request headers are too large")
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self.fail(413, "Request bodies are not accepted")
            return
        local_host, local_port = self.server.server_address[:2]
        host_literal = f"[{local_host}]" if ":" in local_host else local_host
        allowed_hosts = {f"{host_literal}:{local_port}", f"localhost:{local_port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self.fail(403, "A local Host header is required")
            return
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            self.fail(403, "Cross-origin requests are not accepted")
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.netloc or parsed.scheme or parsed.fragment:
                raise ValueError
            query = parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
            )
        except ValueError:
            self.fail(400, "Invalid request URL")
            return
        if parsed.path in ROUTES and not query:
            filename, mime = ROUTES[parsed.path]
            self.send_payload(200, (ASSETS / filename).read_bytes(), mime)
        elif parsed.path == "/api/cases" and not query:
            fields = {"id", "title", "description", "source", "review_status", "task", "service"}
            self.send_payload(
                200,
                {
                    "mode": "replay",
                    "source": "synthetic",
                    "semantic_accuracy_measured": False,
                    "cases": [
                        {key: value for key, value in case.items() if key in fields}
                        for case in self.server.cases
                    ],
                },
            )
        elif parsed.path == "/api/replay":
            self.run_replay(query)
        else:
            self.fail(404, "Route not found")

    def run_replay(self, query):
        if set(query) - {"case_id", "strategy"} or any(
            len(values) != 1 for values in query.values()
        ):
            self.fail(400, "Only case_id and strategy are supported")
            return
        case_id = query.get("case_id", [""])[0]
        strategy = query.get("strategy", ["agent"])[0]
        if not CASE_ID.fullmatch(case_id) or strategy not in {"agent", "baseline"}:
            self.fail(400, "Invalid case ID or strategy")
            return
        if case_id not in {case["id"] for case in self.server.cases}:
            self.fail(404, "Unknown built-in case")
            return
        if not self.server.replay_slot.acquire(blocking=False):
            self.fail(429, "A replay is already running")
            return

        async def execute():
            return await asyncio.wait_for(
                self.server.replay(
                    case_id,
                    strategy=strategy,
                    mode="replay",
                    max_calls=24,
                    timeout_seconds=self.server.timeout_seconds,
                ),
                timeout=self.server.timeout_seconds,
            )

        try:
            result = asyncio.run(execute())
            self.send_payload(
                200, {**result, "mode": "replay", "semantic_accuracy_measured": False}
            )
        except TimeoutError:
            self.fail(504, "Replay timed out")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.fail(500, "Replay failed; inspect the local case and environment")
        finally:
            self.server.replay_slot.release()


def build_server(
    port=9911, *, host="127.0.0.1", cases=None, replay: Callable | None = None, timeout_seconds=20
):
    """Bind only a loopback IP literal; injection keeps HTTP tests independent of models."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("host must be a loopback IP literal") from error
    if not address.is_loopback:
        raise ValueError("host must be a loopback IP literal")
    if not 0 <= port <= 65535 or not 0 < timeout_seconds <= 60:
        raise ValueError("Invalid port or replay timeout (maximum 60 seconds)")
    if cases is None:
        from app.evaluation.scenarios import load_cases

        cases = load_cases()
    if replay is None:
        from app.evaluation.comparison import run_case

        replay = run_case
    server_type = ReplayServer
    if address.version == 6:

        class IPv6ReplayServer(ReplayServer):
            address_family = socket.AF_INET6

        server_type = IPv6ReplayServer
    return server_type((host, port), cases=cases, replay=replay, timeout_seconds=timeout_seconds)
