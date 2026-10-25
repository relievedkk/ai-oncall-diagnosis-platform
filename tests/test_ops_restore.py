import json

import pytest
from test_ops_backup import FakeDocker, FakeRuntime, source

from oncall_ops.backup import BackupError, create_backup
from oncall_ops.drill import restore_isolated


class RestoreDocker:
    def __init__(self):
        self.volumes = []
        self.restored = []
        self.launched = False

    def new_volume(self, name, owner):
        assert owner in name
        self.volumes.append(name)

    def restore_volume(self, name, archive, image):
        assert name in self.volumes
        self.restored.append(name)

    def launch_restored(self, manifest, mappings, owner, target, credentials):
        assert not ({item['name'] for item in manifest['volumes']} & set(mappings.values()))
        self.launched = True
        return {'verified': True, 'components': ['postgres', 'rabbitmq', 'etcd', 'minio', 'standalone']}


def test_restore_only_creates_new_owned_volumes_and_preserves_files(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    docker = RestoreDocker()
    target = tmp_path / 'isolated'
    report = restore_isolated(backup, target, docker=docker, credentials={})
    assert report['status'] == 'verified'
    assert docker.launched and len(docker.restored) == 5
    assert (target / 'files' / 'uploads' / '.documents' / 'doc.json').exists()
    assert json.loads((target / 'restore-report.json').read_text())['status'] == 'verified'


def test_nonempty_restore_target_rejected_before_creating_docker_resources(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'important.txt').write_text('keep')
    docker = RestoreDocker()
    with pytest.raises(BackupError):
        restore_isolated(backup, target, docker=docker, credentials={})
    assert not docker.volumes
    assert (target / 'important.txt').read_text() == 'keep'


def test_failed_component_verification_never_claims_restore_passed(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    docker = RestoreDocker()
    docker.launch_restored = lambda *args: {
        'verified': False, 'checks': {'postgres': True, 'rabbitmq': False, 'milvus': True},
        'observed': {'rabbitmq': {'message_sha256': 'observed'}},
    }
    target = tmp_path / 'isolated'
    with pytest.raises(BackupError):
        restore_isolated(backup, target, docker=docker, credentials={})
    report = json.loads((target / 'restore-report.json').read_text())
    assert report['status'] == 'failed'
    assert report['verification']['checks']['rabbitmq'] is False
    assert report['verification']['observed']['rabbitmq']['message_sha256'] == 'observed'



def test_manifest_rejects_duplicate_source_volumes(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    path = backup / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['volumes'][1]['name'] = manifest['volumes'][0]['name']
    path.write_text(json.dumps(manifest))
    docker = RestoreDocker()
    with pytest.raises(BackupError):
        restore_isolated(backup, tmp_path / 'restore', docker=docker, credentials={})
    assert not docker.volumes


def test_manifest_archive_traversal_rejected(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    path = backup / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['volumes'][0]['archive'] = '../outside.tar'
    path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        restore_isolated(backup, tmp_path / 'restore', docker=RestoreDocker(), credentials={})


def test_symlink_restore_root_rejected_before_resource_creation(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = tmp_path / 'link'
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable')
    docker = RestoreDocker()
    with pytest.raises(BackupError):
        restore_isolated(backup, target, docker=docker, credentials={})
    assert not docker.volumes
    assert not list(outside.iterdir())


def test_drill_rejects_changed_source_contents_after_backup_restart(tmp_path, monkeypatch):
    import oncall_ops.drill as module
    from oncall_ops.drill import run_drill
    class DrillDocker(FakeDocker):
        def __init__(self):
            super().__init__(FakeRuntime())
            self.captures = 0
        def run(self, args, **kwargs):
            return json.dumps([{'Id': 'sha256:fixture'}]) if args[0] == 'image' else ''
        def new_volume(self, *args):
            pass
        def restore_volume(self, *args):
            pass
        def compose_spec(self, *args, **kwargs):
            return {}
        def _compose_start(self, spec, owner, target, credentials):
            return self, self.discover(target)
        def wait_healthy(self, records):
            pass
        def capture(self, records):
            self.captures += 1
            return {'postgres': {'count': self.captures}, 'rabbitmq': {}, 'milvus': {}}
        def stop(self, records):
            self.running = False
        def launch_restored(self, *args):
            return {'verified': True}
        def cleanup_owned(self, journal):
            return []
    def seed(adapter, containers, fixture):
        (fixture / 'uploads').mkdir()
        (fixture / 'uploads' / 'synthetic.txt').write_text('synthetic')
        (fixture / 'config').mkdir()
        (fixture / 'config' / 'service_catalog.json').write_text('{}')
    monkeypatch.setattr(module, '_seed', seed)
    with pytest.raises(BackupError):
        run_drill(tmp_path, tmp_path / 'drill', docker=DrillDocker())
    report = json.loads((tmp_path / 'drill' / 'drill-report.json').read_text())
    assert report['status'] == 'failed'
