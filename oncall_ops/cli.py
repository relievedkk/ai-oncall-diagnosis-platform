"""Windows operational command surface. Importing it never initializes the app."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.request import Request

from oncall_ops.common import atomic_json, read_env
from oncall_ops.native_health import native_ready, open_http
from oncall_ops.runtime import ManagedRuntime
from oncall_ops.supervisor import ServiceSpec, Supervisor, lock_is_available


def build_environment(root: Path, data_root: Path, config_root: Path) -> dict:
    env_file = Path(config_root) / '.env'
    if not env_file.is_file():
        raise ValueError('Configuration .env is missing; configure the project before starting')
    environment = {**read_env(env_file), **os.environ}
    environment.update({
        'ONCALL_DATA_ROOT': str(Path(data_root).resolve()),
        'ONCALL_CONFIG_ROOT': str(Path(config_root).resolve()),
        'SHARED_MODEL_QUOTA_ENABLED': 'true', 'DATABASE_AUTO_CREATE': 'false',
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
        'ALLOWED_INDEX_ROOTS': ','.join([str((Path(data_root) / 'uploads').resolve()),
                                       str((Path(root) / 'aiops-docs').resolve())]),
    })
    environment.setdefault('NO_PROXY', 'localhost,127.0.0.1,::1,host.docker.internal')
    # Docker containers use their own /app roots, not Windows host paths.
    environment['CLS_LOG_SPOOL_DOCKER_PATH'] = str((Path(data_root) / 'logs' / 'cls-events').resolve())
    return environment


def refresh_release_services(services, state: Path):
    pointer = Path(state) / 'active-release.json'
    if not pointer.exists():
        return
    record = json.loads(pointer.read_text(encoding='utf-8'))
    root, python = Path(record['path']).resolve(), str(record['python'])
    if not root.is_dir() or not Path(python).is_absolute():
        raise ValueError('Active release pointer is invalid')
    for service in services:
        service.cwd = root
        service.command[0] = python


def _fresh_status(path: Path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if time.time() - datetime.fromisoformat(value['last_run']).timestamp() > 15 or value.get('stopping'):
            raise ValueError('stale')
        return value
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _probe_url(url, key=None, expected_run_id=None):
    headers = {'X-API-Key': key} if key else {}
    try:
        with open_http(Request(url, headers=headers), timeout=3) as response:
            return response.status == 200 and (expected_run_id is None or
                json.load(response).get('operations_run_id') == expected_run_id)
    except (OSError, ValueError):
        return False


def _wait_stopped(state, run_id, *, timeout=55, poll=0.25, lock_free=None):
    state = Path(state)
    lock_free = lock_free or (lambda: lock_is_available(state))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads((state / 'supervisor.json').read_text(encoding='utf-8'))
            if value['run_id'] != run_id:
                raise RuntimeError('Supervisor identity changed during stop; no new process was touched')
            if (value.get('stop_complete') is True and all(
                    service.get('pid') is None and service.get('status') == 'stopped'
                    for service in value['services'].values()) and lock_free()):
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Missing or stale output cannot establish that a process exited.
        time.sleep(poll)
    raise RuntimeError('Graceful managed stop timed out; inspect owned process state')


def _supervise(args, root, data, configuration, state, environment):
    port = int(environment.get('PORT', '9900'))
    official = bool(environment.get('TENCENTCLOUD_SECRET_ID') and environment.get('TENCENTCLOUD_SECRET_KEY'))
    environment.update({
        'MCP_CLS_URL': f'http://127.0.0.1:{3000 if official else 8003}/mcp',
        'MCP_CLS_DOCKER_URL': f'http://host.docker.internal:{3000 if official else 8003}/mcp',
    })
    services = [
        ServiceSpec('cls', [args.python, '-m', 'mcp_servers.cls_gateway' if official else 'mcp_servers.cls_server'], root, env=environment),
        ServiceSpec('monitor', [args.python, '-m', 'mcp_servers.monitor_server'], root, env=environment),
        ServiceSpec('api', [args.python, '-m', 'app.server'], root, health_url=f'http://127.0.0.1:{port}/live', env=environment),
    ]
    refresh_release_services(services, state)
    supervisor = Supervisor(
        root,
        state / 'runtime',
        services,
        shutdown_timeout=40,
        run_id=environment.get('ONCALL_SUPERVISOR_RUN_ID'),
    )
    try:
        supervisor.start()
        while not supervisor.snapshot()['stopping']:
            # The release driver publishes the pointer before clearing pause.
            # Refresh while all old children are stopped; shared roots remain in env.
            if supervisor.paused:
                refresh_release_services(services, state)
            supervisor.tick()
            time.sleep(0.5)
    finally:
        supervisor.stop()
    return 0


def _start(args, root, data, configuration, state, environment):
    if _fresh_status(state / 'runtime' / 'supervisor.json'):
        raise ValueError('A managed supervisor is already running; use status or stop')
    for key in ('API_KEY', 'ALERTMANAGER_WEBHOOK_TOKEN', 'MCP_CLS_TOKEN'):
        if len(environment.get(key, '')) < 32 or environment[key].startswith('replace-with-'):
            raise ValueError('Configuration needs independent strong API/Webhook/CLS credentials')
    if environment.get('AUTH_ENABLED', '').lower() != 'true':
        raise ValueError('Managed configuration requires AUTH_ENABLED=true')
    if not Path(args.python).is_file():
        raise ValueError('Project Python is missing; run uv sync or supply --python')
    from oncall_ops.docker import DockerRunner

    state.mkdir(parents=True, exist_ok=True)
    (data / 'uploads').mkdir(parents=True, exist_ok=True)
    (data / 'logs' / 'cls-events').mkdir(parents=True, exist_ok=True)
    official = bool(environment.get('TENCENTCLOUD_SECRET_ID') and environment.get('TENCENTCLOUD_SECRET_KEY'))
    environment['MCP_CLS_DOCKER_URL'] = f'http://host.docker.internal:{3000 if official else 8003}/mcp'
    active = state / 'active-release.json'
    record = None
    if active.exists():
        from oncall_ops.releases import validate_release

        record = json.loads(active.read_text(encoding='utf-8'))
        manifest = validate_release(Path(record['path']))
        if (manifest['version'] != record['version']
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', record['worker_image'])):
            raise ValueError('Active release identity or Worker image is invalid')
        if not Path(record['python']).is_file():
            raise ValueError('Active release Python is missing; no application version was changed')
        environment['ONCALL_WORKER_IMAGE'] = record['worker_image']
    docker = DockerRunner(args.docker, environment=environment, env_file=configuration / '.env')
    docker.run(['info', '--format', '{{.ServerVersion}}'])
    compose = ['compose', '-f', str(root / 'vector-database.yml'), '--env-file', str(configuration / '.env')]
    if record:
        # A restart uses the previously checked schema and immutable image. Running
        # old migration code or rebuilding from the developer checkout breaks rollback.
        docker.run([*compose, 'up', '-d', '--no-build', '--wait',
                    'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'], cwd=root, timeout=600)
        docker.run([*compose, 'up', '-d', '--no-deps', '--no-build',
                    'diagnosis-critical-worker', 'diagnosis-default-worker',
                    'remediation-worker', 'outbox-publisher'], cwd=root, timeout=180)
    else:
        docker.run([*compose, 'up', '-d', '--build'], cwd=root, timeout=1800)
    docker.run(['compose', '-p', 'oncallagent-monitoring', '-f', str(root / 'monitoring.yml'),
                '--env-file', str(configuration / '.env'), 'up', '-d', '--build'], cwd=root, timeout=600)
    if args.foreground:
        return _supervise(args, root, data, configuration, state, environment)
    expected_run_id = uuid.uuid4().hex
    launch_environment = {**environment, 'ONCALL_SUPERVISOR_RUN_ID': expected_run_id}
    command = [args.python, '-m', 'oncall_ops.cli', '--root', str(root), '--data-root', str(data),
               '--config-root', str(configuration), '--python', args.python, '--docker', args.docker, 'supervise']
    log = (state / 'supervisor-launch.log').open('ab')
    try:
        creationflags = 0
        if os.name == 'nt':
            creationflags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                             | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        child = subprocess.Popen(command, cwd=root, env=launch_environment, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=creationflags)
    finally:
        log.close()
    deadline = time.monotonic() + 120
    owned_status_seen = False
    while time.monotonic() < deadline:
        status = _fresh_status(state / 'runtime' / 'supervisor.json')
        api_base = f'http://127.0.0.1:{environment.get("PORT", "9900")}'
        if status and status.get('run_id') == expected_run_id:
            owned_status_seen = True
            if (native_ready(status, environment, api_base, expected_run_id=expected_run_id,
                             timeout=15, deadline=deadline)
                    and time.monotonic() < deadline):
                print(json.dumps({'status': 'ready', 'run_id': status['run_id'], 'state': str(state)}, ensure_ascii=False))
                return 0
        if child.poll() is not None:
            raise RuntimeError('Managed supervisor exited before readiness; inspect local logs')
        time.sleep(1)
    if owned_status_seen:
        atomic_json(state / 'runtime' / 'control.json', {'run_id': expected_run_id, 'shutdown': True})
    raise RuntimeError('Managed startup readiness timed out; stop requested, inspect local logs')


def parser():
    result = argparse.ArgumentParser(description='Windows OnCall operational safeguards')
    result.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument('--data-root', type=Path)
    result.add_argument('--config-root', type=Path)
    result.add_argument('--python')
    result.add_argument('--docker', default='docker')
    commands = result.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('--foreground', action='store_true', help='Keep the supervisor attached for Task Scheduler')
    for name in ('supervise', 'stop', 'status'):
        commands.add_parser(name)
    probe = commands.add_parser('probe')
    probe.add_argument('--settings', type=Path)
    probe.add_argument('--once', action='store_true')
    probe.add_argument('--interval', type=float, default=15)
    backup = commands.add_parser('backup')
    backup.add_argument('--destination', type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--target', required=True, type=Path)
    drill = commands.add_parser('drill')
    drill.add_argument('--target', required=True, type=Path)
    drill.add_argument('--keep-resources', action='store_true')
    package = commands.add_parser('package')
    package.add_argument('--version', required=True)
    package.add_argument('--schema', action='append', required=True)
    deploy = commands.add_parser('deploy')
    deploy.add_argument('release', type=Path)
    deploy.add_argument('--bootstrap', action='store_true')
    commands.add_parser('rollback')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    root = args.root.resolve()
    data, configuration = (args.data_root or root).resolve(), (args.config_root or root).resolve()
    state = data / '.runtime' / 'operations'
    args.python = str(Path(args.python).resolve()) if args.python else str(root / '.venv' / 'Scripts' / 'python.exe')
    try:
        if args.command == 'status':
            value = _fresh_status(state / 'runtime' / 'supervisor.json')
            print(json.dumps(value or {'status': 'unavailable'}, ensure_ascii=False))
            return 0 if value else 1
        if args.command == 'stop':
            value = _fresh_status(state / 'runtime' / 'supervisor.json')
            if not value:
                raise ValueError('Managed supervisor unavailable; no foreign process was stopped')
            atomic_json(state / 'runtime' / 'control.json', {'run_id': value['run_id'], 'maintenance': True, 'shutdown': True})
            _wait_stopped(state / 'runtime', value['run_id'])
            print(json.dumps({'status': 'stopped'}))
            return 0
        environment = build_environment(root, data, configuration)
        if args.command == 'supervise':
            return _supervise(args, root, data, configuration, state, environment)
        if args.command == 'start':
            return _start(args, root, data, configuration, state, environment)
        if args.command == 'probe':
            from oncall_ops.probe import run_once

            if args.interval <= 0:
                raise ValueError('Probe interval must be positive')
            settings = args.settings or root / 'config' / 'windows-operations.example.json'
            config = json.loads(settings.read_text(encoding='utf-8'))
            config['env_file'] = str(configuration / '.env')
            while True:
                report = run_once(config, state / 'probe')
                print(json.dumps(report, ensure_ascii=False))
                if args.once:
                    return report['exit_code']
                time.sleep(args.interval)
        if args.command == 'package':
            from oncall_ops.releases import package_release

            path = package_release(root, state / 'releases', args.version, compatible_schemas=args.schema)
            print(json.dumps({'release': str(path)}, ensure_ascii=False))
            return 0
        from oncall_ops.docker import DockerRunner

        docker = DockerRunner(args.docker, environment=environment, env_file=configuration / '.env')
        docker.credentials = environment
        if args.command == 'backup':
            from oncall_ops.backup import create_backup

            print('Maintenance window: new work will be rejected; services pause during cold snapshot.', file=sys.stderr)
            path = create_backup(root, args.destination or state / 'backups', docker=docker,
                                 runtime=ManagedRuntime(state / 'runtime'), data_root=data, config_root=configuration)
            print(json.dumps({'backup': str(path)}, ensure_ascii=False))
        elif args.command in {'restore', 'drill'}:
            from oncall_ops.drill import restore_isolated, run_drill

            report = (restore_isolated(args.backup, args.target, docker=docker, credentials=environment)
                      if args.command == 'restore' else run_drill(root, args.target, docker=docker,
                                                               keep_resources=args.keep_resources))
            print(json.dumps(report, ensure_ascii=False))
        elif args.command in {'deploy', 'rollback'}:
            from oncall_ops.release_driver import ReleaseDriver
            from oncall_ops.releases import activate_release

            driver = ReleaseDriver(root, state, python=args.python, config_root=configuration, data_root=data,
                                   docker=args.docker, api_url=f'http://127.0.0.1:{environment.get("PORT", "9900")}',
                                   auth_token_env='API_KEY')
            release = args.release if args.command == 'deploy' else Path(json.loads(
                (state / 'active-release.json').read_text(encoding='utf-8'))['previous']['path'])
            print('Maintenance window: application code and Worker images will switch together.', file=sys.stderr)
            print(json.dumps(activate_release(release, state, driver=driver,
                                             bootstrap=getattr(args, 'bootstrap', False)), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        # Operational modules use public errors; never print subprocess output/env.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        print(json.dumps({'status': 'failed', 'error': message}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
