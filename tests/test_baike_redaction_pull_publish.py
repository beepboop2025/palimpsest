"""Baike redaction — publication timing.

collectors/baike_redaction.py answers "has the state encyclopedia forked from the
open record". These tests cover the separate question the publisher has to answer
honestly: when did we last look, as against when did the answer last move. A
rewrite index that holds still is a finding — an entry stays scrubbed — and an
abstain that keeps abstaining for the same reason is a finding too. Neither may
read on the site as a collector that died.

The abstain guard is tested alongside the heartbeat, because the tempting way to
make a signal look alive is to let a round with nothing to say publish a number
anyway. A republished null is honest; a manufactured 0 is not.

Offline: BaikeRedactionWatch is replaced wholesale, so no fetch, no rate ceiling
sleep, and nothing but the writer runs.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.baike_redaction_pull as pull

ROOT = Path(__file__).resolve().parents[1]


def _observation(*, comparable: bool, forked: bool) -> dict:
    """One entity's read, in the shape main() destructures.

    A non-comparable entity is modelled as a Baike fetch failure, which is what
    the live runs actually hit: the source refuses the client, so there is no
    state-side text to diff the open record against.
    """
    if not comparable:
        return {
            "status": "partial",
            "baike": {"present": False, "interstitial": "fetch_failed"},
            "wiki": {"present": True},
            "divergences": [],
        }
    fork = types.SimpleNamespace(
        kind=pull.ENCYCLOPEDIA_FORK, detail="wiki_only_sensitive=3"
    )
    return {
        "status": "ok",
        "baike": {"present": True, "interstitial": ""},
        "wiki": {"present": True},
        "divergences": [fork] if forked else [],
    }


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the encyclopedia reads stubbed."""
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "baike-redaction-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "baike-redaction-history.jsonl"))

    def run(comparable=10, forked=9):
        rows = [
            _observation(comparable=i < comparable, forked=i < forked)
            for i in range(len(pull.ENTITIES))
        ]

        class _Watch:
            def __init__(self, **kw):
                self._rows = iter(rows)

            def observe(self, entity):
                return next(self._rows)

        monkeypatch.setattr(pull, "BaikeRedactionWatch", _Watch)
        pull._publish_collected(datetime.now(timezone.utc), _Watch())
        path = tmp_path / "baike-redaction-latest.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "baike-redaction-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: an entry that stays scrubbed never moved the rewrite
    index, so the file stopped being rewritten and the site called a working
    signal stale."""
    run, _ = publish
    first = run()
    second = run()

    assert first["rewrite_index"] == 90.0
    assert second["rewrite_index"] == first["rewrite_index"]
    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time"
    )


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, _ = publish
    first = run()
    second = run()

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the movement record. Heartbeats belong in the reading, not
    here, or the record of what changed fills up with what did not."""
    run, tmp_path = publish
    run()
    run()
    run()

    assert len(_history(tmp_path)) == 1


def test_a_moved_answer_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(comparable=10, forked=9)
    moved = run(comparable=10, forked=5)

    assert moved["rewrite_index"] == 50.0
    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the
    previous generated_at is the honest answer for when it last moved."""
    run, tmp_path = publish
    first = run()

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "baike-redaction-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run()
    assert second["last_changed_at"] == first["generated_at"]


def test_a_repeated_abstain_is_republished_and_stays_an_abstain(publish):
    """The abstain is this collector's reading when too few entities are
    comparable, and it goes stale the same way a number does. Refreshing it must
    not turn the null into a value: a heartbeat republishes the abstain, it does
    not resolve it."""
    run, tmp_path = publish
    first = run(comparable=0, forked=0)
    second = run(comparable=0, forked=0)

    assert first["status"] == "insufficient_data"
    assert second["status"] == "insufficient_data"
    assert second["rewrite_index"] is None
    assert second["n_comparable"] == 0
    assert second["generated_at"] > first["generated_at"]
    assert second["last_changed_at"] == first["last_changed_at"]
    assert len(_history(tmp_path)) == 1


def test_too_few_comparable_entities_still_abstains(publish):
    """The threshold is the honesty guard: an index over one or two entities is
    not a measurement of the panel. Nothing about the heartbeat may lower it."""
    run, _ = publish
    out = run(comparable=pull.MIN_COMPARABLE - 1, forked=1)

    assert out["status"] == "insufficient_data"
    assert out["rewrite_index"] is None


def test_disabled_round_does_not_stamp_a_fresh_observation_time(publish, monkeypatch):
    """A disabled round never opened a socket, so it
    has no observation time to offer. The heartbeat is a claim about looking, and
    this round did not look."""
    run, tmp_path = publish
    first = run(comparable=0, forked=0)

    pull.main()

    with open(tmp_path / "baike-redaction-latest.json", encoding="utf-8") as f:
        after = json.load(f)
    assert after["generated_at"] == first["generated_at"]
    assert after["pipeline_checked_at"] > first["pipeline_checked_at"]
    assert after["collector_status"] == "disabled_no_authorized_access"
    assert after["collector_reason"] == pull.DISABLED_REASON
    assert after["status"] == "disabled"
    assert after["observation_status"] == first["status"]
    assert after["valid_for_series"] is False
    assert after["quarantined_history"] == pull.QUARANTINED_HISTORY
    assert len(_history(tmp_path)) == 1


def test_all_error_fixture_run_does_not_stamp_a_fresh_observation_time(
    publish, monkeypatch
):
    """An all-error run has no observation evidence and must leave the last reading intact."""
    run, tmp_path = publish
    first = run(comparable=0, forked=0)

    class _AllErrorWatch:
        def observe(self, entity):
            raise OSError("fixture transport failure")

    pull._publish_collected(datetime.now(timezone.utc), _AllErrorWatch())
    with open(tmp_path / "baike-redaction-latest.json", encoding="utf-8") as f:
        after = json.load(f)
    assert after["generated_at"] == first["generated_at"]
    assert after["pipeline_checked_at"] > first["pipeline_checked_at"]
    assert after["collector_status"] == "error_no_observation"
    assert "error:OSError" in after["collector_reason"]
    assert len(_history(tmp_path)) == 1


def test_erasure_workflow_contains_no_baike_writer_or_activation_knob():
    workflow = (ROOT / ".github" / "workflows" / "erasure-refresh.yml").read_text(
        encoding="utf-8"
    )
    assert "PALIMPSEST_PROXY" not in workflow
    assert "PALIMPSEST_LIVE" not in workflow
    assert "scripts.baike_redaction_pull" not in workflow
    assert "scripts.baike_public_snapshot_pull" not in workflow


def test_deployment_docs_and_container_do_not_offer_a_baike_proxy_path():
    security = (ROOT / "SECURITY-HARDENING.md").read_text(encoding="utf-8")
    docker_readme = (ROOT / "ops" / "docker" / "README.md").read_text(encoding="utf-8")
    compose = (ROOT / "ops" / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    sources = (ROOT / "docs" / "OSINT_SOURCES.md").read_text(encoding="utf-8")

    assert "injected by `erasure-refresh.yml`" not in security
    assert "cannot activate Baike collection" in security
    assert "does not forward `PALIMPSEST_PROXY`" in docker_readme
    assert "PALIMPSEST_PROXY" not in compose
    assert (
        "No environment variable or proxy argument enables Baike acquisition"
        in " ".join(sources.split())
    )


def test_public_copy_names_disabled_state_and_quarantine():
    researchers = (ROOT / "for-researchers.html").read_text(encoding="utf-8")
    index = (ROOT / "readings" / "index.html").read_text(encoding="utf-8")
    assert "Baike disabled and retained as stale" in researchers
    assert "invalid 90.0 method-v1 point is quarantined" in index
    assert "Every self-refreshing feed" not in index


def test_checked_in_method_v1_numeric_point_is_retained_but_quarantined():
    rows = [
        json.loads(line)
        for line in (ROOT / "readings" / "baike-redaction-history.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    bad = next(row for row in rows if row.get("rewrite_index") == 90.0)
    assert bad["generated_at"] == "2026-07-30T20:25:46.041900+00:00"
    assert bad["status"] == "ok", (
        "the original append-only evidence row stays unchanged"
    )
    erratum = next(
        row for row in rows if row.get("event") == "historical_validation_erratum"
    )
    assert erratum["invalidates_generated_at"] == bad["generated_at"]
    assert erratum["excluded_rewrite_index"] == 90.0
    assert erratum["valid_for_series"] is False
    assert erratum["validation_status"] == "quarantined"
    assert "generic HTTP 404" in erratum["exclusion_reason"]
    assert not any(row.get("valid_for_series") is True for row in rows)


def test_new_offline_fixture_history_declares_series_validity(publish):
    run, tmp_path = publish
    run(comparable=10, forked=5)
    row = _history(tmp_path)[0]
    assert row["method_version"] == pull.METHOD_VERSION
    assert row["valid_for_series"] is True
