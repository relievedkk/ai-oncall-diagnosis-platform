"""Cold restore into newly owned resources and a synthetic, opt-in Docker drill."""
from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path

from oncall_ops.backup import BackupError, create_backup, extract_files, validate_backup
from oncall_ops.common import atomic_json


def restore_isolated(backup: Path, target_root: Path, *, docker, credentials: dict) -> dict:
    started = time.monotonic()
    if Path(target_root).is_symlink() or getattr(Path(target_root), 'is_junction', lambda: False)():
        raise BackupError('Restore target must not be a link or junction')
    backup, target = Path(backup).resolve(), Path(target_root).resolve()
    manifest = validate_backup(backup)
    if target == backup or target.is_relative_to(backup) or backup.is_relative_to(target):
        raise BackupError('Restore target aliases backup data')
    if target.exists() and (target.is_symlink() or not target.is_dir() or any(target.iterdir())):
        raise BackupError('Restore target must be new and empty')
    owner = 'oncall-restore-' + uuid.uuid4().hex[:16]
    mappings = {item['name']: f'{owner}-volume-{index}' for index, item in enumerate(manifest['volumes'])}
    if set(mappings) & set(mappings.values()):
        raise BackupError('Restore target aliases source volumes')
    target.mkdir(parents=True, exist_ok=True)
    journal = {'owner': owner, 'resources': [], 'status': 'creating'}
    atomic_json(target / 'ownership.json', journal)
    report = {'status': 'failed', 'owner': owner, 'snapshot_at': manifest['snapshot_at'],
              'source_backup': manifest['id'], 'verification': {'verified': False}}
    try:
        extract_files(backup, target / 'files')
        helper = next(item['image'] for item in manifest['containers'] if item['service'] == 'postgres')
        for volume in manifest['volumes']:
            name = mappings[volume['name']]
            # Journal intended resources before mutation; cleanup still requires live label verification.
            journal['resources'].append({'kind': 'volume', 'name': name})
            atomic_json(target / 'ownership.json', journal)
            docker.new_volume(name, owner)
            docker.restore_volume(name, backup / volume['archive'], helper)
        verification = docker.launch_restored(manifest, mappings, owner, target, credentials)
        report['verification'] = verification
        if not verification.get('verified'):
            raise BackupError('Restored application fingerprints differ or checks are incomplete')
        report.update(status='verified', verification=verification)
        journal = json.loads((target / 'ownership.json').read_text(encoding='utf-8'))
        journal['status'] = 'verified'
        atomic_json(target / 'ownership.json', journal)
        return report
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        if hasattr(docker, 'cleanup_owned'):
            journal = json.loads((target / 'ownership.json').read_text(encoding='utf-8'))
            report['cleanup_errors'] = docker.cleanup_owned(journal)
        raise BackupError('Isolated restore failed; inspect restore-report and ownership journal') from exc
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        atomic_json(target / 'restore-report.json', report)


class FixtureRuntime:
    """No native writers: this runtime belongs only to the five-component synthetic stack."""
    def pause(self):
        pass

    def resume(self):
        pass

    def release_identity(self):
        return 'synthetic-drill-v1'


def _fixture_credentials():
    return {'POSTGRES_USER': 'oncall_drill', 'POSTGRES_DB': 'oncall_drill',
            'POSTGRES_PASSWORD': secrets.token_hex(20), 'RABBITMQ_DEFAULT_USER': 'oncall_drill',
            'RABBITMQ_DEFAULT_PASS': secrets.token_hex(20), 'MINIO_ACCESS_KEY': 'oncall_drill',
            'MINIO_SECRET_KEY': secrets.token_hex(20)}


def _seed(docker, containers, fixture):
    records = {item['service']: item for item in containers}
    docker._sql(records['postgres']['id'], '''
CREATE TABLE alembic_version(version_num varchar(64) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('synthetic-drill-schema-v1');
CREATE TABLE diagnosis_jobs(id integer PRIMARY KEY, status text, task_key text);
INSERT INTO diagnosis_jobs VALUES (1, 'completed', 'synthetic-task-1');
CREATE TABLE diagnosis_reports(id integer PRIMARY KEY, diagnosis_id integer, report jsonb);
INSERT INTO diagnosis_reports VALUES (1, 1, '{"summary":"synthetic-report"}');
CREATE TABLE checkpoints(thread_id text PRIMARY KEY, checkpoint jsonb);
INSERT INTO checkpoints VALUES ('synthetic-task-1', '{"step":3,"complete":true}');
CREATE TABLE knowledge_document_versions(document_id text PRIMARY KEY, version integer, sha256 text);
INSERT INTO knowledge_document_versions VALUES ('synthetic-doc', 1, 'synthetic-version-1');
''')
    (fixture / 'uploads' / '.documents').mkdir(parents=True)
    (fixture / 'uploads' / '.documents' / 'synthetic.json').write_text(
        '{"document_id":"synthetic-doc","version":1}', encoding='utf-8')
    (fixture / 'uploads' / 'synthetic.txt').write_text('oncall synthetic recovery fixture\n', encoding='utf-8')
    (fixture / 'config').mkdir()
    atomic_json(fixture / 'config' / 'service_catalog.json', {'services': {'synthetic': {'owner': 'drill'}}})
    rabbit = records['rabbitmq']['id']
    docker._rabbit(rabbit, 'PUT', 'queues/%2F/oncall.drill', {'durable': True, 'auto_delete': False, 'arguments': {}})
    result = docker._rabbit(rabbit, 'POST', 'exchanges/%2F/amq.default/publish',
                            {'properties': {'delivery_mode': 2}, 'routing_key': 'oncall.drill',
                             'payload': 'synthetic-queue-message-1', 'payload_encoding': 'string'})
    if not result['routed']:
        raise BackupError('Synthetic queue fixture could not be routed')
    from pymilvus import MilvusClient
    client = MilvusClient(uri=f"http://127.0.0.1:{docker._port(records['standalone']['id'], 19530)}", timeout=30)
    try:
        client.create_collection(collection_name='oncall_restore_fixture', dimension=4, metric_type='COSINE',
                                 consistency_level='Strong')
        client.insert(collection_name='oncall_restore_fixture', data=[
            {'id': 1, 'vector': [1.0, 0.0, 0.0, 0.0], 'tag': 'synthetic'},
            {'id': 2, 'vector': [0.0, 1.0, 0.0, 0.0], 'tag': 'synthetic-other'}])
        client.flush(collection_name='oncall_restore_fixture')
    finally:
        client.close()


def run_drill(root: Path, target_root: Path, *, docker, credentials: dict | None = None,
              keep_resources: bool = False) -> dict:
    """Execute a new isolated synthetic source -> cold archive -> new isolated target.

    Existing local images are inspected by ID. No application worker, cloud query,
    notification or model/embedding transport is started. Artifacts remain on disk.
    """
    started = time.monotonic()
    if Path(target_root).is_symlink() or getattr(Path(target_root), 'is_junction', lambda: False)():
        raise BackupError('Drill target must not be a link or junction')
    target = Path(target_root).resolve()
    if target.exists() and (not target.is_dir() or target.is_symlink() or any(target.iterdir())):
        raise BackupError('Drill output directory must be new and empty')
    # Fail before creating any fixture resource if Docker Desktop is unavailable.
    docker.run(['info'], timeout=15)
    images = {}
    defaults = {'postgres': 'postgres:16.4-alpine', 'rabbitmq': 'ai-oncall-rabbitmq:4.1-hardened',
                'etcd': 'quay.io/coreos/etcd:v3.5.18', 'minio': 'minio/minio:RELEASE.2023-03-20T20-16-18Z',
                'standalone': 'milvusdb/milvus:v2.5.10'}
    for service, tag in defaults.items():
        images[service] = json.loads(docker.run(['image', 'inspect', tag]))[0]['Id']
    target.mkdir(parents=True, exist_ok=True)
    fixture = target / 'fixture'
    fixture.mkdir()
    owner = 'oncall-fixture-' + uuid.uuid4().hex[:16]
    credentials = dict(credentials or _fixture_credentials())
    journal = {'owner': owner, 'resources': [], 'status': 'creating'}
    atomic_json(fixture / 'ownership.json', journal)
    report = {'status': 'failed', 'synthetic': True, 'schema': 'synthetic-drill-schema-v1',
              'production_schema_migrations_exercised': False, 'stage_seconds': {}, 'cleanup_errors': []}
    stage = time.monotonic()
    try:
        volumes = {service: f'{owner}-{service}-data' for service in images}
        for name in volumes.values():
            journal['resources'].append({'kind': 'volume', 'name': name})
            atomic_json(fixture / 'ownership.json', journal)
            docker.new_volume(name, owner)
        spec = docker.compose_spec(images, volumes, owner, credentials, rabbit_hostname=owner + '-rabbit')
        adapter, containers = docker._compose_start(spec, owner, fixture, credentials)
        _seed(adapter, containers, fixture)
        report['stage_seconds']['fixture_seed'] = round(time.monotonic() - stage, 3)
        stage = time.monotonic()
        backup = create_backup(fixture, target / 'backups', docker=adapter, runtime=FixtureRuntime())
        report['stage_seconds']['cold_backup'] = round(time.monotonic() - stage, 3)
        stage = time.monotonic()
        adapter.wait_healthy(containers)
        if adapter.capture(containers) != validate_backup(backup)['baseline']:
            raise BackupError('Source contents changed after original containers restarted')
        report['source_restart_verified'] = True
        report['stage_seconds']['source_restart_verify'] = round(time.monotonic() - stage, 3)
        stage = time.monotonic()
        restored = restore_isolated(backup, target / 'restored', docker=docker, credentials=credentials)
        report['stage_seconds']['restore_verify'] = round(time.monotonic() - stage, 3)
        # File bytes are validated by the manifest and extracted before component launch.
        from oncall_ops.backup import digest
        for path in (fixture / 'uploads').rglob('*'):
            if path.is_file():
                restored_path = target / 'restored' / 'files' / 'uploads' / path.relative_to(fixture / 'uploads')
                if digest(path) != digest(restored_path):
                    raise BackupError('Restored upload fingerprint mismatch')
        report.update(status='verified', restore=restored, uploads_verified=True,
                      snapshot_at=restored['snapshot_at'], backup=str(backup))
        return report
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        raise BackupError('Synthetic restore drill failed; inspect drill-report.json') from exc
    finally:
        if not keep_resources:
            for directory in (target / 'restored', fixture):
                path = directory / 'ownership.json'
                if path.is_file():
                    report['cleanup_errors'].extend(docker.cleanup_owned(json.loads(path.read_text(encoding='utf-8'))))
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['resources_retained'] = keep_resources
        atomic_json(target / 'drill-report.json', report)
