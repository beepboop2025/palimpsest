"""Tests for collectors.blocklist_archaeology — client-embedded blocklist archaeology.

    PYTHONPATH=. python3 -m pytest tests/test_blocklist_archaeology.py -q

Pure/offline. Every fixture is a hard-coded, dated, PUBLISHED fact (Citizen Lab's open
chat-censorship corpus): the parse + version-diff core, the DDTI emission, and the
governance-gated collector are all exercised with no network and no real Chinese
infrastructure. The validation cases mirror the domain brief (YY COVID adds, YY June-4
numeronyms, LINE v21→v22 counts, the WeChat AND-combination, and the encoding guards).
"""

from datetime import datetime, timedelta, timezone

from collectors.blocklist_archaeology import (
    BLOCKLIST_ADD,
    BLOCKLIST_REMOVE,
    EXHAUSTIVE,
    SAMPLED,
    BlocklistArchaeologyCollector,
    BlocklistArtifact,
    BlocklistEncryptedError,
    Keyword,
    classify_script,
    decode_bytes,
    detect_format,
    diff_versions,
    emit_novelty_observations,
    normalize_term,
    novelty_inputs,
    parse_blocklist,
    parse_combination,
    severity_of,
    term_to_observation,
    _default_local_load,
)
from processors.ddti_index import compute_selectivity_novelty
from core.governance import KillSwitch


# ── fixture helpers ─────────────────────────────────────────────────────────────────────────

def _art(app, version, date, completeness=EXHAUSTIVE, encoding=""):
    return BlocklistArtifact(
        app=app, version=version,
        observed_at=datetime(*date, tzinfo=timezone.utc),
        source_ref=f"citizenlab/chat-censorship/{app}/{version}.txt",
        completeness=completeness, encoding=encoding,
    )


def _parse(app, version, date, raw, *, completeness=EXHAUSTIVE, encoding="", **kw):
    return parse_blocklist(raw, _art(app, version, date, completeness, encoding), **kw)


# ── normalization & classification ───────────────────────────────────────────────────────────

def test_normalize_lowercases_ascii_only_and_preserves_cjk():
    assert normalize_term("  Falun GONG  ") == "falun gong"
    assert normalize_term("武汉不明肺炎") == "武汉不明肺炎"   # CJK untouched
    assert normalize_term("# a comment line") == ""           # comments drop
    assert normalize_term("   ") == ""                        # blanks drop
    # NFKC folds a fullwidth digit; the term stays one token (no split).
    assert normalize_term("ＶＩＩＶ事") == "viiv事"


def test_normalize_does_not_fold_simplified_into_traditional():
    # 武汉 (simplified) and 武漢 (traditional) are DISTINCT terms — which one the censor listed
    # is itself intelligence; folding would destroy that signal (pitfall §5.2).
    assert normalize_term("武汉") != normalize_term("武漢")
    assert classify_script("武汉") == "simplified"
    assert classify_script("武漢") == "traditional"


def test_classify_script_buckets():
    assert classify_script("8964") == "numeric"
    assert classify_script("viiv") == "latin"
    assert classify_script("天安门1989") == "mixed"   # mixed-script, ONE keyword
    assert classify_script("p4病毒实验室") == "mixed"


# ── format detection ─────────────────────────────────────────────────────────────────────────

def test_detect_format_plaintext_csv_combination():
    assert detect_format("武汉\n上海\n北京\n".encode()) == "plaintext"
    assert detect_format(b"keyword,date_added,language\x0a\xe6,2020-01-01,zh") == "csv"
    assert detect_format("\xe7;\xe5".encode() + "美国疾控中心;冠状病毒\n".encode()) == "combination"


# ── 1. YY COVID add (primary validation case) ──────────────────────────────────────────────

_YY_BEFORE = "武汉\n上海\n北京\n疫情事件\n病毒感染\n".encode("utf-8")
# 2019-12-31 build adds the first Wuhan-pneumonia batch (Citizen Lab, Censored Contagion, T1).
_YY_AFTER = (
    "武汉\n上海\n北京\n疫情事件\n病毒感染\n"
    "武汉不明肺炎\n武汉海鲜市场\n沙士变异\n武汉卫生委员会\np4病毒实验室\n爆發sars疫情\n"
).encode("utf-8")


def test_yy_covid_add_surfaces_the_batch_as_novelty():
    old = _parse("yy", "2019-12-30", (2019, 12, 30), _YY_BEFORE)
    new = _parse("yy", "2019-12-31", (2019, 12, 31), _YY_AFTER)
    diff = diff_versions(old, new)
    added = {kw.term for kw in diff.added}
    for term in ("武汉不明肺炎", "武汉海鲜市场", "沙士变异", "武汉卫生委员会",
                 "p4病毒实验室", "爆發sars疫情"):
        assert term in added
    assert diff.removed == []

    obs = emit_novelty_observations(diff)
    assert len(obs) == 6
    one = next(o for o in obs if o["text"] == "武汉不明肺炎")
    assert one["deletion_signal"] == BLOCKLIST_ADD
    assert one["detected_at"] == datetime(2019, 12, 31, tzinfo=timezone.utc)
    assert one["source"] == "blocklist:yy@2019-12-31"
    assert one["url"] == new.artifact.source_ref           # provenance, not a CN endpoint
    assert "_caveat" not in one                              # exhaustive -> full confidence


def test_yy_covid_traditional_variant_tagged_traditional():
    new = _parse("yy", "2019-12-31", (2019, 12, 31), _YY_AFTER)
    kw = new.keywords["爆發sars疫情"]
    assert kw.script in ("traditional", "mixed")             # 發 is traditional-distinct


# ── 2. YY COVID removal — a removal is NOT novelty ─────────────────────────────────────────

def test_removals_are_not_novelty_unless_requested():
    # 2020-02-10 drops two earlier terms (relaxation, not a new directive).
    later = ("武汉\n上海\n北京\n武汉不明肺炎\n").encode("utf-8")
    old = _parse("yy", "2019-12-31", (2019, 12, 31), _YY_AFTER)
    new = _parse("yy", "2020-02-10", (2020, 2, 10), later)
    diff = diff_versions(old, new)
    removed = {kw.term for kw in diff.removed}
    assert "病毒感染" in removed and "疫情事件" in removed

    # default: removals never emitted, never carry blocklist_add
    default_obs = emit_novelty_observations(diff)
    assert all(o["deletion_signal"] == BLOCKLIST_ADD for o in default_obs)
    assert "病毒感染" not in {o["text"] for o in default_obs}

    # explicit: removals emitted, stamped blocklist_remove (downstream never counts as novelty)
    with_rem = emit_novelty_observations(diff, emit_removals=True)
    rem = [o for o in with_rem if o["deletion_signal"] == BLOCKLIST_REMOVE]
    assert {o["text"] for o in rem} == removed


# ── 3. YY June-4 numeronyms via dated CSV — per-keyword Date Added flows through ────────────

_JUNE4_CSV = (
    "keyword,date_added,date_removed,language,category\n"
    "六四30,2019-05-28,,Simplified Chinese,june4_tiananmen\n"
    "三十周年,2019-05-29,,Simplified Chinese,june4_tiananmen\n"
    "VIIV事,2019-05-29,,English,june4_tiananmen\n"
    "天安门镇压,2019-05-31,,Simplified Chinese,june4_tiananmen\n"
).encode("utf-8")


def test_june4_csv_dates_flow_and_numeronyms_are_high_severity():
    old = _parse("yy", "2019-05-01", (2019, 5, 1), b"")
    new = _parse("yy", "2019-06-01", (2019, 6, 1), _JUNE4_CSV, fmt="csv")
    assert new.keywords["六四30"].category == "june4_tiananmen"

    diff = diff_versions(old, new)
    obs = {o["text"]: o for o in emit_novelty_observations(diff)}
    # per-row Date Added becomes detected_at (not the artifact's 2019-06-01 observe date)
    assert obs["六四30"]["detected_at"] == datetime(2019, 5, 28, tzinfo=timezone.utc)
    assert obs["天安门镇压"]["detected_at"] == datetime(2019, 5, 31, tzinfo=timezone.utc)
    # numeronym (digit) and roman-numeral evasion classify high — lexical, auditable.
    # "VIIV事" normalizes its ASCII run to lowercase -> "viiv事".
    assert obs["六四30"]["severity"] == "high"
    assert obs["viiv事"]["severity"] == "high"


# ── 4. LINE v21→v22 — set-difference counts + mixed-script single keyword ───────────────────

def _line_pair():
    shared = [f"共享词{i}" for i in range(222)]
    bo_xilai = [f"薄熙来事{i}" for i in range(147)]   # all 147 removals relate to Bo Xilai
    fresh = [f"新增词{i}" for i in range(312)]
    mixed = "天安门1989"                               # ONE keyword, not split on the digit
    v21 = ("\n".join(shared + bo_xilai + [mixed]) + "\n").encode("utf-8")   # 370 terms
    v22 = ("\n".join(shared + fresh + [mixed]) + "\n").encode("utf-8")      # 535 terms
    return _parse("line", "v21", (2014, 1, 1), v21), _parse("line", "v22", (2014, 2, 1), v22)


def test_line_v21_v22_diff_counts_and_mixed_script_keyword():
    old, new = _line_pair()
    assert len(old.keywords) == 370 and len(new.keywords) == 535
    diff = diff_versions(old, new)
    assert len(diff.added) == 312
    assert len(diff.removed) == 147
    assert all(kw.term.startswith("薄熙来事") for kw in diff.removed)  # the Bo Xilai removals
    # 天安门1989 is present in both -> not in the diff, and parsed as a single mixed keyword
    kw = new.keywords["天安门1989"]
    assert kw.is_combination is False and kw.components == ()
    assert "天安门1989" not in {k.term for k in diff.added}


# ── 5. WeChat AND-combination (sampled) ─────────────────────────────────────────────────────

def test_parse_combination_splits_on_and_delimiters():
    assert parse_combination("美国疾控中心;冠状病毒") == ("美国疾控中心", "冠状病毒")
    assert parse_combination("a；b+c\td") == ("a", "b", "c", "d")
    assert parse_combination("单独关键词") == ("单独关键词",)  # single component, not a combo


def test_wechat_combination_preserved_and_sampled_forces_low():
    old = _parse("wechat", "2020-01-01", (2020, 1, 1), b"", completeness=SAMPLED)
    new = _parse("wechat", "2020-03-01", (2020, 3, 1),
                 "美国疾控中心;冠状病毒\n".encode("utf-8"), completeness=SAMPLED)
    combo = new.keywords["美国疾控中心 + 冠状病毒"]
    assert combo.is_combination is True
    assert combo.components == ("美国疾控中心", "冠状病毒")

    [obs] = emit_novelty_observations(diff_versions(old, new))
    # terms = components + joined form; text = joined form (one finding, both components mineable)
    assert obs["terms"] == ["美国疾控中心", "冠状病毒", "美国疾控中心 + 冠状病毒"]
    assert obs["text"] == "美国疾控中心 + 冠状病毒"
    # sampled / non-exhaustive -> novelty confidence suppressed (loud caveat, not silent)
    assert obs["severity"] == "low"
    assert obs["_caveat"] == "sampled-nonexhaustive"


# ── 6. Encoding guards (fail loud) ──────────────────────────────────────────────────────────

def test_legacy_gb18030_decodes_and_warns_loud():
    raw = "测试敏感词\n反revolution\n".encode("gb18030")
    text, enc, warning = decode_bytes(raw)
    assert enc == "gb18030"
    assert "测试敏感词" in text
    assert warning                                   # non-empty: fell back past utf-8
    parsed = _parse("qq2004", "leaked", (2004, 1, 1), raw)
    assert parsed.decode_warning                      # surfaced on the ParsedBlocklist
    assert "测试敏感词" in parsed.keywords


def test_utf8_bom_is_stripped():
    raw = b"\xef\xbb\xbf" + "敏感词\n另一个\n".encode("utf-8")
    text, enc, warning = decode_bytes(raw)
    assert enc == "utf-8" and warning == ""
    assert not text.startswith("﻿")
    parsed = _parse("yy", "bom", (2020, 1, 1), raw)
    assert "敏感词" in parsed.keywords                # BOM not glued onto the first term


def test_encrypted_blob_without_decryptor_fails_loud():
    import base64
    blob = base64.b64encode(bytes(range(256)) * 2)   # ciphertext-like: undecodable as text
    assert detect_format(blob) == "encrypted"
    try:
        parse_blocklist(blob, _art("line", "cbw", (2014, 1, 1)))
        assert False, "expected BlocklistEncryptedError"
    except BlocklistEncryptedError:
        pass


def test_injected_decryptor_is_used_when_supplied():
    import base64
    plain = "解密后的关键词\n另一词\n".encode("utf-8")
    blob = base64.b64encode(bytes(range(256)))       # detected as encrypted
    # caller owns the key/legality; here a trivial fake decryptor returns the published plaintext
    parsed = parse_blocklist(blob, _art("line", "cbw", (2014, 1, 1)),
                             fmt="encrypted", decryptor=lambda _b: plain)
    assert "解密后的关键词" in parsed.keywords


# ── diffing invariants (pitfall §5.4) ───────────────────────────────────────────────────────

def test_diff_is_order_dedup_and_encoding_invariant():
    a = _parse("yy", "a", (2020, 1, 1), "武汉\n上海\n北京\n".encode("utf-8"))
    # same set: reordered, duplicated, and re-encoded as gb18030 -> must be an EMPTY diff
    b = _parse("yy", "b", (2020, 1, 2), "北京\n武汉\n武汉\n上海\n".encode("gb18030"))
    diff = diff_versions(a, b)
    assert diff.added == [] and diff.removed == []
    assert a.fingerprint() == b.fingerprint()         # replayable, identical content address


def test_lone_wildcard_is_not_a_keyword():
    parsed = _parse("yy", "w", (2020, 1, 1), "法轮\n*\n敏感\n".encode("utf-8"))
    assert "*" not in parsed.keywords
    assert "法轮" in parsed.keywords and "敏感" in parsed.keywords


# ── severity is lexical only (Line 2) ───────────────────────────────────────────────────────

def test_severity_is_lexical_and_auditable():
    new = _parse("yy", "s", (2020, 1, 1), "8964\nthe\n维权抗议\n".encode("utf-8"),
                 category_map={"维权抗议": "rights_protest"})
    assert severity_of(new.keywords["8964"]) == "high"          # numeronym
    assert severity_of(new.keywords["the"]) == "low"            # latin stopword
    assert severity_of(new.keywords["维权抗议"]) == "high"       # high-salience category
    assert severity_of(new.keywords["维权抗议"]).islower()       # plain string, no model object


# ── term_to_observation mirrors the DDTI schema ────────────────────────────────────────────

def test_term_to_observation_has_the_full_ddti_shape():
    new = _parse("yy", "2019-12-31", (2019, 12, 31), _YY_AFTER)
    obs = term_to_observation(new.keywords["武汉不明肺炎"], new.artifact)
    assert set(obs) >= {"terms", "detected_at", "title", "text", "url",
                        "source", "deletion_signal", "severity"}
    assert obs["title"].startswith("[blocklist:add]")
    assert isinstance(obs["detected_at"], datetime) and obs["detected_at"].tzinfo is not None


# ── collector: governance-gated, injected I/O, fail-soft ───────────────────────────────────

def _fixtures():
    return {
        "citizenlab/chat-censorship/yy/2019-12-30.txt": _YY_BEFORE,
        "citizenlab/chat-censorship/yy/2019-12-31.txt": _YY_AFTER,
    }


def _loader(store):
    return lambda art: store[art.source_ref]


def test_collector_diffs_consecutive_versions_and_emits_novelty():
    store = _fixtures()
    arts = [_art("yy", "2019-12-31", (2019, 12, 31)),    # deliberately out of order
            _art("yy", "2019-12-30", (2019, 12, 30))]
    col = BlocklistArchaeologyCollector(arts, load_fn=_loader(store), known_terms=set())
    obs = col.collect()                                   # sorts by observed_at internally
    added_terms = {o["text"] for o in obs}
    assert "武汉不明肺炎" in added_terms
    assert all(o["deletion_signal"] == BLOCKLIST_ADD for o in obs)
    assert col.skipped == []


def test_collector_run_feeds_gazetteer_and_ddti_index():
    store = _fixtures()
    arts = [_art("yy", "2019-12-30", (2019, 12, 30)),
            _art("yy", "2019-12-31", (2019, 12, 31))]
    res = BlocklistArchaeologyCollector(arts, load_fn=_loader(store), known_terms=set()).run()
    assert res["status"] == "success"
    assert res["n_observations"] == 6
    # the index ingests the obs stream unchanged (n_terms is 0 here only because these are
    # historical 2019 fixture dates, outside the index's 30-day current window — the schema
    # flows, which is the contract under test).
    assert "ranked" in res["index"] and "n_terms" in res["index"]
    assert res["ledger"]["policy"].startswith("advisory-only")   # human-ratified, never auto


def test_collector_skips_encrypted_artifact_without_false_zero():
    import base64
    blob = base64.b64encode(bytes(range(256)) * 2)
    store = {
        "enc": blob,
        "citizenlab/chat-censorship/yy/2019-12-31.txt": _YY_AFTER,
    }
    enc_art = BlocklistArtifact("line", "cbw", datetime(2014, 1, 1, tzinfo=timezone.utc),
                                source_ref="enc", completeness=EXHAUSTIVE)
    arts = [enc_art,
            _art("yy", "2019-12-30", (2019, 12, 30)),  # provide both yy so a diff still happens
            _art("yy", "2019-12-31", (2019, 12, 31))]
    # the yy-before bytes too:
    store["citizenlab/chat-censorship/yy/2019-12-30.txt"] = _YY_BEFORE
    col = BlocklistArchaeologyCollector(arts, load_fn=_loader(store), known_terms=set())
    obs = col.collect()
    assert any(s["reason"] == "encrypted-no-decryptor" for s in col.skipped)  # recorded, loud
    assert "武汉不明肺炎" in {o["text"] for o in obs}                          # not a false zero


def test_collector_abstains_when_kill_switch_engaged(tmp_path):
    store = _fixtures()
    arts = [_art("yy", "2019-12-30", (2019, 12, 30)),
            _art("yy", "2019-12-31", (2019, 12, 31))]
    ks = KillSwitch(path=str(tmp_path / "halt"))
    ks.engage("test")
    col = BlocklistArchaeologyCollector(arts, load_fn=_loader(store),
                                        known_terms=set(), kill_switch=ks)
    obs = col.collect()
    assert obs == []                       # halted -> no collection, no fabricated data
    assert col.skipped                     # the halt is surfaced, not silent


def test_default_local_load_refuses_remote_urls():
    art = BlocklistArtifact("yy", "v", datetime(2020, 1, 1, tzinfo=timezone.utc),
                            source_ref="https://example.cn/list.txt")
    try:
        _default_local_load(art)
        assert False, "expected a refusal for a remote source_ref"
    except RuntimeError as e:
        assert "out-of-tree" in str(e)


# ── the blocklist_remove stamp is LOAD-BEARING (must_fix regression guard) ───────────────────

def test_novelty_inputs_strips_removals_that_the_index_would_miscount():
    # A relaxation (blocklist_remove) has hist_count==0 in the obs stream, so if it reached the
    # index it would be scored novelty=1.0 / positive attention — a censorship RELAXATION reported
    # as a brand-new high-novelty censor-attention event, the inverse of its meaning. This test
    # pins both the hazard and the boundary filter that neutralises it.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    add = {"terms": ["新增词"], "detected_at": now, "title": "add", "url": "u1",
           "source": "blocklist:yy@now", "deletion_signal": BLOCKLIST_ADD, "severity": "high"}
    rem = {"terms": ["旧词"], "detected_at": now, "title": "rem", "url": "u2",
           "source": "blocklist:yy@now", "deletion_signal": BLOCKLIST_REMOVE, "severity": "low"}

    # Unfiltered: the index has no notion of deletion_signal, so the REMOVAL is miscounted as a
    # brand-new max-novelty term. (This is the exact defect the must_fix describes.)
    raw = compute_selectivity_novelty([add, rem], now)
    assert any(r["term"] == "旧词" and r["novelty"] == 1.0 for r in raw["ranked"])

    # Filtered through novelty_inputs (what run() applies): the removal is gone, the add stays.
    filtered = compute_selectivity_novelty(novelty_inputs([add, rem]), now)
    ranked = {r["term"] for r in filtered["ranked"]}
    assert "新增词" in ranked
    assert "旧词" not in ranked


def test_collector_run_keeps_removals_out_of_the_novelty_index_end_to_end():
    # Full path: emit_removals=True feeds real relaxations into collect(); run() must record them
    # as events yet keep them out of the DDTI selectivity/novelty index. Current-dated so the
    # index's 30-day window actually ingests the terms (unlike the 2019 historical fixtures).
    t_new = datetime.now(timezone.utc).replace(microsecond=0)
    t_old = t_new - timedelta(days=1)

    def _art_at(ver, when):
        return BlocklistArtifact(app="yy", version=ver, observed_at=when,
                                 source_ref=f"mem/yy/{ver}", completeness=EXHAUSTIVE)

    store = {
        "mem/yy/old": "武汉\n病毒感染\n疫情事件\n".encode("utf-8"),
        "mem/yy/new": "武汉\n新增敏感词\n".encode("utf-8"),   # drops two, adds one
    }
    col = BlocklistArchaeologyCollector(
        [_art_at("old", t_old), _art_at("new", t_new)],
        load_fn=lambda a: store[a.source_ref], known_terms=set(), emit_removals=True)
    res = col.run()
    assert res["status"] == "success"

    # The event record DOES carry the two removals, stamped blocklist_remove (loud, not dropped).
    rem = [o for o in res["observations"] if o["deletion_signal"] == BLOCKLIST_REMOVE]
    assert {o["text"] for o in rem} == {"病毒感染", "疫情事件"}

    # But the novelty index never counts them: only the genuine ADD ranks; removals are absent.
    ranked = {r["term"] for r in res["index"]["ranked"]}
    assert "新增敏感词" in ranked
    assert "病毒感染" not in ranked and "疫情事件" not in ranked


def test_current_dated_add_actually_ranks_with_correct_max_novelty():
    # A genuine addition on a current-dated blocklist SHOULD rank, and novelty=1.0 is correct for
    # it (a brand-new directive) — the exact opposite of the mislabelled-removal case above. This
    # exercises the live novelty path for this surface (the historical fixtures never enter it).
    now = datetime.now(timezone.utc).replace(microsecond=0)
    a_old = BlocklistArtifact("yy", "old", now - timedelta(days=1), source_ref="m/old")
    a_new = BlocklistArtifact("yy", "new", now, source_ref="m/new")
    old = parse_blocklist("武汉\n上海\n".encode("utf-8"), a_old)
    new = parse_blocklist("武汉\n上海\n新疆抗议\n".encode("utf-8"), a_new)

    index = compute_selectivity_novelty(emit_novelty_observations(diff_versions(old, new)), now)
    assert index["n_terms"] >= 1
    ranked = {r["term"]: r for r in index["ranked"]}
    assert "新疆抗议" in ranked
    assert ranked["新疆抗议"]["is_new"] is True
    assert ranked["新疆抗议"]["novelty"] == 1.0     # a real ADD legitimately scores max novelty


# ── roman-numeral severity: no over-claim on benign latin words ─────────────────────────────

def test_severity_does_not_overclaim_benign_roman_letter_words():
    # Words composed only of i/v/x/l/c/d/m that are NOT valid roman numerals must stay 'medium',
    # not 'high' — the earlier `set(term) <= _ROMAN` test graded these as high false positives.
    for word in ("mild", "civil", "lid", "dim", "mimic", "civic", "vivid"):
        kw = Keyword(term=word, script="latin", raw=word)
        assert severity_of(kw) == "medium", word
    # A well-formed roman numeral (a real date/number evasion) still grades high — auditable.
    for roman in ("iv", "ix", "mcmlxxxix"):   # 4, 9, 1989
        kw = Keyword(term=roman, script="latin", raw=roman)
        assert severity_of(kw) == "high", roman


# ── _looks_encrypted entropy guard: a printable latin base64 token is NOT 'encrypted' ────────

def test_printable_base64_token_is_not_misread_as_encrypted():
    import base64
    # A base64 token whose decode is mostly-printable but NOT valid utf-8/gb18030 (a lone 0xFF
    # breaks both codecs) reaches the entropy guard: 60/61 printable bytes is far too readable to
    # be cbw.dat-style ciphertext, so it must NOT be misclassified as 'encrypted'.
    decoded = b"A" * 30 + b"\xff" + b"B" * 30
    blob = base64.b64encode(decoded)
    assert detect_format(blob) != "encrypted"
    # Real high-entropy ciphertext (uniform byte distribution, ~37% printable) still trips the
    # detector — the guard narrows false positives without weakening genuine ciphertext detection.
    cipher = base64.b64encode(bytes(range(256)) * 2)
    assert detect_format(cipher) == "encrypted"
