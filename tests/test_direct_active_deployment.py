"""Real topology parsers must recover a preserved predecessor after snapshot failure."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = '11111111-1111-4111-8111-111111111111'
ENVIRONMENT = '22222222-2222-4222-8222-222222222222'
SERVICE = '33333333-3333-4333-8333-333333333333'
PREDECESSOR = '44444444-4444-4444-8444-444444444444'
FAILED = '55555555-5555-4555-8555-555555555555'
IMAGE = 'sha256:' + 'a' * 64


def fixture():
    active = {
        'id': PREDECESSOR, 'status': 'SUCCESS', 'deploymentStopped': False,
        'createdAt': '2026-09-23T04:38:23.620Z',
        'instances': [{'id': 'one', 'status': 'RUNNING'}],
        'meta': {
            'imageDigest': IMAGE, 'reason': 'deploy', 'buildOnly': False,
            'volumeMounts': [],
            'serviceManifest': {
                'build': {'builder': 'DOCKERFILE', 'dockerfilePath': 'ops/railway/Dockerfile.static'},
                'deploy': {'healthcheckPath': '/healthz', 'numReplicas': 1,
                           'cronSchedule': None, 'requiredMountPath': None},
            },
        },
    }
    latest = {
        'id': FAILED, 'status': 'FAILED', 'deploymentStopped': True,
        'createdAt': '2026-09-23T04:55:35.325Z', 'instances': [],
        'meta': {'reason': 'deploy', 'buildOnly': False,
                 'configErrors': ['Failed to create code snapshot.']},
    }
    instance = {
        'environmentId': ENVIRONMENT, 'serviceId': SERVICE,
        'serviceName': 'palimpsest-publication', 'source': None,
        'cronSchedule': None, 'nextCronRunAt': None,
        'latestDeployment': latest, 'activeDeployments': [active],
    }
    payload = {
        'id': PROJECT,
        'services': {'edges': [{'node': {'id': SERVICE, 'name': 'palimpsest-publication'}}]},
        'environments': {'edges': [{'node': {
            'id': ENVIRONMENT, 'canAccess': True, 'deletedAt': None,
            'volumeInstances': {'edges': []},
            'serviceInstances': {'edges': [{'node': instance}]},
        }}]},
    }
    return payload, instance


def parse(parser, payload, tmp_path, monkeypatch):
    raw = json.dumps(payload).encode()
    if parser == 'publisher':
        text = (ROOT / 'ops/railway/palimpsest-railway-publish').read_text()
        start = text.index('  topology_row="$(')
        source = text[text.index("<<'PY'\n", start) + 7:]
        source = source[:source.index('\nPY\n')]
        path = tmp_path / 'topology.json'
        path.write_bytes(raw)
        result = subprocess.run([sys.executable, '-I', '-S', '-', str(path), PROJECT,
                                 ENVIRONMENT, SERVICE, PREDECESSOR],
                                input=source, text=True, capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr)
        return result.stdout.split('\t')[0]
    filename = ('reconcile-direct-publication-candidate' if parser == 'reconciler'
                else 'rotate-direct-publication-base')
    namespace = runpy.run_path(str(ROOT / 'ops/railway' / filename))
    if parser == 'reconciler':
        function = namespace['_topology']
        for key, value in [('PROJECT_ID', PROJECT), ('ENVIRONMENT_ID', ENVIRONMENT), ('SERVICE_ID', SERVICE)]:
            monkeypatch.setitem(function.__globals__, key, value)
        return function(raw, deployment=PREDECESSOR, image=IMAGE, reason='deploy')['image_digest']
    return namespace['_validate_topology'](raw, project_id=PROJECT, environment_id=ENVIRONMENT,
                                          service_id=SERVICE, deployment_id=PREDECESSOR)['image_digest']


@pytest.mark.parametrize('parser', ['reconciler', 'rotation', 'publisher'])
@pytest.mark.parametrize('shape', ['failed_latest', 'same_latest', 'historical'])
def test_single_serving_predecessor_is_accepted(parser, shape, tmp_path, monkeypatch):
    payload, instance = fixture()
    if shape != 'failed_latest':
        instance['latestDeployment'] = deepcopy(instance['activeDeployments'][0])
    if shape == 'historical':
        del instance['activeDeployments']
    assert parse(parser, payload, tmp_path, monkeypatch) == IMAGE


@pytest.mark.parametrize('parser', ['reconciler', 'rotation', 'publisher'])
@pytest.mark.parametrize('case', [
    'missing_active', 'null_active', 'zero_active', 'multiple_active', 'unrelated_active',
    'active_stopped', 'active_failed', 'active_multiple_instances', 'latest_building',
    'latest_queued', 'latest_running', 'latest_not_stopped', 'latest_has_instance',
    'latest_old', 'latest_naive_clock', 'latest_bad_id', 'attached_source',
    'different_latest_status', 'different_latest_image',
])
def test_ambiguous_or_changed_topology_is_rejected(parser, case, tmp_path, monkeypatch):
    payload, instance = fixture()
    active = instance['activeDeployments'][0]
    latest = instance['latestDeployment']
    if case == 'missing_active': del instance['activeDeployments']
    elif case == 'null_active': instance['activeDeployments'] = None
    elif case == 'zero_active': instance['activeDeployments'] = []
    elif case == 'multiple_active': instance['activeDeployments'].append(deepcopy(active))
    elif case == 'unrelated_active': active['id'] = '66666666-6666-4666-8666-666666666666'
    elif case == 'active_stopped': active['deploymentStopped'] = True
    elif case == 'active_failed': active['status'] = 'FAILED'
    elif case == 'active_multiple_instances': active['instances'].append({'id': 'two', 'status': 'RUNNING'})
    elif case.startswith('latest_') and case.split('_')[-1] in {'building', 'queued', 'running'}:
        latest['status'] = case.split('_')[-1].upper()
    elif case == 'latest_not_stopped': latest['deploymentStopped'] = False
    elif case == 'latest_has_instance': latest['instances'] = [{'status': 'RUNNING'}]
    elif case == 'latest_old': latest['createdAt'] = '2026-09-22T04:55:35.325Z'
    elif case == 'latest_naive_clock': latest['createdAt'] = '2026-09-23T04:55:35.325'
    elif case == 'latest_bad_id': latest['id'] = 'unknown'
    elif case == 'attached_source': instance['source'] = {'image': None, 'repo': 'unrelated'}
    elif case.startswith('different_latest'):
        instance['latestDeployment'] = deepcopy(active)
        if case.endswith('status'): instance['latestDeployment']['status'] = 'FAILED'
        else: instance['latestDeployment']['meta']['imageDigest'] = 'sha256:' + 'b' * 64
    with pytest.raises((RuntimeError, ValueError)):
        parse(parser, payload, tmp_path, monkeypatch)


def test_predecessor_proof_rechecks_both_origins_and_active_identity(monkeypatch):
    namespace = runpy.run_path(str(ROOT / 'ops/railway/reconcile-direct-publication-candidate'))
    prove = namespace['_prove_predecessor']
    scope = prove.__globals__
    for key, value in [('PROJECT_ID', PROJECT), ('ENVIRONMENT_ID', ENVIRONMENT), ('SERVICE_ID', SERVICE)]:
        monkeypatch.setitem(scope, key, value)
    payload, instance = fixture()
    active = scope['_topology'](json.dumps(payload).encode())
    candidate = {'rollback_evidence': {'topology': active}}
    monkeypatch.setitem(scope, '_status', lambda *_: json.dumps(payload).encode())
    monkeypatch.setitem(scope, '_live_manifests', lambda *_: (b'exact', b'exact'))
    assert prove(candidate, b'exact', b'exact', active, 'token-name', 'redacted')
    monkeypatch.setitem(scope, '_live_manifests', lambda *_: (b'exact', b'changed'))
    assert not prove(candidate, b'exact', b'exact', active, 'token-name', 'redacted')
    monkeypatch.setitem(scope, '_live_manifests', lambda *_: (b'exact', b'exact'))
    instance['activeDeployments'].append(deepcopy(instance['activeDeployments'][0]))
    assert not prove(candidate, b'exact', b'exact', active, 'token-name', 'redacted')
