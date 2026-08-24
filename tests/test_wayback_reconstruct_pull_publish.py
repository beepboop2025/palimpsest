"""Wayback reconstruction — publication timing.

The fleet-wide fix that added last_changed_at to the other pulls does not apply
to this one, and these tests are the record of why. The other drivers gated the
reading itself on change, so a finding that held still stopped refreshing
generated_at and the observatory ended up labelling its own healthy signal
stale. This driver has never had that gate: scripts/wayback_reconstruct_pull.py
opens readings/wayback-latest.json and writes it on every round that survives
the reachability guard, so "when did we last look" is already answered honestly
each time and the site cannot call a working reconstruction dead. The module
already says so in prose next to METHOD_VERSION, and that comment is only true
for as long as the unconditional write stays unconditional.

The second half of the reference pattern does not transfer either. Elsewhere the
history file is gated on change so the movement record does not fill with
heartbeats. Here a history row is not a movement claim, it is a coverage claim:
n_reachable says how many watched URLs the Archive answered for this round, and
that number is the thing most likely to move without any censorship event moving
at all. Gating the append would erase the difference between "the Archive was
thin this round" and "we did not run", which is the same conflation the fleet
just spent a wave removing from the readings.

Both properties are load-bearing and easy to undo by accident. Adding a
write-if-changed gate here would look like a tidy-up, and would reintroduce the
exact bug the fleet just finished fixing, so they are pinned here rather than
left as a reading of the source.

Offline by construction: the only impure seam in the collector is the injected
CDX fetch, so the tests hand it synthetic CDX rows and the real reconstruction
logic runs unchanged. Nothing touches archive.org. The clock is stubbed too, so
two rounds are distinguishable no matter how fast the suite runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collectors.undertext import DELETION

import scripts.wayback_reconstruct_pull as pull


URL = "https://baike.baidu.com/item/%E9%9D%92%E5%B9%B4%E5%A4%B1%E4%B8%9A%E7%8E%87"
WATCHLIST = [{"url": URL, "term": "青年失业率", "domain": "ECONOMY"}]

_HEADER = ["timestamp", "original", "statuscode", "digest", "mimetype", "length"]

# One live capture then another with the same digest: nothing moved, which is the
# ordinary state of a watched page and exactly the state that used to read as a
# dead feed on the other signals.
STABLE = [
    ("20240101000000", URL, "200", "AAAAAAAA", "text/html", "10240"),
    ("20250101000000", URL, "200", "AAAAAAAA", "text/html", "10240"),
]
# The same page, later found gone. The deletion is bracketed by the two captures.
DELETED = [
    ("20240101000000", URL, "200", "AAAAAAAA", "text/html", "10240"),
    ("20250101000000", URL, "404", "-", "text/html", "0"),
]


def _high_change_timeline(url: str, *, n_transitions: int = 2_000) -> list[tuple]:
    """One deletion plus enough live digest changes to reach the exact total."""

    started = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rows = [
        (
            (started + timedelta(hours=offset)).strftime("%Y%m%d%H%M%S"),
            url,
            "200",
            f"DIGEST-{offset:04d}",
            "text/html",
            "10240",
        )
        for offset in range(n_transitions)
    ]
    rows.append(
        (
            (started + timedelta(hours=n_transitions)).strftime("%Y%m%d%H%M%S"),
            url,
            "404",
            "-",
            "text/html",
            "0",
        )
    )
    return rows


class _Clock:
    """Advances twelve hours per round, matching the collector's cron cadence."""

    def __init__(self) -> None:
        self.t = datetime(2026, 7, 17, 3, 23, 0, tzinfo=timezone.utc)

    def now(self, tz=None) -> datetime:
        self.t += timedelta(hours=12)
        return self.t


class _Unreachable(OSError):
    """What an injected fetch raises when CDX does not answer for a URL."""


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the CDX fetch stubbed."""
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "wayback-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "wayback-history.jsonl"))
    monkeypatch.setattr(pull, "datetime", _Clock())
    # The rate ceiling sleeps in real seconds to be polite to a shared public
    # API. That politeness is not what these tests are about, and four rounds
    # against a 0.5/s bucket would cost the suite several seconds of nothing.
    monkeypatch.setattr(pull, "RateCeiling", lambda **kw: None)
    # The kill switch stays real so the governance wiring is genuinely exercised,
    # but it is pointed at a path that does not exist, so a halt file left behind
    # on a developer's machine cannot decide the outcome of a test.
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "absent-halt-file"))

    def run(rows, watchlist=WATCHLIST, *, raw_payload=None):
        def fetch(url, **kw):
            if raw_payload is not None:
                return raw_payload
            if rows is None:
                raise _Unreachable("CDX did not answer")
            return json.dumps([_HEADER] + [list(r) for r in rows])

        monkeypatch.setattr(pull, "load_watchlist", lambda: list(watchlist))
        monkeypatch.setattr(pull, "default_cdx_fetch", fetch)
        pull.main()
        return _reading(tmp_path)

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "wayback-latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "wayback-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_an_unchanged_round_still_refreshes_the_observation_time(publish):
    """The bug this guards against being introduced here: a watched page that is
    simply still there never moves any number in the reading, so a
    write-if-changed gate would stop rewriting the file and the site would call a
    working reconstruction stale. This driver must keep publishing every round's
    own look-time."""
    run, _ = publish
    first = run(STABLE)
    second = run(STABLE)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time"
    )


def test_the_unchanged_round_really_was_unchanged(publish):
    """Without this the test above would pass even if the rounds differed, and
    would then be pinning nothing at all."""
    run, _ = publish
    first = run(STABLE)
    second = run(STABLE)

    for field in (
        "n_watched",
        "n_reachable",
        "n_deletions",
        "n_mutations",
        "method_version",
    ):
        assert second[field] == first[field]
    assert second["reconstructions"] == first["reconstructions"]
    assert first["reconstructions"][0]["event"] == "stable"


def test_the_history_records_every_round_not_only_the_moved_ones(publish):
    """A row here is a coverage claim, not a movement claim. n_reachable answers
    how much of the watchlist the Archive served this round, and a gap in that
    series would be indistinguishable from a round that never ran."""
    run, tmp_path = publish
    run(STABLE)
    run(STABLE)
    run(STABLE)

    rows = _history(tmp_path)
    assert len(rows) == 3
    assert [r["n_reachable"] for r in rows] == [1, 1, 1]
    assert len({r["generated_at"] for r in rows}) == 3


def test_wayback_history_binds_the_cardinality_method_version(publish):
    run, tmp_path = publish
    run(STABLE)

    rows = _history(tmp_path)
    assert [row["method_version"] for row in rows] == [pull.METHOD_VERSION]


def test_a_moved_finding_reaches_both_the_reading_and_the_history(publish):
    """The page was live, then the Archive found it gone. That is the event this
    signal exists to catch, and it has to be visible in both files."""
    run, tmp_path = publish
    stable = run(STABLE)
    moved = run(DELETED)

    assert stable["n_deletions"] == 0
    assert moved["n_deletions"] == 1
    assert moved["reconstructions"][0]["event"] == DELETION
    # The deletion moment is only known to within the capture bracket, so what is
    # published is that bracket rather than a false-precise instant.
    assert moved["reconstructions"][0]["latency_bracket_s"] > 0
    assert moved["generated_at"] > stable["generated_at"]

    rows = _history(tmp_path)
    assert [r["n_deletions"] for r in rows] == [0, 1]


def test_two_thousand_changes_publish_only_the_primary_deletion(publish):
    run, _ = publish
    reading = run(_high_change_timeline(URL))

    assert reading["n_transitions_total"] == 2_000
    assert reading["n_transitions_published"] == 1
    assert reading["n_transitions_omitted"] == 1_999
    assert len(reading["ddti_observations"]) == 1
    assert reading["ddti_observations"][0]["deletion_signal"] == DELETION
    assert reading["reconstructions"][0]["event"] == DELETION
    assert reading["transition_counts"] == {
        "total": {"deletion": 1, "mutation": 1_999, "other": 0},
        "published": {"deletion": 1, "mutation": 0, "other": 0},
        "omitted": {"deletion": 0, "mutation": 1_999, "other": 0},
    }


def test_an_all_unreachable_round_publishes_nothing_at_all(publish):
    """The abstain path. CDX answered for no watched URL, so there is no
    observation to stamp. The heartbeat is for rounds that produced something
    publishable, and an Archive that went silent is not one of them: the file
    must keep the last honest observation time rather than be restamped as
    freshly looked at."""
    run, tmp_path = publish
    first = run(STABLE)
    run(None)

    after = _reading(tmp_path)
    assert after["generated_at"] == first["generated_at"], (
        "an abstaining round must not restamp the reading as freshly observed"
    )
    assert after["n_reachable"] == 1
    assert len(_history(tmp_path)) == 1


def test_an_all_malformed_round_does_not_advance_reachability(publish):
    run, tmp_path = publish
    first = run(STABLE)
    run(STABLE, raw_payload="<html>upstream error</html>")

    after = _reading(tmp_path)
    assert after == first
    assert len(_history(tmp_path)) == 1


def test_a_valid_empty_cdx_result_preserves_the_watched_url(publish):
    run, _ = publish
    reading = run([])

    assert reading["n_reachable"] == 1
    assert reading["reconstructions"][0]["event"] == "no_baseline"
    assert reading["reconstructions"][0]["url"] == URL
    assert reading["reconstructions"][0]["locator"] == URL


def test_an_empty_watchlist_publishes_nothing(publish):
    """Nothing was watched, so nothing was observed. Same rule as the unreachable
    round: no input means no new observation time."""
    run, tmp_path = publish
    first = run(STABLE)
    run(STABLE, watchlist=[])

    assert _reading(tmp_path)["generated_at"] == first["generated_at"]
    assert len(_history(tmp_path)) == 1


def test_a_first_ever_round_writes_both_the_reading_and_the_history(publish):
    run, tmp_path = publish
    assert _reading(tmp_path) is None

    first = run(STABLE)

    assert first["generated_at"] == "2026-07-17T15:23:00+00:00"
    assert first["method_version"] == pull.METHOD_VERSION
    assert _history(tmp_path) == [
        {
            "generated_at": first["generated_at"],
            "method_version": pull.METHOD_VERSION,
            "n_watched": 1,
            "n_reachable": 1,
            "n_deletions": 0,
            "n_mutations": 0,
            "n_transitions_total": 0,
            "n_transitions_published": 0,
            "n_transitions_omitted": 0,
        }
    ]


def test_a_corrupt_previous_reading_cannot_stop_a_round(publish):
    """The other drivers have to read the previous file to decide whether
    anything moved, which is a parse that can fail. This one never consults it,
    so there is no backfill path and no decode error to swallow, and a truncated
    file left behind by a killed run is simply replaced. Anyone adding a gate
    here inherits that failure mode along with it."""
    run, tmp_path = publish
    first = run(STABLE)
    (tmp_path / "wayback-latest.json").write_text("{ truncated", encoding="utf-8")

    second = run(STABLE)

    assert second["generated_at"] > first["generated_at"]
    assert second["n_watched"] == 1
    assert second["reconstructions"][0]["term"] == "青年失业率"


def test_max_watchlist_wayback_and_undertext_fit_and_archive(
    tmp_path,
    monkeypatch,
):
    from core.artifact_store import DEFAULT_MAX_BYTES
    from core.collector_fleet import SNAPSHOT_OUTPUTS, run_snapshot_job
    from scripts import undertext_pull

    assert DEFAULT_MAX_BYTES == 16 * 1024 * 1024
    watchlist = json.loads(Path(pull.WATCHLIST).read_text(encoding="utf-8"))[
        "watchlist"
    ]
    assert len(watchlist) == 21
    assert len({entry["url"] for entry in watchlist}) == len(watchlist)

    generated = tmp_path / "generated"
    readings = generated / "readings"
    readings.mkdir(parents=True)
    monkeypatch.setattr(pull, "READINGS", str(readings))
    monkeypatch.setattr(pull, "OUT", str(readings / "wayback-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(readings / "wayback-history.jsonl"))
    monkeypatch.setattr(pull, "load_watchlist", lambda: list(watchlist))
    monkeypatch.setattr(pull, "KillSwitch", None)
    monkeypatch.setattr(pull, "RateCeiling", lambda **_kwargs: None)
    monkeypatch.setattr(pull, "datetime", _Clock())

    def fetch(url, **_kwargs):
        return json.dumps(
            [_HEADER]
            + [list(row) for row in _high_change_timeline(url, n_transitions=1_999)],
            ensure_ascii=False,
        )

    monkeypatch.setattr(pull, "default_cdx_fetch", fetch)
    pull.main()

    wayback_path = readings / "wayback-latest.json"
    wayback = json.loads(wayback_path.read_text(encoding="utf-8"))
    assert wayback["n_watched"] == len(watchlist)
    assert all(row["n_captures"] == 2_000 for row in wayback["reconstructions"])
    assert wayback["n_transitions_total"] == 1_999 * len(watchlist)
    assert wayback["n_transitions_published"] == len(watchlist)
    assert wayback["n_transitions_omitted"] == 1_998 * len(watchlist)
    assert len(wayback["ddti_observations"]) == len(watchlist)
    assert len({row["url"] for row in wayback["ddti_observations"]}) == len(watchlist)
    assert wayback_path.stat().st_size < DEFAULT_MAX_BYTES

    class _ReadyKillSwitch:
        def is_halted(self) -> bool:
            return False

    monkeypatch.setattr(undertext_pull, "READINGS", readings)
    monkeypatch.setattr(
        undertext_pull,
        "OUT",
        readings / "undertext-latest.json",
    )
    monkeypatch.setattr(
        undertext_pull,
        "HIST",
        readings / "undertext-history.jsonl",
    )
    monkeypatch.setattr(undertext_pull, "KillSwitch", _ReadyKillSwitch)
    monkeypatch.setattr(undertext_pull, "_live_surfaces_enabled", lambda: False)
    monkeypatch.setattr(undertext_pull, "load_china_lake_receipt", lambda: None)
    monkeypatch.setattr(undertext_pull, "open_existing_database", lambda: None)
    undertext = undertext_pull.main(
        now=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    )
    assert undertext is not None
    undertext_path = readings / "undertext-latest.json"
    assert undertext_path.stat().st_size < DEFAULT_MAX_BYTES

    payloads = {
        "wayback": wayback_path.read_bytes(),
        "undertext": undertext_path.read_bytes(),
    }
    archive_repo = tmp_path / "archive-repo"
    monkeypatch.setenv("PALIMPSEST_OBSERVATION_ARCHIVE_ENABLED", "1")
    monkeypatch.delenv("PALIMPSEST_OBSERVATION_DIR", raising=False)
    monkeypatch.delenv("PALIMPSEST_OBSERVATION_MAX_BYTES", raising=False)

    def install_snapshot(name, root):
        target = root / SNAPSHOT_OUTPUTS[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payloads[name])

    for name in ("wayback", "undertext"):
        result = run_snapshot_job(
            name,
            root=archive_repo,
            invoke=install_snapshot,
            kill_switch=_ReadyKillSwitch(),
        )
        assert result["status"] == "success"
        assert result["artifact"]["original_bytes"] == len(payloads[name])
        assert result["artifact"]["original_bytes"] < DEFAULT_MAX_BYTES
        assert (archive_repo / result["artifact"]["archive_path"]).is_file()
