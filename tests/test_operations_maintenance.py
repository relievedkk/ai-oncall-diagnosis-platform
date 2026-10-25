import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.core.maintenance import MaintenanceController, MaintenanceMiddleware


def control(root, **values):
    (root / 'control.json').write_text(json.dumps({'run_id': 'run-one', **values}), encoding='utf-8')


@pytest.mark.asyncio
async def test_maintenance_blocks_new_work_but_drains_existing_stream(tmp_path):
    control(tmp_path, maintenance=False)
    controller = MaintenanceController(tmp_path, 'run-one')
    started, finish = asyncio.Event(), asyncio.Event()
    app = FastAPI()
    app.add_middleware(MaintenanceMiddleware, controller=controller)

    @app.post('/api/chat')
    async def chat():
        started.set()
        await finish.wait()
        return {'ok': True}

    @app.get('/live')
    async def live():
        return {'ok': True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        first = asyncio.create_task(client.post('/api/chat'))
        await started.wait()
        assert controller.active_requests == 1
        control(tmp_path, maintenance=True)
        denied = await client.post('/api/chat')
        assert denied.status_code == 503
        assert denied.headers['retry-after'] == '5'
        assert (await client.get('/live')).status_code == 200
        assert controller.active_requests == 1
        finish.set()
        assert (await first).status_code == 200
        assert controller.active_requests == 0
        status = json.loads((tmp_path / 'api-state.json').read_text())
        assert status['run_id'] == 'run-one'
        assert status['active_requests'] == 0


def test_control_is_fail_closed_and_shutdown_requires_matching_run(tmp_path):
    controller = MaintenanceController(tmp_path, 'run-one')
    assert controller.read_control()['maintenance'] is True
    (tmp_path / 'control.json').write_text('{bad', encoding='utf-8')
    assert controller.read_control()['maintenance'] is True
    (tmp_path / 'control.json').write_text(json.dumps({'run_id': 'foreign', 'shutdown': True}))
    assert controller.read_control()['shutdown'] is False
    control(tmp_path, shutdown=True)
    assert controller.read_control()['shutdown'] is True


@pytest.mark.asyncio
async def test_cancelled_or_failed_request_releases_drain_counter(tmp_path):
    control(tmp_path, maintenance=False)
    controller = MaintenanceController(tmp_path, 'run-one')

    async def broken(scope, receive, send):
        raise asyncio.CancelledError()

    middleware = MaintenanceMiddleware(broken, controller=controller)
    with pytest.raises(asyncio.CancelledError):
        await middleware({'type': 'http', 'path': '/api/upload', 'method': 'POST'}, None, None)
    assert controller.active_requests == 0


def test_unmanaged_runtime_has_no_control_channel():
    controller = MaintenanceController(None, '')
    assert controller.read_control() == {'maintenance': False, 'shutdown': False}


@pytest.mark.asyncio
async def test_completed_request_invalidates_durable_sample_taken_while_request_was_active(tmp_path):
    control(tmp_path, maintenance=False)
    controller = MaintenanceController(tmp_path, 'run-one')

    async def committing(scope, receive, send):
        controller.durable_work = 0
        controller.durable_checked_at = 12345.0

    await MaintenanceMiddleware(committing, controller=controller)(
        {'type': 'http', 'path': '/api/aiops/jobs', 'method': 'POST'}, None, None,
    )
    assert controller.active_requests == 0
    assert controller.durable_checked_at is None
    assert controller.durable_work is None


@pytest.mark.asyncio
async def test_durable_sample_does_not_commit_across_request_admission_generation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.core.maintenance import watch_operational_control

    control(tmp_path, maintenance=False)
    controller = MaintenanceController(tmp_path, 'run-one')
    sampling, release = asyncio.Event(), asyncio.Event()

    async def jobs():
        sampling.set()
        await release.wait()
        return {'job_counts': {}}

    async def actions():
        return {'action_counts': {}}

    monkeypatch.setattr('app.repositories.diagnosis.diagnosis_repository.get_operational_metrics', jobs)
    monkeypatch.setattr('app.repositories.remediation.remediation_repository.get_operational_metrics', actions)
    server = SimpleNamespace(started=True, should_exit=False)
    watcher = asyncio.create_task(watch_operational_control(server, controller, interval=0.001))
    await sampling.wait()

    async def committing(scope, receive, send):
        return None

    await MaintenanceMiddleware(committing, controller=controller)(
        {'type': 'http', 'path': '/api/aiops/jobs', 'method': 'POST'}, None, None,
    )
    release.set()
    await asyncio.sleep(0.02)
    server.should_exit = True
    await watcher
    assert controller.durable_checked_at is None
