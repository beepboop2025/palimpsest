#!/usr/bin/env python3
"""Restore the existing approved reader ACL after private atomic output writes."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct

WAREHOUSE = Path('/var/lib/palimpsest/common-crawl')
FILENAMES = ('archive-news-context.json', 'common-crawl-features.jsonl')
WRITER_UID = WRITER_GID = 10001
READER_UID = 1001
ACL_ATTRIBUTE = 'system.posix_acl_access'
MAX_BYTES = 64 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_size,
            info.st_mtime_ns, info.st_nlink)


def approved_acl(raw):
    require(len(raw) == 44 and struct.unpack('<I', raw[:4])[0] == 2, 'unexpected ACL format')
    entries = list(struct.iter_unpack('<HHI', raw[4:]))
    fixed = {(1, 6, 0xFFFFFFFF), (2, 4, READER_UID), (4, 0, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)}
    masks = [entry for entry in entries if entry[0] == 16]
    require(len(masks) == 1 and masks[0][1] in (0, 4) and masks[0][2] == 0xFFFFFFFF and
            len(set(entries)) == 5 and set(entries) - set(masks) == fixed,
            'unexpected context reader ACL')
    replacement = struct.pack('<I', 2) + b''.join(
        struct.pack('<HHI', tag, 4 if tag == 16 else permissions, owner)
        for tag, permissions, owner in entries)
    return replacement, masks[0][1]


def snapshot(path, fd):
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            (before.st_uid, before.st_gid) == (WRITER_UID, WRITER_GID) and
            before.st_size <= MAX_BYTES, 'unsafe context file')
    require(identity(path.lstat()) == identity(before) and not path.is_symlink(), 'context path changed')
    raw = os.getxattr(fd, ACL_ATTRIBUTE)
    desired, mask = approved_acl(raw)
    require(stat.S_IMODE(before.st_mode) == (0o600 | (mask << 3)), 'context mode differs from ACL')
    os.lseek(fd, 0, os.SEEK_SET)
    digest, size = hashlib.sha256(), 0
    while block := os.read(fd, 1024 * 1024):
        size += len(block)
        require(size <= before.st_size, 'context grew while read')
        digest.update(block)
    require(size == before.st_size and identity(os.fstat(fd)) == identity(before) and
            os.getxattr(fd, ACL_ATTRIBUTE) == raw, 'context changed while read')
    return identity(before), digest.hexdigest(), raw, desired


@contextmanager
def warehouse_lock(root):
    path = root / '.common-crawl.lock'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                (info.st_uid, info.st_gid) == (WRITER_UID, WRITER_GID) and
                stat.S_IMODE(info.st_mode) == 0o640 and info.st_size == 0,
                'unsafe warehouse lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(identity(path.lstat()) == identity(info), 'warehouse lock changed')
        yield
        require(identity(path.lstat()) == identity(info), 'warehouse lock changed')
    finally:
        os.close(fd)


def restore_reader_access(root=WAREHOUSE):
    require((os.geteuid(), os.getegid()) == (WRITER_UID, WRITER_GID), 'writer identity required')
    root = Path(root)
    derived = root / 'derived'
    for directory in (root, derived):
        info = directory.lstat()
        require(directory.resolve(strict=True) == directory.absolute() and directory.is_dir(),
                'unsafe context directory')
        require((info.st_uid, info.st_gid) == (WRITER_UID, WRITER_GID) and
                not stat.S_IMODE(info.st_mode) & 0o022, 'unsafe context directory ownership or mode')
    descriptors = []
    with warehouse_lock(root):
        try:
            # Validate both complete policies before granting either permission.
            for name in FILENAMES:
                path = derived / name
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append((path, fd))
            before = [snapshot(path, fd) for path, fd in descriptors]
            for (path, fd), expected in zip(descriptors, before):
                require(snapshot(path, fd) == expected, 'context changed before ACL restoration')
                if expected[2] != expected[3]:
                    os.setxattr(fd, ACL_ATTRIBUTE, expected[3])
                    os.fsync(fd)
            for (path, fd), expected in zip(descriptors, before):
                after = snapshot(path, fd)
                require(after[:2] == expected[:2] and after[2] == expected[3],
                        'context bytes or ACL changed during restoration')
            return {'status': 'restored', 'reader_uid': READER_UID,
                    'files': [path.name for path, _ in descriptors],
                    'bytes_unchanged': True, 'other_acl_entries_unchanged': True}
        finally:
            for _, fd in descriptors:
                os.close(fd)


if __name__ == '__main__':
    try:
        print(json.dumps(restore_reader_access(), sort_keys=True))
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'unavailable', 'reason': str(error)}, sort_keys=True))
        raise SystemExit(1)
