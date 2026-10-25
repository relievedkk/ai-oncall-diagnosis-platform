import io
import json
import subprocess

import pytest

from oncall_ops.release_driver import ReleaseDriver
from oncall_ops.releases import ReleaseError


class Runtime:
    def __init__(self):
        self.run_id = 'owned'
        self.events = []
        self.maintenance = False
        self.paused = False

    def pause(self):
        self.events.append('pause')
        self.maintenance = True
        self.paused = True

    def _supervisor(self):
        return {'run_id': self.run_id, 'services': self.services,
                'maintenance': self.maintenance, 'paused': self.paused,
                'pause_safe': self.paused, 'admission_epoch': 0}

    def _intent(self, **values):
        self.events.append(values)
        self.maintenance = values.get('maintenance', self.maintenance)
        self.paused = values.get('pause', self.paused)

    def _wait(self, predicate):
        assert predicate(self._supervisor())

    def resume(self):
        self.events.append('resume')
        self.maintenance = False
        self.paused = False


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output = ''
        if 'inspect' in command and 'image' in command:
            output = 'sha256:' + 'a' * 64
        if 'ps' in command:
            output = 'container-1 container-2 container-3 container-4'
        if 'inspect' in command and 'image' not in command:
            output = json.dumps([{'Id': f'container-{index}', 'Image': 'sha256:abc',
                                  'State': {'Running': True}} for index in range(1, 5)])
        if '--schema' in command:
            output = json.dumps({'schema': 'schema-1'})
        return subprocess.CompletedProcess(command, 0, output, '')


def setup(tmp_path, monkeypatch, **kwargs):
    root, config, data = [tmp_path / name for name in ('source', 'config', 'data')]
    for path in (root, config, data):
        path.mkdir()
    (config / '.env').write_text('API_KEY=private-test-key\nPOSTGRES_PASSWORD=secret')
    monkeypatch.setenv('API_KEY', 'private-test-key')
    monkeypatch.delenv('TENCENTCLOUD_SECRET_ID', raising=False)
    monkeypatch.delenv('TENCENTCLOUD_SECRET_KEY', raising=False)
    runner, runtime = Runner(), Runtime()
    runtime.services = {name: {'status': 'running', 'cwd': str(root), 'executable': 'chosen-python'}
                        for name in ('api', 'cls', 'monitor')}
    for index, service in enumerate(runtime.services.values()):
        service['pid'] = 13000 + index
    kwargs.setdefault('protocol_check', lambda *_args, **_kwargs: True)
    driver = ReleaseDriver(root, data / 'ops', python='chosen-python', config_root=config,
                           data_root=data, runner=runner, runtime=runtime,
                           smoke_timeout=0.01, poll=0.001, **kwargs)
    return driver, runner, runtime


def test_prepare_installs_locked_version_environment_and_inspects_unique_worker(tmp_path, monkeypatch):
    driver, runner, _ = setup(tmp_path, monkeypatch)
    package = tmp_path / 'releases' / 'v1'
    package.mkdir(parents=True)
    record = driver.prepare(package, {'version': 'v1'})
    commands = [call[0] for call in runner.calls]
    assert commands[0] == ['uv', 'sync', '--frozen', '--no-dev', '--python', 'chosen-python']
    assert runner.calls[0][1]['cwd'] == package
    assert runner.calls[0][1]['env']['UV_PROJECT_ENVIRONMENT'] == str(package / '.venv')
    assert commands[1][:2] == ['docker', 'build']
    assert commands[2][:3] == ['docker', 'image', 'inspect']
    assert record['worker_image'] == 'sha256:' + 'a' * 64
    assert str(package / '.venv') in record['python']
    assert all(call[1]['timeout'] > 0 and call[1]['shell'] is False for call in runner.calls)


def test_schema_uses_selected_python_and_config_env_without_argv_secrets(tmp_path, monkeypatch):
    driver, runner, _ = setup(tmp_path, monkeypatch)
    assert driver.schema() == 'schema-1'
    command, options = runner.calls[0]
    assert command == ['chosen-python', '-m', 'oncall_ops.release_smoke', '--schema']
    assert options['env']['POSTGRES_PASSWORD'] == 'secret'
    assert options['env']['ALLOWED_INDEX_ROOTS'].split(',') == [
        str(driver.data_root / 'uploads'), str(driver.root / 'aiops-docs')]
    assert 'secret' not in repr(command)


def test_start_switches_workers_and_native_while_retaining_maintenance(tmp_path, monkeypatch):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    driver.pause()
    driver.start_version({'version': 'v2', 'path': '/release/v2', 'worker_image': 'sha256:abc'})
    command, options = next(call for call in runner.calls if 'up' in call[0])
    assert command[-8:] == ['up', '-d', '--no-deps', '--no-build',
                            'diagnosis-critical-worker', 'diagnosis-default-worker',
                            'remediation-worker', 'outbox-publisher']
    assert options['env']['ONCALL_WORKER_IMAGE'] == 'sha256:abc'
    assert options['env']['SHARED_MODEL_QUOTA_ENABLED'] == 'true'
    assert '--env-file' in command
    assert runtime.events[-1] == {'maintenance': True, 'pause': False, 'shutdown': False}
    driver.resume()
    assert runtime.events[-1] == 'resume'


def test_smoke_checks_authenticated_reads_only_and_rejects_401(tmp_path, monkeypatch):
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        service = 'cls' if ':8003/' in request.full_url else 'monitor'
        response = io.BytesIO(json.dumps({'operations_run_id': 'owned', 'service': service}).encode())
        response.status = 200
        return response

    driver, _, _ = setup(tmp_path, monkeypatch, opener=opener)
    record = {'version': 'v1', 'path': str(driver.root), 'python': 'chosen-python'}
    assert driver.smoke(record)
    assert [item[0].full_url for item in requests] == [
        'http://127.0.0.1:9900/live', 'http://127.0.0.1:8003/ops/live',
        'http://127.0.0.1:8004/ops/live', 'http://127.0.0.1:9900/ready',
        'http://127.0.0.1:9900/api/aiops/jobs?limit=1']
    assert all(not item[0].get_header('X-api-key') for item in requests[:3])
    assert requests[-1][0].get_header('X-api-key') == 'private-test-key'
    assert all(item[0].get_method() == 'GET' for item in requests)
    driver.opener = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('private-test-key'))
    assert not driver.smoke(record)


def test_command_error_does_not_echo_secret_output(tmp_path, monkeypatch):
    driver, _, _ = setup(tmp_path, monkeypatch)
    driver.runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, 'private-test-key', 'secret')
    with pytest.raises(ReleaseError) as error:
        driver.schema()
    assert 'secret' not in str(error.value) and 'private-test-key' not in str(error.value)


def test_start_rejects_workers_that_are_not_running_same_image(tmp_path, monkeypatch):
    driver, runner, _ = setup(tmp_path, monkeypatch)
    driver.pause()
    original = runner.__call__

    def mismatch(command, **kwargs):
        if 'ps' in command:
            return subprocess.CompletedProcess(command, 0, 'container-1', '')
        if 'inspect' in command and 'image' not in command:
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {'Image': 'sha256:wrong', 'State': {'Running': True}}] * 4), '')
        return original(command, **kwargs)

    driver.runner = mismatch
    with pytest.raises(ReleaseError, match='Worker'):
        driver.start_version({'worker_image': 'sha256:abc'})


def test_smoke_rejects_old_native_service_even_when_all_http_reads_are_200(tmp_path, monkeypatch):
    def opener(request, timeout):
        response = io.BytesIO(b'{"operations_run_id":"old-supervisor"}')
        response.status = 200
        return response

    driver, _, runtime = setup(tmp_path, monkeypatch, opener=opener)
    record = {'version': 'v2', 'path': str(driver.root), 'python': 'chosen-python'}
    assert not driver.smoke(record)
    runtime.services['api']['cwd'] = str(tmp_path / 'old-release')
    assert not driver.smoke(record)


def test_smoke_rejects_failed_mcp_protocol_and_never_sends_key_to_foreign_listener(tmp_path, monkeypatch):
    requests = []
    foreign = False
    def opener(request, timeout):
        requests.append(request)
        service = 'cls' if ':8003/' in request.full_url else 'monitor'
        response = io.BytesIO(json.dumps({'operations_run_id': 'foreign' if foreign else 'owned', 'service': service}).encode())
        response.status = 200
        return response
    driver, _, _ = setup(tmp_path, monkeypatch, opener=opener, protocol_check=lambda *_args, **_kwargs: False)
    record = {'version': 'v2', 'path': str(driver.root), 'python': 'chosen-python'}
    assert not driver.smoke(record)
    requests.clear()
    foreign = True
    assert not driver.smoke(record)
    assert requests and all(not request.get_header('X-api-key') for request in requests)


@pytest.mark.parametrize('failure_stage', ['drain', 'worker_stop'])
def test_pause_recovery_starts_only_original_running_container_ids(tmp_path, monkeypatch, failure_stage):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    original = runner.__call__
    running_ids = {'container-1', 'container-2'}

    def mixed_workers(command, **kwargs):
        if 'inspect' in command and 'image' not in command:
            output = json.dumps([{'Id': identity, 'Image': f'sha256:old{identity[-1]}',
                                  'State': {'Running': identity in running_ids}}
                                 for identity in command[2:]])
            runner.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, output, '')
        if 'start' in command:
            running_ids.update(command[2:])
        if 'stop' in command and failure_stage == 'worker_stop':
            running_ids.discard('container-1')
            runner.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 1, '', '')
        return original(command, **kwargs)

    driver.runner = mixed_workers
    if failure_stage == 'drain':
        runtime.pause = lambda: (_ for _ in ()).throw(TimeoutError('drain'))
    with pytest.raises((TimeoutError, ReleaseError)):
        driver.pause()
    driver.recover_pause_failure()
    starts = [call[0] for call in runner.calls if 'start' in call[0]]
    assert starts == [['docker', 'start', 'container-1', 'container-2']]
    assert not any('up' in command for command, _ in runner.calls)
    assert runtime.events[-1] == {'maintenance': False, 'pause': False, 'shutdown': False}


def test_pause_recovery_restart_failure_keeps_maintenance_asserted(tmp_path, monkeypatch):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    original = runner.__call__

    def cannot_restart(command, **kwargs):
        if 'start' in command:
            return subprocess.CompletedProcess(command, 1, '', '')
        return original(command, **kwargs)

    driver.runner = cannot_restart
    runtime.pause = lambda: (_ for _ in ()).throw(TimeoutError('drain'))
    with pytest.raises(TimeoutError):
        driver.pause()
    with pytest.raises(ReleaseError, match='manual recovery'):
        driver.recover_pause_failure()
    assert runtime.events[-1] == {'maintenance': True, 'pause': True, 'shutdown': False}


def test_abort_candidate_requires_closed_admission_zero_durable_work_and_exact_worker_exit(
        tmp_path, monkeypatch):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    driver.pause()
    driver.start_version({'version': 'v2', 'path': '/release/v2',
                          'python': 'new-python', 'worker_image': 'sha256:abc'})
    previous = {'version': 'v1', 'path': str(driver.root),
                'python': 'old-python', 'schema': 'schema-1', 'worker_image': 'sha256:old'}
    original = runner.__call__
    stopped = set()

    def guarded(command, **kwargs):
        if '--rollback-safe' in command:
            runner.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0,
                                               json.dumps({'schema': 'schema-1', 'durable_work': 0}), '')
        if command[:2] == ['docker', 'stop']:
            stopped.update(command[4:])
        if 'inspect' in command and 'image' not in command:
            runner.calls.append((command, kwargs))
            output = json.dumps([{'Id': identity, 'Image': 'sha256:abc',
                                  'State': {'Running': identity not in stopped}}
                                 for identity in command[2:]])
            return subprocess.CompletedProcess(command, 0, output, '')
        return original(command, **kwargs)

    driver.runner = guarded
    driver.abort_candidate(previous)
    probe = next((command, options) for command, options in runner.calls
                 if '--rollback-safe' in command)
    assert probe[0][0] == 'old-python'
    assert probe[1]['cwd'] == driver.root
    assert set(stopped) == {'container-1', 'container-2', 'container-3', 'container-4'}
    assert runtime.events[-1] == {'maintenance': True, 'pause': True, 'shutdown': False}


@pytest.mark.parametrize('unsafe', ['owner', 'maintenance', 'durable', 'worker', 'worker_replaced'])
def test_abort_candidate_fails_closed_on_changed_owner_open_admission_work_or_worker(
        tmp_path, monkeypatch, unsafe):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    driver.pause()
    driver.start_version({'version': 'v2', 'path': '/release/v2',
                          'python': 'new-python', 'worker_image': 'sha256:abc'})
    previous = {'version': 'v1', 'path': str(driver.root),
                'python': 'old-python', 'schema': 'schema-1', 'worker_image': 'sha256:old'}
    original = runner.__call__
    stopped = set()

    if unsafe == 'owner':
        runtime.run_id = 'changed-owner'
    elif unsafe == 'maintenance':
        runtime.services = runtime.services
        base = runtime._supervisor
        runtime._supervisor = lambda: {**base(), 'maintenance': False}

    def unsafe_runner(command, **kwargs):
        if '--rollback-safe' in command:
            value = {'schema': 'schema-1', 'durable_work': 1 if unsafe == 'durable' else 0}
            return subprocess.CompletedProcess(command, 0, json.dumps(value), '')
        if 'inspect' in command and 'image' not in command and unsafe == 'worker':
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {'Id': identity, 'Image': 'sha256:foreign', 'State': {'Running': True}}
                for identity in command[2:]]), '')
        if 'ps' in command and unsafe == 'worker_replaced':
            return subprocess.CompletedProcess(command, 0, 'replacement-1\nreplacement-2\nreplacement-3\nreplacement-4\n', '')
        if command[:2] == ['docker', 'stop'] and unsafe == 'worker_replaced':
            stopped.update(command[4:])
            return subprocess.CompletedProcess(command, 0, '', '')
        if 'inspect' in command and 'image' not in command and unsafe == 'worker_replaced':
            return subprocess.CompletedProcess(command, 0, json.dumps([
                {'Id': identity, 'Image': 'sha256:abc', 'State': {'Running': identity not in stopped}}
                for identity in command[2:]]), '')
        return original(command, **kwargs)

    driver.runner = unsafe_runner
    with pytest.raises(ReleaseError, match='candidate'):
        driver.abort_candidate(previous)


@pytest.mark.parametrize(('maintenance', 'paused'), [(False, False), (True, False), (True, True)])
def test_pause_recovery_preserves_original_control_and_never_starts_stopped_workers(
        tmp_path, monkeypatch, maintenance, paused):
    driver, runner, runtime = setup(tmp_path, monkeypatch)
    native = {**runtime._supervisor(), 'maintenance': maintenance, 'paused': paused}
    runtime._supervisor = lambda: native
    runtime.pause = lambda: (_ for _ in ()).throw(TimeoutError('drain'))

    def wait(predicate):
        assert predicate(native)

    runtime._wait = wait
    original = runner.__call__

    def stopped_workers(command, **kwargs):
        if 'inspect' in command and 'image' not in command:
            runner.calls.append((command, kwargs))
            output = json.dumps([{'Id': identity, 'Image': 'sha256:old',
                                  'State': {'Running': False}} for identity in command[2:]])
            return subprocess.CompletedProcess(command, 0, output, '')
        return original(command, **kwargs)

    driver.runner = stopped_workers
    with pytest.raises(TimeoutError):
        driver.pause()
    driver.recover_pause_failure()
    assert not any('start' in command or 'up' in command for command, _ in runner.calls)
    assert runtime.events[-1] == {'maintenance': maintenance, 'pause': paused, 'shutdown': False}
