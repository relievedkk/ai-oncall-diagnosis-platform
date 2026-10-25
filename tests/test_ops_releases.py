import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from oncall_ops.releases import ReleaseError, activate_release, package_release, validate_release


def source(tmp_path):
    root = tmp_path / 'source'
    (root / 'app').mkdir(parents=True)
    (root / 'app' / 'main.py').write_text('VERSION = 1')
    (root / 'static').mkdir()
    (root / 'static' / 'app.js').write_text('// current uncommitted UI')
    (root / '.env').write_text('API_KEY=never-package')
    (root / 'uploads').mkdir()
    (root / 'uploads' / 'private.txt').write_text('never-package')
    (root / 'uv.lock').write_text('version = 1')
    return root


class Driver:
    def __init__(self, fail=''):
        self.started = []
        self.paused = 0
        self.resumed = 0
        self.aborted = 0
        self.fail = fail

    def schema(self):
        return 'schema-1'

    def prepare(self, path, manifest):
        return {'python': str(path / '.venv' / 'Scripts' / 'python.exe'),
                'worker_image': f'sha256:{manifest["version"]}'}

    def pause(self):
        self.paused += 1

    def start_version(self, record):
        self.started.append(record['version'])

    def smoke(self, record):
        return record['version'] != self.fail

    def resume(self):
        self.resumed += 1

    def abort_candidate(self, previous):
        self.aborted += 1


def test_package_preserves_current_ui_but_excludes_secrets_and_data(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    assert validate_release(package)['version'] == 'v1'
    assert (package / 'static' / 'app.js').read_text() == '// current uncommitted UI'
    assert not (package / '.env').exists()
    assert not (package / 'uploads').exists()


def test_failed_smoke_rolls_back_code_and_worker_image_together(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)
    driver = Driver(fail='v2')
    with pytest.raises(ReleaseError, match='rolled back'):
        activate_release(new, state, driver=driver)
    pointer = json.loads((state / 'active-release.json').read_text())
    assert pointer['version'] == 'v1'
    assert pointer['worker_image'] == 'sha256:v1'
    assert driver.started == ['v2', 'v1']
    assert driver.aborted == 1
    assert driver.paused == 1
    assert driver.resumed == 1
    assert json.loads((state / 'deployment.json').read_text())['status'] == 'rolled_back'


def test_candidate_failure_uses_guarded_abort_instead_of_second_api_drain(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)

    class CandidateNeverStarts(Driver):
        def start_version(self, record):
            self.started.append(record['version'])
            if record['version'] == 'v2':
                raise RuntimeError('candidate import failed before api-state')

        def pause(self):
            self.paused += 1
            if self.paused > 1:
                raise AssertionError('rollback must not request a new API drain')

    driver = CandidateNeverStarts()
    with pytest.raises(ReleaseError, match='rolled back'):
        activate_release(new, state, driver=driver)
    assert driver.aborted == 1
    assert driver.started == ['v2', 'v1']
    assert json.loads((state / 'active-release.json').read_text())['version'] == 'v1'


def test_incompatible_schema_rejected_before_pause(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-2'])
    driver = Driver()
    with pytest.raises(ReleaseError, match='schema'):
        activate_release(package, tmp_path / 'state', driver=driver, bootstrap=True)
    assert driver.paused == 0 and not driver.started


def test_tampered_release_rejected_before_prepare(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    (package / 'app' / 'main.py').write_text('tampered')
    with pytest.raises(ReleaseError, match='integrity'):
        activate_release(package, tmp_path / 'state', driver=Driver(), bootstrap=True)


def test_unsafe_version_and_overwrite_are_rejected(tmp_path):
    root = source(tmp_path)
    with pytest.raises(ReleaseError):
        package_release(root, tmp_path / 'releases', '../escape', compatible_schemas=['schema-1'])
    package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    with pytest.raises(ReleaseError):
        package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])


def test_rollback_failure_keeps_runtime_paused_and_marks_failed(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)
    driver = Driver()
    driver.smoke = lambda record: False
    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(new, state, driver=driver)
    assert driver.resumed == 0
    assert json.loads((state / 'deployment.json').read_text())['status'] == 'rollback_failed'


def test_previous_package_is_revalidated_before_rollback_start(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)
    driver = Driver()

    def smoke(record):
        if record['version'] == 'v2':
            (old / 'app' / 'main.py').write_text('changed while new version was running')
            return False
        return True

    driver.smoke = smoke
    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(new, state, driver=driver)
    assert driver.started == ['v2']
    assert driver.resumed == 0


def test_first_activation_requires_explicit_bootstrap(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    with pytest.raises(ReleaseError, match='bootstrap'):
        activate_release(package, tmp_path / 'state', driver=Driver())


def test_pre_switch_pause_failure_restores_original_state_and_is_retryable(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)
    original_pointer = (state / 'active-release.json').read_bytes()

    class PauseFailure(Driver):
        recovered = False

        def pause(self):
            raise TimeoutError('drain timed out')

        def recover_pause_failure(self):
            self.recovered = True

    driver = PauseFailure()
    with pytest.raises(ReleaseError, match='before switching'):
        activate_release(new, state, driver=driver)
    assert driver.recovered and not driver.started
    assert (state / 'active-release.json').read_bytes() == original_pointer
    assert json.loads((state / 'deployment.json').read_text())['status'] == 'pre_switch_failed'
    assert activate_release(new, state, driver=Driver())['version'] == 'v2'


def test_pre_switch_failed_recovery_keeps_manual_recovery_journal(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    state = tmp_path / 'state'

    class UnrecoverablePause(Driver):
        def pause(self):
            raise TimeoutError('drain timed out')

        def recover_pause_failure(self):
            raise OSError('original workers unavailable')

    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(package, state, driver=UnrecoverablePause(), bootstrap=True)
    assert not (state / 'active-release.json').exists()
    assert json.loads((state / 'deployment.json').read_text())['manual_recovery'] is True
    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(package, state, driver=Driver(), bootstrap=True)


def test_bootstrap_failure_stays_in_maintenance(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    driver = Driver(fail='v1')
    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(package, tmp_path / 'state', driver=driver, bootstrap=True)
    assert driver.resumed == 0


def test_manifest_path_traversal_is_rejected(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    manifest_file = package / 'release-manifest.json'
    manifest = json.loads(manifest_file.read_text())
    manifest['files']['../escape'] = '0' * 64
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(ReleaseError):
        validate_release(package)


def test_missing_manifest_reports_integrity_failure(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    (package / 'release-manifest.json').unlink()
    with pytest.raises(ReleaseError, match='integrity'):
        validate_release(package)


def test_activation_rejects_symlink_package_before_resolving_it(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    alias = tmp_path / 'linked-release'
    try:
        alias.symlink_to(package, target_is_directory=True)
    except OSError:
        pytest.skip('Windows symlink creation requires developer mode or an elevated token')
    with pytest.raises(ReleaseError, match='integrity'):
        activate_release(alias, tmp_path / 'state', driver=Driver(), bootstrap=True)


def test_default_schema_compatibility_is_only_migration_head(tmp_path):
    root = source(tmp_path)
    migrations = root / 'migrations' / 'versions'
    migrations.mkdir(parents=True)
    (migrations / 'one.py').write_text("revision = 'one'\ndown_revision = None")
    (migrations / 'two.py').write_text("revision = 'two'\ndown_revision = 'one'")
    package = package_release(root, tmp_path / 'releases', 'v1')
    assert validate_release(package)['compatible_schemas'] == ['two']


def test_packaging_works_without_python312_path_is_junction(tmp_path, monkeypatch):
    monkeypatch.setattr(type(Path()), 'is_junction', None)
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    assert validate_release(package)['version'] == 'v1'


def test_nested_secrets_and_unmanifested_source_are_rejected(tmp_path):
    root = source(tmp_path)
    (root / 'app' / '.env').write_text('secret')
    package = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    assert not (package / 'app' / '.env').exists()
    (package / 'app' / 'extra.py').write_text('malicious')
    with pytest.raises(ReleaseError, match='integrity'):
        validate_release(package)


def test_unfinished_journal_prevents_new_deployment(tmp_path):
    package = package_release(source(tmp_path), tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'deployment.json').write_text(json.dumps({'status': 'switching'}))
    with pytest.raises(ReleaseError, match='manual recovery'):
        activate_release(package, state, driver=Driver(), bootstrap=True)


def test_previous_pointer_version_must_match_validated_previous_package(tmp_path):
    root, state = source(tmp_path), tmp_path / 'state'
    old = package_release(root, tmp_path / 'releases', 'v1', compatible_schemas=['schema-1'])
    new = package_release(root, tmp_path / 'releases', 'v2', compatible_schemas=['schema-1'])
    activate_release(old, state, driver=Driver(), bootstrap=True)
    pointer_file = state / 'active-release.json'
    pointer = json.loads(pointer_file.read_text())
    pointer['version'] = 'tampered-version'
    pointer_file.write_text(json.dumps(pointer))
    driver = Driver()
    with pytest.raises(ReleaseError, match='pointer'):
        activate_release(new, state, driver=driver)
    assert driver.paused == 0


def test_two_native_http_releases_restore_previous_service_on_bad_health(tmp_path):
    """Actual native subprocesses; Worker image orchestration is separately faked."""
    root, state = source(tmp_path), tmp_path / 'state'
    server = '''
import pathlib, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
VERSION = {version!r}
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if VERSION == 'v1' else 503)
        self.end_headers()
        self.wfile.write(VERSION.encode())
    def log_message(self, *args):
        pass
service = HTTPServer(('127.0.0.1', 0), Handler)
pathlib.Path(sys.argv[1]).write_text(str(service.server_port))
service.serve_forever()
'''
    packages = []
    for version in ('v1', 'v2'):
        (root / 'app' / 'main.py').write_text(server.format(version=version))
        packages.append(package_release(root, tmp_path / 'releases', version,
                                        compatible_schemas=['schema-1']))

    class NativeDriver(Driver):
        process = None
        port = None

        def pause(self):
            super().pause()
            if self.process:
                self.process.terminate()
                self.process.wait(timeout=5)
                self.process = None

        def start_version(self, record):
            super().start_version(record)
            port_file = tmp_path / f"port-{len(self.started)}"
            self.process = subprocess.Popen(
                [sys.executable, str(Path(record['path']) / 'app' / 'main.py'), str(port_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            deadline = time.monotonic() + 8
            while not port_file.exists() and time.monotonic() < deadline:
                assert self.process.poll() is None
                time.sleep(0.02)
            self.port = int(port_file.read_text())

        def smoke(self, record):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/live', timeout=2) as response:
                    return response.status == 200 and response.read().decode() == record['version']
            except urllib.error.HTTPError:
                return False

    driver = NativeDriver()
    try:
        activate_release(packages[0], state, driver=driver, bootstrap=True)
        with pytest.raises(ReleaseError, match='rolled back'):
            activate_release(packages[1], state, driver=driver)
        pointer = json.loads((state / 'active-release.json').read_text())
        assert pointer['version'] == 'v1' and driver.smoke(pointer)
        assert driver.started == ['v1', 'v2', 'v1']
    finally:
        driver.pause()
