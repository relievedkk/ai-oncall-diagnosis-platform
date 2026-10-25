import json
import threading
import time
from types import SimpleNamespace

import pytest

from oncall_ops.cli import build_environment, main, refresh_release_services
from oncall_ops.supervisor import ServiceSpec


def test_managed_restart_keeps_active_release_worker_image_and_never_rebuilds(tmp_path, monkeypatch):
    from oncall_ops import cli, docker
    from oncall_ops.releases import package_release

    (tmp_path / 'app').mkdir()
    (tmp_path / 'app' / 'main.py').write_text('VERSION = 1')
    (tmp_path / '.env').write_text('AUTH_ENABLED=true')
    python = tmp_path / 'python.exe'
    python.touch()
    state = tmp_path / '.runtime' / 'operations'
    release = package_release(tmp_path, state / 'releases', 'v1', compatible_schemas=['schema-1'])
    record = {'version': 'v1', 'path': str(release), 'python': str(python), 'worker_image': 'sha256:' + 'a' * 64}
    (state / 'active-release.json').write_text(json.dumps(record))
    calls, environments = [], []

    class FakeDocker:
        def __init__(self, *_args, environment, **_kwargs):
            environments.append(environment.copy())

        def run(self, arguments, **_kwargs):
            calls.append(arguments)
            return ''

    monkeypatch.setattr(docker, 'DockerRunner', FakeDocker)
    monkeypatch.setattr(cli, '_supervise', lambda *args: 0)
    environment = {'AUTH_ENABLED': 'true', **dict.fromkeys((
        'API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'), 'a' * 64)}
    args = SimpleNamespace(python=str(python), docker='docker', foreground=True)
    assert cli._start(args, tmp_path, tmp_path, tmp_path, state, environment) == 0
    assert environments[0]['ONCALL_WORKER_IMAGE'] == record['worker_image']
    application_commands = [command for command in calls if any('vector-database.yml' in str(x) for x in command)]
    assert application_commands and all('--build' not in command for command in application_commands)
    assert any('--no-deps' in command and 'diagnosis-critical-worker' in command for command in application_commands)


def test_managed_environment_uses_stable_roots_and_shared_quota(tmp_path):
    (tmp_path / '.env').write_text('API_KEY=local-secret\nSHARED_MODEL_QUOTA_ENABLED=false\n')
    values = build_environment(tmp_path, tmp_path / 'data', tmp_path)
    assert values['SHARED_MODEL_QUOTA_ENABLED'] == 'true'
    assert values['ONCALL_DATA_ROOT'] == str((tmp_path / 'data').resolve())
    assert str((tmp_path / 'data' / 'uploads').resolve()) in values['ALLOWED_INDEX_ROOTS']


def test_resume_refreshes_native_release_without_changing_shared_data(tmp_path):
    release = tmp_path / 'v2'
    release.mkdir()
    pointer = {'version': 'v2', 'path': str(release), 'python': str(release / 'python.exe')}
    (tmp_path / 'active-release.json').write_text(json.dumps(pointer))
    services = [ServiceSpec('api', ['old-python', '-m', 'app.server'], tmp_path,
                            env={'ONCALL_DATA_ROOT': str(tmp_path / 'data')})]
    refresh_release_services(services, tmp_path)
    assert services[0].command[0] == pointer['python']
    assert services[0].cwd == release
    assert services[0].env['ONCALL_DATA_ROOT'] == str(tmp_path / 'data')


def test_status_without_supervisor_returns_nonzero_and_never_claims_running(tmp_path, capsys):
    assert main(['--root', str(tmp_path), 'status']) == 1
    assert 'unavailable' in capsys.readouterr().out


def test_start_missing_configuration_fails_before_any_launch(tmp_path, capsys):
    assert main(['--root', str(tmp_path), 'start']) == 2
    assert 'configuration' in capsys.readouterr().out.lower()


def test_stop_waits_for_completed_ack_and_lock_release_not_stopping_flag(tmp_path):
    from oncall_ops.cli import _wait_stopped
    from oncall_ops.common import atomic_json

    state = tmp_path / 'runtime'
    state.mkdir()
    initial = {'run_id': 'run-one', 'stopping': True,
               'services': {'api': {'pid': 12345, 'status': 'running'}}}
    atomic_json(state / 'supervisor.json', initial)
    complete = threading.Event()

    def finish():
        time.sleep(0.08)
        atomic_json(state / 'supervisor.json', {**initial, 'stop_complete': True,
                    'services': {'api': {'pid': None, 'status': 'stopped'}}})
        complete.set()

    thread = threading.Thread(target=finish)
    thread.start()
    try:
        _wait_stopped(state, 'run-one', timeout=1, poll=0.01, lock_free=lambda: complete.is_set())
        assert complete.is_set()
    finally:
        thread.join()


@pytest.mark.parametrize('failure', ['backoff', 'protocol', 'foreign'])
def test_start_never_reports_ready_for_failed_mcp_or_foreign_supervisor(tmp_path, monkeypatch, failure):
    from oncall_ops import cli, docker
    python = tmp_path / 'python.exe'
    python.touch()
    state = tmp_path / 'operations'
    launched_environment = {}

    def fresh_status(*_args):
        if not launched_environment:
            return None
        return {
            'run_id': 'foreign' if failure == 'foreign' else launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
            'pid': 999,
            'services': {
                name: {
                    'status': 'backoff' if failure == 'backoff' and name == 'monitor' else 'running',
                    'pid': 200 + index,
                }
                for index, name in enumerate(('api', 'cls', 'monitor'))
            },
        }

    monkeypatch.setattr(cli, '_fresh_status', fresh_status)
    monkeypatch.setattr(cli, '_probe_url', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli, 'native_ready', lambda *_args, **_kwargs: False, raising=False)
    clock = iter([0, 1, 121])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(clock, 121))
    monkeypatch.setattr(cli.time, 'sleep', lambda *_args: None)
    monkeypatch.setattr(docker, 'DockerRunner', lambda *_args, **_kwargs: SimpleNamespace(run=lambda *_a, **_k: ''))
    def launch(*_args, env, **_kwargs):
        launched_environment.update(env)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, 'Popen', launch)
    environment = {'AUTH_ENABLED': 'true', **dict.fromkeys((
        'API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'), 'a' * 64)}
    args = SimpleNamespace(python=str(python), docker='docker', foreground=False)
    with pytest.raises(RuntimeError, match='readiness timed out'):
        cli._start(args, tmp_path, tmp_path, tmp_path, state, environment)
    control = state / 'runtime' / 'control.json'
    if failure == 'foreign':
        assert not control.exists()
    else:
        assert json.loads(control.read_text()) == {
            'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
            'shutdown': True,
        }


def test_start_timeout_targets_a_previously_observed_owned_run(tmp_path, monkeypatch):
    from oncall_ops import cli, docker

    python = tmp_path / 'python.exe'
    python.touch()
    state = tmp_path / 'operations'
    launched_environment = {}
    reads = [0]

    def fresh_status(*_args):
        reads[0] += 1
        if reads[0] != 2:
            return None
        return {
            'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
            'pid': 999,
            'services': {
                name: {'status': 'running', 'pid': 200 + index}
                for index, name in enumerate(('api', 'cls', 'monitor'))
            },
        }

    def launch(*_args, env, **_kwargs):
        launched_environment.update(env)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(cli, '_fresh_status', fresh_status)
    monkeypatch.setattr(cli, 'native_ready', lambda *_args, **_kwargs: False)
    clock = iter([0, 1, 121])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(clock, 121))
    monkeypatch.setattr(cli.time, 'sleep', lambda *_args: None)
    monkeypatch.setattr(
        docker, 'DockerRunner',
        lambda *_args, **_kwargs: SimpleNamespace(run=lambda *_a, **_k: ''),
    )
    monkeypatch.setattr(cli.subprocess, 'Popen', launch)
    environment = {
        'AUTH_ENABLED': 'true',
        **dict.fromkeys(('API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'), 'a' * 64),
    }
    args = SimpleNamespace(python=str(python), docker='docker', foreground=False)

    with pytest.raises(RuntimeError, match='readiness timed out'):
        cli._start(args, tmp_path, tmp_path, tmp_path, state, environment)
    assert json.loads((state / 'runtime' / 'control.json').read_text()) == {
        'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
        'shutdown': True,
    }


def test_background_start_allows_cold_mcp_protocol_initialization(tmp_path, monkeypatch):
    from oncall_ops import cli, docker

    python = tmp_path / 'python.exe'
    python.touch()
    state = tmp_path / 'operations'
    launched_environment, launch_options = {}, {}
    probe_timeouts, expected_ids = [], []

    def fresh_status(*_args):
        if not launched_environment:
            return None
        return {
            'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
            'pid': 999,
            'services': {
                name: {'status': 'running', 'pid': 200 + index}
                for index, name in enumerate(('api', 'cls', 'monitor'))
            },
        }

    def cold_protocol_ready(*_args, timeout=3, expected_run_id=None, **_kwargs):
        probe_timeouts.append(timeout)
        expected_ids.append(expected_run_id)
        return timeout >= 10 and expected_run_id == launched_environment['ONCALL_SUPERVISOR_RUN_ID']

    def launch(*_args, env, **_kwargs):
        launched_environment.update(env)
        launch_options.update(_kwargs)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(cli, '_fresh_status', fresh_status)
    monkeypatch.setattr(cli, 'native_ready', cold_protocol_ready)
    clock = iter([0, 1, 2, 121])
    monkeypatch.setattr(cli.time, 'monotonic', lambda: next(clock, 121))
    monkeypatch.setattr(cli.time, 'sleep', lambda *_args: None)
    monkeypatch.setattr(
        docker,
        'DockerRunner',
        lambda *_args, **_kwargs: SimpleNamespace(run=lambda *_a, **_k: ''),
    )
    monkeypatch.setattr(
        cli.subprocess,
        'Popen',
        launch,
    )
    environment = {
        'AUTH_ENABLED': 'true',
        **dict.fromkeys(('API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'), 'a' * 64),
    }
    args = SimpleNamespace(python=str(python), docker='docker', foreground=False)

    assert cli._start(args, tmp_path, tmp_path, tmp_path, state, environment) == 0
    assert probe_timeouts == [15]
    assert expected_ids == [launched_environment['ONCALL_SUPERVISOR_RUN_ID']]
    assert len(expected_ids[0]) == 32
    if cli.os.name == 'nt':
        required = (
            cli.subprocess.DETACHED_PROCESS
            | cli.subprocess.CREATE_NEW_PROCESS_GROUP
            | cli.subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
        assert launch_options['creationflags'] & required == required


def test_background_start_rejects_readiness_that_finishes_after_deadline(tmp_path, monkeypatch):
    from oncall_ops import cli, docker

    python = tmp_path / 'python.exe'
    python.touch()
    state = tmp_path / 'operations'
    launched_environment = {}
    now = [0.0]

    def fresh_status(*_args):
        if not launched_environment:
            return None
        return {
            'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
            'pid': 999,
            'services': {
                name: {'status': 'running', 'pid': 300 + index}
                for index, name in enumerate(('api', 'cls', 'monitor'))
            },
        }

    def late_ready(*_args, deadline=None, **_kwargs):
        assert deadline == 120
        now[0] = 121
        return True

    def launch(*_args, env, **_kwargs):
        launched_environment.update(env)
        return SimpleNamespace(pid=123, poll=lambda: None)

    monkeypatch.setattr(cli, '_fresh_status', fresh_status)
    monkeypatch.setattr(cli, 'native_ready', late_ready)
    monkeypatch.setattr(cli.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda *_args: None)
    monkeypatch.setattr(
        docker,
        'DockerRunner',
        lambda *_args, **_kwargs: SimpleNamespace(run=lambda *_a, **_k: ''),
    )
    monkeypatch.setattr(cli.subprocess, 'Popen', launch)
    environment = {
        'AUTH_ENABLED': 'true',
        **dict.fromkeys(('API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'), 'a' * 64),
    }
    args = SimpleNamespace(python=str(python), docker='docker', foreground=False)

    with pytest.raises(RuntimeError, match='readiness timed out'):
        cli._start(args, tmp_path, tmp_path, tmp_path, state, environment)
    assert json.loads((state / 'runtime' / 'control.json').read_text()) == {
        'run_id': launched_environment['ONCALL_SUPERVISOR_RUN_ID'],
        'shutdown': True,
    }
