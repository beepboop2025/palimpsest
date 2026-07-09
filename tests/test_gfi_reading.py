"""k-sampling + Wilson-band behaviour of the GFI reading runner (scripts/, offline).

Covers the two methodological guarantees added after Jennifer Pan's review (2026-07-07):
  1. cells are scored as censored PROPORTIONS with a Wilson band, so hosted-API
     non-determinism cannot flip a single-sample label into a fake reading;
  2. a drift event fires ONLY on a category flip whose bands do not overlap — a flip
     compatible with sampling noise is not an event.
Plus the transport-failure contract: an unreachable backend ABSTAINS and is excluded
from the denominator, never classified as a refusal (no false zeros).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import generative_firewall_reading as gfr

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


@pytest.fixture()
def pipeline(monkeypatch):
    """Run the full offline pipeline against canned responses with controlled variation."""
    calls = {}

    def fake_fetch(key, model_id, prompt):
        i = calls[(model_id, prompt)] = calls.get((model_id, prompt), -1) + 1
        if "llama" in model_id:
            return ANSWER
        if any(t in prompt for t in ("端午节", "川菜", "Dragon Boat", "Sichuan")):
            return CONTROL_ANSWER
        # the Pan scenario: a borderline cell — 2 of 5 samples answer, 3 refuse
        if "deepseek" in model_id and "彭帅" in prompt:
            return ANSWER if i in (1, 3) else REFUSAL_ZH
        # a flaky-transport cell: two timeouts (None) then refusals
        if "qwen" in model_id and "润 移民" in prompt:
            return None if i < 2 else REFUSAL_ZH
        return REFUSAL_ZH if "请客观" in prompt else ANSWER

    monkeypatch.setattr(gfr, "fetch_one", fake_fetch)
    rounds = gfr.run_panel("dummy-key", gfr.build_probes(), k=5)
    rows = gfr.aggregate_cells(rounds)
    rp, co = gfr.consensus_forks(rows)
    summ, per_concept, forks = gfr.summarize(rows, rp, co)
    return rows, summ, per_concept

def _cell(rows, concept_frag, model_frag):
    return next(r for r in rows if concept_frag in r["concept"] and model_frag in r["model_id"]
                and r["cohort"] == gfr.COHORT_ZH)


def test_borderline_cell_scored_as_proportion(pipeline):
    rows, _, _ = pipeline
    cell = _cell(rows, "彭帅", "deepseek")
    assert cell["p_censored"] == 0.6                       # 3/5, not a coin-flip binary label
    assert cell["ci_lo"] < 0.3 and cell["ci_hi"] > 0.85    # k=5 bands are honest about width
    assert cell["label"] == "refused"                      # majority label for display


def test_transport_failures_abstain_not_refuse(pipeline):
    rows, _, _ = pipeline
    cell = _cell(rows, "润", "qwen")
    assert cell["total_samples"] == 5 and cell["valid_samples"] == 3
    assert cell["p_censored"] == 1.0                       # proportion over VALID samples only


def test_index_carries_wilson_band(pipeline):
    _, summ, _ = pipeline
    assert summ["gfi"] is not None
    assert summ["gfi_lo"] < summ["gfi"] < summ["gfi_hi"]
    assert summ["methodology"] == f"k{gfr.K_SAMPLES}-wilson95"
    assert summ["controls_clean"]


def test_drift_rebaselines_across_methodology_change(pipeline):
    _, summ, _ = pipeline
    old = {"date": "2026-07-06", "gfi": 40.0,
           "concept_states": {"politics/六四事件": {"deepseek/deepseek-chat": "answered"}}}
    d = gfr.compute_drift(old, summ)
    assert d["baseline"] and "rebaselined" in d


def test_drift_gated_on_band_separation(pipeline):
    _, summ, _ = pipeline
    prev = json.loads(json.dumps(summ["concept_stats"]))
    lo0, hi0 = gfr.wilson(0, 5)
    clean = {"label": "answered", "p": 0.0, "lo": lo0, "hi": hi0, "n": 5}
    peng = next(c for c in prev if "彭帅" in c)            # now 3/5: bands overlap -> no event
    tank = next(c for c in prev if "坦克人" in c)          # now 5/5: bands separate -> event
    prev[peng]["deepseek/deepseek-chat"] = dict(clean)
    prev[tank]["deepseek/deepseek-chat"] = dict(clean)
    d = gfr.compute_drift({"concept_stats": prev}, summ)
    fired = {(x["concept"], x["model"]) for x in d["newly_censored"]}
    assert (tank, "deepseek-chat") in fired
    assert (peng, "deepseek-chat") not in fired
    assert not d["relaxed"]


def test_identical_runs_produce_no_events(pipeline):
    _, summ, _ = pipeline
    d = gfr.compute_drift({"concept_stats": summ["concept_stats"]}, summ)
    assert not d["newly_censored"] and not d["relaxed"]


def test_dashboard_and_dataset_render(pipeline):
    rows, summ, per_concept = pipeline
    hist = [{"date": "2026-07-05", "gfi": 38.0},
            {"date": "2026-07-06", "gfi": 41.0},
            {"date": "2026-07-07", "gfi": summ["gfi"],
             "gfi_lo": summ["gfi_lo"], "gfi_hi": summ["gfi_hi"]}]
    assert "<polygon" in gfr.sparkline(hist)               # band envelope drawn
    drift = {"newly_censored": [], "relaxed": [], "baseline": False}
    page = gfr.build_dashboard(summ, per_concept, rows, drift, hist)
    assert "95% band" in page and "3/5" in page
    dataset = [{k: v for k, v in r.items() if k != "_rep_obs"} for r in rows]
    json.dumps({"summary": summ, "dataset": dataset}, ensure_ascii=False)


def test_wilson_sane_at_edges():
    lo, hi = gfr.wilson(0, 5)
    assert lo == 0.0 and 0.4 < hi < 0.5
    lo, hi = gfr.wilson(5, 5)
    assert 0.5 < lo < 0.6 and hi == 1.0
    assert gfr.wilson(0, 0) == (None, None)                # no valid samples -> no interval
