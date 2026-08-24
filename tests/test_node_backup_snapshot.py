import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "ops" / "backup" / "node_backup_snapshot.py"
SNAPSHOT_ID = "20260813T010203Z"


def _load_verifier_module():
    spec = importlib.util.spec_from_file_location(
        "palimpsest_node_backup_snapshot", VERIFIER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier_module()


def _manifest(
    *, artifact_roots: str = "readings,data,newswire,analysis,witness"
) -> bytes:
    return (
        "format_version=4\n"
        f"snapshot_id={SNAPSHOT_ID}\n"
        "created_at_utc=2026-08-13T01:02:04Z\n"
        "host=palimpsest-fixture\n"
        "compose_project=palimpsest\n"
        "postgres_version=16.14\n"
        f"artifact_roots={artifact_roots}\n"
        "contents=postgres.dump,postgres.list,artifacts.tar.gz,artifacts.list\n"
    ).encode()


def _archive(members: list[tuple[str, str, bytes | None]] | None = None):
    witness_record = (
        json.dumps(
            {
                "ts": "2026-08-13T01:02:03+00:00",
                "n": 7,
                "head": "a" * 64,
                "root": "b" * 64,
                "alerts": 0,
            }
        ).encode()
        + b"\n"
    )
    selected = members or [
        ("analysis", "directory", None),
        ("analysis/delivery", "directory", None),
        (
            "analysis/delivery/wire-claim-audits-latest.json",
            "file",
            b"{}\n",
        ),
        ("analysis/private", "directory", None),
        ("analysis/private/state.json", "file", b"{}\n"),
        ("readings", "directory", None),
        ("readings/reading.json", "file", b"{}\n"),
        ("data", "directory", None),
        ("data/index.json", "file", b"{}\n"),
        ("newswire", "directory", None),
        ("newswire/newswire-latest.json", "file", b"{}\n"),
        ("witness", "directory", None),
        (
            "witness/erasure-ledger.witness.jsonl",
            "file",
            witness_record,
        ),
        (
            "witness/eval-registry.witness.jsonl",
            "file",
            witness_record,
        ),
        (
            "witness/public-freshness-state.json",
            "file",
            b'{"conditions":{},"schema_version":"palimpsest-public-freshness-state.v1"}\n',
        ),
    ]
    payload = io.BytesIO()
    listing: list[str] = []
    with tarfile.open(
        fileobj=payload, mode="w:gz", format=tarfile.PAX_FORMAT
    ) as archive:
        for name, kind, content in selected:
            member = tarfile.TarInfo(name)
            if name == "witness" or name.startswith("witness/"):
                member.uid = verifier.WITNESS_UID
                member.gid = verifier.WITNESS_GID
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                member.mode = 0o755 if name == "witness" else 0o700
                archive.addfile(member)
                listing.append(f"{name.rstrip('/')}/")
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = str(content)
                archive.addfile(member)
                listing.append(name)
            else:
                assert content is not None
                member.type = tarfile.REGTYPE
                member.mode = 0o644 if name in verifier.WITNESS_HISTORY_FILES else 0o600
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
                listing.append(name)
    return payload.getvalue(), listing


def _refresh_checksums(snapshot: Path) -> None:
    lines = []
    for name in verifier.HASHED_FILES:
        digest = hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    (snapshot / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    for path in snapshot.iterdir():
        path.chmod(0o600)


def _fixture(tmp_path: Path) -> Path:
    snapshot = tmp_path / SNAPSHOT_ID
    snapshot.mkdir(mode=0o700)
    archive_payload, listing = _archive()
    (snapshot / "MANIFEST.txt").write_bytes(_manifest())
    (snapshot / "artifacts.tar.gz").write_bytes(archive_payload)
    (snapshot / "artifacts.list").write_text(
        "".join(f"{name}\n" for name in listing), encoding="utf-8"
    )
    (snapshot / "postgres.dump").write_bytes(b"PGDMP\x01fixture")
    (snapshot / "postgres.list").write_text(
        "; Archive created at 2026-08-13 01:02:03 UTC\n",
        encoding="utf-8",
    )
    (snapshot / "SHA256SUMS").touch()
    _refresh_checksums(snapshot)
    snapshot.chmod(0o700)
    return snapshot


def _replace_archive_member(
    snapshot: Path,
    target: str,
    *,
    payload: bytes | None = None,
    mode: int | None = None,
    uid: int | None = None,
) -> None:
    source_path = snapshot / "artifacts.tar.gz"
    replacement = io.BytesIO()
    with (
        tarfile.open(source_path, mode="r:gz") as source,
        tarfile.open(
            fileobj=replacement, mode="w:gz", format=tarfile.PAX_FORMAT
        ) as destination,
    ):
        for original in source:
            member = tarfile.TarInfo(original.name)
            member.type = original.type
            member.mode = (
                original.mode if mode is None or original.name != target else mode
            )
            member.uid = original.uid if uid is None or original.name != target else uid
            member.gid = original.gid
            member.mtime = original.mtime
            if original.isreg():
                extracted = source.extractfile(original)
                assert extracted is not None
                content = extracted.read()
                if original.name == target and payload is not None:
                    content = payload
                member.size = len(content)
                destination.addfile(member, io.BytesIO(content))
            else:
                destination.addfile(member)
    source_path.write_bytes(replacement.getvalue())
    _refresh_checksums(snapshot)


def _verify(snapshot: Path, **overrides):
    options = {
        "snapshot_id": SNAPSHOT_ID,
        "expected_uid": os.getuid(),
        "expected_gid": os.getgid(),
    }
    options.update(overrides)
    return verifier.verify_snapshot(snapshot, **options)


def test_good_snapshot_produces_deterministic_restore_proof(tmp_path):
    snapshot = _fixture(tmp_path)

    first = _verify(snapshot)
    second = _verify(snapshot)

    assert first == second
    assert first["schema"] == "palimpsest-node-backup-verification.v1"
    assert first["status"] == "verified"
    assert first["snapshot"] == SNAPSHOT_ID
    assert first["counts"] == {
        "artifact_directories": 7,
        "artifact_files": 8,
        "artifact_members": 15,
        "checksum_entries": 5,
        "snapshot_files": 6,
        "witness_history_records": 2,
    }
    assert set(first["digests"]) == set(verifier.HASHED_FILES)


def test_artifact_traversal_is_rejected_even_with_fresh_checksums(tmp_path):
    snapshot = _fixture(tmp_path)
    payload, listing = _archive(
        [
            ("analysis", "directory", None),
            ("readings", "directory", None),
            ("data", "directory", None),
            ("newswire", "directory", None),
            ("readings/../escape", "file", b"private"),
        ]
    )
    (snapshot / "artifacts.tar.gz").write_bytes(payload)
    (snapshot / "artifacts.list").write_text(
        "".join(f"{name}\n" for name in listing), encoding="utf-8"
    )
    _refresh_checksums(snapshot)

    with pytest.raises(verifier.VerificationError, match="unsafe path"):
        _verify(snapshot)


def test_artifact_link_is_rejected_even_under_an_allowed_root(tmp_path):
    snapshot = _fixture(tmp_path)
    payload, listing = _archive(
        [
            ("analysis", "directory", None),
            ("readings", "directory", None),
            ("data", "directory", None),
            ("newswire", "directory", None),
            ("analysis/private-state", "symlink", b"/etc/shadow"),
        ]
    )
    (snapshot / "artifacts.tar.gz").write_bytes(payload)
    (snapshot / "artifacts.list").write_text(
        "".join(f"{name}\n" for name in listing), encoding="utf-8"
    )
    _refresh_checksums(snapshot)

    with pytest.raises(verifier.VerificationError, match="link or special"):
        _verify(snapshot)


def test_extra_snapshot_entry_is_rejected(tmp_path):
    snapshot = _fixture(tmp_path)
    (snapshot / "unexpected.txt").write_text("surprise\n", encoding="utf-8")

    with pytest.raises(verifier.VerificationError, match="inventory is not exact"):
        _verify(snapshot)


def test_required_snapshot_file_cannot_be_a_symlink(tmp_path):
    snapshot = _fixture(tmp_path)
    target = snapshot / "postgres.dump"
    outside = tmp_path / "postgres.dump.outside"
    target.rename(outside)
    target.symlink_to(outside)

    with pytest.raises(verifier.VerificationError, match="single-link regular"):
        _verify(snapshot)


def test_required_snapshot_file_cannot_have_a_second_hard_link(tmp_path):
    snapshot = _fixture(tmp_path)
    os.link(snapshot / "postgres.dump", tmp_path / "postgres.dump.alias")

    with pytest.raises(verifier.VerificationError, match="single-link regular"):
        _verify(snapshot)


def test_production_mode_enforces_mode_but_scratch_restore_ignores_it(tmp_path):
    snapshot = _fixture(tmp_path)
    (snapshot / "postgres.dump").chmod(0o644)

    with pytest.raises(verifier.VerificationError, match="mode is invalid"):
        _verify(snapshot)

    assert _verify(snapshot, scratch_restore=True)["status"] == "verified"


def test_production_owner_contract_is_configurable_and_scratch_ignores_it(tmp_path):
    snapshot = _fixture(tmp_path)
    impossible_uid = os.getuid() + 10_000

    with pytest.raises(verifier.VerificationError, match="ownership or mode"):
        _verify(snapshot, expected_uid=impossible_uid)

    assert (
        _verify(
            snapshot,
            expected_uid=impossible_uid,
            expected_gid=os.getgid() + 10_000,
            scratch_restore=True,
        )["status"]
        == "verified"
    )


def test_manifest_contract_mismatch_is_rejected(tmp_path):
    snapshot = _fixture(tmp_path)
    (snapshot / "MANIFEST.txt").write_bytes(_manifest(artifact_roots="readings,data"))
    _refresh_checksums(snapshot)

    with pytest.raises(verifier.VerificationError, match="artifact roots"):
        _verify(snapshot)


def test_legacy_manifest_cannot_claim_witness_covered_restore(tmp_path):
    snapshot = _fixture(tmp_path)
    (snapshot / "MANIFEST.txt").write_bytes(
        _manifest().replace(b"format_version=4\n", b"format_version=3\n")
    )
    _refresh_checksums(snapshot)

    with pytest.raises(verifier.VerificationError, match="format version is not 4"):
        _verify(snapshot)


@pytest.mark.parametrize(
    ("payload", "mode", "uid", "match"),
    [
        (b'{"n":1}\n', None, None, "fields are not exact"),
        (
            b'{"ts":"2026-08-13T01:02:03+00:00","ts":"duplicate","n":1,'
            b'"head":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"root":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            b'"alerts":0}\n',
            None,
            None,
            "duplicate JSON fields",
        ),
        (None, 0o666, None, "history mode is invalid"),
        (None, None, 0, "ownership metadata is invalid"),
    ],
)
def test_witness_history_restore_contract_rejects_malformed_payload_or_metadata(
    tmp_path, payload, mode, uid, match
):
    snapshot = _fixture(tmp_path)
    _replace_archive_member(
        snapshot,
        "witness/eval-registry.witness.jsonl",
        payload=payload,
        mode=mode,
        uid=uid,
    )

    with pytest.raises(verifier.VerificationError, match=match):
        _verify(snapshot)


def test_payload_hash_mismatch_is_rejected(tmp_path):
    snapshot = _fixture(tmp_path)
    (snapshot / "postgres.dump").write_bytes(b"tampered")

    with pytest.raises(verifier.VerificationError, match="fails its checksum"):
        _verify(snapshot)


def test_artifact_list_must_exactly_match_the_archive(tmp_path):
    snapshot = _fixture(tmp_path)
    lines = (snapshot / "artifacts.list").read_text(encoding="utf-8").splitlines()
    (snapshot / "artifacts.list").write_text(
        "".join(f"{line}\n" for line in lines[:-1]), encoding="utf-8"
    )
    _refresh_checksums(snapshot)

    with pytest.raises(verifier.VerificationError, match="does not match"):
        _verify(snapshot)


def test_pack_uses_exact_open_files_and_builds_an_inspectable_outer_tar(tmp_path):
    snapshot = _fixture(tmp_path)
    output = tmp_path / "transport.tar"

    packed = verifier.pack_snapshot(
        snapshot,
        snapshot_id=SNAPSHOT_ID,
        output=output,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    inspected = verifier.inspect_outer_archive(output, snapshot_id=SNAPSHOT_ID)

    assert packed["status"] == "packed"
    assert packed["archive_sha256"] == inspected["archive_sha256"]
    assert packed["digests"] == inspected["digests"]
    assert inspected["counts"] == {"members": 7, "snapshot_files": 6}
    assert output.stat().st_mode & 0o777 == 0o600
    with tarfile.open(output, mode="r:") as archive:
        assert archive.getnames() == [
            SNAPSHOT_ID,
            *(f"{SNAPSHOT_ID}/{name}" for name in sorted(verifier.SNAPSHOT_FILES)),
        ]


def test_pack_fsyncs_the_output_file_and_parent_directory(tmp_path, monkeypatch):
    snapshot = _fixture(tmp_path)
    output = tmp_path / "transport.tar"
    original_fsync = verifier.os.fsync
    flushed: list[str] = []

    def recording_fsync(descriptor):
        metadata = os.fstat(descriptor)
        flushed.append("directory" if stat.S_ISDIR(metadata.st_mode) else "file")
        return original_fsync(descriptor)

    monkeypatch.setattr(verifier.os, "fsync", recording_fsync)
    verifier.pack_snapshot(
        snapshot,
        snapshot_id=SNAPSHOT_ID,
        output=output,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )

    assert "file" in flushed
    assert flushed[-1] == "directory"


def test_pack_rejects_a_path_swap_after_opening_and_removes_partial_output(
    tmp_path, monkeypatch
):
    snapshot = _fixture(tmp_path)
    output = tmp_path / "transport.tar"
    original_read = verifier._HashingReader.read
    swapped = False

    def swap_once(reader, size=-1):
        nonlocal swapped
        if not swapped:
            swapped = True
            original = snapshot / "postgres.dump"
            held = snapshot / "postgres.dump.held"
            original.rename(held)
            original.symlink_to(held)
        return original_read(reader, size)

    monkeypatch.setattr(verifier._HashingReader, "read", swap_once)

    with pytest.raises(verifier.VerificationError, match="snapshot file changed"):
        verifier.pack_snapshot(
            snapshot,
            snapshot_id=SNAPSHOT_ID,
            output=output,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )
    assert not output.exists()


def _outer_archive(
    path: Path,
    members: list[tuple[str, str, bytes | None]],
) -> None:
    with tarfile.open(path, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, kind, payload in members:
            member = tarfile.TarInfo(name)
            member.uid = os.getuid()
            member.gid = os.getgid()
            if kind == "directory":
                member.type = tarfile.DIRTYPE
                member.mode = 0o700
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.mode = 0o600
                member.linkname = str(payload)
                archive.addfile(member)
            else:
                assert payload is not None
                member.type = tarfile.REGTYPE
                member.mode = 0o600
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))


def test_outer_inspection_rejects_a_link_member(tmp_path):
    output = tmp_path / "adversarial.tar"
    members = [(SNAPSHOT_ID, "directory", None)]
    for name in sorted(verifier.SNAPSHOT_FILES):
        kind = "symlink" if name == "postgres.dump" else "file"
        members.append((f"{SNAPSHOT_ID}/{name}", kind, b"/etc/shadow"))
    _outer_archive(output, members)

    with pytest.raises(verifier.VerificationError, match="link or special"):
        verifier.inspect_outer_archive(output, snapshot_id=SNAPSHOT_ID)


def test_outer_inspection_rejects_traversal_and_extra_members(tmp_path):
    output = tmp_path / "adversarial.tar"
    members = [(SNAPSHOT_ID, "directory", None)]
    members.extend(
        (f"{SNAPSHOT_ID}/{name}", "file", b"payload")
        for name in sorted(verifier.SNAPSHOT_FILES)
    )
    members.append((f"{SNAPSHOT_ID}/../escape", "file", b"private"))
    _outer_archive(output, members)

    with pytest.raises(verifier.VerificationError, match="inventory|unsafe"):
        verifier.inspect_outer_archive(output, snapshot_id=SNAPSHOT_ID)


def test_outer_inspection_rejects_a_symlink_archive_path(tmp_path):
    snapshot = _fixture(tmp_path)
    output = tmp_path / "transport.tar"
    verifier.pack_snapshot(
        snapshot,
        snapshot_id=SNAPSHOT_ID,
        output=output,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    alias = tmp_path / "transport-alias.tar"
    alias.symlink_to(output)

    with pytest.raises(verifier.VerificationError, match="opened safely"):
        verifier.inspect_outer_archive(alias, snapshot_id=SNAPSHOT_ID)
