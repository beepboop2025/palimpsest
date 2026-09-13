"""Real Linux ACL regression: private atomic writes must remain readable to the bridge."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'ops/common-crawl/restore-context-reader.py'
SPEC = importlib.util.spec_from_file_location('reader_acl', HELPER)
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


def acl(mask=0, extra=(), owner=6, named=4, group=0, other=0):
    entries = [(1, owner, 0xFFFFFFFF), (2, named, 1001), *extra,
               (4, group, 0xFFFFFFFF), (16, mask, 0xFFFFFFFF), (32, other, 0xFFFFFFFF)]
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)


def test_only_existing_exact_reader_policy_can_be_unmasked():
    desired, mask = reader.approved_acl(acl())
    assert mask == 0 and desired == acl(mask=4)
    assert reader.approved_acl(desired) == (desired, 4)


@pytest.mark.parametrize('raw', [b'', acl(mask=6), acl(owner=7), acl(named=6),
                               acl(group=4), acl(other=4), acl(extra=((2, 6, 1002),))])
def test_unexpected_acl_policy_refused(raw):
    with pytest.raises(ValueError):
        reader.approved_acl(raw)


def test_helper_and_unit_are_checksum_bound_with_existing_sandbox():
    installer = (ROOT / 'ops/common-crawl/install-host-bundle.sh').read_text()
    assert 'ops/common-crawl/restore-context-reader.py:restore-context-reader.py:0444' in installer
    assert 'REVISION record-context-attempt.py restore-context-reader.py backup/README.md' in installer
    unit = (ROOT / 'ops/systemd/palimpsest-common-crawl-context.service').read_text()
    assert 'ExecStartPost=/usr/bin/python3 -B /usr/local/libexec/palimpsest-common-crawl/current/restore-context-reader.py' in unit
    assert 'User=10001\nGroup=10001' in unit
    assert 'ProtectSystem=strict' in unit and 'IPAddressDeny=any' in unit
    assert 'ReadWritePaths=/var/lib/palimpsest/common-crawl' in unit


def actor(uid, source, *args):
    def identity():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    return subprocess.run([sys.executable, '-I', '-S', '-B', '-c', source, *map(str, args)],
                          preexec_fn=identity, capture_output=True, text=True, timeout=20)


def invoke(fixture, uid=10001):
    return actor(uid, "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('reader',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.restore_reader_access(sys.argv[2])))",
                 fixture['helper'], fixture['root'])


def fingerprint(path):
    info = path.lstat()
    return {'identity': reader.identity(info), 'mode': stat.S_IMODE(info.st_mode),
            'ctime': info.st_ctime_ns, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'xattrs': {key: os.getxattr(path, key) for key in os.listxattr(path)}}


@pytest.fixture
def linux_files():
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.skip('requires actual Linux root to exercise separate writer and reader UIDs')
    with tempfile.TemporaryDirectory(prefix='archive-reader-acl-', dir='/tmp') as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        helper = base / 'reader.py'
        shutil.copyfile(HELPER, helper)
        helper.chmod(0o444)
        warehouse = base / 'warehouse'
        derived = warehouse / 'derived'
        for directory in (warehouse, derived):
            directory.mkdir(mode=0o750)
            os.chown(directory, 10001, 10001)
            os.setxattr(directory, reader.ACL_ATTRIBUTE, acl(mask=5, owner=7, named=5))
        os.setxattr(derived, 'system.posix_acl_default', acl(mask=4))
        lock = warehouse / '.common-crawl.lock'
        lock.write_bytes(b'')
        os.chown(lock, 10001, 10001)
        lock.chmod(0o640)
        create = actor(10001, '''import os,sys
from pathlib import Path
root=Path(sys.argv[1])
for name in ('archive-news-context.json','common-crawl-features.jsonl'):
    temporary=root/(name+'.temporary')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.write(fd,b'{"private":"fixture-only"}\\n')
    os.fchmod(fd,0o600)
    os.fsync(fd)
    os.close(fd)
    os.replace(temporary,root/name)
''', derived)
        assert create.returncode == 0, create.stderr
        files = [derived / name for name in reader.FILENAMES]
        for path in files:
            assert os.getxattr(path, reader.ACL_ATTRIBUTE) == acl()
            os.setxattr(path, 'user.fixture-provenance', b'unchanged')
        unrelated = derived / 'unrelated-private.json'
        unrelated.write_bytes(b'untouched')
        os.chown(unrelated, 10001, 10001)
        unrelated.chmod(0o600)
        yield {'root': warehouse, 'helper': helper, 'files': files,
               'lock': lock, 'unrelated': unrelated}


def test_actual_atomic_producer_and_reader_access_without_content_changes(linux_files):
    fixture = linux_files
    paths = fixture['files']
    before = [fingerprint(path) for path in paths]
    untouched = fingerprint(fixture['unrelated'])
    read = "from pathlib import Path; import sys; [print(Path(p).read_text()) for p in sys.argv[1:]]"
    denied = actor(1001, read, *paths)
    assert denied.returncode != 0 and 'PermissionError' in denied.stderr
    restored = invoke(fixture)
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)['bytes_unchanged']
    readable = actor(1001, read, *paths)
    assert readable.returncode == 0 and readable.stdout.count('fixture-only') == 2
    for path, old in zip(paths, before):
        new = fingerprint(path)
        assert new['identity'] == old['identity'] and new['sha256'] == old['sha256']
        assert new['mode'] == 0o640
        assert new['xattrs'] == {**old['xattrs'], reader.ACL_ATTRIBUTE: acl(mask=4)}
    assert fingerprint(fixture['unrelated']) == untouched
    stable = [fingerprint(path) for path in paths]
    assert invoke(fixture).returncode == 0
    assert [fingerprint(path) for path in paths] == stable


@pytest.mark.parametrize('failure', ['extra_reader', 'named_write', 'other_read', 'wrong_mask',
                                   'wrong_owner', 'hardlink', 'symlink', 'busy_lock', 'root_identity'])
def test_entire_batch_refuses_unsafe_policy_before_any_grant(linux_files, failure):
    fixture = linux_files
    first, second = fixture['files']
    held = None
    if failure == 'extra_reader':
        os.setxattr(second, reader.ACL_ATTRIBUTE, acl(extra=((2, 6, 1002),)))
    elif failure == 'named_write':
        os.setxattr(second, reader.ACL_ATTRIBUTE, acl(named=6))
    elif failure == 'other_read':
        os.setxattr(second, reader.ACL_ATTRIBUTE, acl(other=4))
    elif failure == 'wrong_mask':
        os.setxattr(second, reader.ACL_ATTRIBUTE, acl(mask=6))
    elif failure == 'wrong_owner':
        os.chown(second, 0, 0)
    elif failure == 'hardlink':
        os.link(second, second.parent / 'hardlink')
    elif failure == 'symlink':
        second.unlink()
        second.symlink_to(first)
    elif failure == 'busy_lock':
        held = fixture['lock'].open('rb')
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = [fingerprint(path) for path in fixture['files']]
    try:
        result = invoke(fixture, uid=0 if failure == 'root_identity' else 10001)
        assert result.returncode != 0
        assert [fingerprint(path) for path in fixture['files']] == before
    finally:
        if held:
            held.close()
