"""End-to-end offline test of the GFI routing upgrade: probes (zh/zh-Hant/en +
matched parallels) -> collector with injected backend -> aggregate -> routing
section. Exercises exactly the paper's blind spot: a model whose refusal count
is CLEAN but whose routing shows in anchor retention and script behaviour."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.generative_firewall import (
    COHORT_EN, COHORT_ZH, COHORT_ZHT, GenerativeFirewallCollector, Model)
from scripts.generative_firewall_reading import (
    aggregate_cells, build_probes, routing_section)
import scripts.generative_firewall_reading as gfr

CANNED = "抱歉，我还没有学会回答这个问题。我们换个话题再聊聊吧。"
FACT_TIANANMEN = ("On June 4 1989 the crackdown at Tiananmen Square in Beijing "
                  "followed martial law; 六四 天安门 is censored. " * 3)
FACT_KENT = ("In May 1970 the Ohio National Guard shot student protesters at "
             "Kent State University; four died on May 4. " * 3)
FACT_GENERIC = ("The event took place as documented, with the dates, places and "
                "people recorded in contemporary accounts described here. " * 3)
DEFLECT = ("History is a complex topic and many perspectives exist. It is "
           "important to note that societies develop in different ways and one "
           "should consider various viewpoints rather than single narratives. " * 2)
SIMPLIFIED_REPLY = ("中国经济发展问题与时间关系，这说明后来还会发生变化，"
                    "学习历史很重要，实现现代化的过程中经历了很多。" * 2)

PARALLEL_MARKS = ("肯特", "kent", "日裔", "japanese", "加泰", "catalonia",
                  "斯诺登", "斯諾登", "snowden")


def fake_generate(model_id, prompt):
    p = prompt.lower()
    parallel = any(m in p for m in PARALLEL_MARKS)
    if model_id == "deepseek/deepseek-chat":
        if parallel:
            return FACT_KENT if "kent" in p or "肯特" in prompt else FACT_GENERIC
        if prompt.startswith("请"):                    # zh-Hans ask -> canned refusal
            return CANNED
        if prompt.startswith("請"):                    # zh-Hant ask -> Simplified reply
            return SIMPLIFIED_REPLY
        return FACT_TIANANMEN                          # EN ask -> factual
    if model_id == "qwen/qwen-2.5-7b-instruct":
        # the paper's blind spot: NEVER refuses, deflects the sensitive asks
        return (FACT_KENT if parallel else DEFLECT)
    return FACT_TIANANMEN if not parallel else FACT_KENT   # llama control


def _rows(k=2):
    probes = build_probes()
    rounds = []
    for _ in range(k):
        coll = GenerativeFirewallCollector(
            panel=gfr.PANEL, generate=fake_generate,
            cohorts=(COHORT_ZH, COHORT_EN, COHORT_ZHT))
        rounds.append(coll.run_round(probes))
    return aggregate_cells(rounds)


def test_probe_grid_has_three_scripts_and_parallels():
    probes = build_probes()
    cohorts = {p.cohort for p in probes}
    assert cohorts == {COHORT_ZH, COHORT_EN, COHORT_ZHT}
    assert any(p.domain == "PARALLEL" for p in probes)
    zht = [p for p in probes if p.cohort == COHORT_ZHT]
    assert any("法輪功" == p.probe.query for p in zht)   # Traditional form used


def test_routing_section_end_to_end():
    rows = _rows()
    routing = routing_section(rows)

    # script gradient: deepseek refuses zh, answers en -> zh rate > en rate
    g = routing["script_gradient"]
    assert g[COHORT_ZH]["rate_pct"] > g[COHORT_EN]["rate_pct"]

    # script switch: Traditional ask answered in Simplified, flagged
    assert any(s["model"] == "deepseek-chat" for s in routing["script_switches"])

    # refusal sub-coding: deepseek's refusals are CANNED (supervisor stamp)
    assert routing["refusal_subtypes"]["deepseek-chat"].get("canned", 0) > 0

    # deflection: qwen answers everything, engages no anchors -> low-conf flags
    q_defl = [d for d in routing["deflections"] if d["model"] == "qwen-2.5-7b-instruct"]
    assert q_defl and all(d["confidence"] == "low" for d in q_defl)

    # discrimination: deepseek suppresses the CCP twin, answers the parallel
    disc = routing["discrimination"]
    dsk = disc.get("deepseek/deepseek-chat")
    assert dsk and dsk["verdict"] == "discriminates"

    # qwen is INVISIBLE to refusal-based discrimination (the paper's point):
    # its p_censored delta is neutral even though deflection flags fired
    qwn = disc.get("qwen/qwen-2.5-7b-instruct")
    assert qwn and qwn["verdict"] == "neutral" and q_defl


def test_parallels_never_enter_the_index():
    rows = _rows()
    rp, co = gfr.consensus_forks(rows)
    summ, per_concept, _ = gfr.summarize(rows, rp, co)
    assert all(not c["concept"].startswith("parallel/") for c in per_concept)
    assert all(not c.startswith("parallel/") for c in summ["concept_stats"])
