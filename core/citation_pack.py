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


SITE = "https://palimpsest.info"
REPO = "https://github.com/beepboop2025/palimpsest"


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
