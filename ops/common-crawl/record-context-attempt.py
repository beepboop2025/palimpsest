#!/usr/bin/env python3
"""Record the private context attempt without shell/systemd format expansion."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

REVISION = Path('/usr/local/libexec/palimpsest-common-crawl/current/REVISION')
MARKER = Path('/etc/palimpsest/deployed-commit')
OUTPUT = Path('/var/lib/palimpsest/common-crawl/derived/archive-news-context.last-attempt.json')


def read_revision(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128:
            raise ValueError('unsafe revision file')
        return os.read(fd, 129)
    finally:
        os.close(fd)


def record_attempt(revision=REVISION, marker=MARKER, output=OUTPUT, *, now=None):
    left, right = read_revision(revision), read_revision(marker)
    matched = left == right and re.fullmatch(rb'[0-9a-f]{40}\n', left) is not None
    parent = output.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
        raise ValueError('attempt directory must be an existing owned directory')
    before = None
    if os.path.lexists(output):
        before = output.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o640:
            raise ValueError('unsafe attempt receipt')
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    document = {'schema': 'palimpsest-archive-context-refresh-attempt/v1',
                'revision_pin': 'match' if matched else 'mismatch',
                'attempted_at': stamp.isoformat().replace('+00:00', 'Z'),
                'note': 'The source revision must match the protected deployed marker before context refresh.'}
    raw = (json.dumps(document, sort_keys=True) + '\n').encode()
    fd, temporary = tempfile.mkstemp(prefix='.archive-context-attempt-', dir=output.parent)
    try:
        os.fchmod(fd, 0o640)
        if before is not None:
            os.fchown(fd, before.st_uid, before.st_gid)
            for name in getattr(os, 'listxattr', lambda _path: [])(output):
                if name == 'system.posix_acl_access':
                    os.setxattr(fd, name, os.getxattr(output, name))
        with os.fdopen(fd, 'wb') as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if before is None:
            if os.path.lexists(output):
                raise ValueError('attempt receipt appeared')
        else:
            current = output.lstat()
            if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns):
                raise ValueError('attempt receipt changed')
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return 0 if matched else 1


if __name__ == '__main__':
    raise SystemExit(record_attempt())
