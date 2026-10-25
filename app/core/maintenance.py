"""Local owner-controlled admission/drain channel, independent of model availability."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from starlette.responses import JSONResponse


class MaintenanceController:
    def __init__(self, state_root: Path | None, run_id: str):
        self.root = Path(state_root).resolve() if state_root else None
        self.run_id = run_id
        self.active_requests = 0
        self.generation = 0
        self.durable_work: int | None = None
        self.durable_checked_at: float | None = None

    def request_changed(self):
        self.generation += 1
        self.durable_work = None
        self.durable_checked_at = None

    def read_control(self) -> dict:
        if self.root is None or not self.run_id:
            return {'maintenance': False, 'shutdown': False}
        try:
            value = json.loads((self.root / 'control.json').read_text(encoding='utf-8'))
            if not isinstance(value, dict) or value.get('run_id') != self.run_id:
                raise ValueError('control owner mismatch')
            return {
                'maintenance': value.get('maintenance') is True,
                'shutdown': value.get('shutdown') is True,
                'pause': value.get('pause') is True,
            }
        except (OSError, ValueError, TypeError):
            return {'maintenance': True, 'shutdown': False, 'control_error': True}

    def write_state(self) -> None:
        if self.root is None or not self.run_id:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / 'api-state.json'
        temp = path.with_name(f'.{uuid.uuid4().hex}.tmp')
        value = {
            'run_id': self.run_id, 'active_requests': self.active_requests,
            'durable_work': self.durable_work, 'durable_checked_at': self.durable_checked_at,
            'updated_at': time.time(), **self.read_control(),
        }
        try:
            with temp.open('x', encoding='utf-8') as output:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


class MaintenanceMiddleware:
    def __init__(self, app, controller: MaintenanceController | None = None):
        self.app = app
        self.controller = controller or maintenance_controller

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        tracked = scope['type'] == 'http' and path.startswith('/api/') and (
            scope.get('method') not in {'GET', 'HEAD', 'OPTIONS'} or path.endswith('/events')
        ) and not path.startswith('/api/auth/')
        if not tracked:
            return await self.app(scope, receive, send)
        if self.controller.read_control()['maintenance']:
            return await JSONResponse(
                {'detail': '系统正在维护，已接纳任务继续执行，请稍后重试'},
                status_code=503, headers={'Retry-After': '5'},
            )(scope, receive, send)
        # There is no await between checking admission and taking ownership.
        self.controller.active_requests += 1
        self.controller.request_changed()
        try:
            self.controller.write_state()
            await self.app(scope, receive, send)
        finally:
            self.controller.active_requests -= 1
            self.controller.request_changed()
            self.controller.write_state()


async def watch_operational_control(server, controller=None, *, interval=0.5):
    controller = controller or maintenance_controller
    if controller.root is None:
        return
    next_sample = 0.0
    while not server.should_exit:
        control = controller.read_control()
        if control['shutdown'] or control.get('pause'):
            server.should_exit = True
            return
        if server.started and time.monotonic() >= next_sample:
            from app.repositories.diagnosis import diagnosis_repository
            from app.repositories.remediation import remediation_repository

            generation = controller.generation
            try:
                if controller.active_requests:
                    raise ValueError('HTTP work has not drained')
                jobs, actions = await asyncio.wait_for(asyncio.gather(
                    diagnosis_repository.get_operational_metrics(),
                    remediation_repository.get_operational_metrics(),
                ), timeout=3.0)
                if controller.active_requests or controller.generation != generation:
                    raise ValueError('Admission changed while durable state was sampled')
                controller.durable_work = sum(
                    jobs['job_counts'].get(status, 0) for status in ('queued', 'running', 'retrying')
                ) + sum(actions['action_counts'].get(status, 0) for status in (
                    'approved', 'executing', 'reconciling', 'verifying', 'rolling_back',
                ))
                controller.durable_checked_at = time.time()
            except Exception:
                controller.durable_work = None  # Unknown is never permission to snapshot.
                controller.durable_checked_at = None
            next_sample = time.monotonic() + 3.0
        controller.write_state()
        await asyncio.sleep(interval)


maintenance_controller = MaintenanceController(
    Path(os.environ['ONCALL_OPERATIONS_STATE_ROOT'])
    if os.environ.get('ONCALL_OPERATIONS_STATE_ROOT') else None,
    os.environ.get('ONCALL_OPERATIONS_RUN_ID', ''),
)
