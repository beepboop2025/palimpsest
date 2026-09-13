"""Exercise the publisher's real untracked companion copy and offline admission."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from core import sealed_ledger as sealed
from scripts import archive_context_refresh as bridge
from tests.test_archive_context_refresh import source, project

ROOT = Path(__file__).resolve().parents[1]
PUBLISHER = ROOT / 'ops/railway/palimpsest-railway-publish'


def public_snapshot(root, now):
    root.mkdir(parents=True)
    context, features = source(now=now)
    document = project(context, features, now=now)
    (root / bridge.LATEST).write_bytes(bridge.canonical(document) + b'\n')
    row = {name: document[name] for name in ('generated_at', 'n_events_contextualized',
                                           'n_observations_joined', 'context_sha256')}
    (root / bridge.HISTORY).write_bytes(bridge.canonical(row) + b'\n')
    sealed.append_seal(str(root / bridge.LEDGER), bridge.SOURCE, document, now=now)
    return document


def test_real_untracked_companion_copy_reaches_offline_admission(tmp_path):
    now = datetime.now(timezone.utc)
    snapshot = tmp_path / 'snapshot/readings'
    document = public_snapshot(snapshot, now)
    original = {name: (snapshot / name).read_bytes() for name in bridge.FILES}
    checkout = tmp_path / 'checkout'
    (checkout / 'readings').mkdir(parents=True)
    # The publisher has already selected this ledger by its monotonic overlay.
    shutil.copyfile(snapshot / bridge.LEDGER, checkout / 'readings' / bridge.LEDGER)
    text = PUBLISHER.read_text()
    array = re.search(r'declare -ar PUBLIC_COMPANION_PATHS=\(.*?\n\)', text, re.S).group()
    function = re.search(r'^copy_host_public_file\(\) \{.*?^\}', text, re.S | re.M).group()
    loop = re.search(r'for tracked_path in "\$\{PUBLIC_COMPANION_PATHS\[@\]\}"; do.*?\ndone', text, re.S).group()
    env = {**os.environ, 'snapshot_readings': str(snapshot), 'checkout': str(checkout)}
    result = subprocess.run(['bash', '-euc', array + '\n' + function + '\n' + loop],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert {name: (checkout / 'readings' / name).read_bytes() for name in bridge.FILES} == original
    program = re.search(r"<<'PYARCHIVE'\n(.*?)\nPYARCHIVE", text, re.S).group(1)
    result = subprocess.run([sys.executable, '-B', '-c', program, str(snapshot)], cwd=checkout,
                            env={**os.environ, 'PYTHONPATH': str(ROOT)},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['generated_at'] == document['generated_at']
    assert {name: (snapshot / name).read_bytes() for name in bridge.FILES} == original
    assert {name: (checkout / 'readings' / name).read_bytes() for name in bridge.FILES} == original


@pytest.mark.parametrize('failure', ['missing_latest', 'missing_history', 'missing_ledger',
                                   'wrong_newest_seal', 'wrong_history', 'private_fields',
                                   'future', 'symlink'])
def test_publication_refuses_incomplete_unsealed_or_stale_archive(tmp_path, failure):
    now = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)
    root = tmp_path / 'readings'
    document = public_snapshot(root, now)
    if failure.startswith('missing_'):
        name = {'missing_latest': bridge.LATEST, 'missing_history': bridge.HISTORY,
                'missing_ledger': bridge.LEDGER}[failure]
        (root / name).unlink()
    elif failure == 'wrong_newest_seal':
        sealed.append_seal(str(root / bridge.LEDGER), bridge.SOURCE, {'foreign': True}, now=now)
    elif failure == 'wrong_history':
        (root / bridge.HISTORY).write_bytes(b'')
    elif failure == 'private_fields':
        document['raw_body'] = 'PRIVATE SOURCE PROSE'
        document.pop('context_sha256')
        import hashlib
        document['context_sha256'] = hashlib.sha256(bridge.canonical(document)).hexdigest()
        (root / bridge.LATEST).write_bytes(bridge.canonical(document) + b'\n')
        sealed.append_seal(str(root / bridge.LEDGER), bridge.SOURCE, document, now=now)
    elif failure == 'future':
        now -= timedelta(minutes=6)
    elif failure == 'symlink':
        target = tmp_path / 'latest-target.json'
        (root / bridge.LATEST).rename(target)
        (root / bridge.LATEST).symlink_to(target)
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    with pytest.raises((OSError, ValueError)):
        bridge.verify_public_snapshot(root, now=now)
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before


def test_unrelated_newer_ledger_seal_preserves_archive_admission(tmp_path):
    now = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)
    root = tmp_path / 'readings'
    public_snapshot(root, now)
    sealed.append_seal(str(root / bridge.LEDGER), 'independent-collector', {'count': 7}, now=now)
    assert bridge.verify_public_snapshot(root, now=now)['newest_seal'] == 'PASS'


def test_valid_old_archive_keeps_original_bytes_and_reports_stale_clocks(tmp_path):
    now = datetime(2026, 9, 14, 0, tzinfo=timezone.utc)
    root = tmp_path / 'readings'
    document = public_snapshot(root, now)
    before = {name: (root / name).read_bytes() for name in bridge.FILES}
    result = bridge.verify_public_snapshot(root, now=now + timedelta(days=2))
    assert result['status'] == 'verified' and result['freshness'] == 'stale'
    assert result['generated_at'] == document['generated_at'] and len(result['stale_clocks']) == 3
    assert {name: (root / name).read_bytes() for name in bridge.FILES} == before


def test_absent_bootstrap_is_unavailable_but_dropped_host_pair_is_refused(tmp_path):
    root = tmp_path / 'readings'; root.mkdir()
    snapshot = tmp_path / 'snapshot'
    assert bridge.verify_public_snapshot(root)['status'] == 'unavailable'
    public_snapshot(snapshot, datetime.now(timezone.utc))
    with pytest.raises(ValueError, match='immutable host snapshot'):
        bridge.verify_public_snapshot(root, source=snapshot)
