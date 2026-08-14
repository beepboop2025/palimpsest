"""China econ telemetry — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory labelled its own healthy signal stale. This
driver has always written the reading unconditionally (scripts/china_econ_pull.py
line 106, no gate), so "when did we last look" is already answered honestly every
round. Its compatibility history is keyed by DATA date and is rewritten when a
source revises that date; the separate observation ledger preserves every vintage.

That property is load-bearing and easy to undo by accident, so it is pinned here
rather than left as a reading of the source. "When did the answer move" is
already carried by asof, the last trading day the portal actually reported.

Offline: the portal path is stubbed out entirely and only the writer runs. The
clock is stubbed too, because the reading stamps whole seconds and two rounds in
the same test second would otherwise be indistinguishable.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import collectors.china_econ as china_collector
from collectors.china_econ import ChinaEconCollection, FamilyCollection
from collectors.cny_fix_gap import read_parity
import scripts.china_econ_pull as pull


DAY = "2026-07-17"
BENCHMARKS = {"shibor_on": 1.42, "fdr007": 1.55, "usdcny_parity": 7.1234}
RAW_HASHES = {
    "shibor": "a" * 64,
    "repo_fixing": "b" * 64,
    "central_parity": "c" * 64,
}


def _family(metric):
    if metric.startswith("shibor_"):
        return "shibor"
    if metric.startswith(("fr0", "fdr")):
        return "repo_fixing"
    if metric == "usdcny_parity":
        return "central_parity"
    raise AssertionError(metric)


def _collection(rows):
    family_values = {}
    for day, values in rows.items():
        for metric, value in values.items():
            family_values.setdefault(_family(metric), {}).setdefault(day, {})[
                metric
            ] = value
    provenance = {
        family: FamilyCollection(
            values=values,
            raw_sha256=RAW_HASHES[family],
            evidence_url=f"https://www.chinamoney.com.cn/ags/test/{family}",
        )
        for family, values in family_values.items()
    }
    return ChinaEconCollection(values=rows, provenance=provenance)


class _Clock:
    """Advances six hours per round, matching the collector's cron cadence."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 17, 3, 41, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=6)
        return self.t


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the portal path stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "china-econ-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "china-econ-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())
    monkeypatch.setattr(pull, "REQUIRED_BENCHMARK_KEYS", frozenset(BENCHMARKS))

    def run(rows):
        # The whole input the writer needs is a date -> benchmarks mapping, so
        # the three throttle-spaced portal calls never happen.
        monkeypatch.setattr(pull, "collect", lambda start, end: _collection(rows))
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "china-econ-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "china-econ-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _observations(tmp_path):
    path = tmp_path / "china-econ-observations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_collector_hashes_exact_response_bytes_and_keeps_request_url(monkeypatch):
    raw = b'{"records": [], "responseTime": "2026-08-04T00:00:00Z"}'

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, amount=None):
            return raw

    monkeypatch.setattr(
        china_collector.urllib.request,
        "urlopen",
        lambda request, timeout: Response(),
    )
    response = china_collector._get(
        "/ags/test?period=2026-08", "https://example.test", retries=0
    )
    assert response is not None
    assert response.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert response.evidence_url == (
        "https://www.chinamoney.com.cn/ags/test?period=2026-08"
    )


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: benchmarks that do not
    move would stop rewriting the file, and the site would call a working feed
    stale. This driver must keep publishing every round's own look-time."""
    run, _ = publish
    first = run({DAY: dict(BENCHMARKS)})
    second = run({DAY: dict(BENCHMARKS)})

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time"
    )


def test_an_unchanged_round_appends_no_history(publish):
    """Compatibility history is latest-per-date. Re-reading an unchanged day
    already on file must not add a row or alter its value."""
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    run({DAY: dict(BENCHMARKS)})
    run({DAY: dict(BENCHMARKS)})

    assert len(_history(tmp_path)) == 1
    assert len(_observations(tmp_path)) == len(BENCHMARKS)


def test_asof_is_what_carries_movement(publish):
    """asof answers "when did the answer move" for this signal, which is why no
    separate last_changed_at is derived: a benchmark series moves when the portal
    reports a new trading day, and that date is already published."""
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    held = run({DAY: dict(BENCHMARKS)})
    moved = run({DAY: dict(BENCHMARKS), "2026-07-18": dict(BENCHMARKS)})

    assert held["asof"] == first["asof"]
    assert moved["asof"] == "2026-07-18"
    assert len(_history(tmp_path)) == 2


def test_a_revisit_completes_a_partial_day_without_shrinking_it(publish):
    """A throttled round can leave a day half-recorded. The next round has to
    fill the gap and append the completed row, never overwrite it with less."""
    run, tmp_path = publish
    run({DAY: {"shibor_on": 1.42}})
    run({DAY: dict(BENCHMARKS)})

    rows = _history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["fdr007"] == 1.55
    assert rows[0]["shibor_on"] == 1.42


def test_a_newer_partial_date_never_replaces_the_complete_public_snapshot(publish):
    """Families publish at different times, so max(date) is not completeness."""
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    newer = "2026-07-18"

    after = run(
        {
            DAY: dict(BENCHMARKS),
            newer: {"shibor_on": 1.43, "usdcny_parity": 7.12},
        }
    )

    assert after["generated_at"] > first["generated_at"]
    assert after["asof"] == DAY
    assert after["benchmarks"] == BENCHMARKS
    assert _history(tmp_path)[-1] == {
        "date": newer,
        "shibor_on": 1.43,
        "usdcny_parity": 7.12,
    }
    assert any(row["period_end"] == newer for row in _observations(tmp_path))


def test_long_form_vintages_are_bitemporal_and_revision_preserving(publish):
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    changed = dict(BENCHMARKS)
    changed["fdr007"] = 1.61
    run({DAY: changed})

    rows = _observations(tmp_path)
    fdr = [r for r in rows if r["series_id"] == "cn.cfets.fdr007"]
    assert [r["revision"] for r in fdr] == [0, 1]
    assert [r["value"] for r in fdr] == [1.55, 1.61]
    assert fdr[0]["released_at"] == fdr[0]["collected_at"]
    assert "first_observed_upper_bound" in fdr[0]["metadata"]["release_time_semantics"]
    assert fdr[0]["source_id"] == "cfets_benchmarks"
    assert "/ags/" in fdr[0]["evidence_url"]
    assert fdr[0]["raw_sha256"] == RAW_HASHES["repo_fixing"]
    assert _history(tmp_path)[0]["fdr007"] == 1.61


def test_a_revised_parity_updates_the_compatibility_consumer(publish):
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    revised = {**BENCHMARKS, "usdcny_parity": 7.2}
    run({DAY: revised})

    history_path = tmp_path / "china-econ-history.jsonl"
    assert _history(tmp_path)[0]["usdcny_parity"] == 7.2
    assert read_parity(str(history_path))[DAY] == 7.2


def test_backfilled_days_are_not_pretended_known_on_their_data_date(publish):
    run, tmp_path = publish
    old_day = "2025-12-31"
    reading = run({old_day: dict(BENCHMARKS)})
    row = _observations(tmp_path)[0]

    assert row["period_start"] == old_day
    assert row["released_at"] == reading["generated_at"].replace("Z", "+00:00")
    assert not row["released_at"].startswith(old_day)


def test_an_abstaining_round_publishes_nothing_at_all(publish):
    """No benchmark family answered, so there is no reading. The heartbeat is
    for rounds that produced something publishable, and a silent portal is not
    one of them — the file must keep the last honest observation time."""
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    run({})

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the reading as freshly observed"
    )
    assert len(_history(tmp_path)) == 1


def test_a_truncated_observation_ledger_fails_closed_before_publication(publish):
    run, tmp_path = publish
    first = run({DAY: dict(BENCHMARKS)})
    ledger = tmp_path / "china-econ-observations.jsonl"
    with ledger.open("ab") as handle:
        handle.write(b'{"partial":')

    with pytest.raises(pull.LedgerIntegrityError, match="record boundary"):
        run({DAY: {**BENCHMARKS, "fdr007": 1.61}})
    assert _reading(tmp_path) == first


def test_tampered_observation_id_is_rejected(publish):
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    ledger = tmp_path / "china-econ-observations.jsonl"
    rows = _observations(tmp_path)
    rows[0]["observation_id"] = "0" * 64
    ledger.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(pull.LedgerIntegrityError, match="observation_id"):
        pull.validate_observation_ledger(str(ledger))


def test_tampered_provenance_is_rejected(publish):
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})
    ledger = tmp_path / "china-econ-observations.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    rows[0]["raw_sha256"] = "f" * 64
    ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    with pytest.raises(pull.LedgerIntegrityError, match="observation_id"):
        pull.validate_observation_ledger(str(ledger))


def test_a_first_ever_round_writes_both_the_reading_and_the_history(publish):
    run, tmp_path = publish
    assert _reading(tmp_path) is None

    first = run({DAY: dict(BENCHMARKS)})

    assert first["generated_at"] == "2026-07-17T09:41:00Z"
    assert first["method_version"] == pull.METHOD_VERSION
    assert _history(tmp_path) == [{"date": DAY, **BENCHMARKS}]


def test_a_publish_also_refreshes_the_exact_byte_observation_manifest(publish):
    run, tmp_path = publish
    run({DAY: dict(BENCHMARKS)})

    ledger = tmp_path / "china-econ-observations.jsonl"
    manifest_path = tmp_path / "china-econ-observations-latest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["n_observations"] == len(BENCHMARKS)
    assert document["artifact"]["records"] == len(BENCHMARKS)
    assert document["artifact"]["bytes"] == ledger.stat().st_size
    assert document["artifact"]["sha256"] == hashlib.sha256(
        ledger.read_bytes()
    ).hexdigest()
    assert document["generated_at"] == "2026-07-17T09:41:00Z"
