"""Concrete Windows native/Compose release adapter with bounded operations."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from oncall_ops.common import read_env
from oncall_ops.native_health import native_ready, open_http
from oncall_ops.releases import ReleaseError
from oncall_ops.runtime import ManagedRuntime

WORKERS = ['diagnosis-critical-worker', 'diagnosis-default-worker',
           'remediation-worker', 'outbox-publisher']


class ReleaseDriver:
    def __init__(self, root, state, *, python, config_root, data_root, docker='docker',
                 uv='uv', project_name=None, api_url='http://127.0.0.1:9900',
                 auth_token_env='API_KEY', runner=None, opener=None, runtime=None,
                 smoke_timeout=30, poll=0.5, protocol_check=None):
        self.root, self.state = Path(root).resolve(), Path(state).resolve()
        self.config_root, self.data_root = Path(config_root).resolve(), Path(data_root).resolve()
        self.python, self.docker, self.uv = str(python), str(docker), str(uv)
        self.project_name, self.api_url = project_name, api_url.rstrip('/')
        self.auth_token_env = auth_token_env
        self.runner, self.opener = runner or subprocess.run, opener or open_http
        self.protocol_check = protocol_check
        self.runtime = runtime or ManagedRuntime(self.state / 'runtime')
        self.smoke_timeout, self.poll = smoke_timeout, poll
        self._pause_snapshot = None
        self._candidate_guard = None

    def _env(self):
        env_file = self.config_root / '.env'
        values = read_env(env_file) if env_file.exists() else {}
        return {**values, **os.environ, 'DATA_ROOT': str(self.data_root),
                'CONFIG_ROOT': str(self.config_root),
                'ONCALL_DATA_ROOT': str(self.data_root), 'ONCALL_CONFIG_ROOT': str(self.config_root),
                'ALLOWED_INDEX_ROOTS': f'{self.data_root / "uploads"},{self.root / "aiops-docs"}',
                'SHARED_MODEL_QUOTA_ENABLED': 'true', 'PYTHONUTF8': '1'}

    def _run(self, command, *, cwd=None, env=None, timeout=120):
        try:
            result = self.runner(list(map(str, command)), cwd=cwd or self.root,
                                 env=env or self._env(), timeout=timeout, shell=False,
                                 capture_output=True, text=True, encoding='utf-8', errors='replace',
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            raise ReleaseError('Release subprocess failed or timed out') from None
        if result.returncode:
            # stdout/stderr may contain expanded Compose credentials: never echo it.
            raise ReleaseError('Release subprocess returned a failing exit code')
        return result.stdout.strip()

    def _compose(self):
        command = [self.docker, 'compose', '--project-directory', str(self.root),
                   '-f', str(self.root / 'vector-database.yml')]
        env_file = self.config_root / '.env'
        if env_file.exists():
            command += ['--env-file', str(env_file)]
        if self.project_name:
            command += ['-p', self.project_name]
        return command

    def schema(self):
        output = self._run([self.python, '-m', 'oncall_ops.release_smoke', '--schema'], timeout=30)
        try:
            value = json.loads(output)['schema']
            if not isinstance(value, str) or not value:
                raise ValueError('Missing schema')
            return value
        except (ValueError, KeyError, TypeError):
            raise ReleaseError('Database schema probe returned invalid data') from None

    def prepare(self, path, manifest):
        path = Path(path).resolve()
        for shared in (self.data_root, self.config_root):
            if shared == path or path in shared.parents:
                raise ReleaseError('Shared data/config roots must be outside release directories')
        env = {**self._env(), 'UV_PROJECT_ENVIRONMENT': str(path / '.venv')}
        self._run([self.uv, 'sync', '--frozen', '--no-dev', '--python', self.python],
                  cwd=path, env=env, timeout=1800)
        # Tags are unique build handles; runtime always receives the immutable image ID.
        tag = f'oncall-release:{uuid.uuid4().hex}'
        self._run([self.docker, 'build', '-f', str(path / 'Dockerfile.worker'),
                   '-t', tag, str(path)], cwd=path, timeout=1800)
        image = self._run([self.docker, 'image', 'inspect', '--format', '{{.Id}}', tag])
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise ReleaseError('Worker image inspect did not return an immutable image ID')
        executable = path / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        return {'python': str(executable), 'worker_image': image}

    def pause(self):
        self._pause_snapshot = None
        native = self.runtime._supervisor()
        ids = self._run([*self._compose(), 'ps', '-aq', *WORKERS]).split()
        workers = json.loads(self._run([self.docker, 'inspect', *ids])) if ids else []
        if {item['Id'] for item in workers} != set(ids):
            raise ReleaseError('Cannot snapshot original Worker container identities')
        # Complete the snapshot before mutating maintenance or stopping services.
        self._pause_snapshot = {
            'run_id': native['run_id'], 'maintenance': native['maintenance'],
            'paused': native['paused'],
            'native_running': [name for name, service in native['services'].items()
                               if service['status'] == 'running'],
            'workers': {item['Id']: item['Image'] for item in workers if item['State']['Running']},
        }
        self.runtime.run_id = native['run_id']
        self.runtime.pause()
        self._run([*self._compose(), 'stop', '--timeout', '40', *WORKERS], timeout=180)
        native = self.runtime._supervisor()
        if (native['run_id'] != self.runtime.run_id or not native.get('maintenance')
                or not native.get('paused') or not native.get('pause_safe')):
            raise ReleaseError('Initial release drain did not produce a safe paused runtime')
        self._candidate_guard = {
            'run_id': native['run_id'], 'admission_epoch': native.get('admission_epoch', 0),
            'candidate_image': None,
        }

    def recover_pause_failure(self):
        snapshot = self._pause_snapshot
        if snapshot is None:
            return  # Snapshot discovery failed before any maintenance mutation.
        try:
            if self.runtime._supervisor()['run_id'] != snapshot['run_id']:
                raise ReleaseError('Managed supervisor changed during pause recovery')
            ids = list(snapshot['workers'])
            if ids:
                # Starting exact existing IDs preserves image/config/volume identity
                # and never starts a previously stopped or absent Compose service.
                self._run([self.docker, 'start', *ids], timeout=180)
                workers = json.loads(self._run([self.docker, 'inspect', *ids]))
                if ({item['Id'] for item in workers} != set(ids)
                        or any(not item['State']['Running']
                               or item['Image'] != snapshot['workers'][item['Id']] for item in workers)):
                    raise ReleaseError('Original Worker state was not restored')
            self.runtime._intent(maintenance=snapshot['maintenance'], pause=snapshot['paused'], shutdown=False)
            self.runtime._wait(lambda value: value.get('paused') == snapshot['paused']
                               and value.get('maintenance') == snapshot['maintenance']
                               and (snapshot['paused'] or all(
                                   value['services'][name]['status'] == 'running'
                                   for name in snapshot['native_running'])))
        except Exception:
            try:
                if self.runtime._supervisor()['run_id'] == snapshot['run_id']:
                    self.runtime._intent(maintenance=True, pause=True, shutdown=False)
            except Exception:
                pass  # Never write control for another supervisor identity.
            raise ReleaseError('Original runtime restoration failed; manual recovery required') from None

    def start_version(self, record):
        if self._candidate_guard is None:
            raise ReleaseError('Release candidate lacks a proven initial drain')
        self._candidate_guard['candidate_image'] = record['worker_image']
        env = {**self._env(), 'ONCALL_WORKER_IMAGE': record['worker_image']}
        self._run([*self._compose(), 'up', '-d', '--no-deps', '--no-build', *WORKERS],
                  env=env, timeout=180)
        ids = self._run([*self._compose(), 'ps', '-q', *WORKERS], env=env).split()
        try:
            if len(ids) != len(WORKERS):
                raise ValueError('Missing worker')
            workers = json.loads(self._run([self.docker, 'inspect', *ids], env=env))
            if len(workers) != len(WORKERS) or any(
                    item['Image'] != record['worker_image'] or not item['State']['Running']
                    for item in workers):
                raise ValueError('Wrong worker image or stopped worker')
        except (ValueError, KeyError, TypeError):
            raise ReleaseError('Worker running state or immutable image verification failed') from None
        self._candidate_guard['worker_ids'] = ids
        # The supervisor refreshes the new pointer before launching native services.
        # Maintenance stays asserted until all health/business reads have passed.
        self.runtime._supervisor()
        self.runtime._intent(maintenance=True, pause=False, shutdown=False)
        self.runtime._wait(lambda value: not value.get('paused') and bool(value.get('services'))
                           and all(item['status'] == 'running' for item in value['services'].values()))

    def abort_candidate(self, previous):
        """Stop a failed candidate without relying on its API drain publisher."""
        guard = self._candidate_guard
        try:
            if not guard or not guard.get('candidate_image'):
                raise ValueError('missing proven candidate guard')
            native = self.runtime._supervisor()
            if (native['run_id'] != guard['run_id'] or self.runtime.run_id != guard['run_id']
                    or not native.get('maintenance')
                    or native.get('admission_epoch', 0) != guard['admission_epoch']):
                raise ValueError('candidate admission or supervisor ownership changed')

            ids = self._run([*self._compose(), 'ps', '-aq', *WORKERS]).split()
            if not guard.get('worker_ids') or set(ids) != set(guard['worker_ids']):
                raise ValueError('candidate Worker IDs changed')
            if ids:
                workers = json.loads(self._run([self.docker, 'inspect', *ids]))
                if ({item['Id'] for item in workers} != set(ids)
                        or any(item['Image'] != guard['candidate_image'] for item in workers)):
                    raise ValueError('candidate Worker identity changed')
                self._run([self.docker, 'stop', '--time', '40', *ids], timeout=180)
                stopped = json.loads(self._run([self.docker, 'inspect', *ids]))
                if ({item['Id'] for item in stopped} != set(ids)
                        or any(item['State']['Running'] for item in stopped)):
                    raise ValueError('candidate Worker did not stop')
                current = self._run([*self._compose(), 'ps', '-aq', *WORKERS]).split()
                if set(current) != set(ids):
                    raise ValueError('candidate Worker set changed during abort')

            output = self._run(
                [previous['python'], '-m', 'oncall_ops.release_smoke', '--rollback-safe'],
                cwd=Path(previous['path']), env=self._env(), timeout=30,
            )
            proof = json.loads(output)
            if proof.get('schema') != previous['schema'] or proof.get('durable_work') != 0:
                raise ValueError('database is incompatible or durable work remains')
            native = self.runtime._supervisor()
            if (native['run_id'] != guard['run_id'] or not native.get('maintenance')
                    or native.get('admission_epoch', 0) != guard['admission_epoch']):
                raise ValueError('candidate admission changed during database proof')
            self.runtime._intent(maintenance=True, pause=True, shutdown=False)
            self.runtime._wait(lambda value: value.get('run_id') == guard['run_id']
                               and value.get('maintenance') is True
                               and value.get('admission_epoch', 0) == guard['admission_epoch']
                               and value.get('paused') is True and value.get('pause_safe') is True)
        except Exception:
            raise ReleaseError('Failed candidate could not be aborted safely; maintenance retained') from None

    def smoke(self, record):
        environment = self._env()
        token = environment.get(self.auth_token_env)
        if not token:
            return False
        deadline = time.monotonic() + self.smoke_timeout
        while True:
            try:
                native = self.runtime._supervisor()
                healthy = (native['run_id'] == self.runtime.run_id
                           and self.runtime.run_id is not None
                           and all(native['services'][name]['status'] == 'running'
                                   and Path(native['services'][name]['cwd']).resolve()
                                   == Path(record['path']).resolve()
                                   and Path(native['services'][name]['executable']).resolve()
                                   == Path(record['python']).resolve()
                                   for name in ('api', 'cls', 'monitor')))
            except (RuntimeError, KeyError, TypeError, OSError):
                healthy = False
            # The shared checker confirms public run identities for API and both
            # MCP servers before any authenticated request can reach a listener.
            healthy = healthy and native_ready(
                native, {**environment, 'API_KEY': token}, self.api_url,
                expected_run_id=self.runtime.run_id, opener=self.opener,
                protocol_check=self.protocol_check,
            )
            if healthy:
                request = urllib.request.Request(self.api_url + '/api/aiops/jobs?limit=1',
                                                 headers={'X-API-Key': token}, method='GET')
                try:
                    with self.opener(request, timeout=3) as response:
                        healthy = healthy and response.status == 200
                        response.read(1024 * 1024)
                except (OSError, urllib.error.URLError, TimeoutError, ValueError, AttributeError):
                    healthy = False
            if healthy:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(self.poll, max(0, deadline - time.monotonic())))

    def resume(self):
        self.runtime.resume()
        self._candidate_guard = None
