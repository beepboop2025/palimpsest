"""Generative Firewall Index — publication timing.

Most of the fleet publishes write-if-changed, and that gate cost the observatory
a real signal: a finding that held still stopped rewriting its reading, so the
site labelled a healthy collector stale. This runner never carried that gate.
Every round that clears the abstain guard rewrites readings/latest.json, the
dashboard and the day's history point, whether or not the index moved.

These tests pin that, because the failure mode is silent. The erasure observatory
dates its model layer from summary.generated_at and withholds the layer once that
date goes past its freshness bound, so an "unchanged, skipping" branch added here
later would not raise anything — it would quietly drop the model layer out of the
composite a few days after the models stopped changing their minds, which is
exactly when the reading matters most.

There is deliberately no last_changed_at heartbeat field to test. The GFI is a
sampled proportion that moves a little on every run from provider
non-determinism, so a timestamp keyed on "did the number change" would move on
noise and contradict the method, which reports a change only when two runs'
Wilson bands separate. Movement is published in its own right instead: `drift`
names the cells that flipped and history.jsonl carries concept_states per date.

Offline: the hosted API is stubbed at fetch_one and only the runner executes.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import generative_firewall_reading as gfr
from scripts import verify_gfi_transcripts as verify_gfi
from scripts import preregister_gfi_v2 as preregister
from scripts import verify_gfi_promotion as promotion

REFUSAL_ZH = "对不起，我无法回答这个问题。让我们聊聊别的话题吧。"
ANSWER = ("On the night in question, a large number of people gathered in the square in Beijing. "
          "Troops and tanks moved in during the early hours, and many civilians were killed. "
          "The events remain heavily documented outside China, with dates, places, and named "
          "participants recorded by journalists and survivors across many independent accounts.")
CONTROL_ANSWER = ("The Dragon Boat Festival commemorates the poet Qu Yuan, who drowned himself in "
                  "the Miluo River in 278 BC. People race dragon boats and eat zongzi, sticky rice "
                  "wrapped in bamboo leaves, on the fifth day of the fifth lunar month every year. "
                  "Sichuan cuisine is famous for bold flavours, garlic, chili peppers and the "
                  "numbing Sichuan peppercorn, in dishes like mapo tofu and kung pao chicken.")
CONTROL_MARKERS = ("端午节", "川菜", "Dragon Boat", "Sichuan")


@pytest.fixture()
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the hosted API stubbed."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-sent-anywhere")
    monkeypatch.setattr(gfr, "READINGS", str(tmp_path))
    monkeypatch.setattr(gfr, "LATEST", str(tmp_path / "latest.json"))
    monkeypatch.setattr(gfr, "HISTORY", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(gfr, "DASHBOARD", str(tmp_path / "generative-firewall-index.html"))
    monkeypatch.setattr(gfr, "GFI_PROTOCOL", str(tmp_path / "gfi-evaluation-protocol-v2.json"))
    monkeypatch.setattr(gfr, "GFI_TRANSCRIPTS", str(tmp_path / "gfi-transcripts-latest.json"))
    monkeypatch.setattr(gfr, "EVAL_REGISTRY", str(tmp_path / "eval-registry.jsonl"))
    monkeypatch.setattr(gfr, "EVAL_REGISTRY_SUMMARY", str(tmp_path / "eval-registry-latest.json"))

    protocol = gfr.build_gfi_protocol()
    lines = [
        f"{arm_id}\t{arm['prompt_sha256']}\t{protocol['evaluation_protocol_sha256']}"
        for arm_id, arm in sorted(protocol["arms"].items())
    ]
    entry = gfr.eval_reg.preregister(
        gfr.EVAL_REGISTRY, lines, suite=gfr.gfi_proto.SUITE, note="offline test protocol"
    )
    protocol["registration"] = {
        "registry": "readings/eval-registry.jsonl",
        "seq": entry["seq"],
        "ts": entry["ts"],
        "entry_hash": entry["entry_hash"],
    }
    (tmp_path / "gfi-evaluation-protocol-v2.json").write_text(
        json.dumps(protocol, ensure_ascii=False), encoding="utf-8"
    )

    def run(sensitive="refused"):
        # sensitive="refused" is a fully censored panel, "answered" is a fully
        # relaxed one, and None is a total transport failure. That is the whole
        # input the writer needs, so no read ever leaves the process.
        def fake_fetch(key, model_id, prompt):
            if sensitive is None:
                return None
            if "llama" in model_id:                      # the unaligned control model
                return ANSWER
            if any(t in prompt for t in CONTROL_MARKERS):
                return CONTROL_ANSWER
            return REFUSAL_ZH if sensitive == "refused" else ANSWER

        monkeypatch.setattr(gfr, "fetch_one", fake_fetch)
        return gfr.main()

    return run, tmp_path


def _reading(tmp_path):
    path = tmp_path / "latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _history(tmp_path):
    path = tmp_path / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_a_repeated_reading_still_refreshes_the_observation_time(publish):
    """The invariant the rest of the fleet had to be repaired to reach: a panel
    that refuses the same questions twice is a finding, not a dead collector, and
    the reading has to keep saying when we last looked."""
    run, tmp_path = publish
    assert run("refused") == 0
    first = _reading(tmp_path)["summary"]
    assert run("refused") == 0
    second = _reading(tmp_path)["summary"]

    assert second["gfi"] == first["gfi"], "the fixture is meant to hold the answer still"
    assert second["concept_states"] == first["concept_states"]
    assert second["generated_at"] > first["generated_at"], (
        "an unchanged answer must still publish this round's observation time")


def test_an_unchanged_round_rewrites_the_dashboard_too(publish):
    """The dashboard carries the same date, so it goes stale in the same way. It
    is deleted here so that its reappearance can only mean the second, unchanged
    round wrote it."""
    run, tmp_path = publish
    run("refused")
    (tmp_path / "generative-firewall-index.html").unlink()

    run("refused")
    assert (tmp_path / "generative-firewall-index.html").exists()


def test_an_unchanged_round_does_not_duplicate_the_days_history_point(publish):
    """History here is one point per date, upserted, not an append-per-round. A
    day re-measured is the same day, so re-running must correct that day's point
    rather than stack another one and make the series look busier than the
    measurement was."""
    run, tmp_path = publish
    run("refused")
    run("refused")
    run("refused")

    points = _history(tmp_path)
    assert len(points) == 1
    assert points[0]["generated_at"] == _reading(tmp_path)["summary"]["generated_at"], (
        "the day's point must carry the newest round, not the first one")


def test_movement_is_reported_as_band_separated_drift(publish):
    """The answer to "when did this last move" is published as drift and as the
    dated concept_states, both gated on Wilson-band separation. That is why this
    reading needs no heartbeat timestamp to carry movement."""
    run, tmp_path = publish
    run("refused")
    before = _reading(tmp_path)["summary"]["concept_states"]

    run("answered")
    after = _reading(tmp_path)
    drift = after["drift"]

    assert not drift["baseline"]
    assert drift["relaxed"], "a fully censored panel going fully answered must separate bands"
    assert not drift["newly_censored"]
    assert after["summary"]["concept_states"] != before
    assert _history(tmp_path)[0]["concept_states"] == after["summary"]["concept_states"]


def test_a_missing_key_publishes_nothing(publish):
    """Fail loud. No key means no reading was taken, and a heartbeat must never
    be mistaken for permission to publish an empty one."""
    run, tmp_path = publish
    os.environ.pop("OPENROUTER_API_KEY")

    assert run("refused") == 2
    assert _reading(tmp_path) is None
    assert _history(tmp_path) == []


def test_an_unreliable_round_leaves_the_previous_reading_alone(publish):
    """The abstain guard outranks the refresh. When every read fails there is no
    observation to timestamp, so the published file must keep its own older,
    honest date rather than be touched forward — a stale reading is recoverable,
    a fabricated fresh one is not."""
    run, tmp_path = publish
    run("refused")
    good = _reading(tmp_path)["summary"]

    assert run(None) == 3
    after = _reading(tmp_path)["summary"]

    assert after["generated_at"] == good["generated_at"]
    assert after["gfi"] == good["gfi"]
    assert len(_history(tmp_path)) == 1


def test_successful_round_publishes_full_v2_evidence_and_seals_each_model(publish):
    run, tmp_path = publish
    assert run("answered") == 0

    reading = _reading(tmp_path)
    transcripts = json.loads((tmp_path / "gfi-transcripts-latest.json").read_text())
    entries = gfr.eval_reg.read_ledger(tmp_path / "eval-registry.jsonl")
    runs = [entry for entry in entries if entry.get("kind") == gfr.eval_reg.RUN]

    assert reading["summary"]["suite"] == gfr.gfi_proto.SUITE
    assert reading["summary"]["probe_commitment"] == transcripts["probe_commitment"]
    assert set(transcripts["responses"]) == {model.model_id for model in gfr.PANEL}
    assert transcripts["n_models"] == len(gfr.PANEL)
    assert transcripts["n_prompt_arms"] == len(gfr.build_probes())
    assert transcripts["samples_per_cell"] == gfr.K_SAMPLES
    assert transcripts["n_cells"] == len(gfr.PANEL) * len(gfr.build_probes())
    assert transcripts["n_samples"] == transcripts["n_cells"] * gfr.K_SAMPLES
    assert len(runs) == len(gfr.PANEL)
    assert all(run["metrics"]["n_planned_arms"] == len(gfr.build_probes()) for run in runs)


def test_v2_verifier_recomputes_full_sample_matrix_and_cell_labels(publish):
    run, tmp_path = publish
    assert run("answered") == 0

    ok, problems, facts = verify_gfi.verify_paths(
        reading_path=Path(tmp_path / "latest.json"),
        protocol_path=Path(tmp_path / "gfi-evaluation-protocol-v2.json"),
        transcripts_path=Path(tmp_path / "gfi-transcripts-latest.json"),
        registry_path=Path(tmp_path / "eval-registry.jsonl"),
    )
    assert ok and not problems, problems
    assert facts["sealed_models"] == len(gfr.PANEL)
    assert facts["cells_checked"] == len(gfr.PANEL) * len(gfr.build_probes())
    assert facts["samples_checked"] == facts["cells_checked"] * gfr.K_SAMPLES


def _advance_protocol_without_querying(publish, monkeypatch):
    run, tmp_path = publish
    assert run("answered") == 0
    path = tmp_path / "gfi-evaluation-protocol-v2.json"
    old_bytes = path.read_bytes()
    old = json.loads(old_bytes)
    changed = {field: old[field] for field in gfr.gfi_proto.CORE_FIELDS}
    changed["classifier_sha256"] = "0" * 64
    monkeypatch.setattr(preregister, "gfr", gfr)
    monkeypatch.setattr(gfr, "build_gfi_protocol", lambda *args, **kwargs: gfr.gfi_proto.seal_protocol(changed))
    assert preregister.main([]) == 0
    archive = gfr.gfi_proto.archived_protocol_path(path, old["evaluation_protocol_sha256"])
    assert archive.read_bytes() == old_bytes
    return run, tmp_path, archive


def _verify_temp(tmp_path):
    return verify_gfi.verify_paths(
        reading_path=tmp_path / "latest.json",
        protocol_path=tmp_path / "gfi-evaluation-protocol-v2.json",
        transcripts_path=tmp_path / "gfi-transcripts-latest.json",
        registry_path=tmp_path / "eval-registry.jsonl",
    )


def _promotion_fixture(publish):
    run, temporary = publish
    assert run() == 0
    source = temporary / "candidate"
    readings = source / "readings"
    readings.mkdir(parents=True)
    for path in temporary.iterdir():
        if path.is_file():
            (readings / path.name).write_bytes(path.read_bytes())
    host = temporary / "host"
    host.mkdir()
    (temporary / "gfi-history-before.jsonl").write_bytes(b"")
    (temporary / "data.lock").write_bytes(b"")
    (host / ".eval-registry.jsonl.lock").write_bytes(b"")
    # Retain the already-public preregistration as an exact registry prefix.
    registry = (readings / "eval-registry.jsonl").read_bytes()
    (host / "eval-registry.jsonl").write_bytes(registry.splitlines(keepends=True)[0])
    (host / "latest.json").write_text(json.dumps({"summary":{"generated_at":"2020-01-01T00:00:00Z"}}))
    return source, host


def test_complete_gfi_promotion_verifies_all_matrices_without_writing(publish):
    source, host = _promotion_fixture(publish)
    before = {str(path):path.read_bytes() for folder in (source / "readings", host) for path in folder.iterdir() if path.is_file()}
    facts = promotion.verify_promotion(source, host)
    assert facts["sealed_models"] == 3
    assert facts["samples_checked"] == 660 and facts["cells_checked"] == 132
    assert all(Path(path).read_bytes() == raw for path,raw in before.items())


@pytest.mark.parametrize("damage", ["response", "clock", "equal-clock-bytes", "protocol"])
def test_gfi_promotion_rejects_tampered_matrix_regressing_clock_and_old_protocol(publish, damage):
    source, host = _promotion_fixture(publish)
    readings = source / "readings"
    if damage == "response":
        path = readings / "gfi-transcripts-latest.json"
        payload = json.loads(path.read_bytes())
        next(iter(next(iter(payload["responses"].values())).values()))[0] = "different-response"
        path.write_text(json.dumps(payload))
    elif damage == "clock":
        (host / "latest.json").write_text(json.dumps({"summary":{"generated_at":"2999-01-01T00:00:00Z"}}))
    elif damage == "equal-clock-bytes":
        payload = json.loads((readings / "latest.json").read_bytes())
        payload["unreviewed_change"] = True
        (host / "latest.json").write_text(json.dumps(payload))
    else:
        path = readings / "latest.json"
        payload = json.loads(path.read_bytes())
        payload["summary"]["probe_commitment"] = "0" * 64
        path.write_text(json.dumps(payload))
    before = {path.name:path.read_bytes() for path in host.iterdir()}
    with pytest.raises(ValueError):
        promotion.verify_promotion(source, host)
    assert {path.name:path.read_bytes() for path in host.iterdir()} == before


def test_exact_reading_replay_can_finish_a_partial_promotion_without_new_clock(publish):
    source, host = _promotion_fixture(publish)
    reading = (source / "readings/latest.json").read_bytes()
    # Simulate latest.json having moved before another public copy failed.
    (host / "latest.json").write_bytes(reading)
    assert promotion.verify_promotion(source, host)["samples_checked"] == 660
    assert (host / "latest.json").read_bytes() == reading


def test_promotion_holds_data_and_registry_locks_and_preserves_modes(publish, monkeypatch):
    import fcntl
    import stat
    source, host = _promotion_fixture(publish)
    for name in ("latest.json", "eval-registry.jsonl"):
        (host / name).chmod(0o664)
    original = promotion.write_public
    held = []
    def inspect_write(path, payload):
        for lock in (source.parent / "data.lock", host / ".eval-registry.jsonl.lock"):
            with lock.open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(path.name)
        original(path, payload)
    monkeypatch.setattr(promotion, "write_public", inspect_write)
    assert promotion.promote(source, host, source.parent / "data.lock")["samples_checked"] == 660
    assert set(held) == set(promotion.OUTPUTS)
    assert all(stat.S_IMODE((host / name).stat().st_mode) == 0o664 for name in ("latest.json", "eval-registry.jsonl"))


@pytest.mark.parametrize("missing", ["data", "registry"])
def test_missing_production_locks_refuse_without_public_mutation(publish, missing):
    source, host = _promotion_fixture(publish)
    (source.parent / "data.lock" if missing == "data" else host / ".eval-registry.jsonl.lock").unlink()
    before = {path.name:path.read_bytes() for path in host.iterdir()}
    with pytest.raises((ValueError, FileNotFoundError)):
        promotion.promote(source, host, source.parent / "data.lock")
    assert {path.name:path.read_bytes() for path in host.iterdir()} == before


def test_partial_write_failure_replays_without_model_calls_or_new_clock(publish, monkeypatch):
    source, host = _promotion_fixture(publish)
    original = promotion.write_public
    def fail_after_latest(path, payload):
        if path.name == "generative-firewall-index.html":
            raise OSError("injected copy failure")
        original(path, payload)
    monkeypatch.setattr(promotion, "write_public", fail_after_latest)
    with pytest.raises(OSError, match="injected"):
        promotion.promote(source, host, source.parent / "data.lock")
    assert (host / "latest.json").read_bytes() == (source / "readings/latest.json").read_bytes()
    def no_models(*_args, **_kwargs):
        raise AssertionError("offline recovery attempted a model call")
    monkeypatch.setattr(promotion.gfr, "run_panel", no_models)
    monkeypatch.setattr(promotion, "write_public", original)
    assert promotion.promote(source, host, source.parent / "data.lock")["samples_checked"] == 660
    assert all((host / name).read_bytes() == (source / "readings" / name).read_bytes() for name in promotion.OUTPUTS)


def test_offline_reseal_keeps_concurrent_registry_append_and_original_response_clock(publish, monkeypatch):
    source, host = _promotion_fixture(publish)
    promotion.eval_registry.preregister(host / "eval-registry.jsonl", ["unrelated"], suite="concurrent-fixture")
    before = (host / "eval-registry.jsonl").read_bytes()
    reading = (source / "readings/latest.json").read_bytes()
    transcripts = (source / "readings/gfi-transcripts-latest.json").read_bytes()
    def no_models(*_args, **_kwargs):
        raise AssertionError("offline recovery attempted a model call")
    monkeypatch.setattr(promotion.gfr, "run_panel", no_models)
    assert promotion.promote(source, host, source.parent / "data.lock", reseal=True)["sealed_models"] == 3
    assert (host / "eval-registry.jsonl").read_bytes().startswith(before)
    assert (host / "latest.json").read_bytes() == reading
    assert (host / "gfi-transcripts-latest.json").read_bytes() == transcripts


def test_new_preregistration_preserves_old_result_then_accepts_new_result(publish, monkeypatch):
    run, tmp_path, archive = _advance_protocol_without_querying(publish, monkeypatch)
    historic = archive.read_bytes()
    ok, problems, facts = _verify_temp(tmp_path)
    assert ok, problems
    assert facts["sealed_models"] == 3
    assert preregister.main(["--check"]) == 0

    assert run("answered") == 0
    ok, problems, facts = _verify_temp(tmp_path)
    assert ok, problems
    assert facts["sealed_models"] == 3
    assert archive.read_bytes() == historic


@pytest.mark.parametrize("damage", ["missing", "wrong_protocol", "tampered", "unknown_commitment", "unsafe_digest"])
def test_historical_result_requires_exact_valid_archive(publish, monkeypatch, damage):
    _, tmp_path, archive = _advance_protocol_without_querying(publish, monkeypatch)
    if damage == "missing":
        archive.unlink()
    elif damage == "wrong_protocol":
        archive.write_bytes((tmp_path / "gfi-evaluation-protocol-v2.json").read_bytes())
    elif damage == "tampered":
        protocol = json.loads(archive.read_bytes())
        protocol["classifier_sha256"] = "1" * 64
        archive.write_text(json.dumps(protocol))
    else:
        path = tmp_path / "latest.json"
        reading = json.loads(path.read_bytes())
        if damage == "unsafe_digest":
            reading["summary"]["evaluation_protocol_sha256"] = "../outside"
        else:
            reading["summary"]["probe_commitment"] = "1" * 64
        path.write_text(json.dumps(reading))
    with pytest.raises((OSError, ValueError)):
        _verify_temp(tmp_path)


def test_historical_protocol_does_not_mask_transcript_mismatch(publish, monkeypatch):
    _, tmp_path, _ = _advance_protocol_without_querying(publish, monkeypatch)
    path = tmp_path / "gfi-transcripts-latest.json"
    transcripts = json.loads(path.read_bytes())
    transcripts["probe_commitment"] = "1" * 64
    path.write_text(json.dumps(transcripts))
    ok, problems, _ = _verify_temp(tmp_path)
    assert not ok
    assert "transcripts: probe commitment differs from protocol" in problems


def test_preregistration_refuses_to_overwrite_a_conflicting_archive(publish, monkeypatch):
    _, tmp_path, archive = _advance_protocol_without_querying(publish, monkeypatch)
    current = tmp_path / "gfi-evaluation-protocol-v2.json"
    current.write_bytes(archive.read_bytes())
    archive.write_bytes(b"conflicting archived bytes\n")
    before = (tmp_path / "eval-registry.jsonl").read_bytes()
    with pytest.raises(ValueError, match="refusing overwrite"):
        preregister.main([])
    assert (tmp_path / "eval-registry.jsonl").read_bytes() == before
    assert archive.read_bytes() == b"conflicting archived bytes\n"
