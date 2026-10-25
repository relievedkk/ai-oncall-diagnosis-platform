"""Argument-array Docker adapter. No shell-built commands or credential logging."""

from __future__ import annotations

import json
import os
import re
import shutil

# Deliberate argument-array Docker CLI adapter; no shell is used.
import subprocess  # nosec B404
import time
import uuid
from pathlib import Path


class DockerUnavailable(RuntimeError):
    pass


class DockerRunner:
    def __init__(self, executable: str = 'docker', *, compose_file='vector-database.yml', timeout=120,
                 environment=None, env_file=None):
        self.executable = shutil.which(executable)
        if self.executable is None:
            raise DockerUnavailable('Docker CLI is unavailable; install/start Docker Desktop or set --docker')
        self.compose_file, self.timeout = compose_file, timeout
        self.environment, self.env_file = dict(environment or {}), env_file

    def run(self, args, *, timeout=None, cwd=None, env=None, input_text=None):
        args = list(args)
        if args and args[0] == 'compose' and getattr(self, 'env_file', None):
            args[1:1] = ['--env-file', self.env_file]
        effective_env = {**os.environ, **getattr(self, 'environment', {}), **(env or {})}
        result = subprocess.run([self.executable, *map(str, args)], cwd=cwd, capture_output=True,  # nosec B603
                                text=True, encoding='utf-8', errors='replace',
                                timeout=timeout or self.timeout, check=False,
                                env=effective_env, input=input_text,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise DockerUnavailable(f'Docker operation {str(args[0])!r} failed (exit {result.returncode})')
        return result.stdout

    def discover(self, root: Path):
        ids = self.run(['compose', '-f', self.compose_file, 'ps', '-aq'], cwd=root).split()
        if not ids:
            raise DockerUnavailable('Compose stack has no created containers')
        records = json.loads(self.run(['inspect', *ids]))
        result = []
        for record in records:
            service = record['Config']['Labels'].get('com.docker.compose.service')
            mounts = [{'name': item['Name'], 'destination': item['Destination']}
                      for item in record['Mounts'] if item['Type'] == 'volume']
            if service in {'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'} and not mounts:
                raise DockerUnavailable('Persistent component is missing its named volume')
            if service:
                # Only allowlisted metadata; inspect Config.Env is deliberately discarded.
                node = next((value.split('=', 1)[1] for value in record['Config'].get('Env', [])
                             if value.startswith('RABBITMQ_NODENAME=')), None)
                result.append({'id': record['Id'], 'service': service,
                               'running': bool(record['State']['Running']), 'mounts': mounts,
                               'image': record['Image'], 'hostname': record['Config']['Hostname'],
                               'nodename': node if service == 'rabbitmq' else None})
        return result

    def stop(self, containers):
        order = {'standalone': 10, 'minio': 11, 'etcd': 12, 'rabbitmq': 13, 'postgres': 14}
        for item in sorted(containers, key=lambda value: order.get(value['service'], 0)):
            if item['running']:
                self.run(['stop', '--time', '40', item['id']])
        records = json.loads(self.run(['inspect', *[item['id'] for item in containers]]))
        if any(item['State']['Running'] or item['State'].get('ExitCode') == 137 for item in records):
            raise DockerUnavailable('Container did not stop cleanly; snapshot aborted')

    def start(self, containers):
        order = {'postgres': 0, 'rabbitmq': 1, 'etcd': 2, 'minio': 3, 'standalone': 4}
        for item in sorted(containers, key=lambda value: order.get(value['service'], 10)):
            self.run(['start', item['id']])

    def assert_stopped_writers(self, volumes):
        ids = self.run(['ps', '-q']).split()
        if ids:
            records = json.loads(self.run(['inspect', *ids]))
            if any(mount.get('Name') in volumes and mount.get('RW', True)
                   for item in records for mount in item['Mounts']):
                raise DockerUnavailable('A running writer still mounts a source volume')

    def archive_volume(self, volume: str, archive: Path, image: str):
        self._volume_helper(volume, archive, image, restore=False)

    def new_volume(self, name: str, owner: str):
        existing = self.run(['volume', 'ls', '--format', '{{.Name}}']).splitlines()
        if name in existing:
            raise DockerUnavailable('Restore target volume already exists')
        self.run(['volume', 'create', '--label', f'oncall.drill.owner={owner}', name])
        data = json.loads(self.run(['volume', 'inspect', name]))[0]
        if data.get('Labels', {}).get('oncall.drill.owner') != owner:
            raise DockerUnavailable('Restore volume ownership mismatch')

    def restore_volume(self, name: str, archive: Path, image: str):
        self._volume_helper(name, archive, image, restore=True)

    def _volume_helper(self, volume, archive, image, *, restore):
        from oncall_ops.common import atomic_json
        self.safe_name(volume)
        if not image.startswith('sha256:'):
            raise DockerUnavailable('Volume helper requires an existing immutable image ID')
        owner = 'helper-' + uuid.uuid4().hex
        journal = {'owner': owner, 'resources': [{'kind': 'container', 'name': owner}], 'status': 'running'}
        journal_path = archive.parent / f'helper-ownership-{owner}.json'
        atomic_json(journal_path, journal)
        args = ['create', '--name', owner, '--pull=never', '--label', f'oncall.drill.owner={owner}',
                '--network', 'none', '--user', '0:0', '--entrypoint', 'tar',
                '--mount', f'type=volume,src={volume},dst=/data' + ('' if restore else ',readonly'),
                image, '-xpf' if restore else '-cpf', '/tmp/oncall-volume.tar', '-C', '/data']
        if not restore:
            args.append('.')
        succeeded = False
        try:
            self.run(args)
            if restore:
                self.run(['cp', archive, owner + ':/tmp/oncall-volume.tar'], timeout=1800)
            self.run(['start', '--attach', owner], timeout=1800)
            if not restore:
                self.run(['cp', owner + ':/tmp/oncall-volume.tar', archive], timeout=1800)
            succeeded = True
        finally:
            journal['cleanup_errors'] = self.cleanup_owned(journal)
            journal['status'] = 'removed-by-owner' if not journal['cleanup_errors'] else 'cleanup-unconfirmed'
            atomic_json(journal_path, journal)
            if succeeded and journal['cleanup_errors']:
                raise DockerUnavailable('Owned volume helper cleanup was not confirmed')

    @staticmethod
    def safe_name(value: str):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value):
            raise DockerUnavailable('Invalid managed Docker resource name')
        return value

    def stop_writers(self, containers):
        persistent = {'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'}
        writers = [item for item in containers if item['service'] not in persistent]
        if writers:
            self.stop(writers)

    def quiesce_vectors(self, containers):
        from pymilvus import MilvusClient
        record = next(item for item in containers if item['service'] == 'standalone')
        client = MilvusClient(uri=f"http://127.0.0.1:{self._port(record['id'], 19530)}", timeout=30)
        self.vector_resume = []
        try:
            for name in sorted(client.list_collections()):
                client.flush(collection_name=name)
                if int(client.get_load_state(collection_name=name)['state']) in (2, 3):
                    # Record before releasing, so even partial failure can restore original state.
                    self.vector_resume.append(name)
                    client.release_collection(name)
        finally:
            client.close()
        return list(self.vector_resume)

    def resume_vectors(self, containers):
        loaded = getattr(self, 'vector_resume', [])
        if not loaded:
            return
        from pymilvus import MilvusClient
        self.wait_healthy([item for item in containers if item['service'] in
                           {'postgres', 'rabbitmq', 'etcd', 'minio', 'standalone'}])
        record = next(item for item in containers if item['service'] == 'standalone')
        client = MilvusClient(uri=f"http://127.0.0.1:{self._port(record['id'], 19530)}", timeout=30)
        try:
            for name in loaded:
                client.load_collection(name)
            deadline = time.monotonic() + getattr(self, 'vector_load_timeout', 30)
            pending = set(loaded)
            while pending and time.monotonic() < deadline:
                pending = {name for name in pending if int(
                    client.get_load_state(collection_name=name)['state']) != 3}
                if pending:
                    time.sleep(0.2)
            if pending:
                raise DockerUnavailable('Restored Milvus collection did not reach a loaded load state')
        finally:
            client.close()

    @staticmethod
    def compose_spec(images, volumes, owner, credentials, *, rabbit_hostname, rabbit_nodename=None):
        """Compose contains references to ephemeral environment credentials, never their values."""
        paths = {'postgres': '/var/lib/postgresql/data', 'rabbitmq': '/var/lib/rabbitmq',
                 'etcd': '/etcd', 'minio': '/minio_data', 'standalone': '/var/lib/milvus'}
        services = {}
        for service, image in images.items():
            if not image.startswith('sha256:'):
                raise DockerUnavailable('Only locally inspected immutable image IDs are allowed')
            services[service] = {'image': image, 'pull_policy': 'never',
                                 'container_name': f'{owner}-{service}',
                                 'labels': {'oncall.drill.owner': owner},
                                 'volumes': [f'{service}:{paths[service]}']}
        services['postgres']['environment'] = {
            key: '${ONCALL_DRILL_' + key + '}' for key in ('POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD')}
        services['rabbitmq'].update(hostname=rabbit_hostname, ports=['127.0.0.1::15672'], environment={
            key: '${ONCALL_DRILL_' + key + '}' for key in ('RABBITMQ_DEFAULT_USER', 'RABBITMQ_DEFAULT_PASS')})
        if rabbit_nodename:
            services['rabbitmq']['environment']['RABBITMQ_NODENAME'] = rabbit_nodename
        services['etcd']['command'] = ['etcd', '--advertise-client-urls=http://etcd:2379',
                                        '--listen-client-urls=http://0.0.0.0:2379', '--data-dir=/etcd']
        services['minio'].update(command=['minio', 'server', '/minio_data'], environment={
            key: '${ONCALL_DRILL_' + key + '}' for key in ('MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY')})
        services['standalone'].update(command=['milvus', 'run', 'standalone'],
                                     environment={'ETCD_ENDPOINTS': 'etcd:2379', 'MINIO_ADDRESS': 'minio:9000',
                                                  'MINIO_ACCESS_KEY_ID': '${ONCALL_DRILL_MINIO_ACCESS_KEY}',
                                                  # Environment placeholder, never a hardcoded credential.
                                                  'MINIO_SECRET_ACCESS_KEY': '${ONCALL_DRILL_MINIO_SECRET_KEY}'},  # nosec B105
                                     ports=['127.0.0.1::19530'], depends_on=['etcd', 'minio'])
        return {'name': owner, 'services': services,
                'volumes': {service: {'external': True, 'name': name} for service, name in volumes.items()},
                # A distinct bridge is necessary: Docker 29 internal networks suppress
                # even explicit localhost port publication, preventing host verification.
                'networks': {'default': {'name': owner + '-network',
                                         'labels': {'oncall.drill.owner': owner}}}}

    def cleanup_owned(self, journal):
        """Remove only positively re-inspected owner matches, in reverse creation order."""
        failures = []
        for resource in reversed(journal['resources']):
            kind, name = resource['kind'], resource['name']
            if kind not in {'container', 'volume', 'network'}:
                failures.append(f'{kind}:{name}:invalid-kind')
                continue
            try:
                record = json.loads(self.run([kind, 'inspect', name]))[0]
                labels = record.get('Config', {}).get('Labels', {}) if kind == 'container' else record.get('Labels', {})
                if (labels or {}).get('oncall.drill.owner') != journal['owner']:
                    failures.append(f'{kind}:{name}:ownership-mismatch')
                    continue
                self.run([kind, 'rm', *(['--force'] if kind == 'container' else []), name])
            except (DockerUnavailable, KeyError, ValueError):
                failures.append(f'{kind}:{name}:cleanup-unconfirmed')
        return failures

    def _compose_start(self, spec, owner, target, credentials):
        from oncall_ops.common import atomic_json
        compose = target / 'isolated-compose.json'
        atomic_json(compose, spec)
        journal_path = target / 'ownership.json'
        journal = json.loads(journal_path.read_text(encoding='utf-8'))
        # Journal before mutation also covers interruption during compose create.
        journal['resources'].append({'kind': 'network', 'name': owner + '-network'})
        journal['resources'].extend({'kind': 'container', 'name': item['container_name']}
                                    for item in spec['services'].values())
        atomic_json(journal_path, journal)
        environment = {'ONCALL_DRILL_' + key: value for key, value in credentials.items()
                       if isinstance(value, str)}
        self.run(['compose', '-f', compose, '-p', owner, 'up', '-d', '--no-build', '--pull', 'never'],
                 env=environment, timeout=240)
        adapter = DockerRunner(self.executable, compose_file=str(compose), timeout=self.timeout)
        adapter.credentials = credentials
        records = adapter.discover(target)
        adapter.wait_healthy(records)
        return adapter, records

    def wait_healthy(self, containers, timeout=240):
        from http.client import HTTPException
        from urllib.error import URLError
        deadline = time.monotonic() + timeout
        checks = {'postgres': ['pg_isready'], 'rabbitmq': ['rabbitmq-diagnostics', '-q', 'ping'],
                  'etcd': ['etcdctl', 'endpoint', 'health'],
                  'minio': ['curl', '-fsS', 'http://localhost:9000/minio/health/live'],
                  'standalone': ['curl', '-fsS', 'http://localhost:9091/healthz']}
        pending = {item['service']: item for item in containers}
        while pending and time.monotonic() < deadline:
            for service, item in list(pending.items()):
                try:
                    self.run(['exec', item['id'], *checks[service]], timeout=10)
                    if service == 'rabbitmq':
                        # Erlang ping succeeds before the management listener is ready.
                        self._rabbit(item['id'], 'GET', 'overview')
                    del pending[service]
                except (DockerUnavailable, OSError, ValueError, HTTPException, URLError):
                    pass
            if pending:
                time.sleep(1)
        if pending:
            raise DockerUnavailable('Isolated components did not become healthy before deadline')

    def launch_restored(self, manifest, mappings, owner, target, credentials):
        sources = {item['name'] for item in manifest['volumes']}
        if sources.intersection(mappings.values()) or len(set(mappings.values())) != len(mappings):
            raise DockerUnavailable('Restore volume aliases source or another target')
        required = {'POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD', 'RABBITMQ_DEFAULT_USER',
                    'RABBITMQ_DEFAULT_PASS', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY'}
        if not required.issubset(credentials):
            raise DockerUnavailable('Explicit restore credentials are incomplete')
        baseline = manifest.get('baseline')
        if not baseline or not all(key in baseline for key in ('postgres', 'rabbitmq', 'milvus')):
            raise DockerUnavailable('Backup lacks comparable application baseline; cannot verify recovery')
        records = {item['service']: item for item in manifest['containers']}
        images = {key: records[key]['image'] for key in ('postgres', 'rabbitmq', 'etcd', 'minio', 'standalone')}
        volumes = {item['service']: mappings[item['name']] for item in manifest['volumes']}
        spec = self.compose_spec(images, volumes, owner, credentials,
                                 rabbit_hostname=records['rabbitmq']['hostname'],
                                 rabbit_nodename=records['rabbitmq'].get('nodename'))
        # Reproduce all inspected mount destinations, including multiple volumes per service.
        spec['volumes'] = {}
        for service, record in records.items():
            if service not in spec['services']:
                continue
            mounts = []
            for index, mount in enumerate(record['mounts']):
                alias = f'{service}-{index}'
                spec['volumes'][alias] = {'external': True, 'name': mappings[mount['name']]}
                mounts.append(f"{alias}:{mount['destination']}")
            spec['services'][service]['volumes'] = mounts
        adapter, containers = self._compose_start(spec, owner, target, credentials)
        observed = adapter.capture(containers)
        checks = {key: observed[key] == baseline[key] for key in ('postgres', 'rabbitmq', 'milvus')}
        if all(checks.values()):
            adapter.vector_resume = list(manifest.get('milvus_loaded_collections', []))
            adapter.resume_vectors(containers)
        return {'verified': all(checks.values()), 'checks': checks,
                'components': list(images), 'observed': observed}

    def _port(self, container, port):
        bindings = json.loads(self.run(['inspect', container]))[0]['NetworkSettings']['Ports'].get(str(port) + '/tcp')
        if not bindings or bindings[0]['HostIp'] not in {'127.0.0.1', '::1'}:
            raise DockerUnavailable('Verification requires a localhost-only published component port')
        return int(bindings[0]['HostPort'])

    def _rabbit(self, container, method, path, payload=None):
        import base64
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, request, file_pointer, code, message, headers, new_url):
                return None
        credentials = getattr(self, 'credentials', {})
        auth = (credentials['RABBITMQ_DEFAULT_USER'] + ':' + credentials['RABBITMQ_DEFAULT_PASS']).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self._port(container, 15672)}/api/{path}',
                                         data=json.dumps(payload).encode() if payload is not None else None,
                                         method=method, headers={'Authorization': 'Basic ' + base64.b64encode(auth).decode(),
                                                                 'Content-Type': 'application/json'})
        # Literal localhost HTTP endpoint, and redirects cannot forward Authorization.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=15) as response:
            payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                raise DockerUnavailable('Rabbit verification response exceeds bounded size')
            return json.loads(payload) if payload else {}

    def _sql(self, container, sql):
        credentials = getattr(self, 'credentials', {})
        return self.run(['exec', '-i', container, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-A', '-t',
                         '-U', credentials.get('POSTGRES_USER', 'oncall'),
                         '-d', credentials.get('POSTGRES_DB', 'oncall')], input_text=sql, timeout=120).strip()

    def capture(self, containers):
        """Content fingerprints, schema and synthetic nearest neighbour; health alone never passes."""
        import hashlib
        import urllib.parse
        records = {item['service']: item for item in containers}
        postgres = records['postgres']['id']
        table_names = self._sql(postgres, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;").splitlines()
        tables = {}
        for table in table_names:
            quoted = '"' + table.replace('"', '""') + '"'
            # Aggregate hash is bounded in host memory; database sorts hashes rather than whole rows.
            # PostgreSQL identifier is double-quoted with embedded quotes escaped above.
            value = self._sql(postgres, f"SELECT count(*) || ':' || md5(COALESCE(string_agg(h, '' ORDER BY h),'')) "  # nosec B608
                              f"FROM (SELECT md5(row_to_json(t)::text) h FROM {quoted} t) s;")
            tables[table] = value
        schema = self._sql(postgres, 'SELECT version_num FROM alembic_version ORDER BY version_num;').splitlines()
        version = self._sql(postgres, 'SHOW server_version;')
        rabbit = records['rabbitmq']['id']
        queues = {}
        for queue in self._rabbit(rabbit, 'GET', 'queues'):
            if queue.get('consumers', 0):
                raise DockerUnavailable('Queue consumers still active during cold backup baseline')
            reported_count = queue.get('messages', 0)
            if reported_count > 10000:
                raise DockerUnavailable('Queue baseline exceeds bounded drill verification limit')
            messages = self._rabbit(rabbit, 'POST', 'queues/' + urllib.parse.quote(queue['vhost'], safe='') + '/' +
                                    urllib.parse.quote(queue['name'], safe='') + '/get',
                                    {'count': 10001, 'ackmode': 'ack_requeue_true',
                                     'encoding': 'base64', 'truncate': 1048576})
            count = len(messages)
            bodies = sorted(json.dumps({'payload': item['payload'], 'properties': item['properties']}, sort_keys=True)
                            for item in messages)
            incomplete = ((not messages and reported_count > 0)
                          or (messages and messages[-1].get('message_count') != 0))
            if (count > 10000 or incomplete
                    or any(item.get('payload_bytes', 0) > 1048576 for item in messages)):
                raise DockerUnavailable('Queue baseline is incomplete')
            queues[queue['vhost'] + '/' + queue['name']] = {
                'messages': count, 'durable': queue['durable'],
                'sha256': hashlib.sha256(json.dumps(bodies).encode()).hexdigest()}
        from pymilvus import MilvusClient
        client = MilvusClient(uri=f"http://127.0.0.1:{self._port(records['standalone']['id'], 19530)}", timeout=30)
        try:
            collections = {}
            for name in sorted(client.list_collections()):
                # Writers are drained. Persist pending segments before counting rows.
                client.flush(collection_name=name)
                description = client.describe_collection(name)
                fields = [{'name': field['name'], 'type': int(field['type']), 'params': field.get('params', {})}
                          for field in description['fields']]
                collections[name] = {'fields': fields, 'row_count': client.get_collection_stats(name)['row_count']}
            # The drill collection uses a saved 4D vector; it never invokes an embedding provider.
            if 'oncall_restore_fixture' in collections:
                previously_loaded = int(client.get_load_state(collection_name='oncall_restore_fixture')['state']) in (2, 3)
                client.load_collection('oncall_restore_fixture')
                result = client.search(collection_name='oncall_restore_fixture', data=[[1.0, 0.0, 0.0, 0.0]],
                                       limit=1, output_fields=['tag'])
                collections['oncall_restore_fixture']['sample'] = [
                    {'id': hit['id'], 'tag': hit['entity']['tag']} for hit in result[0]]
                if collections['oncall_restore_fixture']['sample'] != [{'id': 1, 'tag': 'synthetic'}]:
                    raise DockerUnavailable('Synthetic vector query returned unexpected result')
                if not previously_loaded:
                    client.release_collection('oncall_restore_fixture')
        finally:
            client.close()
        return {'postgres': {'schema': schema, 'server_version': version, 'tables': tables},
                'rabbitmq': queues, 'milvus': collections}
