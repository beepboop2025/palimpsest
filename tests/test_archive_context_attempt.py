import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('context_attempt',ROOT/'ops/common-crawl/record-context-attempt.py')
attempt=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(attempt)


@pytest.fixture
def paths(tmp_path):
    revision=tmp_path/'REVISION';marker=tmp_path/'deployed-commit';output=tmp_path/'attempt.json'
    revision.write_text('a'*40+'\n');marker.write_text('a'*40+'\n')
    return revision,marker,output


def test_match_records_real_json_and_clock_without_format_expansion(paths):
    revision,marker,output=paths
    assert attempt.record_attempt(*paths)==0
    value=json.loads(output.read_bytes())
    assert value['revision_pin']=='match' and value['attempted_at'].endswith('Z')
    assert '/usr/bin/bash' not in output.read_text() and '%s' not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode)==0o640


def test_pin_mismatch_records_attempt_then_fails_closed(paths):
    revision,marker,output=paths;marker.write_text('b'*40+'\n')
    assert attempt.record_attempt(*paths)==1
    assert json.loads(output.read_bytes())['revision_pin']=='mismatch'


def test_empty_failed_shell_attempt_is_replaced_only_by_metadata(paths):
    revision,marker,output=paths;output.write_bytes(b'');output.chmod(0o640)
    before=(output.stat().st_uid,output.stat().st_gid)
    assert attempt.record_attempt(*paths)==0
    assert (output.stat().st_uid,output.stat().st_gid)==before
    assert set(json.loads(output.read_bytes()))=={'schema','revision_pin','attempted_at','note'}


def test_unsafe_output_symlink_preserves_target(paths):
    revision,marker,output=paths;output.symlink_to(revision);before=revision.read_bytes()
    with pytest.raises(ValueError,match='unsafe attempt'):
        attempt.record_attempt(*paths)
    assert revision.read_bytes()==before


def test_malformed_equal_pin_never_counts_as_match(paths):
    revision,marker,output=paths;revision.write_text('invalid');marker.write_text('invalid')
    assert attempt.record_attempt(*paths)==1


def test_attempt_helper_is_packaged_and_unit_retains_all_dependency_gates():
    unit=(ROOT/'ops/systemd/palimpsest-common-crawl-context.service').read_text()
    assert 'ExecStartPre=/bin/sh -c' not in unit
    assert 'ExecStartPre=/usr/bin/python3 -B /usr/local/libexec/palimpsest-common-crawl/current/record-context-attempt.py' in unit
    assert 'ExecStartPre=/bin/sh /usr/local/libexec/palimpsest-common-crawl/current/verify-host-bundle.sh' in unit
    assert 'ExecStartPre=/usr/bin/cmp -s /usr/local/libexec/palimpsest-common-crawl/current/REVISION /etc/palimpsest/deployed-commit' in unit
    assert 'Requires=palimpsest-public-osint-sync.service' in unit and 'IPAddressDeny=any' in unit
    installer=(ROOT/'ops/common-crawl/install-host-bundle.sh').read_text()
    assert 'ops/common-crawl/record-context-attempt.py:record-context-attempt.py:0444' in installer
    assert 'README.md REVISION record-context-attempt.py restore-context-reader.py backup/README.md' in installer
