"""Citation packs for a catalog dataset, or one signal on one day.

The observatory updates continuously. A citation that names only the homepage
cannot be checked. These helpers emit APA-style text and BibTeX that name the
dataset id, the latest or history file, the generated_at clock, and the
accessed date. A git commit is optional provenance, never a substitute for the
file hash.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import json
import re

from core.bri_observation import (
    BRIEconomicObservation,
    BRIObservationError,
    BUNDLE_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_bytes,
)


SITE = "https://palimpsest.info"
REPO = "https://github.com/beepboop2025/palimpsest"
BRI_WDI_PUBLIC_PATH = "readings/bri-economic-observations-latest.json"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BRI_WDI_BUNDLE_FIELDS = {
    "schema_version",
    "collection_id",
    "generated_at",
    "context_policy",
    "source",
    "registry_sha256",
    "coverage",
    "request_receipts",
    "observations_sha256",
    "observations",
}
_BRI_COUNTRY_NAMES = {
    "CHN": "China",
    "MMR": "Myanmar",
    "PAK": "Pakistan",
}


class CitationError(ValueError):
    """The requested citation cannot be built from the catalog."""


def cite_dataset(
    catalog: Mapping[str, Any],
    dataset_id: str,
    *,
    accessed: str | date | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    dataset = _dataset(catalog, dataset_id)
    accessed_on = _accessed(accessed)
    latest = dataset.get("latest") or ""
    url = _url(latest) if latest else f"{SITE}/data.html"
    year = _year(accessed_on)
    title = str(dataset.get("name") or dataset_id)
    note = (
        f"Palimpsest dataset `{dataset_id}`; landing {dataset.get('landing_page') or 'n/a'}; "
        f"accessed {accessed_on}"
    )
    if commit:
        note += f"; git {commit[:12]}"
    key = _bib_key(dataset_id)
    bibtex = (
        f"@misc{{{key},\n"
        f"  title        = {{{title}}},\n"
        f"  author       = {{Palimpsest}},\n"
        f"  year         = {{{year}}},\n"
        f"  url          = {{{url}}},\n"
        f"  howpublished = {{Palimpsest Evidence Atlas}},\n"
        f"  note         = {{{note}}}\n"
        f"}}"
    )
    apa = (
        f"Palimpsest. ({year}). {title} [dataset]. Palimpsest Evidence Atlas. "
        f"{url} (accessed {accessed_on})."
    )
    return {
        "dataset_id": dataset_id,
        "accessed": accessed_on,
        "url": url,
        "apa": apa,
        "bibtex": bibtex,
        "challenge_url": f"{SITE}/challenge.html#{dataset_id}",
        "latest": latest,
        "history": dataset.get("history"),
        "method": dataset.get("method"),
        "commit": commit,
    }


def cite_signal_day(
    catalog: Mapping[str, Any],
    dataset_id: str,
    day: str,
    *,
    history_path: Path | None = None,
    accessed: str | date | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """Cite one calendar day of a signal. Abstains if that day is not in history."""
    base = cite_dataset(catalog, dataset_id, accessed=accessed, commit=commit)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise CitationError("day must be YYYY-MM-DD")
    row = None
    if history_path is not None and history_path.is_file():
        row = _row_for_day(history_path, day)
    if row is None:
        return {
            **base,
            "day": day,
            "abstention": {
                "code": "day-not-in-history",
                "reason": (
                    f"no history row for {dataset_id} on {day}; cite the dataset "
                    "or name a day that exists in the history file"
                ),
            },
        }
    generated_at = str(row.get("generated_at") or day)
    history = base.get("history") or ""
    url = _url(history) if history else base["url"]
    year = day[:4]
    title = f"{base['dataset_id']} reading for {day}"
    note = f"generated_at {generated_at}; accessed {base['accessed']}"
    key = _bib_key(f"{dataset_id}-{day}")
    bibtex = (
        f"@misc{{{key},\n"
        f"  title        = {{{title}}},\n"
        f"  author       = {{Palimpsest}},\n"
        f"  year         = {{{year}}},\n"
        f"  url          = {{{url}}},\n"
        f"  howpublished = {{Palimpsest sealed reading}},\n"
        f"  note         = {{{note}}}\n"
        f"}}"
    )
    apa = (
        f"Palimpsest. ({year}). {title} [dataset]. {url} "
        f"(generated {generated_at}; accessed {base['accessed']})."
    )
    return {
        **base,
        "day": day,
        "generated_at": generated_at,
        "history_row": {k: row[k] for k in row if k in ("generated_at", "headline", "n_terms", "n_forecasts")},
        "url": url,
        "apa": apa,
        "bibtex": bibtex,
        "abstention": None,
    }


def cite_bri_wdi_observation(
    bundle: Mapping[str, Any],
    observation_id: str,
    *,
    accessed: str | date | None = None,
    bundle_path: str = BRI_WDI_PUBLIC_PATH,
) -> dict[str, Any]:
    """Cite one authenticated World Bank WDI row from the BRI context bundle.

    The result keeps source-marked forecasts and unavailable values distinct
    from observations.  It also repeats the bundle, row, source-row, and raw
    response identities so a formatted citation does not detach a value from
    the evidence object that supplied it.
    """

    document, observation, request = _bri_wdi_observation(bundle, observation_id)
    accessed_on = _accessed(accessed)
    public_url = _url(bundle_path)
    source_url = observation.evidence_url
    period = _bri_period_label(observation)
    country = _BRI_COUNTRY_NAMES[observation.country_code]
    value_text = (
        json.dumps(observation.value, ensure_ascii=False, allow_nan=False)
        if observation.value is not None
        else None
    )

    if observation.evidence_state == "observed":
        claim = (
            f"{country}, indicator {observation.indicator_id}, {period}: "
            f"{value_text} {observation.unit} (observed)."
        )
        citation_state = f"observed value: {value_text} {observation.unit}"
        numeric_claim = True
    elif observation.evidence_state == "forecast":
        claim = (
            f"{country}, indicator {observation.indicator_id}, {period}: "
            f"{value_text} {observation.unit} (World Bank source-marked forecast)."
        )
        citation_state = (
            f"World Bank source-marked forecast: {value_text} {observation.unit}"
        )
        numeric_claim = True
    else:
        claim = (
            f"{country}, indicator {observation.indicator_id}, {period}: "
            "source value unavailable; no numeric claim."
        )
        citation_state = "source value unavailable; no numeric claim"
        numeric_claim = False

    boundary_statement = (
        "Country-period national economic context only; not evidence of a BRI "
        "project, corridor, actor, or causal effect."
    )
    source_last_updated = observation.source_dataset_last_updated.isoformat()
    source_release_upper_bound = str(request["source_release_upper_bound"])
    retrieved_at = str(request["retrieved_at"])
    generated_at = str(document["generated_at"])
    title = (
        f"World Development Indicators: {observation.indicator_id} "
        f"({observation.series_id}) for {country}, {period}"
    )
    note = (
        f"{citation_state}; unit {observation.unit}; evidence_state "
        f"{observation.evidence_state}; dataset generated_at {generated_at}; "
        f"source dataset last updated {source_last_updated}; source release upper "
        f"bound {source_release_upper_bound}; retrieved_at {retrieved_at}; "
        f"collection SHA-256 {document['collection_id']}; observation SHA-256 "
        f"{observation.observation_id}; source-row SHA-256 "
        f"{observation.source_row_sha256}; raw-response SHA-256 "
        f"{observation.raw_response_sha256}; observations SHA-256 "
        f"{document['observations_sha256']}; registry SHA-256 "
        f"{document['registry_sha256']}; request SHA-256 {observation.request_id}; "
        f"acquisition SHA-256 {observation.acquisition_id}; CC-BY-4.0; attribution "
        f"World Bank, World Development Indicators; {boundary_statement} Source "
        f"{source_url}; accessed {accessed_on}"
    )
    key = _bib_key(f"wdi-{observation.observation_id[:16]}")
    year = source_last_updated[:4]
    bibtex = (
        f"@misc{{{key},\n"
        f"  title        = {{{title} [{observation.evidence_state}]}},\n"
        f"  author       = {{World Bank}},\n"
        f"  year         = {{{year}}},\n"
        f"  url          = {{{public_url}}},\n"
        f"  howpublished = {{Palimpsest normalized World Bank WDI bundle}},\n"
        f"  note         = {{{note}}}\n"
        f"}}"
    )
    apa = (
        f"World Bank. ({year}). {title} [{citation_state}; national economic "
        f"context]. {source_url}. Palimpsest normalized bundle {public_url} "
        f"(dataset generated {generated_at}; source dataset last updated "
        f"{source_last_updated}; source release upper bound "
        f"{source_release_upper_bound}; retrieved {retrieved_at}; CC-BY-4.0; "
        f"accessed {accessed_on}). {boundary_statement}"
    )

    return {
        "dataset": {
            "name": "World Development Indicators",
            "publisher": "World Bank",
            "url": public_url,
            "source_url": source_url,
            "generated_at": generated_at,
        },
        "source": {
            "source_id": observation.source_id,
            "publisher": observation.publisher,
            "evidence_url": source_url,
        },
        "country": {"code": observation.country_code, "name": country},
        "indicator": {
            "indicator_id": observation.indicator_id,
            "series_id": observation.series_id,
        },
        "period": {
            "start": observation.period_start.isoformat(),
            "end": observation.period_end.isoformat(),
            "label": period,
        },
        "unit": observation.unit,
        "evidence_state": observation.evidence_state,
        "value": observation.value,
        "numeric_claim": numeric_claim,
        "claim": claim,
        "clocks": {
            "dataset_generated_at": generated_at,
            "source_dataset_last_updated": source_last_updated,
            "source_release_upper_bound": source_release_upper_bound,
            "retrieved_at": retrieved_at,
        },
        "hashes": {
            "collection_id": str(document["collection_id"]),
            "observation_id": observation.observation_id,
            "observations_sha256": str(document["observations_sha256"]),
            "registry_sha256": str(document["registry_sha256"]),
            "source_row_sha256": observation.source_row_sha256,
            "raw_response_sha256": observation.raw_response_sha256,
        },
        "request_id": observation.request_id,
        "acquisition_id": observation.acquisition_id,
        "rights": observation.rights.to_dict(),
        "boundary": {
            "context_scope": observation.context_scope,
            "causality_boundary": observation.causality_boundary,
            "statement": boundary_statement,
        },
        "accessed": accessed_on,
        "url": public_url,
        "source_url": source_url,
        "apa": apa,
        "bibtex": bibtex,
    }


def catalog_bibtex(catalog: Mapping[str, Any], *, accessed: str | date | None = None) -> str:
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        raise CitationError("catalog.datasets is missing")
    blocks = []
    for dataset in datasets:
        if not isinstance(dataset, Mapping) or not dataset.get("id"):
            continue
        blocks.append(cite_dataset(catalog, str(dataset["id"]), accessed=accessed)["bibtex"])
    return "\n\n".join(blocks) + "\n"


def _bri_wdi_observation(
    bundle: Mapping[str, Any], observation_id: str
) -> tuple[dict[str, Any], BRIEconomicObservation, Mapping[str, Any]]:
    if not isinstance(bundle, Mapping) or set(bundle) != _BRI_WDI_BUNDLE_FIELDS:
        raise CitationError("BRI WDI bundle fields changed")
    document = dict(bundle)
    if document.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise CitationError("unsupported BRI WDI bundle schema_version")
    if type(observation_id) is not str or not _SHA256.fullmatch(observation_id):
        raise CitationError("observation_id must be a lowercase SHA-256 digest")
    collection_id = document.get("collection_id")
    if type(collection_id) is not str or not _SHA256.fullmatch(collection_id):
        raise CitationError("BRI WDI collection_id is invalid")

    observations = document.get("observations")
    if type(observations) is not list or not observations:
        raise CitationError("BRI WDI observations are missing")
    observations_sha256 = document.get("observations_sha256")
    registry_sha256 = document.get("registry_sha256")
    try:
        computed_observations_sha256 = sha256_bytes(canonical_json_bytes(observations))
    except (TypeError, ValueError, RecursionError) as exc:
        raise CitationError(f"BRI WDI observations are not canonical JSON: {exc}") from exc
    if type(observations_sha256) is not str or not _SHA256.fullmatch(
        observations_sha256
    ) or computed_observations_sha256 != observations_sha256:
        raise CitationError("BRI WDI observations_sha256 does not authenticate rows")
    if type(registry_sha256) is not str or not _SHA256.fullmatch(registry_sha256):
        raise CitationError("BRI WDI registry_sha256 is invalid")

    collection_payload = dict(document)
    collection_payload.pop("collection_id")
    try:
        computed_collection_id = sha256_bytes(canonical_json_bytes(collection_payload))
    except (TypeError, ValueError, RecursionError) as exc:
        raise CitationError(f"BRI WDI bundle is not canonical JSON: {exc}") from exc
    if computed_collection_id != collection_id:
        raise CitationError("BRI WDI collection_id does not authenticate the bundle")

    selected = None
    seen_ids: set[str] = set()
    state_counts = {"observed": 0, "forecast": 0, "unavailable": 0}
    for index, row in enumerate(observations):
        try:
            candidate = BRIEconomicObservation.from_dict(row)
        except (BRIObservationError, TypeError, ValueError) as exc:
            raise CitationError(f"BRI WDI observation {index} is invalid: {exc}") from exc
        if candidate.observation_id in seen_ids:
            raise CitationError("BRI WDI bundle contains duplicate observation_id values")
        seen_ids.add(candidate.observation_id)
        state_counts[candidate.evidence_state] += 1
        if candidate.observation_id == observation_id:
            selected = candidate
    if selected is None:
        raise CitationError(f"unknown BRI WDI observation {observation_id!r}")

    coverage = document.get("coverage")
    if not isinstance(coverage, Mapping):
        raise CitationError("BRI WDI coverage is missing")
    expected_counts = {
        "observed": coverage.get("observed_rows"),
        "forecast": coverage.get("forecast_rows"),
        "unavailable": coverage.get("unavailable_rows"),
    }
    if coverage.get("source_rows") != len(observations) or state_counts != expected_counts:
        raise CitationError("BRI WDI coverage does not reconcile with observations")

    source = document.get("source")
    expected_source = {
        "source_id": "world_bank_wdi",
        "name": "World Development Indicators",
        "publisher": "World Bank",
        **selected.rights.to_dict(),
    }
    if not isinstance(source, Mapping) or any(
        source.get(key) != value for key, value in expected_source.items()
    ):
        raise CitationError("BRI WDI source or rights are detached from the observation")
    policy = document.get("context_policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("scope") != selected.context_scope
        or policy.get("causality_boundary") != selected.causality_boundary
    ):
        raise CitationError("BRI WDI context boundary is detached from the observation")

    requests = document.get("request_receipts")
    if type(requests) is not list or len(requests) != 1 or not isinstance(requests[0], Mapping):
        raise CitationError("BRI WDI bundle must carry one request receipt")
    request = requests[0]
    expected_request = {
        "acquisition_id": selected.acquisition_id,
        "request_id": selected.request_id,
        "evidence_url": selected.evidence_url,
        "raw_response_sha256": selected.raw_response_sha256,
        "dataset_last_updated": selected.source_dataset_last_updated.isoformat(),
        "source_release_upper_bound": _utc_text(selected.source_release_upper_bound),
        "retrieved_at": _utc_text(selected.retrieved_at),
    }
    if any(request.get(key) != value for key, value in expected_request.items()):
        raise CitationError("BRI WDI request receipt is detached from the observation")
    if document.get("generated_at") != expected_request["retrieved_at"]:
        raise CitationError("BRI WDI generated_at does not match the retrieval clock")
    return document, selected, request


def _bri_period_label(observation: BRIEconomicObservation) -> str:
    if (
        observation.period_start.year == observation.period_end.year
        and observation.period_start.month == 1
        and observation.period_start.day == 1
        and observation.period_end.month == 12
        and observation.period_end.day == 31
    ):
        return str(observation.period_start.year)
    return f"{observation.period_start.isoformat()} to {observation.period_end.isoformat()}"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dataset(catalog: Mapping[str, Any], dataset_id: str) -> Mapping[str, Any]:
    datasets = catalog.get("datasets")
    if not isinstance(datasets, list):
        raise CitationError("catalog.datasets is missing")
    for dataset in datasets:
        if isinstance(dataset, Mapping) and dataset.get("id") == dataset_id:
            return dataset
    raise CitationError(f"unknown dataset {dataset_id!r}")


def _accessed(value: str | date | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _year(accessed_on: str) -> str:
    return accessed_on[:4] if len(accessed_on) >= 4 else "2026"


def _url(repo_path: str) -> str:
    return f"{SITE}/{repo_path.lstrip('/')}"


def _bib_key(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return f"palimpsest-{cleaned}"


def _row_for_day(path: Path, day: str) -> dict[str, Any] | None:
    match = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = str(row.get("generated_at") or "")
        if stamp.startswith(day):
            match = row
    return match
