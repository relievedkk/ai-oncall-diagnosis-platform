"""Opt in only: this never connects to the configured business database/stack."""
import os
import shutil

import pytest

from oncall_ops.docker import DockerRunner


@pytest.mark.skipif(os.environ.get('ONCALL_DOCKER_TESTS') != '1' or not shutil.which('docker'),
                    reason='Real isolated cold restore unverified: requires ONCALL_DOCKER_TESTS=1 and Docker Desktop')
def test_real_isolated_cold_backup_restore(tmp_path):
    from oncall_ops.drill import run_drill
    adapter = DockerRunner()
    try:
        adapter.run(['info'], timeout=15)
    except RuntimeError:
        pytest.skip('Real isolated cold restore unverified: Docker daemon unavailable')
    report = run_drill(tmp_path, tmp_path / 'drill', docker=adapter)
    assert report['status'] == 'verified'
    assert report['restore']['verification']['verified']
    assert set(report['restore']['verification']['checks']) >= {'postgres', 'rabbitmq', 'milvus'}
