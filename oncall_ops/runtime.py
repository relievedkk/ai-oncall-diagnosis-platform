"""Filesystem-owned control of the managed local runtime; no PID adoption."""

import json
import time
from datetime import datetime
from pathlib import Path

from oncall_ops.common import atomic_json


class RuntimeControlError(RuntimeError):
    pass


class ManagedRuntime:
    def __init__(self, state: Path, *, timeout=120, poll=0.25):
        self.state, self.timeout, self.poll = Path(state).resolve(), timeout, poll
        self.run_id = None

    def _supervisor(self):
        try:
            value = json.loads((self.state / 'supervisor.json').read_text(encoding='utf-8'))
            age = time.time() - datetime.fromisoformat(value['last_run']).timestamp()
            if age > 15 or value.get('stopping'):
                raise ValueError('stale')
            if self.run_id is not None and value['run_id'] != self.run_id:
                raise ValueError('ownership changed')
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeControlError('Managed supervisor is unavailable or changed identity') from exc

    def _intent(self, **values):
        atomic_json(self.state / 'control.json', {'run_id': self.run_id, **values})

    def _wait(self, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            supervisor = self._supervisor()
            if predicate(supervisor):
                return
            time.sleep(self.poll)
        raise RuntimeControlError('Maintenance drain/pause timed out; no snapshot was taken')

    def pause(self):
        supervisor = self._supervisor()
        self.run_id = supervisor['run_id']
        if supervisor.get('paused') and supervisor.get('pause_safe'):
            self._intent(maintenance=True, pause=True, shutdown=False)
            return
        maintenance_started = time.time()
        self._intent(maintenance=True, pause=False, shutdown=False)

        def drained(_):
            try:
                state = json.loads((self.state / 'api-state.json').read_text(encoding='utf-8'))
                return (state['run_id'] == self.run_id and state.get('maintenance') is True
                        and state['active_requests'] == 0 and state['durable_work'] == 0
                        and time.time() - state['updated_at'] < 5
                        and time.time() - state['durable_checked_at'] < 6
                        and state['durable_checked_at'] >= maintenance_started)
            except (OSError, ValueError, KeyError, TypeError):
                return False

        self._wait(drained)
        self._intent(maintenance=True, pause=True, shutdown=False)
        self._wait(lambda value: value.get('paused') is True)
        if not self._supervisor().get('pause_safe'):
            raise RuntimeControlError('Native service required forced shutdown; backup aborted')

    def resume(self):
        if self.run_id is None:
            return
        self._supervisor()  # Never overwrite a newly started supervisor's intent.
        self._intent(maintenance=False, pause=False, shutdown=False)
        self._wait(lambda value: not value.get('paused') and bool(value.get('services')) and all(
            entry['status'] == 'running' for entry in value['services'].values()
        ))

    def release_identity(self):
        path = self.state.parent / 'active-release.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))['version']
        except (OSError, ValueError, KeyError):
            return 'development'
