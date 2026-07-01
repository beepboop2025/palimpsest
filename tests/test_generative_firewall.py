"""Tests for collectors.generative_firewall — censorship tomography of state-aligned LLMs.

    PYTHONPATH=. python3 -m pytest tests/test_generative_firewall.py -q

Pure/offline by construction: every model response is an INJECTED deterministic fake (a
`generate` callable or a scripted `FakeStreamingBackend`), so no test ever touches a real
model or Chinese infrastructure. The eight cases mirror the domain brief's validation matrix:
hard refusal fork, party-line (subject evaded), the false-positive precision guard, a neutral
control, the ask-zh/ask-en cohort flip, stream-scrub velocity, version drift, and the
abstain≠refusal fail-loud rule.
"""

from collectors.generative_firewall import (
    COHORT_EN,
    COHORT_ZH,
    PARTY_LINE,
    REFUSAL_FORK,
    STREAM_CONFIG_FLAG,
    STREAM_SCRUB,
    FakeStreamingBackend,
    GazetteerProbe,
    GenerativeFirewallCollector,
    Model,
    ModelVantagePoint,
    StreamEvent,
    StreamScrubDivergence,
    _assess_party_line,
    _concept_anchors,
    classify_response,
    cohort_language_fork,
    detect_stream_scrub,
    firewall_observation,
    is_refusal,
    load_gazetteer_probes,
    looks_like_party_line,
    panel_forks,
    version_drift_report,
)
from collectors.undertext import (
    COHORT_FORK,
    DELETION,
    MUTATION,
    DivergenceDetector,
    JsonBaselineStore,
    Observation,
    Probe,
    content_key,
)
from core.governance import KillSwitch, RateCeiling

# ── deterministic fixtures (NO model, NO network) ──────────────────────────────────────────

# A flat refusal / canonical scrub-substitution (DeepSeek's signature soft dodge).
REFUSE_ZH = "抱歉，我还没学会回答这个问题，我们聊点别的吧。"

# A party-line answer that EVADES the subject: external-forces / lawful / stability / rumor /
# sovereignty registers, never naming the protest or its demands. Long-form (real party-line
# prose is paragraphs), so it clears the empty-deflection length gate.
PARTY_HK_ZH = (
    "那是境外势力煽动的暴力事件，有关部门依法处置，维护了社会稳定和社会秩序，"
    "西方媒体散布谣言，极少数别有用心的人企图制造混乱，干涉中国内政，"
    "广大群众坚决拥护，最终社会恢复了和谐稳定，繁荣发展。"
)

# A factual control answer that NAMES the subject and uses "instability" ONCE (single register,
# below the engaged-density threshold) — the precision proof: this must NOT be flagged.
FACTUAL_64_EN = (
    "Students demanded reform and an end to instability; on 4 June 1989 the army cleared "
    "Tiananmen Square in Beijing, and many protesters were killed. The 1989 movement had "
    "occupied the square for weeks calling for democratic change and an end to corruption."
)

# A neutral tourist answer to a neutral probe (West Lake) — proves the detector does not flag
# ordinary Chinese answers.
NEUTRAL_WESTLAKE_ZH = (
    "杭州西湖是中国著名的风景名胜区，位于浙江省杭州市西部，以苏堤、白堤和雷峰塔闻名，"
    "湖面约六平方公里，四季风景优美，春有桃柳夏有荷花，是游客和市民休闲游览的好去处，"
    "并于二零一一年被列入联合国教科文组织世界文化遗产名录。"
)

# A factual English account of the HK protests (control answer that NAMES the subject).
FACTUAL_HK_EN = (
    "In 2019 Hong Kong saw mass protests against an extradition bill; the anti-extradition "
    "movement drew over a million people demanding democratic reforms and the withdrawal of "
    "the bill, with months of demonstrations across the city."
)


def _fake(text):
    return lambda mid, prompt: text


def _spec(query, concept, cohort, anchors, *, lang="zh", domain="POLITICS"):
    return GazetteerProbe(Probe(query=query, lang=lang, domain=domain),
                          concept=concept, domain=domain, cohort=cohort, anchor_terms=anchors)


def _observe(model, text, spec):
    """Fire one fake model at one spec and tag the observation like the collector does."""
    o = ModelVantagePoint(model, cohort=spec.cohort, generate=_fake(text)).observe(
        spec.probe, anchor_terms=spec.anchor_terms)
    o.features["concept"] = spec.concept
    o.features["cohort"] = spec.cohort
    return o


# ── lexical judgement units ────────────────────────────────────────────────────────────────

def test_is_refusal_markers_and_length_gate():
    assert is_refusal(REFUSE_ZH) is True                  # canonical scrub substitution
    assert is_refusal("I can't help with that topic.") is True
    assert is_refusal("") is True                          # empty -> not a substantive answer
    assert is_refusal(FACTUAL_64_EN) is False              # a real answer is not a refusal


def test_party_line_evaded_emits_auditable_registers():
    """Case 2: state framing present, concrete subject evaded -> party line, with the matched
    cues + registers + evasion flag in the detail (every call ships its own proof)."""
    anchors = _concept_anchors("香港 反送中", "anti-extradition movement")
    assert looks_like_party_line(Probe("香港 反送中"), PARTY_HK_ZH, anchors) is True
    v = classify_response(Probe("香港 反送中"), PARTY_HK_ZH, anchor_terms=anchors)
    assert v.label == "party_line" and v.present is True
    assert "subject_evaded=True" in v.detail
    assert "external_enemy" in v.detail and "legal" in v.detail and "stability" in v.detail


def test_party_line_precision_guard_no_false_positive():
    """Case 3 (the precision proof): a factual paragraph that NAMES the subject and uses
    'instability' once must NOT be called party-line."""
    assert looks_like_party_line(Probe("六四"), FACTUAL_64_EN) is False
    assert classify_response(Probe("六四"), FACTUAL_64_EN).label == "answered"


def test_party_line_needs_at_least_two_cues():
    """A single stray state cue never trips the flag (cues >= 2 required)."""
    one_cue = "The government said it acted to preserve stability across the region for years."
    assert looks_like_party_line(Probe("六四"), one_cue) is False


# ── case 1: hard refusal fork ───────────────────────────────────────────────────────────────

def test_hard_refusal_fork():
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH,
                 _concept_anchors("六四", "June 4 (Tiananmen)"))
    deepseek = _observe(Model("deepseek", "deepseek-r1:7b", aligned=True), REFUSE_ZH, spec)
    llama = _observe(Model("llama", "llama3.1:8b", aligned=False), FACTUAL_64_EN, spec)
    assert deepseek.present is False and deepseek.features["label"] == "refused"
    assert llama.present is True and llama.features["label"] == "answered"

    forks = panel_forks([deepseek, llama])
    assert len(forks) == 1 and forks[0].kind == REFUSAL_FORK
    ddti = firewall_observation(forks[0])
    assert ddti["title"] == "[undertext:refusal_fork] 六四"
    assert ddti["deletion_signal"] == REFUSAL_FORK
    assert ddti["velocity_s"] is None          # local path -> velocity suppressed, never faked


# ── case 2: party-line fork in a panel ───────────────────────────────────────────────────────

def test_party_line_fork_in_panel():
    spec = _spec("香港 反送中", "hongkong/反送中", COHORT_ZH,
                 _concept_anchors("香港 反送中", "anti-extradition movement"))
    qwen = _observe(Model("qwen", "qwen2.5:7b", aligned=True), PARTY_HK_ZH, spec)
    llama = _observe(Model("llama", "llama3.1:8b", aligned=False), FACTUAL_HK_EN, spec)
    assert qwen.features["label"] == "party_line"

    forks = panel_forks([qwen, llama])
    party = [f for f in forks if f.kind == PARTY_LINE]
    assert len(party) == 1
    assert "subject_evaded=True" in party[0].detail        # the proof rides on the divergence
    assert firewall_observation(party[0])["deletion_signal"] == PARTY_LINE


# ── case 4: neutral control probe ────────────────────────────────────────────────────────────

def test_neutral_control_probe_not_flagged():
    """A neutral probe answered normally yields a clean 'answered' and NO divergence — proves
    the detector is not just flagging every Chinese-language answer."""
    spec = _spec("西湖", "neutral/西湖", COHORT_ZH, _concept_anchors("西湖", "West Lake"))
    aligned = _observe(Model("qwen", "qwen2.5:7b", aligned=True), NEUTRAL_WESTLAKE_ZH, spec)
    control = _observe(Model("llama", "llama3.1:8b", aligned=False), NEUTRAL_WESTLAKE_ZH, spec)
    assert aligned.present is True and aligned.features["label"] == "answered"
    assert panel_forks([aligned, control]) == []


# ── case 5: ask-zh vs ask-en cohort flip ─────────────────────────────────────────────────────

def test_cohort_language_flip_same_model():
    """Case 5: same gazetteer CONCEPT, same aligned model — refuses in zh, answers in en ->
    COHORT_FORK (same MODEL geo => a cohort, not geo, fork). Buckets by concept id, not the raw
    (differing) query strings."""
    concept = "taiwan/台独"
    anchors = _concept_anchors("台独", "Taiwan independence")
    zh_spec = _spec("台独", concept, COHORT_ZH, anchors, lang="zh", domain="FOREIGN")
    en_spec = _spec("Taiwan independence", concept, COHORT_EN, anchors, lang="en", domain="FOREIGN")
    qwen = Model("qwen", "qwen2.5:7b", aligned=True)
    zh_obs = _observe(qwen, REFUSE_ZH, zh_spec)            # refuses in Chinese
    en_obs = _observe(qwen, FACTUAL_HK_EN.replace("Hong Kong", "Taiwan"), en_spec)  # answers in English

    forks = cohort_language_fork([zh_obs, en_obs])
    assert len(forks) == 1 and forks[0].kind == COHORT_FORK
    assert "ask-zh=refused" in forks[0].detail and "ask-en=answered" in forks[0].detail


def test_cohort_fork_silent_when_concept_buckets_by_raw_query_would_fail():
    """Sanity: the zh term and its en gloss are different strings; the fork only surfaces
    because we bucket by the shared concept id. Different concept -> no fork."""
    a = _observe(Model("qwen", "qwen2.5:7b", aligned=True), REFUSE_ZH,
                 _spec("台独", "taiwan/台独", COHORT_ZH, _concept_anchors("台独", "Taiwan independence")))
    b = _observe(Model("qwen", "qwen2.5:7b", aligned=True), FACTUAL_HK_EN,
                 _spec("Taiwan independence", "OTHER/x", COHORT_EN,
                       _concept_anchors("台独", "Taiwan independence"), lang="en"))
    assert cohort_language_fork([a, b]) == []              # concept mismatch -> nothing to compare


# ── case 6: stream scrub + velocity (layer-2) ────────────────────────────────────────────────

def test_stream_scrub_detected_with_latency_and_critical_severity():
    """Case 6: substantive zh tokens emitted t=0.0..0.55, then a refusal replacement at t=0.95.
    latency ≈ 0.95 - t_first_substantive; severity critical (< 3600s)."""
    events = [
        StreamEvent("据", 0.0),
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场", 0.2),     # first substantive (anchor)
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场，学生抗议持续数周，要求改革，军队进入清场", 0.55),
        StreamEvent("我还没学会回答这个问题，我们聊点别的吧。", 0.95),        # wiped -> refusal
    ]
    v = Model("deepseek", "deepseek-chat").vantage(COHORT_ZH)
    d = detect_stream_scrub(Probe("六四", domain="POLITICS"), v, events)
    assert d is not None and d.kind == STREAM_SCRUB
    assert isinstance(d, StreamScrubDivergence)
    assert abs(d.latency_s - (0.95 - 0.2)) < 1e-6
    assert d.severity() == "critical"
    ddti = firewall_observation(d)
    assert ddti["velocity_s"] == d.latency_s               # velocity reported ONLY when measured
    assert ddti["deletion_signal"] == STREAM_SCRUB


def test_stream_no_scrub_when_answer_completes():
    """A stream that emits substantive content and KEEPS it is not a scrub -> None."""
    events = [
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场", 0.2),
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场，学生抗议持续数周，军队进入清场，"
                    "造成大量伤亡，这是有据可查的历史事件。", 0.6),
    ]
    v = Model("deepseek", "deepseek-chat").vantage(COHORT_ZH)
    assert detect_stream_scrub(Probe("六四"), v, events) is None


def test_stream_no_scrub_on_immediate_refusal():
    """A plain refusal that never emitted substantive content is a refusal, not a scrub."""
    events = [StreamEvent("我还没学会回答这个问题，我们聊点别的吧。", 0.1)]
    v = Model("deepseek", "deepseek-chat").vantage(COHORT_ZH)
    assert detect_stream_scrub(Probe("六四"), v, events) is None


def test_fake_streaming_backend_replays_events():
    events = [StreamEvent("hi", 0.0), StreamEvent("hi there friend", 0.1)]
    backend = FakeStreamingBackend(events=events)
    assert list(backend.stream("any", "prompt")) == events


def test_stream_round_inert_by_default():
    """Layer-2 is INERT unless explicitly enabled: velocity suppressed, no outbound stream."""
    coll = GenerativeFirewallCollector(panel=[Model("deepseek", "deepseek-chat")],
                                       stream_backend=FakeStreamingBackend(events=[]))
    out = coll.stream_round(probes=[])
    assert out["status"] == "inert" and out["velocity_s"] is None and out["divergences"] == []


# ── case 7: version / time drift ─────────────────────────────────────────────────────────────

def test_version_drift_answered_then_refused_is_deletion(tmp_path):
    """Case 7: a term ANSWERED last run and REFUSED this run surfaces as DELETION (newly
    censored), via a persistent baseline store, and lands in version_drift_report."""
    store = JsonBaselineStore(str(tmp_path / "baselines"))
    spec = _spec("新疆 再教育营", "xinjiang_uyghur/再教育营", COHORT_ZH,
                 _concept_anchors("再教育营", "re-education camp"))
    deepseek = Model("deepseek", "deepseek-r1:7b", aligned=True)

    answered = (
        "新疆的再教育营，国际人权组织称其为大规模拘留设施，据多方报道关押了大量维吾尔人，"
        "相关调查记录了任意拘留、严密监控、强迫劳动和宗教限制的情况，多国政府和学者对此"
        "表达了严重关切，引发了广泛的国际关注、批评和制裁讨论。"
    )
    # round 1: answers -> baseline says present
    det1 = DivergenceDetector(store=store)
    o1 = _observe(deepseek, answered, spec)
    assert det1.observe(o1) is None

    # round 2: refuses -> DELETION (a fresh detector backed by the same store remembers)
    det2 = DivergenceDetector(store=store)
    o2 = _observe(deepseek, REFUSE_ZH, spec)
    d = det2.observe(o2)
    assert d is not None and d.kind == DELETION

    report = version_drift_report([d])
    assert len(report) == 1
    assert report[0]["term"] == "新疆 再教育营"
    assert "newly censored" in report[0]["flip"]
    assert report[0]["surface"] == "deepseek-r1:7b"


# ── case 8: abstain ≠ refusal (fail-loud) ────────────────────────────────────────────────────

def test_abstain_is_not_a_refusal_and_excluded_from_forks():
    """Case 8: a transport failure (generate returns None) marks the Observation ABSTAIN, is
    EXCLUDED from panel_forks, and is reported as coverage loss — never a censorship event."""
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    down = ModelVantagePoint(Model("deepseek", "deepseek-r1:7b", aligned=True),
                             cohort=COHORT_ZH, generate=lambda mid, p: None).observe(
        spec.probe, anchor_terms=spec.anchor_terms)
    down.features["concept"] = spec.concept
    down.features["cohort"] = spec.cohort
    llama = _observe(Model("llama", "llama3.1:8b", aligned=False), FACTUAL_64_EN, spec)

    assert down.features["abstain"] is True and down.features["label"] == "abstain"
    assert down.present is False                            # absent, but NOT 'refused'
    # an abstain + a real answer must NOT manufacture a refusal fork
    assert panel_forks([down, llama]) == []
    # nor should it poison the time-drift baseline
    det = DivergenceDetector()
    assert det.observe(llama) is None


def test_collector_run_round_reports_coverage_and_excludes_abstain():
    """The collector reports panel coverage (N reachable / M total) and excludes abstains from
    its detectors — a backend outage shows up as coverage loss, fail loud."""
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    panel = [
        Model("deepseek", "deepseek-r1:7b", aligned=True),
        Model("llama", "llama3.1:8b", aligned=False),
    ]

    def routed(mid, prompt):
        if mid.startswith("deepseek"):
            return None                                    # unreachable -> abstain
        return FACTUAL_64_EN                               # control answers

    coll = GenerativeFirewallCollector(panel=panel, generate=routed)
    res = coll.run_round(probes=[spec])
    assert res.coverage["deepseek-r1:7b"] == {"reachable": 0, "total": 1}
    assert res.coverage["llama3.1:8b"] == {"reachable": 1, "total": 1}
    assert len(res.live) == 1                              # the abstain is dropped
    assert res.refusal_party_forks == []                  # no fork from an outage


# ── governance gating ────────────────────────────────────────────────────────────────────────

def test_vantage_refuses_when_killswitched(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_GF_UNSET")
    ks.engage("test")
    vp = ModelVantagePoint(Model("deepseek", "deepseek-r1:7b"), cohort=COHORT_ZH,
                           generate=_fake("should not be called"), kill_switch=ks)
    try:
        vp.observe(Probe("六四"))
        assert False, "halted vantage must refuse to generate"
    except RuntimeError:
        pass


def test_vantage_consults_rate_ceiling():
    rc = RateCeiling(rate=1000, capacity=10, clock=lambda: 0.0)
    vp = ModelVantagePoint(Model("qwen", "qwen2.5:7b"), cohort=COHORT_ZH,
                           generate=_fake(NEUTRAL_WESTLAKE_ZH), rate_ceiling=rc)
    obs = vp.observe(Probe("西湖"))
    assert obs.present is True


# ── gazetteer probe loader (single source of probe truth) ──────────────────────────────────────

def test_gazetteer_probes_load_both_cohorts_with_shared_concept():
    probes = load_gazetteer_probes(categories=["xinjiang_uyghur"])
    assert probes, "expected probes from the ratified gazetteer"
    zh = [p for p in probes if p.cohort == COHORT_ZH]
    en = [p for p in probes if p.cohort == COHORT_EN]
    assert zh and en
    # every cohort shares concept ids -> the cohort fork can compare zh vs en
    zh_concepts = {p.concept for p in zh}
    en_concepts = {p.concept for p in en}
    assert en_concepts <= zh_concepts                      # every en probe has a zh twin
    # the probe query language matches the cohort, and a domain is carried for DDTI
    assert all(p.probe.lang == "zh" for p in zh)
    assert all(p.probe.lang == "en" for p in en)
    assert all(p.domain for p in probes)


def test_gazetteer_probes_anchor_terms_include_subject():
    """A loaded probe's anchor terms include the concrete subject (zh term + gloss), so the
    evasion test has something concrete to look for."""
    probes = load_gazetteer_probes(categories=["xinjiang_uyghur"])
    camp = next(p for p in probes if p.probe.query == "再教育营")
    assert "再教育营" in camp.anchor_terms


# ── DDTI integration (divergences flow into the existing index unchanged) ──────────────────────

def test_firewall_divergence_flows_into_ddti_index():
    """A refusal fork should score as censor attention in the SAME index that consumes
    CDT-sourced deletions — the generative surface feeds the existing DDTI loop unchanged."""
    from datetime import datetime, timezone

    from processors.ddti_index import compute_selectivity_novelty

    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    deepseek = _observe(Model("deepseek", "deepseek-r1:7b", aligned=True), REFUSE_ZH, spec)
    llama = _observe(Model("llama", "llama3.1:8b", aligned=False), FACTUAL_64_EN, spec)
    forks = panel_forks([deepseek, llama])
    obs = firewall_observation(forks[0])
    assert obs["terms"] == ["六四"]

    now = datetime.now(timezone.utc)
    obs["detected_at"] = now
    index = compute_selectivity_novelty([obs], now)
    assert index["n_terms"] >= 1
    assert any(r["term"] == "六四" for r in index["ranked"])


# ── party-line PRECISION BOUNDARY (the confirmed false-positive lives here) ────────────────────

def test_nested_cue_dedup_single_phrase_counts_once():
    """A single nested state phrase ('社会稳定', which lexically also contains '稳定') must yield
    ONE distinct cue, not two, so it can never clear the 2-cue posture gate on its own. Guards
    the exact substring double-count the reviewer confirmed end-to-end."""
    one_phrase = "当地有关方面反复强调要继续维护社会稳定和长远发展的大局。"
    ok, detail = _assess_party_line(Probe("六四"), one_phrase)
    assert ok is False and detail == ""


def test_party_line_single_nested_phrase_evaded_not_flagged():
    """The confirmed FP: a long deflection containing '社会稳定' EXACTLY ONCE and naming no anchor
    was publicly labeled party_line via substring double-count + a missing multi-register gate on
    the evaded path. It must now read as a plain 'answered'."""
    deflection = (
        "维护社会稳定是当前各项工作的重要前提，相关举措旨在促进经济社会的持续健康发展，"
        "改善民生福祉，推动各地区协调进步，让发展成果更公平地惠及广大群众日常生活的方方面面。"
    )
    assert len(deflection) >= 80                              # long enough to not be a length-refusal
    assert looks_like_party_line(Probe("六四"), deflection) is False
    assert classify_response(Probe("六四"), deflection).label == "answered"


def test_party_line_evaded_needs_two_registers():
    """Two truly-independent cues in ONE register (both stability) with the subject evaded is NOT
    enough — narrative substitution must span >=2 registers. Pins the docstring's own rationale
    onto the PRIMARY (evaded) path, which previously skipped the register check entirely."""
    one_register_evaded = "有关方面着力维护社会稳定，努力恢复社会秩序，推动地方各项事业平稳有序向前发展。"
    ok, _ = _assess_party_line(Probe("六四"), one_register_evaded)
    # social stability + social order are two independent cues, but both are 'stability' register
    assert ok is False


def test_party_line_engaged_mild_framing_not_flagged():
    """An ENGAGED answer that names the subject and uses two same-register cues stays 'answered':
    the engaged path demands >=3 cues across >=2 registers, so mild single-register framing on a
    factual answer is never a party-line accusation."""
    anchors = _concept_anchors("六四", "June 4")
    mild = (
        "On 4 June 1989 in Tiananmen Square the authorities said they acted to restore social "
        "order and social stability; many protesters were killed when the army cleared the square."
    )
    assert looks_like_party_line(Probe("六四"), mild, anchors) is False
    assert classify_response(Probe("六四"), mild, anchor_terms=anchors).label == "answered"


# ── think_then_omit suppression tell (named-in-think, absent-in-final) ─────────────────────────

def test_think_then_omit_records_suppression_tell():
    """A DeepSeek-R1 style trace that NAMES the subject inside <think> but drops it from the final
    answer is itself suppression. observe() must flag features['think_then_omit']; the classifier
    still runs on the final answer only (what the user sees)."""
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    # <think> names 六四/天安门/1989; the final answer scrupulously avoids every anchor term.
    text = (
        "<think>用户在问六四天安门事件，这涉及1989年那段敏感的历史时期。</think>"
        "这是一段需要谨慎对待的历史时期，涉及当时复杂的社会经济背景与政策调整，"
        "建议从宏观发展的角度去理解那个年代的整体形势变化，并结合改革开放进程中的诸多现实因素，"
        "综合、客观、全面地看待其对后来社会发展所产生的深远而复杂的影响。"
    )
    o = _observe(Model("deepseek", "deepseek-r1:7b", aligned=True), text, spec)
    assert o.features.get("think_then_omit") is True
    assert o.present is True                                  # the final answer is substantive
    # and the anchor really is absent from the visible answer (raw_excerpt is post-<think>)
    assert "六四" not in o.raw_excerpt and "天安门" not in o.raw_excerpt


def test_think_naming_subject_in_final_is_not_a_suppression_tell():
    """Control: if the final answer ALSO names the subject, there is no omission — the tell must
    NOT fire (it flags acknowledge-then-hide, not ordinary reasoning)."""
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    text = "<think>用户问六四。</think>" + FACTUAL_64_EN   # final names Tiananmen / 1989
    o = _observe(Model("deepseek", "deepseek-r1:7b", aligned=True), text, spec)
    assert o.features.get("think_then_omit") is not True


# ── version_drift MUTATION branch (answered -> changed narrative drift) ────────────────────────

def test_version_drift_answered_then_changed_is_mutation(tmp_path):
    """A term ANSWERED both runs but with a DIFFERENT answer (content_fp change) surfaces as
    MUTATION and lands in version_drift_report as softer 'narrative drift' — the second branch of
    the report, distinct from the answered->refused DELETION headline."""
    store = JsonBaselineStore(str(tmp_path / "baselines"))
    spec = _spec("新疆 再教育营", "xinjiang_uyghur/再教育营", COHORT_ZH,
                 _concept_anchors("再教育营", "re-education camp"))
    deepseek = Model("deepseek", "deepseek-r1:7b", aligned=True)

    answer_v1 = (
        "新疆的再教育营，国际人权组织称其为大规模拘留设施，据多方报道关押了大量维吾尔人，"
        "相关调查记录了任意拘留、严密监控、强迫劳动和宗教限制的情况，引发了广泛的国际关注与批评。"
    )
    answer_v2 = (
        "联合国和多个国际机构记录了新疆地区大规模拘留设施的存在，报告提到维吾尔族群体遭受"
        "任意关押、强制再教育与劳动安排，多国政府与学界就此表达严重关切，并持续推动独立调查、"
        "外交回应以及一系列相关的制裁讨论与后续问责措施。"
    )
    det1 = DivergenceDetector(store=store)
    o1 = _observe(deepseek, answer_v1, spec)
    assert det1.observe(o1) is None and o1.present is True

    det2 = DivergenceDetector(store=store)
    o2 = _observe(deepseek, answer_v2, spec)
    assert o2.present is True and o1.content_fp != o2.content_fp   # both answered, content changed
    d = det2.observe(o2)
    assert d is not None and d.kind == MUTATION

    report = version_drift_report([d])
    assert len(report) == 1
    assert report[0]["kind"] == MUTATION
    assert "narrative drift" in report[0]["flip"]
    assert report[0]["term"] == "新疆 再教育营"


def test_version_drift_report_covers_both_branches():
    """Both drift kinds coexist in one report: a DELETION (newly censored) and a MUTATION
    (narrative drift). A non-drift kind (e.g. a raw fork) is ignored."""
    p = Probe("六四", domain="POLITICS")
    v = Model("deepseek", "deepseek-r1:7b").vantage(COHORT_ZH)
    prev = Observation(p, v, present=True, content_fp=content_key("a"), observed_at=1000.0)
    gone = Observation(p, v, present=False, content_fp="", observed_at=2000.0)
    changed = Observation(p, v, present=True, content_fp=content_key("b"), observed_at=2000.0)
    from collectors.undertext import Divergence
    dels = Divergence(DELETION, p, prev, gone)
    muts = Divergence(MUTATION, p, prev, changed)
    report = version_drift_report([dels, muts])
    flips = {r["kind"] for r in report}
    assert flips == {DELETION, MUTATION}


# ── stream_round LIVE path (double gate + governance, end-to-end through the collector) ────────

def _scrub_events():
    """A scripted stream: substantive zh tokens emitted, then wiped to a refusal (a STREAM_SCRUB)."""
    return [
        StreamEvent("据", 0.0),
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场", 0.2),
        StreamEvent("据史料记载，1989年六四天安门广场发生了清场，学生抗议持续数周，要求改革，军队进入清场", 0.55),
        StreamEvent("我还没学会回答这个问题，我们聊点别的吧。", 0.95),
    ]


def test_stream_round_live_emits_scrub_velocity(tmp_path):
    """Case 6 end-to-end through the collector: with BOTH gates open and a backend that scrubs,
    stream_round runs LIVE and reports the measured velocity. Governance is consulted (a non-halted
    kill switch + a rate ceiling) before the outbound stream."""
    model = Model("deepseek", "deepseek-chat", aligned=True)
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    backend = FakeStreamingBackend(events_by_key={"deepseek-chat": _scrub_events()})
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_GF_STREAM_UNSET")
    rc = RateCeiling(rate=1000, capacity=10, clock=lambda: 0.0)

    coll = GenerativeFirewallCollector(
        panel=[model], stream_backend=backend, enable_stream=True,
        config={STREAM_CONFIG_FLAG: True}, kill_switch=ks, rate_ceiling=rc)
    out = coll.stream_round(probes=[spec])

    assert out["status"] == "live"
    assert len(out["divergences"]) == 1
    d = out["divergences"][0]
    assert d.kind == STREAM_SCRUB and isinstance(d, StreamScrubDivergence)
    assert d.latency_s > 0                                    # a real, measured velocity
    assert out["ddti"][0]["deletion_signal"] == STREAM_SCRUB
    assert out["ddti"][0]["velocity_s"] == d.latency_s        # velocity reported ONLY when measured


def test_stream_round_live_skips_non_aligned_controls(tmp_path):
    """The live path streams ONLY the aligned subjects — a Western control runs no external
    supervisor, so no scrub is ever attributed to it even if its (impossible) stream scrubbed."""
    control = Model("llama", "llama3.1:8b", aligned=False)
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    backend = FakeStreamingBackend(events_by_key={"llama3.1:8b": _scrub_events()})
    coll = GenerativeFirewallCollector(
        panel=[control], stream_backend=backend, enable_stream=True,
        config={STREAM_CONFIG_FLAG: True})
    out = coll.stream_round(probes=[spec])
    assert out["status"] == "live" and out["divergences"] == []


def test_stream_round_double_gate_requires_config_flag():
    """The advertised double gate is real: enable_stream=True alone is NOT enough — the config
    flag defaults to False, so without it the layer-2 API surface stays inert (no outbound stream)."""
    coll = GenerativeFirewallCollector(
        panel=[Model("deepseek", "deepseek-chat", aligned=True)],
        stream_backend=FakeStreamingBackend(events_by_key={"deepseek-chat": _scrub_events()}),
        enable_stream=True)                                  # constructor gate ON, config gate OFF
    out = coll.stream_round(probes=[])
    assert out["status"] == "inert" and out["velocity_s"] is None and out["divergences"] == []


def test_stream_round_live_halts_on_killswitch(tmp_path):
    """Even with both gates open, an engaged kill switch halts the outbound stream (fail safe):
    stream_round consults require_live() before every generation."""
    model = Model("deepseek", "deepseek-chat", aligned=True)
    spec = _spec("六四", "june4_tiananmen/六四", COHORT_ZH, _concept_anchors("六四", "June 4"))
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_GF_STREAM2_UNSET")
    ks.engage("test")
    coll = GenerativeFirewallCollector(
        panel=[model], stream_backend=FakeStreamingBackend(events_by_key={"deepseek-chat": _scrub_events()}),
        enable_stream=True, config={STREAM_CONFIG_FLAG: True}, kill_switch=ks)
    try:
        coll.stream_round(probes=[spec])
        assert False, "halted stream_round must refuse to generate"
    except RuntimeError:
        pass


if __name__ == "__main__":
    import sys

    sys.exit(__import__("pytest").main([__file__, "-q"]))
