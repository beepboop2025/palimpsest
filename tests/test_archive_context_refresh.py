"""Offline archive admission: rights projection, clocks, history and crash recovery."""
import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core import sealed_ledger as sealed
from processors import archive_context as archive
from scripts import archive_context_refresh as refresh
from tests.test_archive_news_context import _feature_row

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/common_crawl_targets.json'
NOW = datetime(2026, 8, 20, 7, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('archive admission attempted network')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'sendto', denied)


def seal_context(document):
    unsigned = dict(document)
    unsigned.pop('context_sha256', None)
    unsigned['context_sha256'] = hashlib.sha256(refresh.canonical(unsigned)).hexdigest()
    return refresh.canonical(unsigned)


def source(*, now=NOW):
    feature = _feature_row()
    features = refresh.canonical(feature) + b'\n'
    wire = {'schema_version': 'palimpsest-newswire.v1', 'generated_at': (now - timedelta(minutes=10)).isoformat(),
            'events': [{'event_id': 'event-' + '1' * 24, 'version_id': 'eventv-' + '2' * 24,
                        'url': 'https://palimpsest.info/news/wire/event-' + '1' * 24 + '/',
                        'published_at': '2026-08-20T01:00:00Z', 'topics': ['economy', 'policy'],
                        'evidence_strength': 'single-source', 'evidence_groups': [{'group_id': 'source'}],
                        'evidence_refs': [{'source_id': 'china-digital-times'}],
                        'declared_links': {'scan_signal_ids': ['private-instrument'], 'economic_signal_ids': []}}]}
    osint = {'schema_version': 'osint-china.v1', 'generated_at': (now - timedelta(minutes=20)).isoformat(),
             'signals': [{'id': 'private-instrument', 'live': True,
                          'metric': {'value': 123456789, 'label': 'PRIVATE SIGNAL PROSE'}}]}
    context = archive.build_archive_context(wire, osint, [feature], hashlib.sha256(features).hexdigest(),
                                            archive.load_config(CONFIG), now=now)
    return context, features


def project(context, features, now=NOW):
    return refresh.project(seal_context(context), features, config=archive.load_config(CONFIG), now=now)


def setup(tmp_path):
    derived = tmp_path / 'derived'; derived.mkdir()
    host = tmp_path / 'readings'; host.mkdir()
    state = tmp_path / 'state'; state.mkdir(mode=0o700)
    lock = state / 'refresh.lock'; lock.touch(mode=0o600)
    data_lock = tmp_path / 'data.lock'; data_lock.touch(mode=0o600)
    context, features = source()
    (derived / 'archive-news-context.json').write_bytes(seal_context(context))
    (derived / 'common-crawl-features.jsonl').write_bytes(features)
    previous, prior_features = source(now=NOW-timedelta(minutes=30))
    previous_public = project(previous, prior_features, now=NOW-timedelta(minutes=30))
    prior_raw = json.dumps(previous_public, ensure_ascii=False, indent=2).encode() + b'\n'
    (host / refresh.LATEST).write_bytes(prior_raw)
    row = {key: previous_public[key] for key in ['generated_at', 'n_events_contextualized', 'n_observations_joined', 'context_sha256']}
    (host / refresh.HISTORY).write_bytes(refresh.canonical(row) + b'\n')
    for name in [refresh.LATEST, refresh.HISTORY]:
        (host / name).chmod(0o660)
    sealed.append_seal(str(host / refresh.LEDGER), refresh.SOURCE, previous_public, now=NOW-timedelta(minutes=30))
    args = {'context': derived / 'archive-news-context.json', 'features': derived / 'common-crawl-features.jsonl',
            'host': host, 'state': state, 'data_lock': data_lock, 'refresh_lock': lock, 'config_path': CONFIG,
            'now': NOW, 'kill_switch': type('Live', (), {'is_halted': lambda self: False})()}
    return args


def test_projection_keeps_exact_archive_metadata_and_original_clocks_only():
    context, features = source()
    assert context['events'][0]['signal_context'][0]['metric']['value'] == 123456789
    document = project(context, features)
    encoded = json.dumps(document)
    assert '123456789' not in encoded and 'PRIVATE SIGNAL PROSE' not in encoded
    for key in ['signal_context', 'model_features', 'editorial_priority', 'training_label', 'warc_filename', 'raw_text']:
        assert key not in encoded
    assert document['events'][0]['archive_context'] == context['events'][0]['archive_context']
    assert document['generated_at'] == context['generated_at']
    assert document['source_snapshot']['newswire_generated_at'] == context['newswire_generated_at']
    assert document['publication_policy'] == refresh.POLICY
    assert document['status'] == 'partial' and set(document['families'].values()) == {'missing'}
    assert document['n_events_contextualized'] == 1 and document['n_observations_joined'] == 0


@pytest.mark.parametrize('field', ['generated_at', 'newswire_generated_at', 'osint_generated_at'])
@pytest.mark.parametrize('delta', [-180, 10])
def test_stale_and_future_source_clocks_fail(field, delta):
    context, features = source()
    context[field] = (NOW + timedelta(minutes=delta)).isoformat()
    with pytest.raises(ValueError, match='clock|stale|future'):
        project(context, features)


@pytest.mark.parametrize('change', ['unknown-top', 'unknown-event', 'policy', 'event-authority', 'receipt-value', 'receipt-future', 'url-credentials', 'url-private-path', 'count', 'digest'])
def test_mutated_or_unsafe_context_fails(change):
    context, features = source()
    if change == 'unknown-top': context['raw_body'] = 'private'
    elif change == 'unknown-event': context['events'][0]['raw_body'] = 'private'
    elif change == 'policy': context['publication_policy']['automatic_publication'] = 'allowed'
    elif change == 'event-authority': context['events'][0]['automatic_publication_eligible'] = True
    elif change == 'receipt-value': context['events'][0]['archive_context'][0]['unique_urls'] += 1
    elif change == 'receipt-future': context['events'][0]['published_at'] = '2026-07-01T00:00:00Z'
    elif change == 'url-credentials': context['events'][0]['event_url'] = 'https://' + 'credential' + '@example.org/'
    elif change == 'url-private-path': context['events'][0]['event_url'] = 'https://example.org/private?token=fixture'
    elif change == 'count': context['n_events_contextualized'] = 99
    elif change == 'digest': context['feature_export_sha256'] = '0' * 64
    with pytest.raises((ValueError, archive.ValidationError)): project(context, features)


@pytest.mark.parametrize('change', ['rights', 'products', 'aliases', 'host', 'schema', 'future', 'rate', 'extra'])
def test_feature_rights_identity_clock_and_shape_fail(change):
    context, features = source()
    row = json.loads(features)
    if change == 'rights': row['rights']['training_use'] = 'unreviewed'
    elif change == 'products': row['products'].append('unreviewed-product')
    elif change == 'aliases': row['aliases'].append('unreviewed.example')
    elif change == 'host': row['host'] = 'unreviewed.example'
    elif change == 'schema': row['schema_version'] = 'unknown'
    elif change == 'future': row['available_at'] = '2026-08-21T00:00:00Z'
    elif change == 'rate': row['features']['mutation_rate'] = 1.5
    elif change == 'extra': row['raw_body'] = 'private'
    row.pop('feature_sha256'); row['feature_sha256'] = hashlib.sha256(refresh.canonical(row)).hexdigest()
    features = refresh.canonical(row) + b'\n'
    context['feature_export_sha256'] = hashlib.sha256(features).hexdigest()
    with pytest.raises((ValueError, archive.ValidationError)): project(context, features)


def test_refresh_preserves_all_history_and_ledger_and_permissions(tmp_path):
    args = setup(tmp_path); host = args['host']
    before = {name: (host/name).read_bytes() for name in [refresh.LEDGER, refresh.HISTORY]}
    result = refresh.refresh(**args)
    assert result['status'] == 'refreshed' and result['coverage_status'] == 'partial'
    for name, raw in before.items(): assert (host/name).read_bytes().startswith(raw)
    for name in [refresh.LATEST, refresh.HISTORY]: assert (host/name).stat().st_mode & 0o777 == 0o660
    doc = json.loads((host/refresh.LATEST).read_bytes())
    refresh.verify_public(doc, host/refresh.LEDGER, (host/refresh.HISTORY).read_bytes())
    assert not (args['state']/'pending').exists()
    assert len(list((args['state']/'completed').iterdir())) == 1
    assert refresh.refresh(**args)['status'] == 'unchanged'


def test_foreign_ledger_append_during_projection_is_retained(tmp_path, monkeypatch):
    args = setup(tmp_path); original = refresh.project
    captured = []
    def concurrent(*a, **kw):
        value = original(*a, **kw)
        sealed.append_seal(str(args['host']/refresh.LEDGER), 'foreign-writer', {'measurement': 1}, now=NOW)
        captured.append((args['host']/refresh.LEDGER).read_bytes())
        return value
    monkeypatch.setattr(refresh, 'project', concurrent)
    assert refresh.refresh(**args)['status'] == 'refreshed'
    assert (args['host']/refresh.LEDGER).read_bytes().startswith(captured[0])


@pytest.mark.parametrize('fail_at', [1, 2, 3])
def test_partial_promotion_recovers_exact_candidate_without_losing_rows(tmp_path, monkeypatch, fail_at):
    args = setup(tmp_path); original = refresh.io._replace; calls = 0
    def interrupted(*a, **kw):
        nonlocal calls
        calls += 1
        if calls == fail_at: raise OSError('injected interruption')
        return original(*a, **kw)
    monkeypatch.setattr(refresh.io, '_replace', interrupted)
    with pytest.raises(OSError, match='injected'): refresh.refresh(**args)
    pending = args['state']/'pending'; assert pending.is_dir()
    expected = {name: (pending/name).read_bytes() for name in refresh.FILES}
    monkeypatch.setattr(refresh.io, '_replace', original)
    assert refresh.refresh(**args)['status'] == 'unchanged'
    assert all((args['host']/name).read_bytes() == raw for name, raw in expected.items())


def test_pending_recovery_refuses_to_erase_foreign_ledger_append(tmp_path, monkeypatch):
    args = setup(tmp_path); original = refresh.io._replace
    def interrupted(path, *a, **kw):
        if path.name == refresh.HISTORY: raise OSError('injected interruption')
        return original(path, *a, **kw)
    monkeypatch.setattr(refresh.io, '_replace', interrupted)
    with pytest.raises(OSError): refresh.refresh(**args)
    monkeypatch.setattr(refresh.io, '_replace', original)
    ledger = args['host']/refresh.LEDGER
    sealed.append_seal(str(ledger), 'foreign-writer', {'measurement': 2}, now=NOW)
    retained = ledger.read_bytes()
    with pytest.raises(ValueError, match='advanced'): refresh.refresh(**args)
    assert ledger.read_bytes() == retained and (args['state']/'pending').is_dir()


@pytest.mark.parametrize('changed', ['history', 'context', 'features'])
def test_concurrent_producer_or_host_change_is_not_overwritten(tmp_path, monkeypatch, changed):
    args = setup(tmp_path); original = refresh.project
    target = args['host']/refresh.HISTORY if changed == 'history' else args[changed]
    captured = []
    def concurrent(*a, **kw):
        document = original(*a, **kw)
        target.write_bytes(target.read_bytes() + b'\n'); captured.append(target.read_bytes())
        return document
    monkeypatch.setattr(refresh, 'project', concurrent)
    with pytest.raises(ValueError, match='changed'): refresh.refresh(**args)
    assert target.read_bytes() == captured[0]


def test_halt_and_missing_ledger_lock_preserve_outputs(tmp_path):
    args = setup(tmp_path); before = (args['host']/refresh.LATEST).read_bytes()
    args['kill_switch'] = type('Halted', (), {'is_halted': lambda self: True})()
    with pytest.raises(ValueError, match='halted'): refresh.refresh(**args)
    args['kill_switch'] = type('Live', (), {'is_halted': lambda self: False})()
    sealed._lock_path(args['host']/refresh.LEDGER).unlink()
    with pytest.raises(FileNotFoundError): refresh.refresh(**args)
    assert (args['host']/refresh.LATEST).read_bytes() == before


def test_service_has_complete_fail_closed_offline_chain():
    unit = (ROOT/'ops/systemd/palimpsest-archive-context-refresh.service').read_text()
    assert 'Requires=palimpsest-common-crawl-context.service' in unit
    assert 'After=palimpsest-common-crawl-context.service' in unit
    assert 'PrivateNetwork=true' in unit and 'IPAddressDeny=any' in unit
    assert 'SupplementaryGroups=palimpsest-analysis' in unit
    assert 'openrouter' not in unit.casefold()
    assert 'RequiresMountsFor=/opt/palimpsest-archive/source /var/lib/palimpsest/archive-context-refresh' in unit
    timer = (ROOT/'ops/systemd/palimpsest-archive-context-refresh.timer').read_text()
    assert 'OnCalendar=*-*-* *:25:00 UTC' in timer
    import runpy
    watchdog = runpy.run_path(str(ROOT/'ops/railway/palimpsest-direct-watchdog'))
    assert 'palimpsest-archive-context-refresh.timer' in watchdog['TIMERS']
    assert 'palimpsest-archive-context-refresh.service' in watchdog['SERVICES']


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux access ACL contract')
def test_archive_promotion_preserves_exact_linux_acls(tmp_path):
    assert shutil.which('setfacl'), 'Linux acceptance requires setfacl'
    args = setup(tmp_path)
    paths = [args['host']/name for name in refresh.FILES]
    for path in paths:
        subprocess.run(['setfacl', '-m', 'u:65534:r--', str(path)], check=True)
    before = {path: (os.getxattr(path, 'system.posix_acl_access'), path.stat().st_mode) for path in paths}
    assert refresh.refresh(**args)['status'] == 'refreshed'
    assert {path: (os.getxattr(path, 'system.posix_acl_access'), path.stat().st_mode) for path in paths} == before
