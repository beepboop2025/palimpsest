"""The admitted archive projection survives rights staging and is release-critical."""
from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket

import pytest

from core import china_situation, newswire
from scripts import archive_context_refresh as bridge
from scripts import build_data_catalog as atlas
from scripts import build_public_data_catalog as public_catalog
from scripts import stage_pages_rights as rights
from tests.test_archive_context_refresh import NOW, project, source

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'archive_publication_manifest', ROOT / 'ops/railway/build_release_manifest.py')
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)
ARCHIVE_PATHS = tuple('readings/' + name for name in (bridge.LATEST, bridge.HISTORY))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('archive publication fixture attempted network')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'sendto', denied)


@pytest.fixture
def archive_bytes():
    context, features = source()
    document = project(context, features)
    latest = json.dumps(document, ensure_ascii=False, indent=2).encode() + b'\n'
    history_row = {name: document[name] for name in (
        'generated_at', 'n_events_contextualized', 'n_observations_joined', 'context_sha256')}
    history = bridge.canonical(history_row) + b'\n'
    assert '123456789' not in latest.decode() and 'PRIVATE SIGNAL PROSE' not in latest.decode()
    return document, {ARCHIVE_PATHS[0]: latest, ARCHIVE_PATHS[1]: history}


def write(root, relative, raw):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def test_rights_stage_keeps_archive_bytes_and_original_catalog_clock(tmp_path, monkeypatch, archive_bytes):
    document, original = archive_bytes
    for relative, raw in original.items():
        write(tmp_path, relative, raw)
    config = json.loads((ROOT / 'config/public_data_catalog.json').read_text())
    config['datasets'] = [row for row in config['datasets'] if row['id'] == 'archive-news-context']
    assert len(config['datasets']) == 1 and config['datasets'][0].get('publication_allowed') is not False
    write(tmp_path, 'config/public_data_catalog.json', json.dumps(config).encode())
    shutil.copyfile(ROOT / rights.POLICY_RELATIVE_PATH, tmp_path / rights.POLICY_RELATIVE_PATH)
    # Use real current wire/situation validators, with injected historical RSS and zero current events.
    wire = newswire.collect_newswire(newswire.load_source_registry(),
                                    lambda url, **kwargs: (b'<rss version="2.0"><channel><item>'
                                        b'<title>China archive fixture</title><link>https://news.cgtn.com/fixture</link>'
                                        b'<pubDate>Wed, 01 Jan 2020 00:00:00 +0000</pubDate>'
                                        b'</item></channel></rss>'), now=NOW)
    assert wire['events'] == []
    situation = china_situation.build_china_situation(wire, {})
    for relative, value in ((rights.NEWSWIRE_RELATIVE_PATH, wire),
                            (rights.CHINA_SITUATION_RELATIVE_PATH, situation)):
        write(tmp_path, relative, json.dumps(value).encode())
    denied_path = 'readings/china-econ-observations.jsonl'
    write(tmp_path, denied_path, b'{"source_id":"cfets_benchmarks","series_id":"cn.cfets.synthetic","value":987654.321}\n')
    status = rights.stage_pages_tree(tmp_path, publication_sha='1' * 40,
                                    evaluated_at=NOW, admission_at=NOW)
    assert denied_path in status['quarantined_paths']
    assert b'987654.321' not in (tmp_path / denied_path).read_bytes()
    assert set(ARCHIVE_PATHS).isdisjoint(status['quarantined_paths'])
    assert original == {relative: (tmp_path / relative).read_bytes() for relative in original}
    monkeypatch.setattr(atlas, 'ROOT', tmp_path)
    monkeypatch.setattr(atlas, 'CONFIG', tmp_path / 'config/public_data_catalog.json')
    catalog = public_catalog.build_public_catalog(root=tmp_path, now=NOW + timedelta(minutes=15))
    row = catalog['datasets'][0]
    assert row['artifacts']['latest_available'] is True
    assert row['artifacts']['history_available'] is True
    assert row.get('publication_allowed') is not False
    assert row['artifacts']['observed_at'] == document['generated_at']
    assert row['artifacts']['age_seconds'] == 900
    assert row['artifacts']['counts']['n_events_contextualized'] == 1
    assert row['artifacts']['counts']['n_observations_joined'] == 0
    research = atlas.build_research_catalog(now=NOW + timedelta(minutes=15), public_catalog=catalog)
    peer = research['datasets'][0]
    assert peer['urls']['latest'] == 'https://palimpsest.info/' + ARCHIVE_PATHS[0]
    assert peer['artifacts']['observed_at'] == document['generated_at']
    assert peer['artifacts']['evidence_state'] == row['artifacts']['evidence_state']
    assert peer['values_included'] is False
    retained = json.loads((tmp_path / ARCHIVE_PATHS[0]).read_bytes())
    assert retained['events'][0]['archive_context'][0]['last_capture_at'] == '2026-07-24T12:30:00Z'
    assert retained['events'][0]['archive_context'][0]['available_at'] == '2026-08-01T00:00:00Z'


def manifest_tree(root):
    for relative in manifest.CRITICAL_PATHS:
        write(root, relative, b'fixture\n')
    write(root, 'readings/newsroom-latest.json', b'{"schema_version":"palimpsest-news.v1","stories":[]}\n')
    # The public catalog retains configured URLs for unavailable artifacts;
    # the research catalog removes unavailable download URLs.
    write(root, 'readings/public-data-catalog-latest.json', json.dumps({'datasets': [{
        'id': 'archive-news-context', 'artifacts': {'latest_available': False},
        'urls': {'latest': 'https://palimpsest.info/' + ARCHIVE_PATHS[0]}}]}).encode())
    write(root, 'readings/research-catalog-latest.json', json.dumps({'datasets': [{
        'id': 'archive-news-context', 'artifacts': {'evidence_state': 'warming'},
        'urls': {'landing_page': 'https://palimpsest.info/osint-china.html'}}]}).encode())


@pytest.mark.parametrize('missing', ARCHIVE_PATHS)
def test_manifest_rejects_either_missing_archive_companion(tmp_path, archive_bytes, missing):
    _, original = archive_bytes
    # Other required surfaces are small inert files; only newsroom has a parsed
    # contract here. The manifest's real full critical-path list stays unpatched.
    manifest_tree(tmp_path)
    for relative, raw in original.items():
        write(tmp_path, relative, raw)
    complete = manifest.build_manifest(tmp_path, '1' * 40, '2026-08-20T07:15:00Z')
    for relative, raw in original.items():
        assert complete['critical_files'][relative] == {
            'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    (tmp_path / missing).unlink()
    with pytest.raises(manifest.ManifestError, match=missing):
        manifest.build_manifest(tmp_path, '1' * 40, '2026-08-20T07:15:00Z')


def test_manifest_allows_both_absent_before_first_archive_admission(tmp_path):
    manifest_tree(tmp_path)
    assert all(not (tmp_path / relative).exists() for relative in ARCHIVE_PATHS)
    complete = manifest.build_manifest(tmp_path, '1' * 40, '2026-08-20T07:15:00Z')
    assert set(ARCHIVE_PATHS).isdisjoint(complete['critical_files'])


@pytest.mark.parametrize('declaration', ['public_available', 'research_download'])
def test_manifest_refuses_both_lost_files_when_catalog_advertises_archive(tmp_path, declaration):
    manifest_tree(tmp_path)
    if declaration == 'public_available':
        path = tmp_path / 'readings/public-data-catalog-latest.json'
        catalog = json.loads(path.read_bytes())
        catalog['datasets'][0]['artifacts']['latest_available'] = True
    else:
        path = tmp_path / 'readings/research-catalog-latest.json'
        catalog = json.loads(path.read_bytes())
        catalog['datasets'][0]['urls']['latest'] = 'https://palimpsest.info/' + ARCHIVE_PATHS[0]
    path.write_text(json.dumps(catalog))
    assert all(not (tmp_path / relative).exists() for relative in ARCHIVE_PATHS)
    with pytest.raises(manifest.ManifestError, match='archive-news-context'):
        manifest.build_manifest(tmp_path, '1' * 40, '2026-08-20T07:15:00Z')
