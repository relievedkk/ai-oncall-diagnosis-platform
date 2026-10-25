import json
import socket
import sys
import time

import pytest

from oncall_ops.common import atomic_json, read_env
from oncall_ops.supervisor import AlreadyRunning, ServiceSpec, Supervisor


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Child:
    counter = 40000

    def __init__(self):
        Child.counter += 1
        self.pid = Child.counter
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


def make_supervisor(tmp_path, **kwargs):
    children = []

    def spawn(*args, **options):
        child = Child()
        child.options = options
        children.append(child)
        return child

    clock = Clock()
    supervisor = Supervisor(
        tmp_path, tmp_path / "state",
        [ServiceSpec("api", [sys.executable, "-c", "pass"], tmp_path)],
        popen=spawn, clock=clock, shutdown_timeout=0, **kwargs,
    )
    return supervisor, children, clock


def test_atomic_json_and_environment_are_independent(tmp_path):
    destination = tmp_path / "nested" / "state.json"
    atomic_json(destination, {"text": "运行"})
    atomic_json(destination, {"complete": True})
    assert json.loads(destination.read_text()) == {"complete": True}
    assert list(destination.parent.iterdir()) == [destination]
    env = tmp_path / ".env"
    env.write_text('export KEY="secret # value"\nPLAIN=value # comment\nEMPTY=\n', encoding="utf-8")
    assert read_env(env) == {"KEY": "secret # value", "PLAIN": "value", "EMPTY": ""}


def test_atomic_json_retries_transient_windows_sharing_violation(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    real_replace = __import__("os").replace
    attempts = []

    def transient_replace(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError("simulated sharing violation")
        real_replace(source, target)

    monkeypatch.setattr("oncall_ops.common.os.replace", transient_replace)

    atomic_json(destination, {"complete": True})

    assert json.loads(destination.read_text()) == {"complete": True}
    assert len(attempts) == 3


def test_atomic_json_surfaces_persistent_permission_failure_after_bounded_retries(tmp_path, monkeypatch):
    attempts = []

    def denied_replace(*args):
        attempts.append(args)
        raise PermissionError("persistent denial")

    monkeypatch.setattr("oncall_ops.common.os.replace", denied_replace)

    with pytest.raises(PermissionError, match="persistent denial"):
        atomic_json(tmp_path / "state.json", {"complete": True})

    assert len(attempts) == 7


def test_supervisor_uses_valid_caller_supplied_run_identity(tmp_path):
    supervisor, _, _ = make_supervisor(tmp_path, run_id="a" * 32)
    assert supervisor.run_id == "a" * 32


def test_supervisor_rejects_invalid_caller_supplied_run_identity(tmp_path):
    with pytest.raises(ValueError, match="run identity"):
        make_supervisor(tmp_path, run_id="not-a-safe-run-id")


def test_owned_child_restart_backoff_and_control_identity(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path)
    try:
        supervisor.start()
        assert len(children) == 1
        assert children[0].options["env"]["ONCALL_OPERATIONS_RUN_ID"] == supervisor.run_id
        assert children[0].options["env"]["ONCALL_OPERATIONS_STATE_ROOT"] == str(supervisor.state)
        children[0].code = 1
        supervisor.tick()
        assert supervisor.snapshot()["services"]["api"]["next_start_in"] == 1
        clock.now = 0.9
        supervisor.tick()
        assert len(children) == 1
        clock.now = 1
        supervisor.tick()
        assert len(children) == 2
        atomic_json(supervisor.state / "control.json", {"run_id": "foreign", "shutdown": True})
        supervisor.tick()
        assert not children[-1].terminated
    finally:
        supervisor.stop()


def test_duplicate_lock_is_exclusive_and_released(tmp_path):
    first, _, _ = make_supervisor(tmp_path)
    second, _, _ = make_supervisor(tmp_path)
    first.start()
    try:
        with pytest.raises(AlreadyRunning):
            second.start()
    finally:
        first.stop()
    second.start()
    second.stop()


def test_foreign_listener_is_never_killed(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    supervisor, children, _ = make_supervisor(tmp_path)
    try:
        supervisor.start()
        children[0].code = 1
        supervisor.tick()
        supervisor.stop()
        listener.getsockname()
        assert listener.fileno() >= 0
    finally:
        listener.close()
        supervisor.stop()


def test_stop_intent_blocks_restart_and_pause_resumes(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path)
    supervisor.start()
    atomic_json(supervisor.state / "control.json", {"run_id": supervisor.run_id, "maintenance": True})
    children[0].code = 1
    supervisor.tick()
    clock.now = 100
    supervisor.tick()
    assert len(children) == 1
    atomic_json(supervisor.state / "control.json", {"run_id": supervisor.run_id, "maintenance": False})
    supervisor.tick()
    assert len(children) == 2
    children[1].code = 1
    atomic_json(supervisor.state / "control.json", {"run_id": supervisor.run_id, "shutdown": True})
    supervisor.tick()
    clock.now += 100
    supervisor.tick()
    assert len(children) == 2
    assert supervisor.snapshot()["stopping"] is True


def test_restart_budget_backoff_and_sliding_window(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path)
    try:
        supervisor.start()
        for delay in [1, 2, 4, 8, 16]:
            children[-1].code = 1
            supervisor.tick()
            assert supervisor.snapshot()["services"]["api"]["next_start_in"] == delay
            clock.now += delay
            supervisor.tick()
        assert len(children) == 6
        children[-1].code = 1
        supervisor.tick()
        assert supervisor.snapshot()["services"]["api"]["status"] == "degraded"
        clock.now = 32
        supervisor.tick()
        assert len(children) == 6
        clock.now = 332
        supervisor.tick()
        assert len(children) == 7
        children[-1].code = 1
        supervisor.tick()
        assert supervisor.snapshot()["services"]["api"]["next_start_in"] == 30
    finally:
        supervisor.stop()


def test_liveness_three_failures_and_readiness_rejected(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path, health_check=lambda url, timeout: False, health_interval=1)
    supervisor.services[0].health_url = "http://127.0.0.1/live"
    try:
        supervisor.start()
        for index in range(2):
            clock.now = index + 1
            supervisor.tick()
            assert not children[0].terminated
        clock.now = 3
        supervisor.tick()
        assert children[0].terminated
    finally:
        supervisor.stop()
    with pytest.raises(ValueError, match="readiness"):
        ServiceSpec("api", ["python"], tmp_path, health_url="http://127.0.0.1/ready")


def test_real_child_exit_is_restarted(tmp_path):
    counter = tmp_path / "starts.txt"
    code = (
        "import pathlib,time; p=pathlib.Path(__import__('sys').argv[1]); "
        "p.open('a').write('start\\n'); time.sleep(.05)"
    )
    supervisor = Supervisor(tmp_path, tmp_path / "state", [ServiceSpec(
        "synthetic", [sys.executable, "-c", code, str(counter)], tmp_path,
    )], backoff=(0.02,), shutdown_timeout=0)
    try:
        supervisor.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            supervisor.tick()
            if counter.exists() and len(counter.read_text().splitlines()) >= 2:
                break
            time.sleep(0.02)
        assert len(counter.read_text().splitlines()) >= 2
        assert supervisor.snapshot()["services"]["synthetic"]["restarts"] >= 1
    finally:
        supervisor.stop()


def test_pause_stops_owned_children_and_resumes_without_exiting_supervisor(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path)
    supervisor.start()
    children[0].code = 0  # API acknowledged pause and exited gracefully.
    atomic_json(supervisor.state / "control.json", {
        "run_id": supervisor.run_id, "maintenance": True, "pause": True,
    })
    supervisor.tick()
    assert supervisor.snapshot()["paused"] is True
    assert supervisor.snapshot()["pause_safe"] is True
    assert supervisor.snapshot()["stopping"] is False
    clock.now += 100
    supervisor.tick()
    assert len(children) == 1
    atomic_json(supervisor.state / "control.json", {
        "run_id": supervisor.run_id, "maintenance": False, "pause": False,
    })
    supervisor.tick()
    assert len(children) == 2
    assert supervisor.snapshot()["services"]["api"]["restarts"] == 0
    supervisor.stop()


def test_pause_requiring_api_termination_is_not_safe_for_backup(tmp_path):
    supervisor, children, _ = make_supervisor(tmp_path)
    try:
        supervisor.start()
        atomic_json(supervisor.state / "control.json", {
            "run_id": supervisor.run_id, "maintenance": True, "pause": True,
        })
        supervisor.tick()
        assert children[0].terminated
        assert supervisor.snapshot()["paused"] is True
        assert supervisor.snapshot()["pause_safe"] is False
        assert supervisor.snapshot()["unclean_services"] == ["api"]
    finally:
        supervisor.stop()


def test_release_resume_starts_paused_children_once_while_admission_stays_closed(tmp_path):
    supervisor, children, clock = make_supervisor(tmp_path)
    try:
        supervisor.start()
        children[0].code = 0
        atomic_json(supervisor.state / 'control.json', {
            'run_id': supervisor.run_id, 'maintenance': True, 'pause': True,
        })
        supervisor.tick()
        atomic_json(supervisor.state / 'control.json', {
            'run_id': supervisor.run_id, 'maintenance': True, 'pause': False,
        })
        supervisor.tick()
        assert len(children) == 2
        assert supervisor.snapshot()['maintenance'] is True
        children[-1].code = 1
        clock.now += 100
        supervisor.tick()
        assert len(children) == 2  # Explicit resume is permitted; crash loops remain stopped.
    finally:
        supervisor.stop()


def test_admission_epoch_records_any_maintenance_reopening(tmp_path):
    supervisor, children, _ = make_supervisor(tmp_path)
    try:
        supervisor.start()
        atomic_json(supervisor.state / 'control.json', {
            'run_id': supervisor.run_id, 'maintenance': True,
        })
        supervisor.tick()
        closed_epoch = supervisor.snapshot()['admission_epoch']
        atomic_json(supervisor.state / 'control.json', {
            'run_id': supervisor.run_id, 'maintenance': False,
        })
        supervisor.tick()
        assert supervisor.snapshot()['admission_epoch'] == closed_epoch + 1
    finally:
        supervisor.stop()


def test_stop_still_terminates_owned_children_when_status_write_fails(tmp_path, monkeypatch):
    supervisor, children, _ = make_supervisor(tmp_path)
    supervisor.start()

    def failed_write(*args):
        raise OSError("simulated disk failure")

    with monkeypatch.context() as context:
        context.setattr("oncall_ops.supervisor.atomic_json", failed_write)
        with pytest.raises(OSError):
            supervisor.stop()
    assert children[0].terminated
    replacement, _, _ = make_supervisor(tmp_path)
    replacement.start()
    replacement.stop()


def test_initial_status_publish_failure_stops_owned_children(tmp_path, monkeypatch):
    supervisor, children, _ = make_supervisor(tmp_path)
    real_atomic_json = atomic_json
    blocked = True

    def transient_status_failure(path, data):
        nonlocal blocked
        if path.name == "supervisor.json" and blocked:
            blocked = False
            raise PermissionError("simulated reader sharing conflict")
        real_atomic_json(path, data)

    monkeypatch.setattr("oncall_ops.supervisor.atomic_json", transient_status_failure)

    with pytest.raises(PermissionError, match="reader sharing conflict"):
        supervisor.start()
    assert len(children) == 1
    assert children[0].terminated


def test_periodic_status_publish_failure_retries_on_next_tick(tmp_path, monkeypatch):
    supervisor, children, _ = make_supervisor(tmp_path)
    supervisor.start()
    real_atomic_json = atomic_json
    blocked = True

    def transient_status_failure(path, data):
        nonlocal blocked
        if path.name == "supervisor.json" and blocked:
            blocked = False
            raise PermissionError("simulated reader sharing conflict")
        real_atomic_json(path, data)

    monkeypatch.setattr("oncall_ops.supervisor.atomic_json", transient_status_failure)

    try:
        supervisor.tick()
        assert children[0].poll() is None
        supervisor.tick()
        assert json.loads((supervisor.state / "supervisor.json").read_text())["run_id"] == supervisor.run_id
    finally:
        supervisor.stop()


@pytest.mark.parametrize("url", ["file:///windows/win.ini", "ftp://localhost/live", "http://user:secret@localhost/live"])
def test_liveness_rejects_non_http_urls_and_embedded_credentials(tmp_path, url):
    with pytest.raises(ValueError, match="HTTP"):
        ServiceSpec("api", [sys.executable], tmp_path, health_url=url)
