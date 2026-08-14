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
