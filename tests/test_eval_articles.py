"""Publication invariants for evidence-bound eval analysis."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from core import eval_articles


ROOT = Path(__file__).resolve().parents[1]


def _article(collection, slug):
    return next(item for item in collection["articles"] if item["slug"] == slug)


def _sources_with_failed_prior():
    sources = copy.deepcopy(eval_articles.load_sources(root=ROOT))
    for model in sources["previous_full_sweep"]["models"].values():
        model["controls_clean"] = True
    failed = copy.deepcopy(sources["previous_full_sweep"])
    model = failed["models"]["mistralai/mistral-nemo"]
    model["controls_clean"] = False
    sources["previous_failed_full_sweep"] = failed
    return sources


def test_current_eval_collection_is_sealed_cited_and_publishable():
    collection = eval_articles.build(root=ROOT)

    eval_articles.validate_collection(collection)
    assert collection["n_articles"] == len(collection["articles"]) == 2
    assert collection["publication_policy"]["freeform_model_generation"] == "prohibited"
    for article in collection["articles"]:
        receipt = article["evaluation_receipt"]
        assert receipt["publishable"] is True
        assert receipt["citation_coverage"] == 1.0
        assert receipt["sealed_run_count"] == 4
        assert all(gate["passed"] for gate in receipt["gates"])
        assert article["authorship"]["freeform_model_generation"] == "none"


@pytest.mark.parametrize(
    ("arm", "current_method", "prior_method", "required", "forbidden"),
    (
        (
            "full-sweep",
            4,
            4,
            (
                "The two full-sweep records are descriptively comparable",
                "does not assign the change to a model release, provider, or prompt-routing decision",
            ),
            ("not a like-for-like retest", "values are not directly comparable"),
        ),
        (
            "full-sweep",
            4,
            3,
            (
                "Method versions v3 and v4 differ",
                "values are not directly comparable",
                "do not establish a trend",
                "do not identify a model release, provider, or prompt-routing cause",
            ),
            ("Wording consistency moved from", "descriptively comparable"),
        ),
        (
            "canonical",
            4,
            3,
            (
                "A canonical-only current run is not a like-for-like recovery test",
                "not a like-for-like retest that cancels the failed full sweep",
            ),
            ("descriptively comparable", "Wording consistency moved from"),
        ),
    ),
)
def test_control_article_keeps_non_comparable_runs_apart(
    arm, current_method, prior_method, required, forbidden
):
    sources = _sources_with_failed_prior()
    sources["reading"]["arm"] = arm
    sources["reading"]["method_version"] = current_method
    sources["previous_failed_full_sweep"]["method_version"] = prior_method
    collection = eval_articles.build_collection(sources, prior={})
    article = _article(collection, "before-reading-the-score-read-the-controls")
    prose = json.dumps(article, ensure_ascii=False)

    assert article["title"] == "A clean run does not erase a failed one"
    assert "Mistral Nemo" in prose
    assert "full sweep" in prose
    current = next(row for row in article["evidence"] if "Latest Mistral Nemo" in row["label"])
    prior = next(row for row in article["evidence"] if "prior full sweep" in row["label"])
    assert current["value"]["arm"] == arm
    assert prior["value"]["controls_clean"] is False
    assert all(text in prose for text in required)
    assert all(text not in prose for text in forbidden)


def test_uncertainty_article_keeps_zero_denominator_and_interval_together():
    collection = eval_articles.build(root=ROOT)
    article = _article(collection, "zero-observed-is-not-zero-uncertainty")
    numbers = {item["label"]: item for item in article["key_numbers"]}
    prose = json.dumps(article, ensure_ascii=False)

    assert article["title"] == "Zero observed refusals is not zero uncertainty"
    assert numbers["refused families"]["value"] == "0"
    assert numbers["monitored non-control families"]["value"] == "34"
    assert numbers["95% upper interval bound"]["value"] == "10.2%"
    assert "zero-of-34" in prose
    assert "zero plausible event rate" in prose


def test_builder_is_stable_and_links_a_changed_revision():
    sources = _sources_with_failed_prior()
    first = eval_articles.build_collection(sources, prior={})
    repeated = eval_articles.build_collection(sources, prior=first)
    assert [item["revision_id"] for item in repeated["articles"]] == [
        item["revision_id"] for item in first["articles"]
    ]
    assert [item["previous_revision_id"] for item in repeated["articles"]] == [
        item["previous_revision_id"] for item in first["articles"]
    ]

    changed_sources = copy.deepcopy(sources)
    old_sweep = changed_sources["previous_failed_full_sweep"]
    old_sweep["models"]["mistralai/mistral-nemo"]["arm_refusal_rate_pct"] = 3.1
    changed_sources["raw"]["refusal-drift-history"] += b"\n"
    changed = eval_articles.build_collection(changed_sources, prior=first)
    old_article = _article(first, "before-reading-the-score-read-the-controls")
    new_article = _article(changed, "before-reading-the-score-read-the-controls")
    assert new_article["revision_id"] != old_article["revision_id"]
    assert new_article["previous_revision_id"] == old_article["revision_id"]
    assert new_article["published_at"] == old_article["published_at"]


def test_clean_nearest_sweep_does_not_hide_the_latest_failed_sweep():
    sources = _sources_with_failed_prior()

    nearest = sources["previous_full_sweep"]
    failed = sources["previous_failed_full_sweep"]
    assert nearest is not None
    assert failed is not None
    assert all(row["controls_clean"] for row in nearest["models"].values())
    assert any(row["controls_clean"] is False for row in failed["models"].values())

    article = _article(
        eval_articles.build_collection(sources, prior={}),
        "before-reading-the-score-read-the-controls",
    )
    prior = next(row for row in article["evidence"] if "prior full sweep" in row["label"])
    assert "with failed controls" in prior["label"]
    assert prior["value"]["controls_clean"] is False


def test_broken_registry_and_metric_mismatch_fail_closed(tmp_path):
    readings = tmp_path / "readings"
    readings.mkdir()
    for name in (
        "refusal-drift-latest.json",
        "refusal-drift-history.jsonl",
        "eval-registry.jsonl",
    ):
        shutil.copyfile(ROOT / "readings" / name, readings / name)

    registry = readings / "eval-registry.jsonl"
    registry.write_bytes(registry.read_bytes() + b'{}\n')
    with pytest.raises(eval_articles.EvalArticleError, match="registry does not verify"):
        eval_articles.load_sources(root=tmp_path)

    shutil.copyfile(ROOT / "readings" / "eval-registry.jsonl", registry)
    latest = json.loads((readings / "refusal-drift-latest.json").read_text(encoding="utf-8"))
    latest["models"][0]["family_refusal_rate_pct"] += 0.1
    (readings / "refusal-drift-latest.json").write_text(
        json.dumps(latest), encoding="utf-8"
    )
    with pytest.raises(eval_articles.EvalArticleError, match="sealed metrics do not match"):
        eval_articles.load_sources(root=tmp_path)


def test_public_collection_contains_no_typographic_dash():
    raw = json.dumps(eval_articles.build(root=ROOT), ensure_ascii=False)
    assert "\u2013" not in raw
    assert "\u2014" not in raw
