import io
import tarfile

import pytest

from oncall_ops.backup import BackupError, create_backup, validate_archive, validate_backup


class FakeRuntime:
    def __init__(self):
        self.paused = False
        self.resumed = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.resumed = True
        self.paused = False


class FakeDocker:
    def __init__(self, runtime, fail_archive=False):
        self.runtime, self.fail_archive = runtime, fail_archive
        self.running = True
        self.restarted = False

    def discover(self, root):
        return [{'id': name, 'service': name, 'running': True, 'image': 'sha256:fixture',
                 'hostname': 'original-rabbit', 'mounts': [{'name': name + '-data', 'destination': '/data'}]}
                for name in ('postgres', 'rabbitmq', 'etcd', 'minio', 'standalone')]

    def stop(self, containers):
        assert self.runtime.paused
        self.running = False

    def assert_stopped_writers(self, volumes):
        assert not self.running

    def start(self, containers):
        self.restarted = True
        self.running = True

    def archive_volume(self, volume, archive, image):
        assert not self.running
        if self.fail_archive:
            raise RuntimeError('synthetic failure SECRET-MUST-NOT-LEAK')
        with tarfile.open(archive, 'w') as output:
            content = volume.encode()
            item = tarfile.TarInfo('data.bin')
            item.size = len(content)
            output.addfile(item, io.BytesIO(content))


def source(tmp_path):
    root = tmp_path / 'source'
    (root / 'uploads' / '.documents').mkdir(parents=True)
    (root / 'uploads' / '.documents' / 'doc.json').write_text('{"version":1}')
    (root / 'config').mkdir()
    (root / 'config' / 'service_catalog.json').write_text('{"services":{}}')
    (root / '.env').write_text('API_KEY=SECRET-MUST-NOT-LEAK')
    return root


def test_cold_backup_verifies_archives_excludes_credentials_and_resumes(tmp_path):
    root = source(tmp_path)
    runtime = FakeRuntime()
    docker = FakeDocker(runtime)
    backup = create_backup(root, tmp_path / 'backups', docker=docker, runtime=runtime)
    manifest = validate_backup(backup)
    assert manifest['consistency'] == 'cold-stopped-writers'
    assert len(manifest['volumes']) == 5
    assert docker.restarted and runtime.resumed
    assert 'SECRET-MUST-NOT-LEAK' not in (backup / 'manifest.json').read_text()
    with tarfile.open(backup / 'files.tar') as archive:
        assert '.env' not in archive.getnames()
        assert 'uploads/.documents/doc.json' in archive.getnames()


def test_failure_does_not_publish_success_and_restarts_original_services(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime, fail_archive=True)
    with pytest.raises(BackupError) as caught:
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert 'SECRET-MUST-NOT-LEAK' not in str(caught.value)
    assert docker.restarted and runtime.resumed
    assert not list((tmp_path / 'backups').glob('*/manifest.json'))


def test_drain_failure_never_stops_or_reads_volume(tmp_path):
    runtime = FakeRuntime()

    def fail():
        raise TimeoutError('drain')

    runtime.pause = fail
    docker = FakeDocker(runtime)
    with pytest.raises(BackupError):
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert docker.running
    assert runtime.resumed


@pytest.mark.parametrize('kind', ['parent', 'absolute', 'drive', 'symlink', 'hardlink', 'duplicate'])
def test_unsafe_archive_members_are_rejected_before_extracting(tmp_path, kind):
    path = tmp_path / 'bad.tar'
    with tarfile.open(path, 'w') as archive:
        names = {'parent': '../escape', 'absolute': '/etc/escape', 'drive': 'C:/escape'}
        item = tarfile.TarInfo(names.get(kind, 'item'))
        if kind in {'symlink', 'hardlink'}:
            item.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
            item.linkname = '../outside'
        archive.addfile(item)
        if kind == 'duplicate':
            archive.addfile(item)
    with pytest.raises(BackupError):
        validate_archive(path)


def test_tampered_archive_is_rejected(tmp_path):
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    with (backup / 'files.tar').open('ab') as output:
        output.write(b'tampered')
    with pytest.raises(BackupError, match='integrity'):
        validate_backup(backup)


def test_capture_precedes_database_stop_and_manifest_has_baseline(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime)
    order = []
    old_stop = docker.stop
    docker.stop_writers = lambda records: order.append('writers')
    def capture(records):
        assert runtime.paused and docker.running
        order.append('capture')
        return {'postgres': {'schema': ['test'], 'tables': {}}, 'rabbitmq': {}, 'milvus': {}}
    docker.capture = capture
    def stop(records):
        order.append('stop')
        old_stop(records)
    docker.stop = stop
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert order == ['writers', 'capture', 'stop']
    assert validate_backup(backup)['baseline']['postgres']['schema'] == ['test']


def test_partial_stop_failure_restarts_and_resumes(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime)
    def stop(records):
        docker.running = False
        raise RuntimeError('partial stop')
    docker.stop = stop
    with pytest.raises(BackupError):
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert docker.restarted and runtime.resumed


@pytest.mark.parametrize('name', ['folder/File', 'folder/file'])
def test_windows_case_aliases_are_rejected(tmp_path, name):
    path = tmp_path / 'bad.tar'
    with tarfile.open(path, 'w') as archive:
        archive.addfile(tarfile.TarInfo('folder/FILE'))
        archive.addfile(tarfile.TarInfo(name))
    with pytest.raises(BackupError):
        validate_archive(path)


@pytest.mark.parametrize('name', ['CON', 'folder/nul.txt', 'COM1', 'file.'])
def test_windows_device_and_trailing_dot_members_are_rejected(tmp_path, name):
    path = tmp_path / 'bad.tar'
    with tarfile.open(path, 'w') as archive:
        archive.addfile(tarfile.TarInfo(name))
    with pytest.raises(BackupError):
        validate_archive(path)


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows ACL verification')
def test_backup_directory_has_owner_only_protected_acl(tmp_path):
    import os
    import subprocess
    runtime = FakeRuntime()
    backup = create_backup(source(tmp_path), tmp_path / 'backups', docker=FakeDocker(runtime), runtime=runtime)
    script = ('$acl=([IO.DirectoryInfo]::new($env:ONCALL_ACL_TEST)).GetAccessControl(); '
              '$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; '
              'if(-not $acl.AreAccessRulesProtected){exit 2}; '
              'foreach($rule in $acl.Access){'
              'if($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]) -ne $sid){exit 3}}')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            env={**os.environ, 'ONCALL_ACL_TEST': str(backup)}, capture_output=True, timeout=15)
    assert result.returncode == 0



def test_backup_and_restart_errors_are_both_visible_without_secrets(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime, fail_archive=True)
    def start(records):
        raise TimeoutError('SECRET-MUST-NOT-LEAK')
    docker.start = start
    with pytest.raises(BackupError) as caught:
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert 'RuntimeError' in str(caught.value)
    assert 'TimeoutError' in str(caught.value)
    assert 'SECRET-MUST-NOT-LEAK' not in str(caught.value)
    assert runtime.resumed


def test_missing_persistent_volume_never_publishes_valid_backup(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime)
    discover = docker.discover
    def missing(root):
        records = discover(root)
        records[2]['mounts'] = []
        return records
    docker.discover = missing
    with pytest.raises(BackupError):
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    assert not [path for path in (tmp_path / 'backups').iterdir() if not path.name.startswith('.')]
    assert runtime.resumed and docker.restarted


def test_failure_journal_preserves_safe_stage_for_interrupted_drill_debugging(tmp_path):
    runtime = FakeRuntime()
    docker = FakeDocker(runtime)
    def capture(records):
        raise ValueError('SECRET-MUST-NOT-LEAK')
    docker.capture = capture
    with pytest.raises(BackupError):
        create_backup(source(tmp_path), tmp_path / 'backups', docker=docker, runtime=runtime)
    from json import loads
    path = next((tmp_path / 'backups').glob('.pending-*/backup-failure.json'))
    report = loads(path.read_text())
    assert report['stage'] == 'baseline'
    assert report['error_type'] == 'ValueError'
    assert 'SECRET-MUST-NOT-LEAK' not in path.read_text()
