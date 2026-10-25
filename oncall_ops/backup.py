"""Cold snapshots with validated archives; restore never targets source resources."""

from __future__ import annotations

import hashlib
import json
import os

# Bounded owner-only Windows ACL provisioning with a constant script.
import subprocess  # nosec B404
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

from oncall_ops.common import atomic_json


class BackupError(RuntimeError):
    pass


def private_directory(path: Path) -> None:
    """Protect this new owned directory before writing any backup data into it."""
    if os.name != 'nt':
        path.chmod(0o700)
        return
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = ("$ErrorActionPreference='Stop';"
              "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
              "$acl=[Security.AccessControl.DirectorySecurity]::new();"
              "$acl.SetAccessRuleProtection($true,$false);"
              "$rule=[Security.AccessControl.FileSystemAccessRule]::new("
              "$sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow');"
              "$acl.AddAccessRule($rule);"
              "([IO.DirectoryInfo]::new($env:ONCALL_PRIVATE_DIRECTORY)).SetAccessControl($acl)")
    # Constant script; the generated directory is supplied as data in the environment.
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],  # nosec B603
                            env={**os.environ, 'ONCALL_PRIVATE_DIRECTORY': str(path)},
                            capture_output=True, timeout=15, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise BackupError('Could not protect backup directory permissions')


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_archive(path: Path) -> dict:
    names, total = set(), 0
    try:
        with tarfile.open(path, 'r:') as archive:
            for member in archive:
                # Backslashes and drive prefixes are separators on a Windows restore host.
                name = member.name.replace('\\', '/')
                parts = PurePosixPath(name).parts
                canonical = str(PurePosixPath(name)).casefold()
                if (name.startswith('/') or '..' in parts or any(':' in part for part in parts)
                        or any(part.rstrip(' .') != part for part in parts)
                        or any(part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                               *(f'COM{index}' for index in range(10)),
                               *(f'LPT{index}' for index in range(10))} for part in parts)
                        or canonical in names or not (member.isfile() or member.isdir())):
                    raise BackupError('Unsafe or duplicate archive member')
                names.add(canonical)
                if len(names) > 1_000_000:
                    raise BackupError('Archive exceeds maximum member count')
                total += member.size
                if total > 1024 ** 4:
                    raise BackupError('Archive exceeds declared maximum unpacked size')
    except (OSError, tarfile.TarError) as exc:
        raise BackupError('Archive could not be read') from exc
    return {'members': len(names), 'bytes': total}


def _safe_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
        raise BackupError('Invalid manifest archive path')
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise BackupError('Archive path escapes backup')
    return path


def validate_backup(backup: Path) -> dict:
    backup = Path(backup).resolve()
    try:
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        if manifest['format'] != 1 or manifest['consistency'] != 'cold-stopped-writers':
            raise BackupError('Unsupported backup format')
        files = manifest['archives']
        expected = {volume['archive'] for volume in manifest['volumes']} | {'files.tar'}
        if set(files) != expected or len(manifest['volumes']) != len(expected) - 1:
            raise BackupError('Incomplete backup manifest')
        for name, metadata in files.items():
            path = _safe_file(backup, name)
            if path.stat().st_size != metadata['size'] or digest(path) != metadata['sha256']:
                raise BackupError('Backup integrity verification failed')
            validate_archive(path)
        required = {'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'}
        if not required.issubset({item['service'] for item in manifest['containers']}):
            raise BackupError('Required persistent component missing')
        names = [volume['name'] for volume in manifest['volumes']]
        if len(names) != len(set(names)) or any(not isinstance(name, str) or not name for name in names):
            raise BackupError('Duplicate or invalid source volume identity')
        if not required.issubset({volume['service'] for volume in manifest['volumes']}):
            raise BackupError('Required persistent volume missing')
        return manifest
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise BackupError('Invalid or incomplete backup manifest') from exc


def _archive_files(root: Path, destination: Path, upload_root: Path, config_root: Path):
    with tarfile.open(destination, 'w') as archive:
        for prefix, folder in [('uploads', upload_root), ('config', config_root / 'config')]:
            if not folder.is_dir() or folder.is_symlink():
                raise BackupError('Required source directory missing or linked')
            for path in sorted(folder.rglob('*')):
                path.resolve().relative_to(folder.resolve())
                if path.is_symlink():
                    raise BackupError('Linked data is not allowed in a cold backup')
                if path.is_file():
                    archive.add(path, arcname=f'{prefix}/{path.relative_to(folder).as_posix()}', recursive=False)


def create_backup(root: Path, destination: Path, *, docker, runtime,
                  data_root: Path | None = None, config_root: Path | None = None) -> Path:
    root, destination = Path(root).resolve(), Path(destination).resolve()
    data_root, config_root = Path(data_root or root).resolve(), Path(config_root or root).resolve()
    if destination.is_relative_to(data_root / 'uploads'):
        raise BackupError('Backup destination must be outside uploaded data')
    destination.mkdir(parents=True, exist_ok=True)
    identifier = time.strftime('%Y%m%dT%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:12]
    temporary, final = destination / f'.pending-{identifier}', destination / identifier
    temporary.mkdir()
    private_directory(temporary)
    started, containers, stopped, result_error, restart_errors = time.time(), [], False, None, []
    stage = 'drain'
    try:
        runtime.pause()  # Must include confirmed admission/drain and graceful API pause.
        stage = 'discovery'
        containers = docker.discover(root)
        if not {'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'}.issubset(
                {container['service'] for container in containers}):
            raise BackupError('All five persistent components are required')
        # Capture comparable live state after writers drain, before persistent stores stop.
        # Set before stop_writers too: even a partial writer stop must restart originals.
        stopped = True
        stage = 'writer-stop'
        if hasattr(docker, 'stop_writers'):
            docker.stop_writers(containers)
        stage = 'baseline'
        baseline = docker.capture(containers) if hasattr(docker, 'capture') else None
        stage = 'vector-quiesce'
        loaded_collections = docker.quiesce_vectors(containers) if hasattr(docker, 'quiesce_vectors') else []
        stage = 'database-stop'
        docker.stop(containers)
        volumes, seen = [], set()
        for container in containers:
            for mount in container['mounts']:
                if mount['name'] in seen:
                    continue
                seen.add(mount['name'])
                volumes.append({'name': mount['name'], 'destination': mount['destination'],
                                'service': container['service'], 'archive': f'volume-{len(volumes)}.tar'})
        if not volumes:
            raise BackupError('No persistent volumes discovered')
        stage = 'writer-check'
        docker.assert_stopped_writers(seen)
        helper = next(item['image'] for item in containers if item['service'] == 'postgres')
        for volume in volumes:
            stage = 'volume-archive:' + volume['service']
            docker.archive_volume(volume['name'], temporary / volume['archive'], helper)
        stage = 'file-archive'
        _archive_files(root, temporary / 'files.tar', data_root / 'uploads', config_root)
        archives = {}
        for path in sorted(temporary.glob('*.tar')):
            archives[path.name] = {**validate_archive(path), 'size': path.stat().st_size, 'sha256': digest(path)}
        manifest = {
            'format': 1, 'consistency': 'cold-stopped-writers', 'id': identifier,
            'started_at': started, 'snapshot_at': time.time(), 'containers': containers,
            'volumes': volumes, 'archives': archives,
            'baseline': baseline,
            'milvus_loaded_collections': loaded_collections,
            'release': runtime.release_identity() if hasattr(runtime, 'release_identity') else 'development',
        }
        atomic_json(temporary / 'manifest.json', manifest)
        validate_backup(temporary)
        os.replace(temporary, final)
    except Exception as exc:
        result_error = exc
    finally:
        if stopped:
            try:
                docker.start([item for item in containers if item['running']])
                if hasattr(docker, 'resume_vectors'):
                    docker.resume_vectors(containers)
            except Exception as exc:
                restart_errors.append(type(exc).__name__)
        try:
            runtime.resume()
        except Exception as exc:
            restart_errors.append(type(exc).__name__)
    if result_error is not None or restart_errors:
        failure = {'stage': stage, 'error_type': type(result_error).__name__ if result_error else None,
                   'restart_errors': restart_errors}
        # Persist only controlled stage names and exception classes, never subprocess output.
        failure_root = final if final.exists() else temporary
        atomic_json(failure_root / 'backup-failure.json', failure)
        # No raw subprocess output, URLs, environment values or credentials in public errors.
        raise BackupError(f'Backup failed ({type(result_error).__name__ if result_error else "restart"}); '
                          f'stage={stage}; restart_errors={restart_errors}; inspect local operational state') from result_error
    return final


def extract_files(backup: Path, target: Path) -> None:
    validate_backup(backup)
    target = Path(target)
    if target.exists() and (target.is_symlink() or any(target.iterdir())):
        raise BackupError('Restore file target must be new and empty')
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(Path(backup) / 'files.tar', 'r:') as archive:
        # Validation above rejects links and all Windows/POSIX traversal forms.
        for member in archive:
            destination = (target / member.name).resolve()
            destination.relative_to(target.resolve())
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('xb') as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        output.write(chunk)
