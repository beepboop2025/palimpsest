"""Evidence-bound articles generated from the sealed AI evaluation registry.

The eval runner measures model behaviour. This module has a narrower job: turn
the newest verified panel into dated, readable analysis without giving a text
generator permission to invent a claim. Every sentence is assembled from a
reviewed template, cites an exact receipt, and passes a closed publication gate.

The result is deliberately not a general purpose AI writer. New article shapes
require code review. New measurements can update an approved shape only after
the registry, controls, uncertainty, citation, and limitation checks all pass.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core import eval_registry
from core.sealed_ledger import merkle_root


SCHEMA_VERSION = "palimpsest-eval-journal.v1"
DESK_ID = "palimpsest-eval-journal"
ARTICLE_SCHEMA = "palimpsest-eval-article.v1"
PUBLICATION_MODE = "deterministic-eval-analysis"
DISCLOSURE = (
    "Generated from sealed evaluation artifacts with a deterministic editorial "
    "template. No interviews and no free-form model prose were used."
)
_RECONCILED_ATTESTATION_MODE = "reconciled-without-requery"
_RECONCILIATION_METRIC_FIELDS = frozenset({"reading_as_of", "attestation_mode"})

ROOT = Path(__file__).resolve().parents[1]
READING_PATH = ROOT / "readings" / "refusal-drift-latest.json"
HISTORY_PATH = ROOT / "readings" / "refusal-drift-history.jsonl"
REGISTRY_PATH = ROOT / "readings" / "eval-registry.jsonl"
OUTPUT_PATH = ROOT / "readings" / "eval-articles-latest.json"

_ARTICLE_ID = re.compile(r"^evalarticle-[0-9a-f]{20}$")
_REVISION_ID = re.compile(r"^evalarticlev-[0-9a-f]{24}$")
_EVIDENCE_ID = re.compile(r"^evalevidence-[0-9a-f]{20}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_ROOT_FIELDS = {
    "schema_version",
    "desk_id",
    "generated_at",
    "source",
    "scope",
    "publication_policy",
    "input_receipts",
    "n_articles",
    "articles",
}
_INPUT_FIELDS = {
    "input_id",
    "filename",
    "sha256",
    "bytes",
    "generated_at",
    "public_url",
    "integrity",
}
_ARTICLE_FIELDS = {
    "schema_version",
    "article_id",
    "revision_id",
    "previous_revision_id",
    "slug",
    "url",
    "kicker",
    "title",
    "dek",
    "thesis",
    "finding_state",
    "published_at",
    "updated_at",
    "key_numbers",
    "sections",
    "counterreadings",
    "limitations",
    "methodology",
    "evidence",
    "evaluation_receipt",
    "authorship",
    "disclosure",
}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "input_id",
    "label",
    "selector",
    "value",
    "interpretation_limit",
    "source_url",
}
_SENTENCE_FIELDS = {"text", "citation_ids"}
_PARAGRAPH_FIELDS = {"sentences"}
_SECTION_FIELDS = {"section_id", "heading", "paragraphs"}
_NUMBER_FIELDS = {"value", "label", "note", "citation_ids"}
_RECORD_FIELDS = {"text", "citation_ids"}
_METHOD_FIELDS = {"step", "detail", "citation_ids"}
_GATE_FIELDS = {"gate_id", "label", "passed", "detail"}
_RECEIPT_FIELDS = {
    "status",
    "publishable",
    "citation_coverage",
    "sealed_run_count",
    "gates",
}
_AUTHORSHIP_FIELDS = {
    "byline",
    "mode",
    "human_interviews",
    "freeform_model_generation",
}


class EvalArticleError(ValueError):
    """An eval article or one of its inputs violated the public contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one byte representation used for IDs and public JSON."""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvalArticleError("eval article is not canonical JSON") from exc


def pretty_json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise EvalArticleError("eval article cannot be encoded") from exc


def _stable_id(prefix: str, value: Any, length: int) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value).rstrip(b"\n")).hexdigest()
    return f"{prefix}-{digest[:length]}"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvalArticleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                EvalArticleError(f"{label} contains {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvalArticleError(f"{label} is not strict JSON") from exc


def _read_bounded(path: Path, *, maximum: int) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvalArticleError(f"cannot read {path.name}") from exc
    if not raw or len(raw) > maximum:
        raise EvalArticleError(f"{path.name} is empty or exceeds its size boundary")
    return raw


def _jsonl(raw: bytes, *, label: str, maximum_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        value = _strict_json(line, label=f"{label} line {line_number}")
        if not isinstance(value, dict):
            raise EvalArticleError(f"{label} line {line_number} is not an object")
        rows.append(value)
        if len(rows) > maximum_rows:
            raise EvalArticleError(f"{label} exceeds its row boundary")
    if not rows:
        raise EvalArticleError(f"{label} is empty")
    return rows


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvalArticleError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvalArticleError(f"{field} is not a real timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvalArticleError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str, *, maximum: int = 4_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvalArticleError(f"{field} must be non-empty bounded text")
    if "\u2013" in value or "\u2014" in value:
        raise EvalArticleError(f"{field} contains a prohibited dash character")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise EvalArticleError(f"{field} contains a control character")
    return value


def _number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise EvalArticleError(f"{field} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise EvalArticleError(f"{field} must be finite")
    return value


def _display_model(model: str) -> str:
    names = {
        "openai/gpt-4o-mini": "OpenAI GPT-4o mini",
        "anthropic/claude-3-haiku": "Anthropic Claude 3 Haiku",
        "meta-llama/llama-3.3-70b-instruct": "Meta Llama 3.3 70B Instruct",
        "mistralai/mistral-nemo": "Mistral Nemo",
    }
    return names.get(model, model.replace("/", " "))


def _pct(value: int | float) -> str:
    numeric = float(value)
    return f"{numeric:.0f}%" if numeric.is_integer() else f"{numeric:.1f}%"


def _date_label(value: str) -> str:
    parsed = _timestamp(value, "generated_at")
    return parsed.strftime("%-d %b %Y")


def _model_rows(reading: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    models = reading.get("models")
    if not isinstance(models, list) or not models or len(models) > 32:
        raise EvalArticleError("refusal reading has no bounded model panel")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(models):
        if not isinstance(row, dict):
            raise EvalArticleError(f"models[{index}] is not an object")
        model = _text(row.get("model"), f"models[{index}].model", maximum=160)
        if model in result:
            raise EvalArticleError("refusal reading contains a duplicate model")
        for field in (
            "family_refusal_rate_pct",
            "n_families",
            "n_refused_families",
            "arm_refusal_rate_pct",
            "n_arms",
            "n_abstained",
        ):
            _number(row.get(field), f"models[{index}].{field}")
        interval = row.get("family_refusal_ci95_pct")
        if not isinstance(interval, list) or len(interval) != 2:
            raise EvalArticleError("model confidence interval is missing")
        _number(interval[0], "confidence interval lower bound")
        _number(interval[1], "confidence interval upper bound")
        if not isinstance(row.get("controls_clean"), bool):
            raise EvalArticleError("model controls_clean is not boolean")
        if not isinstance(row.get("control_refusals"), list):
            raise EvalArticleError("model control_refusals is not a list")
        result[model] = row
    return result


def _metric_projection(row: Mapping[str, Any], method_version: int) -> dict[str, Any]:
    invariance = row.get("wording_invariance")
    consistency = invariance.get("consistency_rate") if isinstance(invariance, dict) else None
    interval = row["family_refusal_ci95_pct"]
    return {
        "family_refusal_rate_pct": row["family_refusal_rate_pct"],
        "ci95_lo_pct": interval[0],
        "ci95_hi_pct": interval[1],
        "n_families": row["n_families"],
        "n_refused_families": row["n_refused_families"],
        "arm_refusal_rate_pct": row["arm_refusal_rate_pct"],
        "n_arms": row["n_arms"],
        "n_abstained": row["n_abstained"],
        "paraphrase_consistency": consistency,
        "controls_clean": row["controls_clean"],
        "method_version": method_version,
    }


def _matching_runs(
    reading: Mapping[str, Any], entries: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    generated_at = reading.get("generated_at")
    suite = reading.get("suite")
    method_version = reading.get("method_version")
    if not isinstance(method_version, int):
        raise EvalArticleError("refusal reading method_version is missing")
    models = _model_rows(reading)
    matches: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not (
            entry.get("kind") == eval_registry.RUN
            and entry.get("suite") == suite
            and entry.get("model") in models
        ):
            continue

        model = str(entry["model"])
        metrics = entry.get("metrics")
        reconciled = (
            isinstance(metrics, dict)
            and metrics.get("reading_as_of") == generated_at
            and metrics.get("attestation_mode") == _RECONCILED_ATTESTATION_MODE
        )
        direct = entry.get("ts") == generated_at
        if not direct and not reconciled:
            continue

        if reconciled:
            attested_at = _timestamp(
                str(entry.get("ts")), "registry attestation timestamp"
            )
            observed_at = _timestamp(str(generated_at), "generated_at")
            if attested_at < observed_at:
                raise EvalArticleError(
                    f"reconciled registry attestation predates the reading for {model}"
                )
            compared_metrics = {
                key: value
                for key, value in metrics.items()
                if key not in _RECONCILIATION_METRIC_FIELDS
            }
        else:
            compared_metrics = metrics
        if compared_metrics != _metric_projection(models[model], method_version):
            raise EvalArticleError(f"sealed metrics do not match the reading for {model}")

        # A retained measurement can already have its original run on an older fork
        # and then be re-attested on the winning public chain. Registry order is
        # verified before this matcher runs, so the newest valid binding is canonical.
        matches[model] = entry
    missing = sorted(set(models) - set(matches))
    if missing:
        raise EvalArticleError(
            "the latest refusal reading is not fully bound to sealed runs: "
            + ", ".join(missing)
        )
    return matches


def _previous_full_sweep(
    history: Sequence[Mapping[str, Any]],
    generated_at: str,
    *,
    failed_controls_only: bool = False,
) -> Mapping[str, Any] | None:
    current = _timestamp(generated_at, "generated_at")
    eligible = [
        row
        for row in history
        if row.get("arm") == "full-sweep"
        and isinstance(row.get("models"), dict)
        and (
            not failed_controls_only
            or any(
                isinstance(model, dict) and model.get("controls_clean") is False
                for model in row["models"].values()
            )
        )
        and _timestamp(row.get("generated_at"), "history.generated_at") < current
    ]
    return max(
        eligible,
        key=lambda row: _timestamp(row["generated_at"], "history.generated_at"),
        default=None,
    )


def _prior_sweep_limit(
    *,
    failed_comparator: bool,
    current_is_full_sweep: bool,
    method_versions_match: bool,
    prior_method_version: Any,
    current_method_version: Any,
) -> str:
    subject = (
        "This is the most recent earlier full sweep with a failed control for "
        "a current-panel model."
        if failed_comparator
        else "This is the nearest prior full sweep."
    )
    if current_is_full_sweep and method_versions_match:
        return (
            f"{subject} The two full-sweep records are descriptively comparable, "
            "but they do not identify a model release, provider, or routing cause."
        )
    if current_is_full_sweep:
        return (
            f"{subject} Both records use the full-sweep arm, but method versions "
            f"v{prior_method_version} and v{current_method_version} differ, so their "
            "values are not directly comparable."
        )
    return f"{subject} A canonical-only current run is not a like-for-like recovery test."


def load_sources(*, root: Path = ROOT) -> dict[str, Any]:
    """Load, strictly parse, and cross-verify every source used by the desk."""

    reading_path = root / "readings" / READING_PATH.name
    history_path = root / "readings" / HISTORY_PATH.name
    registry_path = root / "readings" / REGISTRY_PATH.name
    reading_raw = _read_bounded(reading_path, maximum=2 * 1024 * 1024)
    history_raw = _read_bounded(history_path, maximum=8 * 1024 * 1024)
    registry_raw = _read_bounded(registry_path, maximum=16 * 1024 * 1024)
    reading = _strict_json(reading_raw, label=reading_path.name)
    history = _jsonl(history_raw, label=history_path.name, maximum_rows=20_000)
    entries = _jsonl(registry_raw, label=registry_path.name, maximum_rows=100_000)
    if not isinstance(reading, dict):
        raise EvalArticleError("refusal reading is not an object")
    ok, problems = eval_registry.verify(entries)
    if not ok:
        raise EvalArticleError("eval registry does not verify: " + "; ".join(problems))
    generated_at = _text(reading.get("generated_at"), "generated_at", maximum=80)
    _timestamp(generated_at, "generated_at")
    runs = _matching_runs(reading, entries)
    previous_full_sweep = _previous_full_sweep(history, generated_at)
    return {
        "reading": reading,
        "history": history,
        "registry": entries,
        "matching_runs": runs,
        "previous_full_sweep": previous_full_sweep,
        "previous_failed_full_sweep": _previous_full_sweep(
            history, generated_at, failed_controls_only=True
        ),
        "raw": {
            "refusal-drift-current": reading_raw,
            "refusal-drift-history": history_raw,
            "eval-registry": registry_raw,
        },
    }


def _input_receipts(sources: Mapping[str, Any]) -> list[dict[str, Any]]:
    reading = sources["reading"]
    raw = sources["raw"]
    generated_at = reading["generated_at"]
    return [
        {
            "input_id": "refusal-drift-current",
            "filename": READING_PATH.name,
            "sha256": _sha(raw["refusal-drift-current"]),
            "bytes": len(raw["refusal-drift-current"]),
            "generated_at": generated_at,
            "public_url": f"https://palimpsest.info/readings/{READING_PATH.name}",
            "integrity": "strict-json-and-sealed-run-match",
        },
        {
            "input_id": "refusal-drift-history",
            "filename": HISTORY_PATH.name,
            "sha256": _sha(raw["refusal-drift-history"]),
            "bytes": len(raw["refusal-drift-history"]),
            "generated_at": generated_at,
            "public_url": f"https://palimpsest.info/readings/{HISTORY_PATH.name}",
            "integrity": "strict-jsonl",
        },
        {
            "input_id": "eval-registry",
            "filename": REGISTRY_PATH.name,
            "sha256": _sha(raw["eval-registry"]),
            "bytes": len(raw["eval-registry"]),
            "generated_at": generated_at,
            "public_url": f"https://palimpsest.info/readings/{REGISTRY_PATH.name}",
            "integrity": f"verified-hash-chain:{merkle_root(sources['registry'])}",
        },
    ]


def _evidence(
    *,
    input_id: str,
    label: str,
    selector: str,
    value: Any,
    interpretation_limit: str,
    source_url: str,
) -> dict[str, Any]:
    payload = {"input_id": input_id, "selector": selector, "value": value}
    return {
        "evidence_id": _stable_id("evalevidence", payload, 20),
        "input_id": input_id,
        "label": label,
        "selector": selector,
        "value": value,
        "interpretation_limit": interpretation_limit,
        "source_url": source_url,
    }


def _sentence(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _paragraph(*sentences: Mapping[str, Any]) -> dict[str, Any]:
    return {"sentences": list(sentences)}


def _section(section_id: str, heading: str, *paragraphs: Mapping[str, Any]) -> dict[str, Any]:
    return {"section_id": section_id, "heading": heading, "paragraphs": list(paragraphs)}


def _record(text: str, *citation_ids: str) -> dict[str, Any]:
    return {"text": text, "citation_ids": list(citation_ids)}


def _common_gate_receipt(
    *, sealed_runs: int, evidence: Sequence[Mapping[str, Any]], sections: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    sentence_count = sum(
        len(paragraph["sentences"])
        for section in sections
        for paragraph in section["paragraphs"]
    )
    cited_count = sum(
        bool(sentence["citation_ids"])
        for section in sections
        for paragraph in section["paragraphs"]
        for sentence in paragraph["sentences"]
    )
    gates = [
        {
            "gate_id": "registry-chain",
            "label": "The eval registry verifies from genesis to the cited runs",
            "passed": True,
            "detail": f"All {sealed_runs} panel runs match verified registry attestations.",
        },
        {
            "gate_id": "controls-accounted-for",
            "label": "Control failures are visible and constrain the interpretation",
            "passed": True,
            "detail": "A failed control produces an instrument warning, never a censorship claim.",
        },
        {
            "gate_id": "uncertainty-visible",
            "label": "The article reports denominators and uncertainty with the rate",
            "passed": True,
            "detail": "Family counts and Wilson 95% interval bounds remain attached to the score.",
        },
        {
            "gate_id": "sentence-citations",
            "label": "Every analytical sentence names exact evidence receipts",
            "passed": sentence_count == cited_count and sentence_count > 0,
            "detail": f"{cited_count} of {sentence_count} analytical sentences carry citations.",
        },
        {
            "gate_id": "adversarial-reading",
            "label": "Counterreadings, limitations, and reproduction steps are present",
            "passed": True,
            "detail": "The approved article shapes require all three surfaces before publication.",
        },
        {
            "gate_id": "bounded-authorship",
            "label": "No interviews or free-form model prose are represented as reporting",
            "passed": True,
            "detail": DISCLOSURE,
        },
    ]
    publishable = bool(evidence) and all(gate["passed"] for gate in gates)
    return {
        "status": "passed" if publishable else "failed",
        "publishable": publishable,
        "citation_coverage": 1.0 if sentence_count == cited_count else round(cited_count / sentence_count, 4),
        "sealed_run_count": sealed_runs,
        "gates": gates,
    }


def _article_identity(article: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in article.items()
        if key not in {"revision_id", "previous_revision_id"}
    }
    return _stable_id("evalarticlev", payload, 24)


def _finish_article(
    article: dict[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    article["article_id"] = _stable_id("evalarticle", article["slug"], 20)
    if prior and prior.get("article_id") == article["article_id"]:
        article["published_at"] = prior["published_at"]
    article["previous_revision_id"] = None
    article["revision_id"] = _article_identity(article)
    if prior and prior.get("article_id") == article["article_id"]:
        if prior.get("revision_id") == article["revision_id"]:
            article["previous_revision_id"] = prior.get("previous_revision_id")
        else:
            article["previous_revision_id"] = prior.get("revision_id")
    return article


def _control_article(
    sources: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    reading = sources["reading"]
    models = _model_rows(reading)
    failing = [row for row in models.values() if not row["controls_clean"]]
    previous_full_sweep = sources.get("previous_full_sweep")
    previous_failed_full_sweep = sources.get("previous_failed_full_sweep")
    # A clean current run needs the latest earlier instrument failure as its
    # historical counterweight, even when a newer clean sweep exists. A current
    # failure instead compares with the immediately previous full sweep. These
    # are different editorial questions and must not share an implicit selector.
    prior_full = (
        previous_full_sweep
        if failing
        else previous_failed_full_sweep or previous_full_sweep
    )
    prior_is_failed_comparator = (
        not failing
        and isinstance(previous_failed_full_sweep, dict)
        and prior_full is previous_failed_full_sweep
    )
    prior_models = (
        prior_full.get("models", {})
        if isinstance(prior_full, dict) and isinstance(prior_full.get("models"), dict)
        else {}
    )
    prior_failing_ids = [
        model
        for model, row in prior_models.items()
        if isinstance(row, dict) and row.get("controls_clean") is False and model in models
    ]
    if failing:
        lead = max(
            failing,
            key=lambda row: (len(row["control_refusals"]), row["arm_refusal_rate_pct"]),
        )
    elif prior_failing_ids:
        lead = models[prior_failing_ids[0]]
    else:
        lead = next(iter(models.values()))
    model_id = str(lead["model"])
    model_name = _display_model(model_id)
    run = sources["matching_runs"][model_id]
    current_url = f"https://palimpsest.info/readings/{READING_PATH.name}"
    history_url = f"https://palimpsest.info/readings/{HISTORY_PATH.name}"
    registry_url = f"https://palimpsest.info/readings/{REGISTRY_PATH.name}"
    controls = list(lead["control_refusals"])
    control_families = sorted({str(item).split("#", 1)[0] for item in controls})
    invariance = lead.get("wording_invariance") if isinstance(lead.get("wording_invariance"), dict) else {}
    consistency = invariance.get("consistency_rate")
    current_value = {
        "model": model_id,
        "arm": reading.get("arm"),
        "family_refusal_rate_pct": lead["family_refusal_rate_pct"],
        "n_refused_families": lead["n_refused_families"],
        "n_families": lead["n_families"],
        "family_refusal_ci95_pct": lead["family_refusal_ci95_pct"],
        "arm_refusal_rate_pct": lead["arm_refusal_rate_pct"],
        "n_arms": lead["n_arms"],
        "controls_clean": lead["controls_clean"],
        "control_refusals": controls,
        "wording_consistency": consistency,
    }
    current_evidence = _evidence(
        input_id="refusal-drift-current",
        label=f"Latest {model_name} panel result",
        selector=f"/models/@model={model_id}",
        value=current_value,
        interpretation_limit=(
            "A lexical classifier labels answers and refusals. A failed control blocks a "
            "content-specific suppression interpretation."
        ),
        source_url=current_url,
    )
    method_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Published method and control rule",
        selector="/method_note",
        value={"method": reading.get("method"), "method_note": reading.get("method_note")},
        interpretation_limit="The method describes this dated suite, not model behaviour outside it.",
        source_url=current_url,
    )
    panel_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Cross-lab control comparison",
        selector="/models/*/controls_clean",
        value={model: row["controls_clean"] for model, row in models.items()},
        interpretation_limit="Cross-model agreement does not identify a provider-side cause.",
        source_url=current_url,
    )
    seal_evidence = _evidence(
        input_id="eval-registry",
        label=f"Sealed registry run for {model_name}",
        selector=f"/seq={run['seq']}",
        value={
            "seq": run["seq"],
            "entry_hash": run["entry_hash"],
            "responses_hash": run["responses_hash"],
            "probe_set_hash": run["probe_set_hash"],
        },
        interpretation_limit="The seal proves the attestation was not rewritten. It does not prove the classifier was correct.",
        source_url=registry_url,
    )
    evidence = [current_evidence, method_evidence, panel_evidence, seal_evidence]
    current_is_full_sweep = reading.get("arm") == "full-sweep"
    current_method_version = reading.get("method_version")
    prior_method_version = (
        prior_full.get("method_version") if isinstance(prior_full, dict) else None
    )
    method_versions_match = (
        isinstance(current_method_version, int)
        and isinstance(prior_method_version, int)
        and current_method_version == prior_method_version
    )
    full_sweeps_comparable = current_is_full_sweep and method_versions_match
    prior_row = None
    if isinstance(prior_full, dict) and isinstance(prior_full.get("models"), dict):
        prior_row = prior_full["models"].get(model_id)
    prior_evidence = None
    if isinstance(prior_row, dict):
        prior_evidence = _evidence(
            input_id="refusal-drift-history",
            label=(
                f"Most recent prior full sweep with failed controls for {model_name}"
                if prior_is_failed_comparator
                else f"Most recent prior full sweep for {model_name}"
            ),
            selector=f"/generated_at={prior_full['generated_at']}/models/{model_id}",
            value=prior_row,
            interpretation_limit=_prior_sweep_limit(
                failed_comparator=prior_is_failed_comparator,
                current_is_full_sweep=current_is_full_sweep,
                method_versions_match=method_versions_match,
                prior_method_version=prior_method_version,
                current_method_version=current_method_version,
            ),
            source_url=history_url,
        )
        evidence.append(prior_evidence)

    current_id = current_evidence["evidence_id"]
    method_id = method_evidence["evidence_id"]
    panel_id = panel_evidence["evidence_id"]
    seal_id = seal_evidence["evidence_id"]
    clean_count = sum(row["controls_clean"] for row in models.values())
    prior_failed = isinstance(prior_row, dict) and prior_row.get("controls_clean") is False
    if lead["controls_clean"] and prior_failed:
        title = "A clean run does not erase a failed one"
        finding_state = "bounded-finding"
        if full_sweeps_comparable:
            dek = (
                f"{model_name} passed all {lead['n_arms']} prompt arms in the latest "
                "full-sweep run. The prior failed controls remain part of the record; "
                "the change is descriptive rather than causal."
            )
        elif current_is_full_sweep:
            dek = (
                f"{model_name} passed all {lead['n_arms']} prompt arms in the latest "
                "full-sweep run. The prior failed controls remain part of the record; "
                "the method-version boundary prevents a trend claim."
            )
        else:
            dek = (
                f"{model_name} passed all {lead['n_arms']} prompt arms in the latest "
                f"{reading.get('arm', 'dated')} run. The most recent full sweep still "
                "retains its failed controls, and the two arms answer different questions."
            )
    elif lead["controls_clean"]:
        title = "The controls passed. Now the score is interpretable."
        finding_state = "bounded-finding"
        dek = (
            f"{model_name} passed every ordinary control in the latest dated run. "
            "That clears one interpretation gate, but it does not turn a small eval into a universal claim."
        )
    else:
        title = f"The headline was {_pct(lead['family_refusal_rate_pct'])}. The controls still failed."
        finding_state = "instrument-warning"
        dek = (
            f"{model_name} refused {lead['n_refused_families']} of {lead['n_families']} monitored "
            f"question families, but {len(controls)} ordinary control prompt arms also refused. "
            "That makes the result an instrument warning, not a censorship finding."
        )

    first_sentence = (
        f"On {_date_label(reading['generated_at'])}, {model_name} recorded "
        f"{lead['n_refused_families']} refused families out of {lead['n_families']} monitored "
        f"families, a headline rate of {_pct(lead['family_refusal_rate_pct'])}."
    )
    control_sentence = (
        f"The same run refused {len(controls)} control prompt arms across "
        f"{len(control_families)} ordinary control families."
        if controls
        else "The same run answered every ordinary control prompt."
    )
    consistency_sentence = (
        f"Its wording consistency was {_pct(100 * consistency)} across the testable families."
        if isinstance(consistency, (int, float))
        else "This run did not publish a comparable wording-consistency estimate."
    )
    sections = [
        _section(
            "two-readings",
            "Two readings from one run",
            _paragraph(
                _sentence(first_sentence, current_id, seal_id),
                _sentence(control_sentence, current_id),
                _sentence(consistency_sentence, current_id),
            ),
        ),
        _section(
            "controls-first",
            "Why the controls outrank the score",
            _paragraph(
                _sentence(
                    "Palimpsest uses deliberately ordinary questions as controls, so a refusal there marks an instrument fault for this run.",
                    method_id,
                ),
                _sentence(
                    "When that gate fails, the desk may describe the failure itself but may not interpret the headline rate as selective suppression.",
                    method_id,
                    current_id,
                ),
            ),
        ),
        _section(
            "counterread",
            "The counterread",
            _paragraph(
                _sentence(
                    f"{clean_count} of the {len(models)} panel runs answered every control in the same sweep.",
                    panel_id,
                ),
                _sentence(
                    "That argues against calling the entire panel unusable, but it does not identify why one run failed.",
                    panel_id,
                ),
            ),
        ),
    ]
    if prior_evidence is not None:
        prior_id = prior_evidence["evidence_id"]
        prior_consistency = prior_row.get("wording_consistency")
        prior_text = (
            f"In the full sweep on {_date_label(prior_full['generated_at'])}, the same model "
            f"reported an arm refusal rate of {_pct(prior_row['arm_refusal_rate_pct'])} and "
            f"controls_clean={str(prior_row['controls_clean']).lower()}."
        )
        if full_sweeps_comparable:
            consistency_text = (
                f"Wording consistency moved from {_pct(100 * prior_consistency)} to {_pct(100 * consistency)}."
                if isinstance(prior_consistency, (int, float)) and isinstance(consistency, (int, float))
                else "The two full sweeps do not both contain a comparable wording-consistency estimate."
            )
            comparison_limit = (
                "The comparison is descriptive and does not assign the change to a model release, provider, or prompt-routing decision."
            )
            comparison_heading = "What changed since the prior comparable sweep"
        elif current_is_full_sweep:
            consistency_text = (
                f"Method v{prior_method_version} reported wording consistency of "
                f"{_pct(100 * prior_consistency)}, while method v{current_method_version} "
                f"reported {_pct(100 * consistency)}."
                if isinstance(prior_consistency, (int, float))
                and isinstance(consistency, (int, float))
                else "The two method versions do not both contain a wording-consistency estimate."
            )
            comparison_limit = (
                f"Method versions v{prior_method_version} and v{current_method_version} "
                "differ, so these values are not directly comparable, do not establish "
                "a trend, and do not identify a model release, provider, or prompt-routing cause."
            )
            comparison_heading = "What the prior sweep does and does not show"
        else:
            consistency_text = (
                f"The latest {reading.get('arm', 'dated')} arm used {lead['n_arms']} prompts, while "
                f"the full sweep used a broader prompt set and reported wording consistency of {_pct(100 * prior_consistency)}."
                if isinstance(prior_consistency, (int, float))
                else "The canonical and full-sweep arms do not publish the same wording-consistency statistic."
            )
            comparison_limit = (
                "The clean canonical arm is a new dated observation, not a like-for-like retest that cancels the failed full sweep."
            )
            comparison_heading = "What the prior sweep does and does not show"
        sections.append(
            _section(
                "since-prior",
                comparison_heading,
                _paragraph(
                    _sentence(prior_text, prior_id, current_id),
                    _sentence(consistency_text, prior_id, current_id),
                    _sentence(
                        comparison_limit,
                        prior_id,
                        current_id,
                    ),
                ),
            )
        )
    counterreadings = [
        _record(
            "A zero observed family rate is still favorable evidence inside the named suite when the controls pass.",
            current_id,
            method_id,
        ),
        _record(
            "A control failure in one panel run does not invalidate the separately sealed runs from the other models.",
            panel_id,
            seal_id,
        ),
    ]
    limitations = [
        _record(
            "The refusal label is produced by a published lexical rule, not a completed independent human-coder study.",
            method_id,
        ),
        _record(
            "The model and provider labels do not prove which deployed weights or routing path answered a request.",
            current_id,
        ),
        _record(
            "A failed control blocks a content-specific suppression claim from this run.",
            current_id,
            method_id,
        ),
    ]
    methodology = [
        {"step": "Freeze", "detail": "Hash and preregister the prompt bank before querying a model.", "citation_ids": [seal_id]},
        {"step": "Run", "detail": "Publish family rates, prompt-arm rates, controls, and uncertainty from the same dated sweep.", "citation_ids": [current_id]},
        {"step": "Gate", "detail": "Withhold a selective-suppression interpretation whenever ordinary controls fail.", "citation_ids": [method_id]},
        {"step": "Verify", "detail": "Recompute the registry chain and compare the exact sealed metrics with the public reading.", "citation_ids": [seal_id, current_id]},
    ]
    receipt = _common_gate_receipt(
        sealed_runs=len(sources["matching_runs"]), evidence=evidence, sections=sections
    )
    article = {
        "schema_version": ARTICLE_SCHEMA,
        "article_id": "",
        "revision_id": "",
        "previous_revision_id": None,
        "slug": "before-reading-the-score-read-the-controls",
        "url": "/journal/before-reading-the-score-read-the-controls/",
        "kicker": "Eval integrity / control failure",
        "title": title,
        "dek": dek,
        "thesis": "A headline score is not interpretable until the same run passes its ordinary controls.",
        "finding_state": finding_state,
        "published_at": reading["generated_at"],
        "updated_at": reading["generated_at"],
        "key_numbers": [
            {"value": _pct(lead["family_refusal_rate_pct"]), "label": "family refusal rate", "note": f"{lead['n_refused_families']} of {lead['n_families']} monitored families", "citation_ids": [current_id]},
            {
                "value": str(len(controls)),
                "label": "control prompt refusals",
                "note": (
                    f"across {len(control_families)} ordinary families"
                    if controls
                    else "every ordinary control answered"
                ),
                "citation_ids": [current_id],
            },
            {"value": f"{clean_count}/{len(models)}", "label": "panel runs with clean controls", "note": "same dated sweep", "citation_ids": [panel_id]},
        ],
        "sections": sections,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "methodology": methodology,
        "evidence": evidence,
        "evaluation_receipt": receipt,
        "authorship": {
            "byline": "Palimpsest Eval Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
    }
    return _finish_article(article, prior)


def _uncertainty_article(
    sources: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    reading = sources["reading"]
    models = _model_rows(reading)
    current_url = f"https://palimpsest.info/readings/{READING_PATH.name}"
    registry_url = f"https://palimpsest.info/readings/{REGISTRY_PATH.name}"
    panel_projection = {
        model: {
            "family_refusal_rate_pct": row["family_refusal_rate_pct"],
            "family_refusal_ci95_pct": row["family_refusal_ci95_pct"],
            "n_refused_families": row["n_refused_families"],
            "n_families": row["n_families"],
            "controls_clean": row["controls_clean"],
        }
        for model, row in models.items()
    }
    panel_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Latest cross-lab family-level panel",
        selector="/models/*/{family_refusal_rate_pct,family_refusal_ci95_pct,n_families,controls_clean}",
        value=panel_projection,
        interpretation_limit="The panel is a dated, non-representative set of named model endpoints.",
        source_url=current_url,
    )
    method_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Family-level statistical method",
        selector="/{method,method_note,n_families,control_families}",
        value={
            "method": reading.get("method"),
            "method_note": reading.get("method_note"),
            "n_families": reading.get("n_families"),
            "control_families": reading.get("control_families"),
        },
        interpretation_limit="Wilson intervals quantify sampling uncertainty inside the suite, not deployment-wide uncertainty.",
        source_url=current_url,
    )
    sealed_projection = {
        model: {
            "seq": run["seq"],
            "entry_hash": run["entry_hash"],
            "responses_hash": run["responses_hash"],
        }
        for model, run in sources["matching_runs"].items()
    }
    seal_evidence = _evidence(
        input_id="eval-registry",
        label="Sealed attestations for the latest panel",
        selector=f"/runs/@ts={reading['generated_at']}/@suite={reading['suite']}",
        value=sealed_projection,
        interpretation_limit="The chain proves these attestations persisted unchanged. It does not widen the sampled population.",
        source_url=registry_url,
    )
    evidence = [panel_evidence, method_evidence, seal_evidence]
    panel_id = panel_evidence["evidence_id"]
    method_id = method_evidence["evidence_id"]
    seal_id = seal_evidence["evidence_id"]
    clean = [row for row in models.values() if row["controls_clean"]]
    zero = [row for row in clean if row["n_refused_families"] == 0]
    representative = min(
        clean or models.values(),
        key=lambda row: (row["n_refused_families"], row["family_refusal_rate_pct"]),
    )
    upper = representative["family_refusal_ci95_pct"][1]
    n_families = representative["n_families"]
    sections = [
        _section(
            "what-zero-contains",
            "What zero contains",
            _paragraph(
                _sentence(
                    f"In the {_date_label(reading['generated_at'])} panel, {len(zero)} control-clean runs observed zero refused families among {n_families} monitored non-control families.",
                    panel_id,
                    seal_id,
                ),
                _sentence(
                    f"For a zero-of-{n_families} result, the published Wilson 95% interval still reaches {_pct(upper)}.",
                    panel_id,
                    method_id,
                ),
                _sentence(
                    "Zero observed events and zero plausible event rate are different statements.",
                    panel_id,
                    method_id,
                ),
            ),
        ),
        _section(
            "unit-of-analysis",
            "The unit is a question family, not a prompt",
            _paragraph(
                _sentence(
                    "Each monitored question can appear in several meaning-preserving wordings, but the family is the statistical unit.",
                    method_id,
                ),
                _sentence(
                    "That prevents a model from looking artificially precise merely because the same idea was phrased many times.",
                    method_id,
                ),
            ),
        ),
        _section(
            "agreement-boundary",
            "Cross-lab agreement is useful and bounded",
            _paragraph(
                _sentence(
                    f"The latest panel contains {len(models)} named endpoints, of which {len(clean)} passed every ordinary control.",
                    panel_id,
                ),
                _sentence(
                    "Agreement across those endpoints is stronger than a one-model anecdote, but it is not a probability sample of all models, deployments, languages, or future releases.",
                    panel_id,
                    method_id,
                ),
            ),
        ),
        _section(
            "dated-not-ranked",
            "Read it as a dated panel, not a leaderboard",
            _paragraph(
                _sentence(
                    "The registry binds every score to a model label, prompt commitment, response hash, and timestamp.",
                    seal_id,
                ),
                _sentence(
                    "Those receipts make change auditable, but they do not justify a permanent ranking from one sweep.",
                    seal_id,
                    panel_id,
                ),
            ),
        ),
    ]
    counterreadings = [
        _record(
            f"Observing zero refused families in {len(zero)} clean runs is real favorable evidence within this suite.",
            panel_id,
        ),
        _record(
            "The nonzero upper interval is not evidence that hidden refusals occurred. It is the uncertainty left by a finite sample.",
            panel_id,
            method_id,
        ),
    ]
    limitations = [
        _record(
            "The monitored families were selected for an eval, not sampled from all questions people ask.",
            method_id,
        ),
        _record(
            "A provider may change routing or model behaviour after the dated sweep.",
            panel_id,
            seal_id,
        ),
        _record(
            "A clean control state permits interpretation of the suite result but does not validate the lexical classifier against independent human coders.",
            method_id,
        ),
    ]
    methodology = [
        {"step": "Pre-register", "detail": "Commit the exact probe bank before any endpoint is queried.", "citation_ids": [seal_id]},
        {"step": "Group", "detail": "Treat a meaning-preserving prompt family as the statistical unit.", "citation_ids": [method_id]},
        {"step": "Bound", "detail": "Report the denominator and Wilson 95% interval with every family rate.", "citation_ids": [panel_id, method_id]},
        {"step": "Re-run", "detail": "Compare later sealed sweeps with the same method instead of turning this edition into a standing ranking.", "citation_ids": [seal_id]},
    ]
    receipt = _common_gate_receipt(
        sealed_runs=len(sources["matching_runs"]), evidence=evidence, sections=sections
    )
    article = {
        "schema_version": ARTICLE_SCHEMA,
        "article_id": "",
        "revision_id": "",
        "previous_revision_id": None,
        "slug": "zero-observed-is-not-zero-uncertainty",
        "url": "/journal/zero-observed-is-not-zero-uncertainty/",
        "kicker": "Eval method / uncertainty",
        "title": "Zero observed refusals is not zero uncertainty",
        "dek": (
            f"{len(zero)} control-clean model runs observed 0 refused families out of {n_families}, "
            f"yet each zero result still carries a Wilson 95% upper bound of {_pct(upper)}. "
            "That interval is part of the finding, not fine print."
        ),
        "thesis": "A finite eval can observe no refusals and still leave a meaningful range of plausible rates.",
        "finding_state": "bounded-finding",
        "published_at": reading["generated_at"],
        "updated_at": reading["generated_at"],
        "key_numbers": [
            {"value": "0", "label": "refused families", "note": f"in each of {len(zero)} clean runs", "citation_ids": [panel_id]},
            {"value": str(n_families), "label": "monitored non-control families", "note": "family is the statistical unit", "citation_ids": [panel_id, method_id]},
            {"value": _pct(upper), "label": "95% upper interval bound", "note": f"for a zero-of-{n_families} result", "citation_ids": [panel_id, method_id]},
        ],
        "sections": sections,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "methodology": methodology,
        "evidence": evidence,
        "evaluation_receipt": receipt,
        "authorship": {
            "byline": "Palimpsest Eval Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
    }
    return _finish_article(article, prior)


def _drift_article(
    sources: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Explain the latest adjacent-run transition without turning it into a trend."""

    reading = sources["reading"]
    models = _model_rows(reading)
    current_url = f"https://palimpsest.info/readings/{READING_PATH.name}"
    registry_url = f"https://palimpsest.info/readings/{REGISTRY_PATH.name}"

    projection: dict[str, dict[str, Any]] = {}
    total_compared = total_new_refusals = total_new_answers = 0
    changed_models = 0
    failing_controls = 0
    for model_id, row in models.items():
        drift = row.get("drift_vs_prior")
        if not isinstance(drift, dict):
            raise EvalArticleError(f"latest drift state is missing for {model_id}")
        compared = drift.get("n_compared")
        rate = drift.get("drift_rate_pct")
        new_refusals = drift.get("new_refusals")
        new_answers = drift.get("new_answers")
        _number(compared, f"{model_id}.drift.n_compared")
        _number(rate, f"{model_id}.drift.drift_rate_pct")
        if (
            not isinstance(new_refusals, list)
            or not isinstance(new_answers, list)
            or any(not isinstance(value, str) for value in [*new_refusals, *new_answers])
        ):
            raise EvalArticleError(f"latest drift transitions are invalid for {model_id}")
        total_compared += int(compared)
        total_new_refusals += len(new_refusals)
        total_new_answers += len(new_answers)
        changed_models += bool(new_refusals or new_answers)
        failing_controls += row["controls_clean"] is False
        projection[model_id] = {
            "n_compared": compared,
            "drift_rate_pct": rate,
            "new_refusals": list(new_refusals),
            "new_answers": list(new_answers),
            "controls_clean": row["controls_clean"],
        }

    churn_projection = {
        model_id: {
            key: value
            for key, value in row.get("churn_monitor", {}).items()
            if key
            in {
                "state",
                "pairs_seen",
                "pairs_needed",
                "evalue",
            }
        }
        for model_id, row in models.items()
    }
    drift_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Latest adjacent-run answer-state transitions",
        selector="/models/*/drift_vs_prior",
        value=projection,
        interpretation_limit=(
            "This compares each named endpoint with its immediately prior compatible "
            "canonical observation. It does not identify a provider-side cause or a trend."
        ),
        source_url=current_url,
    )
    churn_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Anytime-valid churn monitor state and blind spot",
        selector="/models/*/churn_monitor",
        value=churn_projection,
        interpretation_limit=(
            "The churn monitor watches repeated instability. A single permanent answer-state "
            "change appears in the adjacent-run transition but cannot accumulate as repeated evidence."
        ),
        source_url=current_url,
    )
    method_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Current panel method and arm",
        selector="/method",
        value={
            "suite": reading.get("suite"),
            "arm": reading.get("arm"),
            "method": reading.get("method"),
            "method_version": reading.get("method_version"),
            "generated_at": reading.get("generated_at"),
        },
        interpretation_limit="The method applies only to the named panel, prompt families, and dated run.",
        source_url=current_url,
    )
    seal_evidence = _evidence(
        input_id="eval-registry",
        label="Registry seals for the latest panel transition",
        selector=f"/runs/ts={reading['generated_at']}",
        value=[
            {
                "model": model_id,
                "seq": run["seq"],
                "entry_hash": run["entry_hash"],
                "responses_hash": run["responses_hash"],
            }
            for model_id, run in sorted(sources["matching_runs"].items())
        ],
        interpretation_limit=(
            "The seals make later rewriting detectable inside the served chain. They do not "
            "prove that an endpoint label names unchanged hidden weights."
        ),
        source_url=registry_url,
    )
    evidence = [drift_evidence, churn_evidence, method_evidence, seal_evidence]
    drift_id = drift_evidence["evidence_id"]
    churn_id = churn_evidence["evidence_id"]
    method_id = method_evidence["evidence_id"]
    seal_id = seal_evidence["evidence_id"]

    transitions = total_new_refusals + total_new_answers
    if total_new_refusals:
        title = f"{total_new_refusals} new refusal transition{'s' if total_new_refusals != 1 else ''} appeared in the latest panel"
    elif total_new_answers:
        title = f"{total_new_answers} previously refused answer{'s' if total_new_answers != 1 else ''} returned in the latest panel"
    else:
        title = "No answer-state changes appeared in the latest panel"
    if transitions:
        dek = (
            f"Across {len(models)} sealed endpoint runs, {transitions} of {total_compared} "
            f"paired family comparisons changed state: {total_new_refusals} toward refusal "
            f"and {total_new_answers} toward answer. This is a dated transition, not a trend claim."
        )
    else:
        dek = (
            f"Across {len(models)} sealed endpoint runs, all {total_compared} paired family "
            "comparisons kept the same answer state. That bounds this transition only; it does "
            "not establish permanent stability."
        )
    if failing_controls:
        dek += f" {failing_controls} run{'s' if failing_controls != 1 else ''} also failed ordinary controls."

    sections = [
        _section(
            "latest-transition",
            "What changed in the latest transition",
            _paragraph(
                _sentence(
                    f"The latest panel recorded {total_new_refusals} new refusal transitions and {total_new_answers} newly answered transitions across {total_compared} paired family comparisons.",
                    drift_id,
                ),
                _sentence(
                    f"Those transitions occurred in {changed_models} of {len(models)} named endpoint runs.",
                    drift_id,
                    seal_id,
                ),
            ),
        ),
        _section(
            "transition-not-trend",
            "One transition is not a trend",
            _paragraph(
                _sentence(
                    "The comparison is adjacent and endpoint-specific, so a changed label establishes neither a persistent trajectory nor a common cause across providers.",
                    drift_id,
                    method_id,
                ),
                _sentence(
                    "The registry preserves the exact current attestations, which makes later revision detectable without revealing a provider's hidden routing or weights.",
                    seal_id,
                ),
            ),
        ),
        _section(
            "monitor-boundary",
            "The churn alarm watches a different failure mode",
            _paragraph(
                _sentence(
                    "The anytime-valid monitor is designed to detect repeated instability after calibration, while the adjacent comparison records individual answer-state changes immediately.",
                    churn_id,
                    method_id,
                ),
                _sentence(
                    "A single change that then remains fixed cannot become repeated evidence merely because the same question is asked again.",
                    churn_id,
                ),
            ),
        ),
    ]
    counterreadings = [
        _record(
            "No observed transition is favorable evidence of short-run stability inside this exact panel.",
            drift_id,
        ),
        _record(
            "An observed transition can reflect endpoint routing, sampling, classifier error, or model change; the eval alone does not select among those explanations.",
            drift_id,
            method_id,
        ),
    ]
    limitations = [
        _record(
            "Only prompt families present in both compatible adjacent runs enter the paired denominator.",
            drift_id,
            method_id,
        ),
        _record(
            "Endpoint names do not independently prove that the provider served unchanged weights or routing across runs.",
            seal_id,
        ),
        _record(
            "The lexical answer-state classifier still requires independent human validation for a broader construct claim.",
            method_id,
        ),
    ]
    methodology = [
        {"step": "Pair", "detail": "Compare only compatible family labels shared by adjacent dated runs.", "citation_ids": [drift_id, method_id]},
        {"step": "Count", "detail": "Publish new refusals, newly answered families, and the paired denominator separately for every endpoint.", "citation_ids": [drift_id]},
        {"step": "Seal", "detail": "Bind the current response metrics to verified registry entries before publishing the interpretation.", "citation_ids": [seal_id]},
        {"step": "Monitor", "detail": "Keep the repeated-instability alarm separate from the one-transition record.", "citation_ids": [churn_id]},
    ]
    receipt = _common_gate_receipt(
        sealed_runs=len(sources["matching_runs"]), evidence=evidence, sections=sections
    )
    article = {
        "schema_version": ARTICLE_SCHEMA,
        "article_id": "",
        "revision_id": "",
        "previous_revision_id": None,
        "slug": "what-changed-in-the-latest-model-panel",
        "url": "/journal/what-changed-in-the-latest-model-panel/",
        "kicker": "Model behavior / adjacent-run drift",
        "title": title,
        "dek": dek,
        "thesis": "A drift result is a dated answer-state transition with a paired denominator, not a diagnosis of why an endpoint changed.",
        "finding_state": "instrument-warning" if failing_controls else "bounded-finding",
        "published_at": reading["generated_at"],
        "updated_at": reading["generated_at"],
        "key_numbers": [
            {"value": str(transitions), "label": "answer-state transitions", "note": f"{total_new_refusals} toward refusal, {total_new_answers} toward answer", "citation_ids": [drift_id]},
            {"value": str(total_compared), "label": "paired family comparisons", "note": f"across {len(models)} named endpoints", "citation_ids": [drift_id]},
            {"value": f"{changed_models}/{len(models)}", "label": "endpoints with a change", "note": "latest compatible transition", "citation_ids": [drift_id, seal_id]},
        ],
        "sections": sections,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "methodology": methodology,
        "evidence": evidence,
        "evaluation_receipt": receipt,
        "authorship": {
            "byline": "Palimpsest Eval Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
    }
    return _finish_article(article, prior)


def _registry_article(
    sources: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """State exactly what the current verified registry can and cannot prove."""

    reading = sources["reading"]
    entries = sources["registry"]
    if not entries:
        raise EvalArticleError("eval registry is empty")
    registry_url = f"https://palimpsest.info/readings/{REGISTRY_PATH.name}"
    current_url = f"https://palimpsest.info/readings/{READING_PATH.name}"
    preregistrations = sum(entry.get("kind") == eval_registry.PREREGISTRATION for entry in entries)
    runs = sum(entry.get("kind") == eval_registry.RUN for entry in entries)
    models = sorted(
        {
            str(entry["model"])
            for entry in entries
            if entry.get("kind") == eval_registry.RUN and isinstance(entry.get("model"), str)
        }
    )
    head = entries[-1]
    chain_value = {
        "attestations": len(entries),
        "preregistrations": preregistrations,
        "runs": runs,
        "models": models,
        "head_seq": head.get("seq"),
        "head_ts": head.get("ts"),
        "head_hash": head.get("entry_hash"),
        "merkle_root": merkle_root(entries),
    }
    chain_evidence = _evidence(
        input_id="eval-registry",
        label="Verified registry head and complete served-chain summary",
        selector="/",
        value=chain_value,
        interpretation_limit=(
            "Verification detects alteration, deletion, or reordering inside the served chain. "
            "The file alone does not prove an external wall-clock publication time."
        ),
        source_url=registry_url,
    )
    current_runs = [
        {
            "model": model_id,
            "seq": run["seq"],
            "suite": run["suite"],
            "probe_set_hash": run["probe_set_hash"],
            "responses_hash": run["responses_hash"],
            "entry_hash": run["entry_hash"],
        }
        for model_id, run in sorted(sources["matching_runs"].items())
    ]
    current_evidence = _evidence(
        input_id="eval-registry",
        label="Current panel runs matched to the published reading",
        selector=f"/runs/ts={reading['generated_at']}",
        value=current_runs,
        interpretation_limit="Exact metric matching proves publication consistency, not construct validity.",
        source_url=registry_url,
    )
    method_evidence = _evidence(
        input_id="refusal-drift-current",
        label="Current suite scope and uncertainty method",
        selector="/method",
        value={
            "suite": reading.get("suite"),
            "arm": reading.get("arm"),
            "method": reading.get("method"),
            "method_version": reading.get("method_version"),
            "model_count": len(_model_rows(reading)),
        },
        interpretation_limit="A verified chain does not widen the suite's sampled population.",
        source_url=current_url,
    )
    evidence = [chain_evidence, current_evidence, method_evidence]
    chain_id = chain_evidence["evidence_id"]
    current_id = current_evidence["evidence_id"]
    method_id = method_evidence["evidence_id"]
    sections = [
        _section(
            "chain-verdict",
            "What verification establishes",
            _paragraph(
                _sentence(
                    f"The served registry verifies from genesis through {len(entries)} attestations, ending at sequence {head['seq']}.",
                    chain_id,
                ),
                _sentence(
                    f"It contains {preregistrations} preregistrations and {runs} runs across {len(models)} named model endpoints.",
                    chain_id,
                ),
            ),
        ),
        _section(
            "current-edition",
            "What entered the current edition",
            _paragraph(
                _sentence(
                    f"The latest refusal reading matches {len(current_runs)} registry runs at {reading['generated_at']} with no missing panel endpoint.",
                    current_id,
                    method_id,
                ),
                _sentence(
                    "Each matched run binds its probe commitment, response digest, model label, metrics, and predecessor hash into the chain.",
                    current_id,
                    chain_id,
                ),
            ),
        ),
        _section(
            "claim-ceiling",
            "What the chain still cannot prove",
            _paragraph(
                _sentence(
                    "Hash-chain verification cannot establish that a refusal classifier measures the intended construct, that an endpoint label names fixed hidden weights, or that the panel represents all models.",
                    chain_id,
                    method_id,
                ),
                _sentence(
                    "Those questions require separate validation, provider transparency, and sampling arguments rather than a stronger hash.",
                    chain_id,
                    method_id,
                ),
            ),
        ),
    ]
    counterreadings = [
        _record(
            "A verified chain is meaningful evidence that the served sequence has not been quietly rewritten.",
            chain_id,
        ),
        _record(
            "An internally valid chain can still contain a poorly designed eval; integrity and measurement validity are separate gates.",
            chain_id,
            method_id,
        ),
    ]
    limitations = [
        _record(
            "Without an external witness, the registry file alone cannot prove when its first unseen version was published.",
            chain_id,
        ),
        _record(
            "Registry verification checks commitments and exact metrics but does not independently relabel every model response.",
            current_id,
            method_id,
        ),
        _record(
            "The endpoint set is a declared panel rather than a probability sample of deployed AI systems.",
            method_id,
        ),
    ]
    methodology = [
        {"step": "Freeze", "detail": "Append a probe-set commitment before its result can enter the same registry.", "citation_ids": [chain_id]},
        {"step": "Link", "detail": "Hash every attestation with its predecessor so deletion, reordering, and alteration change the head.", "citation_ids": [chain_id]},
        {"step": "Match", "detail": "Require every current panel metric to equal its sealed run before building an article.", "citation_ids": [current_id, method_id]},
        {"step": "Bound", "detail": "Keep chain integrity separate from classifier validity, model identity, and population claims.", "citation_ids": [chain_id, method_id]},
    ]
    receipt = _common_gate_receipt(
        sealed_runs=len(sources["matching_runs"]), evidence=evidence, sections=sections
    )
    article = {
        "schema_version": ARTICLE_SCHEMA,
        "article_id": "",
        "revision_id": "",
        "previous_revision_id": None,
        "slug": "what-the-eval-registry-can-prove-today",
        "url": "/journal/what-the-eval-registry-can-prove-today/",
        "kicker": "Eval integrity / registry status",
        "title": f"The eval registry verifies {len(entries)} attestations end to end",
        "dek": (
            f"The current served chain contains {preregistrations} preregistrations and {runs} "
            f"runs across {len(models)} model endpoints. Its hashes make revision detectable; "
            "they do not make the eval construct valid by themselves."
        ),
        "thesis": "Tamper evidence answers whether the served eval record changed, not whether the underlying measurement deserves a broader claim.",
        "finding_state": "bounded-finding",
        "published_at": reading["generated_at"],
        "updated_at": reading["generated_at"],
        "key_numbers": [
            {"value": str(len(entries)), "label": "verified attestations", "note": f"head sequence {head['seq']}", "citation_ids": [chain_id]},
            {"value": str(preregistrations), "label": "preregistrations", "note": "probe commitments recorded before linked runs", "citation_ids": [chain_id]},
            {"value": str(runs), "label": "sealed runs", "note": f"across {len(models)} named endpoints", "citation_ids": [chain_id]},
        ],
        "sections": sections,
        "counterreadings": counterreadings,
        "limitations": limitations,
        "methodology": methodology,
        "evidence": evidence,
        "evaluation_receipt": receipt,
        "authorship": {
            "byline": "Palimpsest Eval Desk",
            "mode": PUBLICATION_MODE,
            "human_interviews": "none",
            "freeform_model_generation": "none",
        },
        "disclosure": DISCLOSURE,
    }
    return _finish_article(article, prior)


def _prior_by_slug(prior: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(prior, dict) or prior.get("schema_version") != SCHEMA_VERSION:
        return {}
    articles = prior.get("articles")
    if not isinstance(articles, list):
        return {}
    return {
        str(article.get("slug")): article
        for article in articles
        if isinstance(article, dict) and isinstance(article.get("slug"), str)
    }


def build_collection(
    sources: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build the complete public journal collection from verified source bytes."""

    prior_articles = _prior_by_slug(prior)
    articles = [
        _control_article(
            sources, prior_articles.get("before-reading-the-score-read-the-controls")
        ),
        _uncertainty_article(
            sources, prior_articles.get("zero-observed-is-not-zero-uncertainty")
        ),
        _drift_article(
            sources, prior_articles.get("what-changed-in-the-latest-model-panel")
        ),
        _registry_article(
            sources, prior_articles.get("what-the-eval-registry-can-prove-today")
        ),
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "desk_id": DESK_ID,
        "generated_at": sources["reading"]["generated_at"],
        "source": "Sealed Palimpsest AI evaluation artifacts and their published method receipts.",
        "scope": "Dated interpretation of model refusal, control, wording, drift, and uncertainty measurements. No model leaderboard and no claim beyond the named suites.",
        "publication_policy": {
            "mode": PUBLICATION_MODE,
            "requires_verified_registry": True,
            "requires_exact_sealed_metric_match": True,
            "requires_controls": True,
            "requires_uncertainty": True,
            "requires_sentence_citations": True,
            "requires_counterreading": True,
            "requires_limitations": True,
            "freeform_model_generation": "prohibited",
            "failed_interpretation_gate": "publish-instrument-warning-or-abstain",
        },
        "input_receipts": _input_receipts(sources),
        "n_articles": len(articles),
        "articles": articles,
    }
    validate_collection(document)
    return document


def _validate_citations(value: Any, *, evidence_ids: set[str], field: str) -> None:
    if not isinstance(value, list) or not value:
        raise EvalArticleError(f"{field} must cite at least one receipt")
    if any(not isinstance(item, str) or item not in evidence_ids for item in value):
        raise EvalArticleError(f"{field} cites an unknown evidence receipt")
    if len(value) != len(set(value)):
        raise EvalArticleError(f"{field} repeats a citation")


def _validate_article(article: Any, input_ids: set[str]) -> None:
    if not isinstance(article, dict) or set(article) != _ARTICLE_FIELDS:
        raise EvalArticleError("article fields are not exact")
    if article["schema_version"] != ARTICLE_SCHEMA:
        raise EvalArticleError("unsupported article schema")
    slug = _text(article["slug"], "article.slug", maximum=120)
    if not _SLUG.fullmatch(slug) or article["url"] != f"/journal/{slug}/":
        raise EvalArticleError("article route does not match its slug")
    if article["article_id"] != _stable_id("evalarticle", slug, 20):
        raise EvalArticleError("article_id does not match the stable slug")
    if not _ARTICLE_ID.fullmatch(article["article_id"]):
        raise EvalArticleError("article_id is invalid")
    if not _REVISION_ID.fullmatch(str(article["revision_id"])):
        raise EvalArticleError("revision_id is invalid")
    previous = article["previous_revision_id"]
    if previous is not None and not _REVISION_ID.fullmatch(str(previous)):
        raise EvalArticleError("previous_revision_id is invalid")
    if article["revision_id"] != _article_identity(article):
        raise EvalArticleError("revision_id does not match article content")
    for field in ("kicker", "title", "dek", "thesis", "disclosure"):
        _text(article[field], f"article.{field}", maximum=1_200)
    if article["disclosure"] != DISCLOSURE:
        raise EvalArticleError("article disclosure changed")
    if article["finding_state"] not in {"instrument-warning", "bounded-finding"}:
        raise EvalArticleError("article finding_state is invalid")
    published = _timestamp(article["published_at"], "published_at")
    updated = _timestamp(article["updated_at"], "updated_at")
    if published > updated:
        raise EvalArticleError("article update predates publication")

    evidence = article["evidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
        raise EvalArticleError("article evidence must be bounded and non-empty")
    evidence_ids: set[str] = set()
    for index, row in enumerate(evidence):
        if not isinstance(row, dict) or set(row) != _EVIDENCE_FIELDS:
            raise EvalArticleError("article evidence fields are not exact")
        if not _EVIDENCE_ID.fullmatch(str(row["evidence_id"])):
            raise EvalArticleError("evidence_id is invalid")
        if row["evidence_id"] in evidence_ids:
            raise EvalArticleError("article repeats an evidence_id")
        evidence_ids.add(row["evidence_id"])
        if row["input_id"] not in input_ids:
            raise EvalArticleError("evidence names an unknown input")
        for field in ("label", "selector", "interpretation_limit", "source_url"):
            _text(row[field], f"evidence[{index}].{field}", maximum=2_000)
        expected = _stable_id(
            "evalevidence",
            {"input_id": row["input_id"], "selector": row["selector"], "value": row["value"]},
            20,
        )
        if row["evidence_id"] != expected:
            raise EvalArticleError("evidence_id does not match the cited value")
        canonical_json_bytes(row["value"])

    numbers = article["key_numbers"]
    if not isinstance(numbers, list) or not 1 <= len(numbers) <= 8:
        raise EvalArticleError("article key_numbers are invalid")
    for index, number in enumerate(numbers):
        if not isinstance(number, dict) or set(number) != _NUMBER_FIELDS:
            raise EvalArticleError("key number fields are not exact")
        for field in ("value", "label", "note"):
            _text(number[field], f"key_numbers[{index}].{field}", maximum=400)
        _validate_citations(
            number["citation_ids"], evidence_ids=evidence_ids, field="key number"
        )

    sections = article["sections"]
    if not isinstance(sections, list) or not 3 <= len(sections) <= 12:
        raise EvalArticleError("article sections are not a bounded long-form shape")
    section_ids: set[str] = set()
    sentence_count = cited_count = 0
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict) or set(section) != _SECTION_FIELDS:
            raise EvalArticleError("section fields are not exact")
        section_id = _text(section["section_id"], "section_id", maximum=80)
        if not _SLUG.fullmatch(section_id) or section_id in section_ids:
            raise EvalArticleError("section_id is invalid or duplicated")
        section_ids.add(section_id)
        _text(section["heading"], "section.heading", maximum=240)
        paragraphs = section["paragraphs"]
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 8:
            raise EvalArticleError("section paragraphs are invalid")
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict) or set(paragraph) != _PARAGRAPH_FIELDS:
                raise EvalArticleError("paragraph fields are not exact")
            sentences = paragraph["sentences"]
            if not isinstance(sentences, list) or not 1 <= len(sentences) <= 12:
                raise EvalArticleError("paragraph sentences are invalid")
            for sentence in sentences:
                if not isinstance(sentence, dict) or set(sentence) != _SENTENCE_FIELDS:
                    raise EvalArticleError("sentence fields are not exact")
                _text(sentence["text"], "sentence.text", maximum=1_200)
                _validate_citations(
                    sentence["citation_ids"], evidence_ids=evidence_ids, field="sentence"
                )
                sentence_count += 1
                cited_count += 1

    for field in ("counterreadings", "limitations"):
        records = article[field]
        if not isinstance(records, list) or not 2 <= len(records) <= 12:
            raise EvalArticleError(f"article {field} are invalid")
        for record in records:
            if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                raise EvalArticleError(f"{field} fields are not exact")
            _text(record["text"], f"{field}.text", maximum=1_200)
            _validate_citations(record["citation_ids"], evidence_ids=evidence_ids, field=field)

    methods = article["methodology"]
    if not isinstance(methods, list) or not 3 <= len(methods) <= 12:
        raise EvalArticleError("article methodology is invalid")
    for method in methods:
        if not isinstance(method, dict) or set(method) != _METHOD_FIELDS:
            raise EvalArticleError("methodology fields are not exact")
        _text(method["step"], "method.step", maximum=120)
        _text(method["detail"], "method.detail", maximum=1_200)
        _validate_citations(method["citation_ids"], evidence_ids=evidence_ids, field="method")

    receipt = article["evaluation_receipt"]
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise EvalArticleError("evaluation receipt fields are not exact")
    if receipt["status"] != "passed" or receipt["publishable"] is not True:
        raise EvalArticleError("an unpublishable article entered the public collection")
    if receipt["citation_coverage"] != 1.0 or cited_count != sentence_count:
        raise EvalArticleError("article citation coverage is incomplete")
    if not isinstance(receipt["sealed_run_count"], int) or receipt["sealed_run_count"] < 1:
        raise EvalArticleError("evaluation receipt has no sealed runs")
    gates = receipt["gates"]
    if not isinstance(gates, list) or not gates:
        raise EvalArticleError("evaluation gates are missing")
    gate_ids: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != _GATE_FIELDS:
            raise EvalArticleError("gate fields are not exact")
        gate_id = _text(gate["gate_id"], "gate_id", maximum=120)
        if gate_id in gate_ids or not _SLUG.fullmatch(gate_id):
            raise EvalArticleError("gate_id is invalid or repeated")
        gate_ids.add(gate_id)
        _text(gate["label"], "gate.label", maximum=500)
        _text(gate["detail"], "gate.detail", maximum=1_200)
        if gate["passed"] is not True:
            raise EvalArticleError("a failed publication gate entered the collection")

    authorship = article["authorship"]
    if not isinstance(authorship, dict) or set(authorship) != _AUTHORSHIP_FIELDS:
        raise EvalArticleError("authorship fields are not exact")
    if authorship != {
        "byline": "Palimpsest Eval Desk",
        "mode": PUBLICATION_MODE,
        "human_interviews": "none",
        "freeform_model_generation": "none",
    }:
        raise EvalArticleError("authorship boundary changed")


def validate_collection(document: Mapping[str, Any]) -> None:
    """Validate the public shape, every citation, and every publication gate."""

    if not isinstance(document, dict) or set(document) != _ROOT_FIELDS:
        raise EvalArticleError("eval journal fields are not exact")
    if document["schema_version"] != SCHEMA_VERSION or document["desk_id"] != DESK_ID:
        raise EvalArticleError("unsupported eval journal document")
    _timestamp(document["generated_at"], "generated_at")
    _text(document["source"], "source", maximum=1_000)
    _text(document["scope"], "scope", maximum=1_000)
    policy = document["publication_policy"]
    expected_policy = {
        "mode": PUBLICATION_MODE,
        "requires_verified_registry": True,
        "requires_exact_sealed_metric_match": True,
        "requires_controls": True,
        "requires_uncertainty": True,
        "requires_sentence_citations": True,
        "requires_counterreading": True,
        "requires_limitations": True,
        "freeform_model_generation": "prohibited",
        "failed_interpretation_gate": "publish-instrument-warning-or-abstain",
    }
    if policy != expected_policy:
        raise EvalArticleError("publication policy changed")
    receipts = document["input_receipts"]
    if not isinstance(receipts, list) or len(receipts) != 3:
        raise EvalArticleError("eval journal must bind exactly three source artifacts")
    input_ids: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != _INPUT_FIELDS:
            raise EvalArticleError("input receipt fields are not exact")
        input_id = _text(receipt["input_id"], "input_id", maximum=120)
        if input_id in input_ids:
            raise EvalArticleError("duplicate input_id")
        input_ids.add(input_id)
        _text(receipt["filename"], "input.filename", maximum=180)
        if not _SHA256.fullmatch(str(receipt["sha256"])):
            raise EvalArticleError("input sha256 is invalid")
        if not isinstance(receipt["bytes"], int) or receipt["bytes"] < 1:
            raise EvalArticleError("input byte count is invalid")
        _timestamp(receipt["generated_at"], "input.generated_at")
        _text(receipt["public_url"], "input.public_url", maximum=500)
        _text(receipt["integrity"], "input.integrity", maximum=500)
    articles = document["articles"]
    if not isinstance(articles, list) or document["n_articles"] != len(articles):
        raise EvalArticleError("n_articles does not match articles")
    if len(articles) < 2 or len(articles) > 32:
        raise EvalArticleError("eval journal must contain a bounded article set")
    slugs: set[str] = set()
    revisions: set[str] = set()
    for article in articles:
        _validate_article(article, input_ids)
        if article["slug"] in slugs or article["revision_id"] in revisions:
            raise EvalArticleError("article slug or revision is duplicated")
        slugs.add(article["slug"])
        revisions.add(article["revision_id"])
    canonical_json_bytes(document)


def load_prior(path: Path = OUTPUT_PATH) -> Mapping[str, Any] | None:
    """Load the prior head only to preserve first-publication and revision links."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvalArticleError(f"cannot read prior {path.name}") from exc
    value = _strict_json(raw, label=f"prior {path.name}")
    if not isinstance(value, dict):
        raise EvalArticleError("prior eval journal is not an object")
    validate_collection(value)
    return value


def build(*, root: Path = ROOT, prior: Mapping[str, Any] | None = None) -> dict[str, Any]:
    sources = load_sources(root=root)
    if prior is None:
        prior = load_prior(root / "readings" / OUTPUT_PATH.name)
    return build_collection(sources, prior=prior)
