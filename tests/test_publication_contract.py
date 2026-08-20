"""The publication contract: every reading must say when, from what, and out of what.

Palimpsest's promise is "nothing is published without evidence". `core/governance.py`
makes the *safety* rules executable; `core/claim_support.py` makes the *claim* arithmetic
executable. This file does the same for the publication boundary, which is the last place
a number can go wrong before a reader sees it.

The contract is three questions every published reading must answer:

  WHEN         a timestamp, so a stale feed is visible as stale
  FROM WHAT    provenance -- the source, method, or verification command behind it
  OUT OF WHAT  a denominator, so a numerator is never quotable on its own

The third is the one this project keeps relearning. "Zero deletions" means nothing
without the posts watched; a delisted-app count means nothing without the panel size;
203 diverging pools meant nothing without the 204 targets they were drawn from.

WHY THIS IS AN INVENTORY AND NOT A HEURISTIC. An earlier attempt guessed denominators
from field-name substrings and was wrong in both directions: it missed `citation` and
`verify_cmd` as provenance, and it demanded a denominator from `china-econ`, which
publishes benchmark levels and has no population to count. A test that cries wolf gets
suppressed, and a suppressed test protects nothing. So each signal *declares* its fields,
the declaration is checked against the actual file, and a signal with no denominator must
say why in writing. That is the same discipline as `_ALLOWED` in test_egress_policy.py.

THE RATCHET. A new reading that is not registered here fails the first test. That is the
point: it cannot reach the board until someone has answered all three questions for it.
"""

import glob
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.path.join(ROOT, "readings")


def _d(timestamp, provenance, denominator=None, *, reason=None):
    return {"timestamp": timestamp, "provenance": provenance,
            "denominator": denominator, "reason": reason}


# signal -> what it declares. `denominator=None` REQUIRES a written reason.
CONTRACT = {
    # ── measurements with a population ────────────────────────────────────────
    "app-storefront":       _d("generated_at", ["source", "scope"], "n_tracked"),
    "baike-redaction":      _d("generated_at", ["source", "method"], "n_comparable"),
    "cross-layer":          _d("generated_at", ["method"], "n_pairs_tested"),
    "ddti":                 _d("generated_at", ["citation"], "n_observations"),
    "forecast-ledger":      _d("generated_at", ["method"], "n_signals_scored"),
    "gdelt":                _d("generated_at", ["source", "scope"], "n_terms"),
    "github-refuge":        _d("generated_at", ["source", "scope"], "n_watched"),
    "inside-view":          _d("generated_at", ["source", "method"], "panel_size"),
    "net4people":           _d("generated_at", ["source", "method"], "n_recent"),
    "ooni-gfw":             _d(
        "generated_at", ["source", "method"], "n_completed_measurements"),
    "refusal-drift":        _d("generated_at", ["method", "verify_cmd"], "n_probes"),
    "wayback":              _d("generated_at", ["source", "scope"], "n_watched"),
    "weibo-hotsearch":      _d("generated_at", ["source", "method_note"], "board_entries"),
    "censored-planet":      _d("generated_at", ["source", "method"], "n_events"),
    "eval-registry":        _d("generated_at", ["registry", "verify_cmd"], "runs"),
    "eval-assurance":       _d(
        "generated_at", ["sources", "verify"],
        reason="a closed claim checklist rather than a sampled population: every check "
               "is enumerated in checks and the status counts are a complete projection "
               "over that declared checklist, so one top-level denominator would duplicate it."),
    "eval-journal":         _d("generated_at", ["source", "method", "scope"],
                               "n_articles"),
    "eval-articles":        _d(
        "generated_at", ["source", "scope", "publication_policy"], "n_articles"
    ),
    "gfi-transcripts":      _d(
        "generated_at", ["protocol", "probe_commitment", "verify_cmd"], "n_samples"
    ),
    "blocklist":            _d("generated_at", ["source", "attribution"], "n_versions"),
    "research-corpus":      _d("generated_at", ["source", "method", "scope"], "n_sources"),
    "undertext":            _d("generated_at", ["source", "method", "scope"], "n_observations"),
    "erasure-trail":        _d("generated_at", ["source", "method", "scope"], "n_rows"),
    "public-deletion-ledgers": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "official-first-seen": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "news-wire-live": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "archive-news-context": _d(
        "generated_at", ["source", "method", "scope"], "n_events_contextualized"
    ),
    "wikipedia-gazetteer-rc": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "baike-public-snapshot": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "public-hot-boards": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "telegram-public-channels": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "newsroom":             _d("generated_at", ["source", "method", "scope"], "n_stories"),
    "newswire":             _d("generated_at", ["source_registry", "method", "scope"],
                               "n_items"),
    "china-article-stream": _d(
        "generated_at", ["source_wire", "method", "scope"], "n_entries"
    ),
    "social-observations": _d(
        "generated_at", ["source_registry", "scope", "relation"], "n_observations"
    ),
    "china-situation": _d(
        "generated_at", ["inputs", "scope", "relation_policy"],
        reason="a deterministic join across distinct evidence layers: the complete event, "
               "publisher-report, social-observation, measurement-context and reviewed-"
               "Telegram counts are declared separately under coverage, so one top-level "
               "denominator would falsely imply that those populations are commensurate.",
    ),
    "china-censorship-analysis": _d(
        "generated_at", ["evidence", "methodology", "authorship"],
        reason="a cross-instrument analytical article whose cited measurements retain "
               "different populations and denominators; one top-level denominator "
               "would falsely imply that its network, content, and app counts are commensurate.",
    ),
    "dragon-whispers": _d(
        "generated_at", ["input_provenance", "method", "scope", "publication_policy"],
        "n_entries",
    ),
    "china-economic-pulse": _d("generated_at", ["source", "method", "scope"],
                               "n_metrics"),
    "china-econ-observations": _d(
        "generated_at", ["source", "method", "scope"], "n_observations"
    ),
    "china-index": _d("generated_at", ["source", "method", "scope"], "n_sources"),
    "china-econ-forecast": _d(
        "generated_at", ["source", "method", "scope", "snapshot", "configuration"],
        "n_targets",
    ),
    "investigations":       _d("generated_at", ["source", "method", "scope"],
                               "n_cases"),
    "machine-investigations": _d(
        "generated_at", ["source", "method", "scope"], "n_cases"
    ),
    "primary-documents":    _d("generated_at", ["source_registry", "method", "scope"],
                               "n_documents"),
    "corroboration":        _d("generated_at", ["source_inputs", "method", "scope"],
                               "n_events"),
    "network-rounds":       _d("generated_at", ["panel", "method", "scope"],
                               "n_rounds"),
    "source-workflow":      _d("generated_at", ["method", "scope"], "n_records"),
    "evidence-mesh": _d(
        "generated_at", ["source", "method", "scope"],
        reason="an inventory roll-up rather than a sampled measurement: every resource "
               "is enumerated in resources, while summary publishes the complete resource "
               "count and availability-state counts over that same declared inventory."),
    # scheduled first-party import from the fixed external prober
    "bleedthrough":         _d("generated_at", ["method", "scope", "provenance"],
                               "vantages_probed"),
    "data-darkness":        _d("generated_at", ["source", "method_note"],
                               "n_series_watched"),
    "silence-index":        _d("generated_at", ["source", "method_note"],
                               "n_topics_considered"),
    "believability":        _d("generated_at", ["source", "method_note"],
                               "n_components_required"),

    # ── readings with no population to count, each with its reason ────────────
    "china-econ": _d(
        "generated_at", ["source"],
        reason="publishes official CFETS benchmark LEVELS, not a count over a sample; "
               "there is no population to be a denominator of."),
    "stock-connect": _d(
        "generated_at", ["source"],
        reason="publishes the HKEX daily flow print, a single official value per day; "
               "a denominator would be invented, not measured."),
    "cny-fix-gap": _d(
        "generated_at", ["source", "method_note"],
        reason="publishes one derived daily divergence between two official prices "
               "(the PBOC fix and an independent reference rate); there is no sample "
               "and no population to be a denominator of."),
    "circumvention-demand": _d(
        "generated_at", ["source", "method_note"],
        reason="publishes Tor bridge-user levels from the Tor metrics series; the value "
               "is an estimated count of users, not a fraction of a sample we drew."),
    "ioda-outages": _d(
        "generated_at", ["source", "method_note"],
        reason="reports discrete outage events from three external instruments; the "
               "instrument count is published as instruments_firing, and there is no "
               "population of non-events to divide by."),
    "anchors": _d(
        "ts", ["registry_root", "erasure_root"],
        reason="anchoring infrastructure rather than a measurement: it records which "
               "roots were witnessed where, and witnesses are enumerated, not sampled."),
    "editorial-readiness": _d(
        "generated_at", ["source_inputs", "method", "scope"],
        reason="a gate spans different declared populations: wire dossiers are counted "
               "inside wire, while explainers and investigations are enumerated in "
               "packages and summary; one top-level denominator would conflate them."),

    # ── roll-ups over other signals: their denominator is the layer set ───────
    "erasure-observatory": _d(
        "generated_at", ["thesis", "index_scale"],
        reason="a roll-up across erasure layers; its inputs are the layer readings "
               "themselves, enumerated in layers_contributing rather than sampled."),
    "board-alarm": _d(
        "generated_at", ["method", "board_guarantee"],
        reason="merges the per-signal e-detectors; the signals it merged are enumerated "
               "in the signals container, and it draws no sample of its own."),
    "event-flags": _d(
        "generated_at", ["method", "guarantee"],
        reason="per-signal conformal detector; each signal is compared against its own "
               "history, so the population is that signal's history, not a cross-section."),
    "coverage-guard": _d(
        "generated_at", ["method"],
        reason="audits the other signals' coverage; the audited set is enumerated in the "
               "signals container rather than sampled from a larger population."),
    "vantage-fusion": _d(
        "generated_at", ["method"],
        reason="fuses the network vantages that reported; the contributing and excluded "
               "vantages are both enumerated, so a ratio would double-count them."),
    "osint-china": _d(
        "generated_at", ["source", "method", "scope"], "n_signals_total"),
    "nemesis": _d(
        "generated_at", ["source", "method", "scope"],
        reason="an allowlisted operational snapshot whose observed sources and component "
               "counts are explicitly enumerated in coverage and counts; it is not one "
               "ratio drawn from a larger sampled population."),
    "in-path-interference": _d(
        "generated_at", ["source", "method"],
        reason="indices are measurement-weighted means over OONI's published aggregate; "
               "the per-test denominators live inside the tests container, and a single "
               "top-level count would misrepresent a weighted mean."),
    "apple-censorship": _d(
        "generated_at", ["source", "scope"],
        reason="unavailable_pct is computed against the same catalogue measured in peer "
               "storefronts; the comparison set is enumerated in the country container "
               "rather than drawn as a sample."),
}


def _readings():
    return sorted(glob.glob(os.path.join(READINGS, "*-latest.json")))


def _name(path):
    return os.path.basename(path).replace("-latest.json", "")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_every_published_reading_is_registered():
    """A new feed cannot reach the board without answering the three questions."""
    unregistered = [_name(p) for p in _readings() if _name(p) not in CONTRACT]
    assert not unregistered, (
        "these readings publish without a declared publication contract. Add each to "
        "CONTRACT in this test with its timestamp field, its provenance field(s), and "
        "either its denominator or an honest written reason it has none:\n  "
        + "\n  ".join(unregistered))


# Registered, but not publishing yet: a signal that is built and whose contract is agreed
# before its first reading lands. Keeping these here means the first live round cannot break
# the build, and — more usefully — means the three questions were answered while the code was
# being written rather than retrofitted once a number was already on the board.
PENDING = {
    "public-deletion-ledgers",
    "official-first-seen",
    "news-wire-live",
    "wikipedia-gazetteer-rc",
    "baike-public-snapshot",
    "public-hot-boards",
    "telegram-public-channels",
}

# External public products whose contract is agreed here but whose presence is deliberately
# deployment-dependent. Unlike PENDING, these do not "graduate": Nemesis remains optional so a
# repository with no NEMESIS_SNAPSHOT_URL has an honest missing layer rather than a fake zero.
OPTIONAL_EXTERNAL = {
    "nemesis",
}

# Recurring publication jobs whose contract ships with the collector before the first
# successful scheduled round. Unlike PENDING, these feeds are production-ready and may
# already exist; unlike OPTIONAL_EXTERNAL, they are first-party publications rather than
# deployment-specific imports. The exception can be removed once the first row is part of
# every supported checkout, but the contract is enforced immediately in the publishing run.
SCHEDULED_PUBLICATIONS = {
    "bleedthrough",
    "china-economic-pulse",
    "china-censorship-analysis",
    "china-econ-observations",
    "china-index",
    "evidence-mesh",
    "investigations",
    "machine-investigations",
    "newswire",
    "newsroom",
    "dragon-whispers",
    "primary-documents",
    "corroboration",
    "network-rounds",
    "source-workflow",
    "editorial-readiness",
    "eval-articles",
    "research-corpus",
    "social-observations",
    "china-situation",
    "archive-news-context",
}


def test_no_contract_entry_describes_a_reading_that_does_not_exist():
    """Keeps the inventory honest as signals are retired, without punishing signals whose
    contract was agreed before their first round."""
    present = {_name(p) for p in _readings()}
    stale = sorted(
        set(CONTRACT) - present - PENDING - OPTIONAL_EXTERNAL - SCHEDULED_PUBLICATIONS
    )
    assert not stale, (
        f"CONTRACT describes readings that no longer exist: {stale}. Retire the entry, or "
        "move it to PENDING if the signal is built but not publishing yet.")


def test_pending_signals_are_registered_and_do_not_linger_silently():
    """A pending signal must still declare its contract, and must leave PENDING once it
    publishes, so PENDING cannot become a permanent parking space."""
    unregistered = sorted(PENDING - set(CONTRACT))
    assert not unregistered, f"PENDING lists signals with no declared contract: {unregistered}"
    published = {_name(p) for p in _readings()}
    graduated = sorted(PENDING & published)
    assert not graduated, (
        f"these signals now publish and must be removed from PENDING: {graduated}")


def test_optional_external_signals_are_pre_registered_not_silently_required():
    """An optional deployment feed still agrees its contract before its first import."""
    unregistered = sorted(OPTIONAL_EXTERNAL - set(CONTRACT))
    assert not unregistered, f"optional external signals have no contract: {unregistered}"
    assert not (OPTIONAL_EXTERNAL & PENDING), (
        "deployment-dependent signals must not also use one-time PENDING graduation semantics")


def test_scheduled_publications_are_registered_and_have_distinct_semantics():
    """A first-party scheduled feed is checked whether or not its first round has landed."""
    unregistered = sorted(SCHEDULED_PUBLICATIONS - set(CONTRACT))
    assert not unregistered, f"scheduled publications have no contract: {unregistered}"
    assert not (SCHEDULED_PUBLICATIONS & PENDING), (
        "production scheduled publications must not be marked as unfinished")
    assert not (SCHEDULED_PUBLICATIONS & OPTIONAL_EXTERNAL), (
        "first-party scheduled publications must not be marked as optional imports")


def test_archive_news_context_contract_is_scheduled_and_context_only():
    assert CONTRACT["archive-news-context"] == {
        "timestamp": "generated_at",
        "provenance": ["source", "method", "scope"],
        "denominator": "n_events_contextualized",
        "reason": None,
    }
    assert "archive-news-context" in SCHEDULED_PUBLICATIONS
    assert "archive-news-context" not in PENDING


def test_newsroom_contract_keeps_provenance_and_story_denominator_explicit():
    assert CONTRACT["newsroom"] == {
        "timestamp": "generated_at",
        "provenance": ["source", "method", "scope"],
        "denominator": "n_stories",
        "reason": None,
    }
    assert "newsroom" in SCHEDULED_PUBLICATIONS


def test_gfi_transcript_contract_exposes_the_complete_sample_denominator():
    assert CONTRACT["gfi-transcripts"] == {
        "timestamp": "generated_at",
        "provenance": ["protocol", "probe_commitment", "verify_cmd"],
        "denominator": "n_samples",
        "reason": None,
    }


def test_investigations_contract_keeps_cases_and_review_boundary_explicit():
    assert CONTRACT["investigations"] == {
        "timestamp": "generated_at",
        "provenance": ["source", "method", "scope"],
        "denominator": "n_cases",
        "reason": None,
    }
    assert "investigations" in SCHEDULED_PUBLICATIONS


def test_machine_analysis_contract_keeps_cases_and_provenance_explicit():
    assert CONTRACT["machine-investigations"] == {
        "timestamp": "generated_at",
        "provenance": ["source", "method", "scope"],
        "denominator": "n_cases",
        "reason": None,
    }
    assert "machine-investigations" in SCHEDULED_PUBLICATIONS
    assert "evidence-mesh" in SCHEDULED_PUBLICATIONS


def test_bleedthrough_graduated_to_a_scheduled_fixed_origin_import():
    assert CONTRACT["bleedthrough"] == {
        "timestamp": "generated_at",
        "provenance": ["method", "scope", "provenance"],
        "denominator": "vantages_probed",
        "reason": None,
    }
    assert "bleedthrough" not in PENDING
    assert "bleedthrough" in SCHEDULED_PUBLICATIONS


def test_reporting_newsroom_feeds_have_explicit_publication_contracts():
    assert CONTRACT["primary-documents"]["denominator"] == "n_documents"
    assert CONTRACT["corroboration"]["denominator"] == "n_events"
    assert CONTRACT["network-rounds"]["denominator"] == "n_rounds"
    assert CONTRACT["source-workflow"]["denominator"] == "n_records"
    assert CONTRACT["editorial-readiness"]["denominator"] is None
    assert {
        "primary-documents",
        "corroboration",
        "network-rounds",
        "source-workflow",
        "editorial-readiness",
    } <= SCHEDULED_PUBLICATIONS


def test_social_and_situation_contracts_preserve_distinct_evidence_populations():
    assert CONTRACT["social-observations"] == {
        "timestamp": "generated_at",
        "provenance": ["source_registry", "scope", "relation"],
        "denominator": "n_observations",
        "reason": None,
    }
    assert CONTRACT["china-situation"]["provenance"] == [
        "inputs", "scope", "relation_policy"
    ]
    assert CONTRACT["china-situation"]["denominator"] is None
    assert "distinct evidence layers" in CONTRACT["china-situation"]["reason"]
    assert {"social-observations", "china-situation"} <= SCHEDULED_PUBLICATIONS

    social = _load(
        os.path.join(ROOT, "readings", "social-observations-latest.json")
    )
    situation = _load(
        os.path.join(ROOT, "readings", "china-situation-latest.json")
    )
    assert social["n_observations"] == len(social["observations"])
    assert social["relation"] == "attributed-source-report-not-corroboration"
    assert situation["coverage"]["in_scope_events"] == len(
        situation["situations"]
    )
    assert "without converting social circulation" in situation["relation_policy"]


def test_newsroom_discovery_and_live_json_cache_policy_are_explicit():
    sitemap = open(os.path.join(ROOT, "sitemap.xml"), encoding="utf-8").read()
    news_sitemap = open(
        os.path.join(ROOT, "news", "sitemap.xml"), encoding="utf-8"
    ).read()
    robots = open(os.path.join(ROOT, "robots.txt"), encoding="utf-8").read()
    llms = open(os.path.join(ROOT, "llms.txt"), encoding="utf-8").read()
    worker = open(os.path.join(ROOT, "sw.js"), encoding="utf-8").read()

    assert "https://palimpsest.info/news/" in sitemap
    assert "https://palimpsest.info/news/" in llms
    assert "https://palimpsest.info/readings/newsroom-latest.json" in llms
    assert "https://palimpsest.info/news/investigations/" in sitemap
    assert "https://palimpsest.info/news/investigations/" in llms
    assert "https://palimpsest.info/readings/investigations-latest.json" in llms
    assert "https://palimpsest.info/news/analysis/" in sitemap
    assert "https://palimpsest.info/news/analysis/" in llms
    assert "https://palimpsest.info/readings/evidence-mesh-latest.json" in llms
    assert "https://palimpsest.info/readings/machine-investigations-latest.json" in llms
    assert "https://palimpsest.info/news/standards/" in sitemap
    assert "https://palimpsest.info/news/standards/" in llms
    assert "https://palimpsest.info/china/" in sitemap
    assert "https://palimpsest.info/china/" in llms
    assert "https://palimpsest.info/readings/china-index-latest.json" in llms
    assert "https://palimpsest.info/protocol/china-index-v1.schema.json" in llms
    assert "https://palimpsest.info/readings/china-econ-observations.jsonl" in llms
    assert "https://palimpsest.info/readings/china-econ-forecast-latest.json" in llms
    assert "https://palimpsest.info/protocol/economic-forecast-v1.schema.json" in llms
    assert "https://palimpsest.info/news/china/situation/" in sitemap
    assert "https://palimpsest.info/news/china/situation/" in news_sitemap
    for url in (
        "https://palimpsest.info/news/china/situation/",
        "https://palimpsest.info/news/china/situation/feed.json",
        "https://palimpsest.info/news/china/situation/feed.xml",
        "https://palimpsest.info/readings/china-situation-latest.json",
        "https://palimpsest.info/readings/social-observations-latest.json",
        "https://palimpsest.info/readings/social-observations-versions.jsonl",
        "https://palimpsest.info/protocol/china-situation-v1.schema.json",
        "https://palimpsest.info/protocol/social-observations-v1.schema.json",
    ):
        assert url in llms
    assert robots.splitlines().count("Sitemap: https://palimpsest.info/sitemap.xml") == 1
    assert robots.splitlines().count(
        "Sitemap: https://palimpsest.info/news/sitemap.xml"
    ) == 1
    assert robots.splitlines().count(
        "Sitemap: https://palimpsest.info/china/sitemap.xml"
    ) == 1

    assert 'const CACHE = "palimpsest-v16"' in worker
    assert 'const LIVE_NEWSROOM = "/readings/newsroom-latest.json"' in worker
    assert '"/readings/gfi-transcripts-latest.json"' in worker
    for endpoint in (
        "/news/feed.json",
        "/news/feed.xml",
        "/news/instruments/feed.json",
        "/news/instruments/feed.xml",
        "/news/china/feed.json",
        "/news/china/feed.xml",
        "/news/china/analysis/feed.json",
        "/news/china/analysis/feed.xml",
        "/news/china/situation/feed.json",
        "/news/china/situation/feed.xml",
        "/news/china/whispers/feed.json",
        "/news/china/whispers/feed.xml",
    ):
        assert f'"{endpoint}"' in worker
    assert "if (url.pathname === LIVE_NEWSROOM)" in worker
    newsroom_branch = worker[worker.index("if (url.pathname === LIVE_NEWSROOM)"):]
    newsroom_branch = newsroom_branch[:newsroom_branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in newsroom_branch
    assert "caches.match" not in newsroom_branch

    syndication_branch = worker[
        worker.index("if (LIVE_NEWSROOM_SYNDICATION.has(url.pathname))"):
    ]
    syndication_branch = syndication_branch[:syndication_branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in syndication_branch
    assert "caches.match" not in syndication_branch

    assert '"/readings/investigations-latest.json"' in worker
    assert '"/readings/evidence-mesh-latest.json"' in worker
    assert '"/readings/machine-investigations-latest.json"' in worker
    assert '"/readings/china-econ-observations-latest.json"' in worker
    assert '"/readings/china-econ-observations.jsonl"' in worker
    assert '"/readings/china-econ-forecast-latest.json"' in worker
    assert '"/readings/china-index-latest.json"' in worker
    assert '"/readings/china-situation-latest.json"' in worker
    assert '"/readings/social-observations-latest.json"' in worker
    assert '"/readings/social-observations-versions.jsonl"' in worker
    for name in (
        "primary-documents",
        "corroboration",
        "network-rounds",
        "source-workflow",
        "editorial-readiness",
    ):
        assert f'"/readings/{name}-latest.json"' in worker
    evidence_branch = worker[
        worker.index("if (LIVE_EVIDENCE_READINGS.has(url.pathname))"):
    ]
    evidence_branch = evidence_branch[:evidence_branch.index("return;")]
    assert 'fetch(req, { cache: "no-store" })' in evidence_branch
    assert "caches.match" not in evidence_branch
    assert "if (LIVE_INVESTIGATION_CASE.test(url.pathname))" in worker
    assert "if (LIVE_MACHINE_ANALYSIS_REPORT.test(url.pathname))" in worker
    assert "if (LIVE_EVENT_ANALYSIS.test(url.pathname))" in worker
    event_analysis_branch = worker[
        worker.index("if (LIVE_EVENT_ANALYSIS.test(url.pathname))"):
    ]
    event_analysis_branch = event_analysis_branch[
        :event_analysis_branch.index("return;")
    ]
    assert 'fetch(req, { cache: "no-store" })' in event_analysis_branch
    assert "caches.match" not in event_analysis_branch


def test_openapi_publishes_a_concrete_newsroom_feed_contract():
    spec = _load(os.path.join(ROOT, "openapi.json"))
    protocol = _load(os.path.join(ROOT, "protocol", "news-feed-v1.schema.json"))
    operation = spec["paths"]["/readings/newsroom-latest.json"]["get"]
    assert operation["operationId"] == "getNewsroomFeed"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/NewsroomFeed"
    }

    schemas = spec["components"]["schemas"]
    feed = schemas["NewsroomFeed"]
    assert feed["additionalProperties"] is False
    assert feed["properties"]["schema_version"]["const"] == "palimpsest-news.v1"
    assert set(feed["required"]) == {
        "schema_version", "feed_id", "title", "headline", "url", "generated_at",
        "n_stories", "source", "source_commit", "method", "scope", "coverage",
        "sections", "stories",
    }
    assert feed["properties"]["stories"]["items"] == {
        "$ref": "#/components/schemas/NewsroomStory"
    }
    assert set(feed["required"]) == set(protocol["required"])
    assert set(feed["properties"]) == set(protocol["properties"])

    story = schemas["NewsroomStory"]
    assert story["additionalProperties"] is False
    assert set(story["properties"]["status"]["enum"]) == {
        "live", "degraded", "stale", "missing", "corrupt"
    }
    assert story["properties"]["claims"]["items"] == {
        "$ref": "#/components/schemas/NewsroomClaim"
    }
    assert set(story["required"]) == set(protocol["$defs"]["story"]["required"])
    assert set(story["properties"]) == set(
        protocol["$defs"]["story"]["properties"]
    )
    for name in (
        "NewsroomDenominator", "NewsroomMetric", "NewsroomClaim",
        "NewsroomEvidenceInput", "NewsroomEvidence", "NewsroomMethod",
        "NewsroomSection", "NewsroomCoverageCounts", "NewsroomCoverage",
    ):
        assert schemas[name]["additionalProperties"] is False


def test_openapi_publishes_the_external_investigations_contract():
    spec = _load(os.path.join(ROOT, "openapi.json"))
    protocol = _load(
        os.path.join(ROOT, "protocol", "investigations-v1.schema.json")
    )

    assert spec["components"]["schemas"]["Investigations"] == {
        "$ref": "https://palimpsest.info/protocol/investigations-v1.schema.json"
    }
    response = spec["components"]["responses"]["Investigations"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Investigations"
    }
    operation = spec["paths"]["/readings/investigations-latest.json"]["get"]
    assert operation["operationId"] == "getInvestigations"
    assert operation["responses"]["200"] == {
        "$ref": "#/components/responses/Investigations"
    }
    assert protocol["$id"] == (
        "https://palimpsest.info/protocol/investigations-v1.schema.json"
    )
    assert protocol["additionalProperties"] is False
    assert set(protocol["required"]) == {
        "schema_version", "desk_id", "generated_at", "source", "method", "scope",
        "publication_policy", "input_integrity", "n_cases", "cases",
    }


def test_openapi_discovers_the_evidence_mesh_and_machine_analysis_contracts():
    spec = _load(os.path.join(ROOT, "openapi.json"))
    expected = {
        "EvidenceMesh": (
            "evidence-mesh-v1.schema.json",
            "/readings/evidence-mesh-latest.json",
            "getEvidenceMesh",
        ),
        "MachineInvestigations": (
            "machine-investigations-v1.schema.json",
            "/readings/machine-investigations-latest.json",
            "getMachineInvestigations",
        ),
    }
    for name, (schema_name, path, operation_id) in expected.items():
        assert spec["components"]["schemas"][name] == {
            "$ref": f"https://palimpsest.info/protocol/{schema_name}"
        }
        assert spec["components"]["responses"][name]["content"][
            "application/json"
        ]["schema"] == {"$ref": f"#/components/schemas/{name}"}
        operation = spec["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert operation["responses"]["200"] == {
            "$ref": f"#/components/responses/{name}"
        }


def test_openapi_publishes_social_observations_and_china_situation_contracts():
    spec = _load(os.path.join(ROOT, "openapi.json"))
    expected = {
        "SocialObservations": (
            "social-observations-v1.schema.json",
            "/readings/social-observations-latest.json",
            "getSocialObservations",
        ),
        "ChinaSituation": (
            "china-situation-v1.schema.json",
            "/readings/china-situation-latest.json",
            "getChinaSituation",
        ),
    }

    for name, (schema_name, path, operation_id) in expected.items():
        protocol = _load(os.path.join(ROOT, "protocol", schema_name))
        assert protocol["$id"] == f"https://palimpsest.info/protocol/{schema_name}"
        assert protocol["additionalProperties"] is False
        assert spec["components"]["schemas"][name] == {
            "$ref": protocol["$id"]
        }
        assert spec["components"]["responses"][name]["content"][
            "application/json"
        ]["schema"] == {"$ref": f"#/components/schemas/{name}"}
        operation = spec["paths"][path]["get"]
        assert operation["operationId"] == operation_id
        assert operation["responses"]["200"] == {
            "$ref": f"#/components/responses/{name}"
        }


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_reading_declares_when_it_was_measured(path):
    d, c = _load(path), CONTRACT[_name(path)]
    assert c["timestamp"] in d, (
        f"{_name(path)} declares timestamp '{c['timestamp']}', which is not in the file")
    assert d[c["timestamp"]], f"{_name(path)} has an empty timestamp"


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_reading_declares_where_it_came_from(path):
    d, c = _load(path), CONTRACT[_name(path)]
    missing = [f for f in c["provenance"] if f not in d or d[f] in (None, "", [], {})]
    assert not missing, (
        f"{_name(path)} declares provenance {c['provenance']} but these are absent or "
        f"empty: {missing}. A reader cannot check a number whose origin is not stated.")


@pytest.mark.parametrize("path", _readings(), ids=_name)
def test_a_count_is_never_published_without_its_denominator(path):
    d, c = _load(path), CONTRACT[_name(path)]
    if c["denominator"] is None:
        assert c["reason"] and len(c["reason"]) > 40, (
            f"{_name(path)} declares no denominator, so it must say why in writing. "
            "An omission and a considered exemption look identical without one.")
        return
    assert c["denominator"] in d, (
        f"{_name(path)} declares denominator '{c['denominator']}', which is not in the file")
    v = d[c["denominator"]]
    assert isinstance(v, int), (
        f"{_name(path)} denominator '{c['denominator']}' is {type(v).__name__}, not an int")
    assert v >= 0, f"{_name(path)} denominator '{c['denominator']}' is negative: {v}"
