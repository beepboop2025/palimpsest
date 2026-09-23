from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops/railway/verify_readonly_tree.py"
spec = importlib.util.spec_from_file_location("readonly_tree", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def sealed(tmp_path):
    root = tmp_path / "site"
    (root / "nested").mkdir(parents=True)
    (root / "index.html").write_bytes(b"exact public bytes\n")
    (root / "nested" / "executable.py").write_bytes(b"print('sealed')\n")
    (root / "index.html").chmod(0o444)
    (root / "nested" / "executable.py").chmod(0o555)
    (root / "nested").chmod(0o555)
    root.chmod(0o555)
    yield root
    root.chmod(0o755)
    (root / "nested").chmod(0o755)


def identity(root):
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.lstat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *root.rglob("*")]
    }


def test_verification_preserves_every_byte_and_mode(sealed):
    before = identity(sealed)
    assert module.verify(sealed) == {"status": "verified", "files": 2, "directories": 2}
    assert identity(sealed) == before


@pytest.mark.parametrize("relative", [".", "nested", "index.html", "nested/executable.py"])
@pytest.mark.parametrize("write_bit", [0o200, 0o020, 0o002])
def test_any_writable_file_or_directory_fails(sealed, relative, write_bit):
    path = sealed / relative
    path.chmod(stat.S_IMODE(path.stat().st_mode) | write_bit)
    with pytest.raises(ValueError, match="writable artifact"):
        module.verify(sealed)


@pytest.mark.parametrize("target", ["index.html", "nested", "missing"])
def test_symlinks_are_rejected_without_following_them(sealed, target):
    sealed.chmod(0o755)
    (sealed / "link").symlink_to(target)
    sealed.chmod(0o555)
    with pytest.raises(ValueError, match="nonregular"):
        module.verify(sealed)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_fifo_is_rejected_without_opening_it(sealed):
    sealed.chmod(0o755)
    os.mkfifo(sealed / "pipe", 0o444)
    sealed.chmod(0o555)
    with pytest.raises(ValueError, match="nonregular"):
        module.verify(sealed)


def test_root_symlink_and_missing_root_fail_closed(sealed, tmp_path):
    link = tmp_path / "linked-root"
    link.symlink_to(sealed, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        module.verify(link)
    with pytest.raises(FileNotFoundError):
        module.verify(tmp_path / "missing")


def test_isolated_cli_exits_nonzero_for_writable_input(sealed):
    (sealed / "index.html").chmod(0o644)
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--root", str(sealed)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "writable artifact" in result.stderr
