"""Own child process handles, restart with bounds, and honor local control intent."""

import json
import os
import re

# Fixed argument arrays, shell=False; owned child handles only.
import subprocess  # nosec B404
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .common import atomic_json


class AlreadyRunning(RuntimeError):
    """The state directory already has a live lock owner."""


def lock_is_available(state: Path) -> bool:
    """Probe the same kernel lock without creating or adopting a process."""
    try:
        with (Path(state) / 'supervisor.lock').open('r+b') as handle:
            if os.name == 'nt':
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def _validate_health_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Liveness requires an HTTP(S) URL without embedded credentials")
    if parsed.path.rstrip("/") == "/ready":
        raise ValueError("Dependency readiness must not be used as process liveness")


@dataclass
class ServiceSpec:
    name: str
    command: list[str]
    cwd: Path
    health_url: str | None = None
    env: dict[str, str] | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name) or not self.command:
            raise ValueError("Service requires a safe name and nonempty command")
        self.cwd = Path(self.cwd).resolve()
        if self.health_url:
            _validate_health_url(self.health_url)


@dataclass
class _Service:
    spec: ServiceSpec
    process: object | None = None
    log: object | None = None
    next_start: float = 0
    failures: int = 0
    restarts: int = 0
    restart_times: deque = field(default_factory=deque)
    health_failures: int = 0
    next_health: float = 0
    status: str = "pending"
    error: str | None = None
    last_exit: int | None = None


def _http_alive(url, timeout):
    try:
        _validate_health_url(url)
        # HTTP(S) scheme and absence of embedded credentials validated above.
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:  # nosec B310
            return 200 <= response.status < 300
    except (OSError, ValueError):
        return False


class Supervisor:
    """One supervisor per stable state directory; no PID-based process adoption.

    ``start`` acquires the OS lock and starts services; ``tick`` performs one
    bounded inspection. ``run`` loops until stop intent. Injection hooks make
    restart behavior testable without changing system time or existing services.
    """

    def __init__(
        self, root: Path, state: Path, services: list[ServiceSpec], *,
        popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
        backoff=(1, 2, 4, 8, 16, 30), restart_limit=5, restart_window=300,
        health_check=_http_alive, health_interval=10, health_timeout=2,
        shutdown_timeout=10, terminate_timeout=5, poll_interval=0.5,
        run_id=None,
    ):
        self.root = Path(root).resolve()
        self.state = Path(state).resolve()
        self.services = list(services)
        if len({item.name for item in services}) != len(services):
            raise ValueError("Duplicate service name")
        if not backoff or min(backoff) < 0 or restart_limit < 1 or restart_window <= 0:
            raise ValueError("Invalid restart bounds")
        if min(health_interval, health_timeout, terminate_timeout, poll_interval) <= 0 or shutdown_timeout < 0:
            raise ValueError("Invalid supervisor timeout")
        self._entries = {item.name: _Service(item) for item in services}
        if run_id is not None and not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("Invalid supervisor run identity")
        self.run_id = run_id if run_id is not None else uuid.uuid4().hex
        self._popen, self._clock, self._sleep = popen, clock, sleep
        self.backoff, self.restart_limit, self.restart_window = backoff, restart_limit, restart_window
        self.health_check, self.health_interval, self.health_timeout = health_check, health_interval, health_timeout
        self.shutdown_timeout, self.terminate_timeout = shutdown_timeout, terminate_timeout
        self.poll_interval = poll_interval
        self._lock_file = None
        self._started = False
        self._stopping = False
        self.maintenance = False
        self.admission_epoch = 0
        self._maintenance_observed = False
        self.control_error = None
        self.paused = False
        self.pause_safe = False
        self.unclean_services = []

    def acquire(self):
        """Hold a kernel lock, automatically released on crash; never unlink it."""
        if self._lock_file is not None:
            return
        self.state.mkdir(parents=True, exist_ok=True)
        handle = (self.state / "supervisor.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning("Supervisor state directory is already locked") from None
        self._lock_file = handle

    def _release(self):
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def start(self):
        if self._started or self._stopping:
            return
        self.acquire()
        try:
            # A previous run's stop intent cannot control a new run.
            atomic_json(self.state / "control.json", {
                "run_id": self.run_id, "maintenance": False, "pause": False, "shutdown": False,
            })
            self._started = True
            self.tick(strict_state=True)
        except BaseException:
            self.stop()
            raise

    def _read_control(self):
        try:
            data = json.loads((self.state / "control.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid control")
            if data.get("run_id") != self.run_id:
                return {}
            self.control_error = None
            return data
        except (OSError, ValueError):
            # Fail closed: do not restart writers while control intent is unreadable.
            self.control_error = "control_unreadable"
            return {"maintenance": True}

    def _close_log(self, entry):
        if entry.log is not None:
            entry.log.close()
            entry.log = None

    def _launch(self, entry, now):
        logs = self.state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        entry.log = (logs / f"{entry.spec.name}.log").open("ab", buffering=0)
        environment = os.environ.copy()
        environment.update(entry.spec.env or {})
        environment.update({
            "ONCALL_OPERATIONS_STATE_ROOT": str(self.state),
            "ONCALL_OPERATIONS_RUN_ID": self.run_id,
        })
        options = {
            "cwd": str(entry.spec.cwd), "env": environment, "stdin": subprocess.DEVNULL,
            "stdout": entry.log, "stderr": subprocess.STDOUT, "shell": False,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options["startupinfo"] = startup
        if entry.failures and entry.status != "paused":
            entry.restart_times.append(now)
            entry.restarts += 1
        try:
            entry.process = self._popen(list(entry.spec.command), **options)
            entry.status, entry.error = "running", None
            entry.health_failures = 0
            entry.next_health = now + self.health_interval
        except OSError:
            self._close_log(entry)
            entry.error = "spawn_failed"
            self._schedule(entry, now)

    def _schedule(self, entry, now):
        entry.process = None
        self._close_log(entry)
        delay = self.backoff[min(entry.failures, len(self.backoff) - 1)]
        entry.failures += 1
        entry.next_start = now + delay
        entry.status = "backoff"
        self._prune(entry, now)
        if len(entry.restart_times) >= self.restart_limit:
            entry.status = "degraded"

    def _prune(self, entry, now):
        while entry.restart_times and now - entry.restart_times[0] >= self.restart_window:
            entry.restart_times.popleft()

    def _terminate(self, entry):
        process = entry.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.terminate_timeout)
            except subprocess.TimeoutExpired:
                if entry.spec.name not in self.unclean_services:
                    self.unclean_services.append(entry.spec.name)
                process.kill()
                process.wait(timeout=self.terminate_timeout)
        entry.last_exit = process.poll()

    @staticmethod
    def _is_api(entry):
        return entry.spec.name == "api" or (
            entry.spec.health_url and urlsplit(entry.spec.health_url).path.rstrip("/") == "/live"
        )

    def _stop_children(self, *, pause=False, graceful=True):
        """Attempt every owned handle even if one fails; retain unexited handles."""
        errors = []
        api_entries = [entry for entry in self._entries.values() if self._is_api(entry)]
        # MCP has no durable application work; terminate it immediately. API polls
        # the already-published control file to exit through its ASGI lifespan.
        for entry in self._entries.values():
            if not self._is_api(entry):
                try:
                    self._terminate(entry)
                except (OSError, subprocess.TimeoutExpired) as error:
                    errors.append(error)
        deadline = time.monotonic() + (self.shutdown_timeout if graceful else 0)
        while time.monotonic() < deadline:
            if all(entry.process is None or entry.process.poll() is not None for entry in api_entries):
                break
            self._sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        for entry in api_entries:
            if entry.process is not None and entry.process.poll() is None:
                if entry.spec.name not in self.unclean_services:
                    self.unclean_services.append(entry.spec.name)
            try:
                self._terminate(entry)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
        for entry in self._entries.values():
            if entry.process is None or entry.process.poll() is not None:
                entry.process = None
                entry.status = "paused" if pause else "stopped"
                entry.next_start = 0
                self._close_log(entry)
            else:
                entry.status = "stop_failed"
        self.paused = pause and all(entry.process is None for entry in self._entries.values())
        self.pause_safe = self.paused and not self.unclean_services
        return errors

    def tick(self, *, strict_state=False):
        if self._stopping:
            return
        if not self._started:
            self.start()
            return
        control = self._read_control()
        maintenance = bool(control.get("maintenance", False))
        if self._maintenance_observed and self.maintenance and not maintenance:
            self.admission_epoch += 1
        self.maintenance = maintenance
        self._maintenance_observed = True
        if control.get("shutdown"):
            self.stop()
            return
        if control.get("pause"):
            self.maintenance = True
            if not self.paused:
                errors = self._stop_children(pause=True)
                self._write_state(strict=strict_state)
                if errors:
                    raise errors[0]
            else:
                self._write_state(strict=strict_state)
            return
        resuming = self.paused
        if self.paused:
            self.paused = False
            self.pause_safe = False
            self.unclean_services = []
        now = self._clock()
        for entry in self._entries.values():
            process = entry.process
            if process is not None:
                code = process.poll()
                if code is not None:
                    entry.last_exit = code
                    self._schedule(entry, now)
                elif (entry.spec.health_url and now >= entry.next_health and not self.maintenance):
                    entry.next_health = now + self.health_interval
                    try:
                        healthy = self.health_check(entry.spec.health_url, self.health_timeout)
                    except (OSError, ValueError):
                        healthy = False
                    entry.health_failures = 0 if healthy else entry.health_failures + 1
                    if entry.health_failures >= 3:
                        entry.error = "liveness_failed"
                        self._terminate(entry)
                        self._schedule(entry, now)
            explicit_resume = resuming and entry.status == "paused"
            if entry.process is None and (not self.maintenance or explicit_resume) and now >= entry.next_start:
                self._prune(entry, now)
                if len(entry.restart_times) >= self.restart_limit:
                    entry.status = "degraded"
                else:
                    self._launch(entry, now)
        self._write_state(strict=strict_state)

    def snapshot(self):
        now = self._clock()
        return {
            "run_id": self.run_id, "pid": os.getpid(), "root": str(self.root),
            "last_run": datetime.now(UTC).isoformat(), "stopping": self._stopping,
            "maintenance": self.maintenance, "control_error": self.control_error,
            "admission_epoch": self.admission_epoch,
            "paused": self.paused, "pause_safe": self.pause_safe,
            "stop_complete": self._stopping and all(entry.process is None for entry in self._entries.values()),
            "unclean_services": list(self.unclean_services),
            "services": {
                name: {
                    "pid": entry.process.pid if entry.process is not None else None,
                    "status": entry.status, "restarts": entry.restarts,
                    "restart_window_count": len(entry.restart_times),
                    "next_start_in": max(0, entry.next_start - now),
                    "last_exit": entry.last_exit, "health_failures": entry.health_failures,
                    "error": entry.error, "executable": str(entry.spec.command[0]),
                    "cwd": str(entry.spec.cwd), "log": str(self.state / "logs" / f"{name}.log"),
                } for name, entry in self._entries.items()
            },
        }

    def _write_state(self, *, strict=False):
        try:
            atomic_json(self.state / "supervisor.json", self.snapshot())
        except PermissionError:
            if strict:
                raise

    def stop(self):
        if self._stopping and self._lock_file is None:
            return
        self._stopping = True
        if self._lock_file is None:
            return
        errors = []
        control_written = False
        try:
            atomic_json(self.state / "control.json", {
                "run_id": self.run_id, "maintenance": True, "pause": True, "shutdown": True,
            })
            control_written = True
        except OSError as error:
            errors.append(error)
        try:
            self.maintenance = True
            try:
                self._write_state(strict=True)
            except OSError as error:
                errors.append(error)
            errors.extend(self._stop_children(graceful=control_written))
            try:
                self._write_state(strict=True)
            except OSError as error:
                errors.append(error)
        finally:
            # Never permit another supervisor while an owned process still lives.
            if all(entry.process is None for entry in self._entries.values()):
                self._release()
        if errors:
            raise errors[0]

    def run(self):
        try:
            self.start()
            while not self._stopping:
                self.tick()
                if not self._stopping:
                    self._sleep(self.poll_interval)
        finally:
            self.stop()
