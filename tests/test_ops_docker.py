import json

import pytest

from oncall_ops.docker import DockerRunner, DockerUnavailable


def runner():
    adapter = object.__new__(DockerRunner)
    adapter.executable = 'docker'
    adapter.timeout = 1
    adapter.compose_file = 'fixture.json'
    return adapter


def test_helper_is_owned_and_never_pulls(tmp_path):
    adapter = runner()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'inspect' in args:
            owner = calls[0][calls[0].index('--name') + 1]
            return json.dumps([{'Config': {'Labels': {'oncall.drill.owner': owner}}}])
        return ''
    adapter.run = run
    adapter.archive_volume('source', tmp_path / 'data.tar', 'sha256:fixture')
    command = calls[0]
    assert '--pull=never' in command
    assert '--label' in command and any(str(value).startswith('oncall.drill.owner=') for value in command)
    assert command[command.index('--network') + 1] == 'none'


def test_cleanup_refuses_foreign_resource_and_only_removes_owned():
    adapter = runner()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'inspect' in args:
            return json.dumps([{'Labels': {'oncall.drill.owner': 'foreign'}}])
        return ''
    adapter.run = run
    result = adapter.cleanup_owned({'owner': 'mine', 'resources': [{'kind': 'volume', 'name': 'important'}]})
    assert result == ['volume:important:ownership-mismatch']
    assert not any('rm' in call for call in calls)


def test_fixture_has_only_persistent_components_and_fixed_image_ids(tmp_path):
    adapter = runner()
    images = {service: 'sha256:' + service for service in ('postgres', 'rabbitmq', 'etcd', 'minio', 'standalone')}
    spec = adapter.compose_spec(images, {service: service + '-new' for service in images}, 'abc', {},
                                rabbit_hostname='original-node')
    assert set(spec['services']) == set(images)
    assert spec['services']['rabbitmq']['hostname'] == 'original-node'
    assert spec['networks']['default']['name'] == 'abc-network'
    assert all(item['image'].startswith('sha256:') and item['pull_policy'] == 'never'
               for item in spec['services'].values())
    assert spec['services']['standalone']['ports'] == ['127.0.0.1::19530']


def test_environment_and_env_file_are_forwarded_without_secret_arguments(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    adapter = runner()
    adapter.environment = {'ONCALL_DATA_ROOT': str(tmp_path), 'SHARED_MODEL_QUOTA_ENABLED': 'true'}
    captured = {}
    def execute(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout='ok')
    monkeypatch.setattr(subprocess, 'run', execute)
    assert adapter.run(['info']) == 'ok'
    assert captured['env']['ONCALL_DATA_ROOT'] == str(tmp_path)


def test_restore_refuses_source_volume_alias_before_launch(tmp_path):
    adapter = runner()
    adapter.credentials = {}
    manifest = {'baseline': {'postgres': {}, 'rabbitmq': {}, 'milvus': {}},
                'volumes': [{'name': 'business', 'service': 'postgres'}], 'containers': []}
    credentials = dict.fromkeys(('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD', 'RABBITMQ_DEFAULT_USER', 'RABBITMQ_DEFAULT_PASS', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY'), 'synthetic')
    with pytest.raises(DockerUnavailable, match='source'):
        adapter.launch_restored(manifest, {'business': 'business'}, 'new', tmp_path, credentials)


def test_interrupted_helper_records_identity_and_checks_label_before_cleanup(tmp_path):
    adapter = runner()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'start':
            raise DockerUnavailable('timeout')
        if 'inspect' in args:
            journal = json.loads(next(tmp_path.glob('helper-ownership-*.json')).read_text())
            return json.dumps([{'Config': {'Labels': {'oncall.drill.owner': journal['owner']}}}])
        return ''
    adapter.run = run
    with pytest.raises(DockerUnavailable):
        adapter.archive_volume('source', tmp_path / 'data.tar', 'sha256:fixture')
    journal = json.loads(next(tmp_path.glob('helper-ownership-*.json')).read_text())
    assert journal['resources'][0]['kind'] == 'container'
    assert any(call[:2] == ['container', 'rm'] for call in calls)


def test_restore_compares_contents_even_when_all_components_start(tmp_path):
    adapter = runner()
    owner = 'synthetic-owner'
    services = ('postgres', 'rabbitmq', 'etcd', 'minio', 'standalone')
    baseline = {'postgres': {'tables': {'diagnosis_jobs': '1:hash'}}, 'rabbitmq': {'queue': 1}, 'milvus': {'c': 2}}
    manifest = {'baseline': baseline, 'containers': [
        {'service': service, 'image': 'sha256:' + service, 'hostname': 'original-host',
         'nodename': 'rabbit@original-host' if service == 'rabbitmq' else None,
         'mounts': [{'name': service + '-source', 'destination': '/data'}]} for service in services],
        'volumes': [{'service': service, 'name': service + '-source'} for service in services]}
    mappings = {service + '-source': service + '-new' for service in services}
    credentials = dict.fromkeys(('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD',
                                 'RABBITMQ_DEFAULT_USER', 'RABBITMQ_DEFAULT_PASS',
                                 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY'), 'DRILL-CREDENTIAL-SENTINEL')
    specs = []
    from types import SimpleNamespace
    def start(spec, *args):
        specs.append(spec)
        return SimpleNamespace(capture=lambda records: {**baseline, 'postgres': {'tables': {}}}), []
    adapter._compose_start = start
    report = adapter.launch_restored(manifest, mappings, owner, tmp_path, credentials)
    assert report['verified'] is False
    assert report['checks']['postgres'] is False
    assert set(specs[0]['services']) == set(services)
    assert specs[0]['services']['rabbitmq']['environment']['RABBITMQ_NODENAME'] == 'rabbit@original-host'
    assert 'DRILL-CREDENTIAL-SENTINEL' not in json.dumps(specs[0])


def test_rabbit_verification_refuses_redirects(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.error import HTTPError
    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/credential-leak')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = runner()
    adapter.credentials = {'RABBITMQ_DEFAULT_USER': 'drill', 'RABBITMQ_DEFAULT_PASS': 'test'}
    adapter._port = lambda *args: server.server_port
    try:
        with pytest.raises(HTTPError):
            adapter._rabbit('fixture', 'GET', 'queues')
        assert paths == ['/api/queues']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)




def test_rabbit_accepts_empty_successful_creation_response():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.send_response(201)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = runner()
    adapter.credentials = {'RABBITMQ_DEFAULT_USER': 'drill', 'RABBITMQ_DEFAULT_PASS': 'test'}
    adapter._port = lambda *args: server.server_port
    try:
        assert adapter._rabbit('fixture', 'PUT', 'queues/%2F/test', {'durable': True}) == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_capture_reads_queue_contents_even_when_management_count_is_stale(tmp_path, monkeypatch):
    adapter = object.__new__(DockerRunner)
    adapter.credentials = {}
    adapter._sql = lambda *_args: 'synthetic-drill-schema-v1'
    adapter._port = lambda *_args: 19530
    rabbit_calls = []

    def rabbit(_container, method, path, payload=None):
        rabbit_calls.append((method, path, payload))
        if method == 'GET':
            return [{'vhost': '/', 'name': 'oncall.drill', 'messages': 0,
                     'durable': True, 'consumers': 0}]
        return [{'payload': 'c3ludGhldGlj', 'payload_bytes': 9,
                 'properties': {'delivery_mode': 2}, 'message_count': 0}]

    adapter._rabbit = rabbit

    class Milvus:
        def __init__(self, **_kwargs): pass
        def list_collections(self): return []
        def close(self): pass

    monkeypatch.setattr('pymilvus.MilvusClient', Milvus)
    result = adapter.capture([
        {'service': 'postgres', 'id': 'postgres'},
        {'service': 'rabbitmq', 'id': 'rabbit'},
        {'service': 'standalone', 'id': 'milvus'},
    ])
    assert result['rabbitmq']['//oncall.drill']['messages'] == 1
    assert rabbit_calls[-1][2]['count'] == 10001


def test_capture_rejects_partial_rabbit_management_page(monkeypatch):
    adapter = object.__new__(DockerRunner)
    adapter.credentials = {}
    adapter._sql = lambda *_args: 'synthetic-drill-schema-v1'
    adapter._port = lambda *_args: 19530

    def rabbit(_container, method, _path, payload=None):
        if method == 'GET':
            return [{'vhost': '/', 'name': 'oncall.drill', 'messages': 2,
                     'durable': True, 'consumers': 0}]
        return [{'payload': 'c3ludGhldGlj', 'payload_bytes': 9,
                 'properties': {'delivery_mode': 2}, 'message_count': 1}]

    adapter._rabbit = rabbit

    class Milvus:
        def __init__(self, **_kwargs): pass
        def list_collections(self): return []
        def close(self): pass

    monkeypatch.setattr('pymilvus.MilvusClient', Milvus)
    with pytest.raises(DockerUnavailable, match='incomplete'):
        adapter.capture([
            {'service': 'postgres', 'id': 'postgres'},
            {'service': 'rabbitmq', 'id': 'rabbit'},
            {'service': 'standalone', 'id': 'milvus'},
        ])


def test_running_foreign_volume_writer_blocks_snapshot():
    adapter = runner()
    def run(args, **kwargs):
        return 'foreign-id' if args[0] == 'ps' else json.dumps([
            {'Mounts': [{'Name': 'source-volume', 'RW': True}]}])
    adapter.run = run
    with pytest.raises(DockerUnavailable, match='writer'):
        adapter.assert_stopped_writers({'source-volume'})


def test_unclean_dependency_stop_blocks_snapshot():
    adapter = runner()
    adapter.run = lambda args, **kwargs: json.dumps([{'State': {'Running': False, 'ExitCode': 137}}])
    with pytest.raises(DockerUnavailable, match='cleanly'):
        adapter.stop([{'id': 'owned-source', 'service': 'standalone', 'running': True}])


def test_cold_baseline_flushes_and_unloads_vectors_before_stopping(monkeypatch):
    import sys
    from types import SimpleNamespace
    events = []
    class Client:
        def list_collections(self):
            return ['oncall_restore_fixture']
        def flush(self, collection_name):
            events.append('flush')
        def describe_collection(self, name):
            return {'fields': [{'name': 'vector', 'type': 101, 'params': {'dim': 4}}]}
        def get_collection_stats(self, name):
            events.append('stats')
            return {'row_count': 2}
        def get_load_state(self, **kwargs):
            return {'state': 1}
        def load_collection(self, name):
            events.append('load')
        def search(self, **kwargs):
            events.append('query')
            return [[{'id': 1, 'entity': {'tag': 'synthetic'}}]]
        def release_collection(self, name):
            events.append('release')
        def close(self):
            events.append('close')
    monkeypatch.setitem(sys.modules, 'pymilvus', SimpleNamespace(MilvusClient=lambda **kwargs: Client()))
    adapter = runner()
    adapter._sql = lambda container, sql: 'synthetic-schema' if 'version_num' in sql else ''
    adapter._rabbit = lambda *args: []
    adapter._port = lambda *args: 1
    records = [{'service': service, 'id': service} for service in ('postgres', 'rabbitmq', 'standalone')]
    assert adapter.capture(records)['milvus']['oncall_restore_fixture']['sample'][0]['id'] == 1
    assert events == ['flush', 'stats', 'load', 'query', 'release', 'close']


def test_volume_helpers_work_without_windows_host_bind_shares(tmp_path):
    adapter = runner()
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'inspect' in args:
            path = next(tmp_path.glob('helper-ownership-*.json'))
            owner = json.loads(path.read_text())['owner']
            return json.dumps([{'Config': {'Labels': {'oncall.drill.owner': owner}}}])
        return ''
    adapter.run = run
    adapter.archive_volume('synthetic-source', tmp_path / 'data.tar', 'sha256:fixture')
    assert any(call[0] == 'cp' for call in calls)
    assert not any('type=bind' in str(value) for call in calls for value in call)


def test_vector_quiesce_and_resume_restore_only_previously_loaded_collections(monkeypatch):
    import sys
    from types import SimpleNamespace
    events = []
    class Client:
        def list_collections(self):
            return ['loaded', 'unloaded']
        def get_load_state(self, collection_name):
            return {'state': 3 if collection_name == 'loaded' else 1}
        def flush(self, collection_name):
            events.append(('flush', collection_name))
        def release_collection(self, collection_name):
            events.append(('release', collection_name))
        def load_collection(self, collection_name):
            events.append(('load', collection_name))
        def close(self):
            pass
    monkeypatch.setitem(sys.modules, 'pymilvus', SimpleNamespace(MilvusClient=lambda **kwargs: Client()))
    adapter = runner()
    adapter._port = lambda *args: 1
    adapter.wait_healthy = lambda *args: events.append(('healthy', 'stack'))
    records = [{'service': 'standalone', 'id': 'synthetic-id'}]
    assert adapter.quiesce_vectors(records) == ['loaded']
    adapter.resume_vectors(records)
    assert events == [('flush', 'loaded'), ('release', 'loaded'), ('flush', 'unloaded'),
                      ('healthy', 'stack'), ('load', 'loaded')]


@pytest.mark.parametrize('stuck_state', [1, 2])
def test_vector_resume_rejects_collection_that_did_not_load(monkeypatch, stuck_state):
    import sys
    from types import SimpleNamespace

    class Client:
        def load_collection(self, _name): pass
        def get_load_state(self, **_kwargs): return {'state': stuck_state}
        def close(self): pass

    monkeypatch.setitem(sys.modules, 'pymilvus', SimpleNamespace(
        MilvusClient=lambda **_kwargs: Client()))
    adapter = runner()
    adapter.vector_resume = ['loaded']
    adapter.vector_load_timeout = 0.01
    adapter._port = lambda *_args: 1
    adapter.wait_healthy = lambda *_args: None
    with pytest.raises(DockerUnavailable, match='load state'):
        adapter.resume_vectors([{'service': 'standalone', 'id': 'synthetic-id'}])
