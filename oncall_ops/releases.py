"""Immutable source snapshots and fail-closed, journalled application switching.

Database migrations are deliberately outside this module. Compatibility is an
operator declaration; by default only the package's single migration head is allowed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

from oncall_ops.common import atomic_json

DIRECTORIES = {'app', 'mcp_servers', 'oncall_ops', 'oncall_logging', 'static', 'migrations', 'scripts'}
FILES = {'pyproject.toml', 'uv.lock', 'README.md', 'alembic.ini', 'Dockerfile.worker',
         '.dockerignore', 'vector-database.yml'}
EXCLUDED = {'.env', '.venv', 'venv', 'state', 'logs', 'uploads', '__pycache__',
            '.pytest_cache', '.ruff_cache', '.mypy_cache', 'node_modules', '.git'}
MANIFEST = 'release-manifest.json'


class ReleaseError(RuntimeError):
    pass


def _linked(path):
    # Windows junctions/reparse points must also be rejected on Python 3.11,
    # which predates pathlib.Path.is_junction().
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _allowed(value):
    parts = PurePosixPath(value).parts
    if (not parts or '\\' in value or ':' in value or value.startswith('/')
            or any(part in {'.', '..'} for part in parts)):
        return False
    if any(part.lower() in EXCLUDED or part.lower().startswith('.env') for part in parts):
        return False
    return ((len(parts) == 1 and value in FILES)
            or (len(parts) > 1 and parts[0] in DIRECTORIES))


def _heads(root):
    revisions, parents = set(), set()
    for path in sorted((root / 'migrations' / 'versions').glob('*.py')):
        fields = {}
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {'revision', 'down_revision'}:
                        fields[target.id] = ast.literal_eval(node.value)
        if 'revision' in fields:
            revisions.add(fields['revision'])
            prior = fields.get('down_revision')
            parents.update(prior if isinstance(prior, (list, tuple)) else [prior])
    return sorted(revisions - parents)


def _source_files(root):
    for entry in sorted(root.iterdir()):
        if entry.name not in DIRECTORIES | FILES:
            continue
        if _linked(entry):
            raise ReleaseError('Release source links are forbidden')
        if entry.is_dir():
            for current, dirs, files in os.walk(entry, followlinks=False):
                current = Path(current)
                for name in list(dirs):
                    item = current / name
                    if _linked(item):
                        raise ReleaseError('Release source links are forbidden')
                    if name.lower() in EXCLUDED or name.lower().startswith('.env'):
                        dirs.remove(name)
                for name in files:
                    item = current / name
                    if _linked(item):
                        raise ReleaseError('Release source links are forbidden')
                    relative = item.relative_to(root).as_posix()
                    if _allowed(relative) and item.suffix not in {'.pyc', '.pyo'}:
                        yield relative, item
        elif entry.is_file():
            yield entry.name, entry


def package_release(root, releases_root, version, *, compatible_schemas=None) -> Path:
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version):
        raise ReleaseError('Unsafe release version')
    if version.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(10)],
                                        *[f'LPT{i}' for i in range(10)]} or version.endswith('.'):
        raise ReleaseError('Unsafe release version')
    root, releases_root = Path(root).resolve(), Path(releases_root).resolve()
    target = releases_root / version
    if target.exists():
        raise ReleaseError('Release version already exists')
    heads = _heads(root)
    schemas = compatible_schemas if compatible_schemas is not None else heads
    if not schemas or any(not isinstance(value, str) or not value for value in schemas):
        raise ReleaseError('Explicit schema compatibility required without a migration head')
    if compatible_schemas is None and len(heads) != 1:
        raise ReleaseError('Default schema compatibility requires one migration head')
    releases_root.mkdir(parents=True, exist_ok=True)
    staging = releases_root / f'.{version}-{uuid.uuid4().hex}.partial'
    staging.mkdir()
    try:
        hashes = {}
        for relative, path in _source_files(root):
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
            hashes[relative] = _digest(output)
        atomic_json(staging / MANIFEST, {'format': 1, 'version': version, 'files': hashes,
                                       'compatible_schemas': sorted(set(schemas)),
                                       'migration_heads': heads})
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return target


def validate_release(path) -> dict:
    path = Path(path)
    try:
        if _linked(path) or _linked(path / MANIFEST):
            raise ReleaseError('Release integrity: linked root or manifest')
        manifest = json.loads((path / MANIFEST).read_text(encoding='utf-8'))
        if manifest['format'] != 1 or not manifest['compatible_schemas']:
            raise ValueError('Invalid manifest')
        expected = manifest['files']
        if not isinstance(expected, dict) or any(not _allowed(name) for name in expected):
            raise ValueError('Unsafe manifest path')
        actual = dict(_source_files(path))
        if set(actual) != set(expected):
            raise ValueError('Unmanifested source')
        for name, digest in expected.items():
            if not re.fullmatch('[0-9a-f]{64}', digest) or _digest(actual[name]) != digest:
                raise ValueError('SHA256 mismatch')
        if _heads(path) != manifest['migration_heads']:
            raise ValueError('Migration head mismatch')
        return manifest
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        raise ReleaseError('Release integrity validation failed') from exc


def activate_release(package, state, *, driver, bootstrap=False) -> dict:
    package, state = Path(package).absolute(), Path(state).resolve()
    manifest = validate_release(package)
    package = package.resolve()
    state.mkdir(parents=True, exist_ok=True)
    lock = state / 'deployment.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ReleaseError('Deployment already locked; inspect journal for manual recovery') from exc
    os.close(descriptor)
    pointer, journal = state / 'active-release.json', state / 'deployment.json'
    try:
        if journal.exists():
            prior_status = json.loads(journal.read_text(encoding='utf-8')).get('status')
            if prior_status not in {'success', 'rolled_back', 'preflight_failed', 'pre_switch_failed'}:
                raise ReleaseError('Unfinished deployment requires manual recovery')
        previous = json.loads(pointer.read_text(encoding='utf-8')) if pointer.exists() else None
        if previous is None and not bootstrap:
            raise ReleaseError('First deployment requires explicit bootstrap=True')
        schema = driver.schema()
        if schema not in manifest['compatible_schemas']:
            raise ReleaseError('Current schema is incompatible with this release')
        if previous:
            old = validate_release(Path(previous['path']))
            if old['version'] != previous['version']:
                raise ReleaseError('Rollback pointer does not match package version')
            if schema not in old['compatible_schemas']:
                raise ReleaseError('Current schema is incompatible with rollback release')
        atomic_json(journal, {'status': 'preparing', 'version': manifest['version']})
        try:
            prepared = driver.prepare(package, manifest)
            if not prepared.get('python') or not prepared.get('worker_image'):
                raise ReleaseError('Preparation omitted Python or immutable worker image')
        except Exception as exc:
            atomic_json(journal, {'status': 'preflight_failed', 'version': manifest['version']})
            raise ReleaseError('Release preparation failed before switching') from exc
        record = {'version': manifest['version'], 'path': str(package), 'schema': schema,
                  'python': prepared['python'], 'worker_image': prepared['worker_image'],
                  'previous': {key: value for key, value in previous.items() if key != 'previous'}
                  if previous else None}
        atomic_json(journal, {'status': 'pausing', 'version': record['version']})
        try:
            driver.pause()
        except Exception as pause_failure:
            try:
                # No code/image pointer has changed. The adapter restores only
                # resources actually running before this pause attempt.
                driver.recover_pause_failure()
            except Exception as recovery_failure:
                atomic_json(journal, {'status': 'pre_switch_recovery_failed',
                                      'version': record['version'], 'manual_recovery': True})
                raise ReleaseError('Pre-switch pause failed; manual recovery required, maintenance retained') from recovery_failure
            atomic_json(journal, {'status': 'pre_switch_failed', 'version': record['version']})
            raise ReleaseError('Release failed before switching; original runtime restored, deployment may be retried') from pause_failure
        try:
            atomic_json(journal, {'status': 'switching', 'version': record['version']})
            atomic_json(pointer, record)
            driver.start_version(record)
            if not driver.smoke(record):
                raise ReleaseError('Release smoke failed')
            driver.resume()
            atomic_json(journal, {'status': 'success', 'version': record['version']})
            return record
        except Exception as failure:
            # Stop all new writers again before attempting previous code and image.
            try:
                if previous is None:
                    raise ReleaseError('No previous release')
                driver.abort_candidate(previous)
                rollback_manifest = validate_release(Path(previous['path']))
                if (rollback_manifest['version'] != previous['version']
                        or driver.schema() not in rollback_manifest['compatible_schemas']):
                    raise ReleaseError('Rollback package identity or current schema became incompatible')
                atomic_json(journal, {'status': 'rolling_back', 'version': previous['version']})
                atomic_json(pointer, previous)
                driver.start_version(previous)
                if not driver.smoke(previous):
                    raise ReleaseError('Rollback smoke failed')
                driver.resume()
            except Exception as rollback_failure:
                try:
                    driver.pause()
                except Exception:
                    pass  # Maintenance intent remains asserted; journal records failure.
                atomic_json(journal, {'status': 'rollback_failed', 'version': record['version'],
                                      'manual_recovery': True})
                raise ReleaseError('Deployment failed; manual recovery required, maintenance retained') from rollback_failure
            atomic_json(journal, {'status': 'rolled_back', 'version': previous['version'],
                                  'failed_version': record['version']})
            raise ReleaseError('Release failed and rolled back to previous code and worker image') from failure
    finally:
        lock.unlink()
