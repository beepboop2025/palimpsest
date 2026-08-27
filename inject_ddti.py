#!/usr/bin/env python3
"""Publish a computed DDTI index into the Palimpsest Pages site.

Reads a DDTI index JSON (ranked censored terms with threat/attention/novelty,
produced by social_scraper's scripts.ddti_live_pull) and:
  1. Injects it as the __DDTI_EMBED__ snapshot into the site's dashboard HTML so
     opening palimpsest.info's DDTI dashboard shows the LATEST scraped signal.
  2. Writes readings/ddti-latest.json (machine/AI-readable) + appends a compact
     row to readings/ddti-history.jsonl (the public time-series).

Idempotent: only rewrites files whose content actually changed, so the caller
can skip an empty commit when nothing moved.

Usage: inject_ddti.py --index path/to/index.json [--repo .]
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

DASHBOARDS = ["dashboards/ddti_dashboard.html", "dashboards/ddti_observatory.html"]
EMBED_MARKER = "<!--DDTI_EMBED-->"
DDTI_SCOPE = "censor_attention_allocation (numerator-only; not a true deletion rate)"

# The dashboard is a presentation surface, not an observation archive. Keeping a
# hard byte boundary here prevents a future collector field from silently turning
# into tens of megabytes of executable HTML again. The current projection is well
# below this ceiling while still retaining every value either dashboard reads.
MAX_DDTI_EMBED_BYTES = 512 * 1024
MAX_RANKED_TERMS = 10_000
MAX_WINDOW_DAYS = 3_650
MAX_COUNT = 100_000_000
MAX_SCORE = 1_000_000.0

_EMBED_BLOCK = re.compile(
    re.escape(EMBED_MARKER) + r"(<script>window\.__DDTI_EMBED__=.*?</script>)?",
    re.DOTALL,
)

# CDT structural / editorial tags that are not censorship TOPICS — never let one
# be the public headline signal. Matched case-insensitively, exact term.
NOISE_TERMS = {
    "main photo", "photo", "image", "featured", "video", "translation",
    "cdt highlights", "level 2 article", "level 3 article", "china", "chinese",
    "news", "society", "gallery", "caption", "cdt", "china digital times",
}


def _denoise(ranked: list[dict]) -> list[dict]:
    return [r for r in ranked if r.get("term", "").strip().lower() not in NOISE_TERMS]


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def _mapping(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _string(value, path: str, *, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{path} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{path} exceeds {max_length} characters")
    return value


def _number(value, path: str, *, minimum: float, maximum: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be in {minimum:g}..{maximum:g}")
    return value


def _integer(value, path: str, *, maximum: int = MAX_COUNT) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{path} must be an integer in 0..{maximum}")
    return value


def _generated_at(value) -> tuple[str, str]:
    stamp = _string(value, "generated_at", max_length=64)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("generated_at must carry a UTC offset")
    return stamp, parsed.strftime("%Y-%m-%dT%H:%MZ")


def _counts(index: dict) -> dict:
    supplied = index.get("counts")
    if supplied is not None:
        supplied = _mapping(supplied, "counts")
        terms = supplied.get("terms")
        observations = supplied.get("observations")
    else:
        terms = index.get("n_terms")
        observations = index.get("n_observations_used", index.get("n_observations"))
    return {
        "terms": _integer(terms, "counts.terms"),
        "observations": _integer(observations, "counts.observations"),
    }


def presentation_projection(index: dict) -> dict:
    """Return the one bounded DDTI shape consumed by both dashboards.

    Collector snapshots intentionally retain observation records, provenance and
    archive joins. None of that belongs in an executable HTML document. This
    function validates the presentation contract and copies only fields read by
    the clients; unknown collector fields cannot bleed into the embed.

    ``n_observations`` remains accepted as an input alias because it is the
    canonical name in ``readings/ddti-latest.json``. Output has one shape only.
    """

    index = _mapping(index, "index")
    generated_at, _clock = _generated_at(index.get("generated_at"))
    scope = _string(index.get("scope", DDTI_SCOPE), "scope", max_length=512)

    raw_window = _mapping(index.get("window"), "window")
    current_days = _number(
        raw_window.get("current_days"), "window.current_days",
        minimum=0.001, maximum=MAX_WINDOW_DAYS,
    )
    history_days = _number(
        raw_window.get("history_days"), "window.history_days",
        minimum=0.001, maximum=MAX_WINDOW_DAYS,
    )
    if history_days < current_days:
        raise ValueError("window.history_days must be at least window.current_days")

    counts = _counts(index)
    raw_ranked = index.get("ranked")
    if not isinstance(raw_ranked, list) or not raw_ranked:
        raise ValueError("ranked must be a non-empty array")
    if len(raw_ranked) > MAX_RANKED_TERMS:
        raise ValueError(f"ranked exceeds {MAX_RANKED_TERMS} terms")
    if counts["terms"] < len(raw_ranked):
        raise ValueError("counts.terms must not be smaller than ranked")

    ranked = []
    for position, raw in enumerate(raw_ranked):
        path = f"ranked[{position}]"
        raw = _mapping(raw, path)
        burst = raw.get("burst_ratio")
        if burst is not None:
            burst = _number(
                burst, f"{path}.burst_ratio", minimum=0.0, maximum=MAX_SCORE,
            )
        is_new = raw.get("is_new")
        if type(is_new) is not bool:
            raise ValueError(f"{path}.is_new must be a boolean")
        samples = raw.get("samples")
        if not isinstance(samples, list) or len(samples) > 12:
            raise ValueError(f"{path}.samples must be an array of at most 12 items")
        projected_samples = []
        for sample_position, sample in enumerate(samples):
            sample_path = f"{path}.samples[{sample_position}]"
            sample = _mapping(sample, sample_path)
            projected_samples.append({
                "title": _string(
                    sample.get("title"), f"{sample_path}.title",
                    max_length=1_000, allow_empty=True,
                ),
            })
        ranked.append({
            "term": _string(raw.get("term"), f"{path}.term", max_length=1_000),
            "domain": _string(raw.get("domain"), f"{path}.domain", max_length=64),
            "threat": _number(
                raw.get("threat"), f"{path}.threat", minimum=0.0, maximum=MAX_SCORE,
            ),
            "attention": _number(
                raw.get("attention"), f"{path}.attention", minimum=0.0,
                maximum=MAX_SCORE,
            ),
            "novelty": _number(
                raw.get("novelty"), f"{path}.novelty", minimum=0.0, maximum=1.0,
            ),
            "burst_ratio": burst,
            "is_new": is_new,
            "recent_count": _integer(raw.get("recent_count"), f"{path}.recent_count"),
            "hist_count": _integer(raw.get("hist_count"), f"{path}.hist_count"),
            "first_seen": _string(
                raw.get("first_seen"), f"{path}.first_seen", max_length=64,
            ),
            "samples": projected_samples,
        })

    return {
        "generated_at": generated_at,
        "scope": scope,
        "window": {"current_days": current_days, "history_days": history_days},
        "counts": counts,
        "ranked": ranked,
    }


def render_embed_block(index: dict) -> str:
    """Render compact deterministic JSON that cannot terminate its script tag."""

    projection = presentation_projection(index)
    _stamp, clock = _generated_at(projection["generated_at"])
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).translate(str.maketrans({
        "<": r"\u003c",
        ">": r"\u003e",
        "&": r"\u0026",
        "\u2028": r"\u2028",
        "\u2029": r"\u2029",
    }))
    block = (
        f"{EMBED_MARKER}<script>window.__DDTI_EMBED__={payload};"
        f"window.__DDTI_EMBED_AT__={json.dumps(clock)};</script>"
    )
    size = len(block.encode("utf-8"))
    if size > MAX_DDTI_EMBED_BYTES:
        raise ValueError(
            f"DDTI presentation embed is {size} bytes; maximum is "
            f"{MAX_DDTI_EMBED_BYTES}"
        )
    return block


def inject_dashboard(path: Path, index: dict) -> str:
    if not path.exists():
        raise ValueError(f"DDTI dashboard is missing: {path}")
    html = path.read_text(encoding="utf-8")
    marker_count = html.count(EMBED_MARKER)
    matches = list(_EMBED_BLOCK.finditer(html))
    assignments = html.count("window.__DDTI_EMBED__=")
    if marker_count != 1 or len(matches) != 1:
        raise ValueError(
            f"DDTI dashboard must contain exactly one embed marker: {path}"
        )
    has_replaceable_block = matches[0].group(1) is not None
    expected_assignments = 1 if has_replaceable_block else 0
    if assignments != expected_assignments:
        raise ValueError(
            f"DDTI dashboard contains a malformed or duplicate embed block: {path}"
        )
    block = render_embed_block(index)
    new = _EMBED_BLOCK.sub(lambda _match: block, html, count=1)
    if (
        new.count(EMBED_MARKER) != 1
        or new.count("window.__DDTI_EMBED__=") != 1
    ):
        raise ValueError(f"DDTI dashboard embed replacement was not unique: {path}")
    return "updated" if _write_if_changed(path, new) else "unchanged"


def publish_index_file(index_path: str | Path, repo_path: str | Path = ".") -> list[str]:
    """Publish one computed index and return the files that changed.

    Keeping the file-to-site adapter callable lets the always-on measurement
    node use the exact same publication bytes as the public workflow without
    invoking a shell command or teaching the collector a second output schema.
    """

    repo = Path(repo_path).resolve()
    index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    presentation_projection(index)  # reject malformed collector input before any write
    ranked = _denoise(index.get("ranked", []))
    index["ranked"] = ranked  # publish the cleaned ranking everywhere
    if not ranked:
        print("no ranked terms in index — refusing to publish an empty snapshot")
        raise SystemExit(2)

    changed = []

    # 1. dashboards
    for rel in DASHBOARDS:
        status = inject_dashboard(repo / rel, index)
        print(f"  {rel}: {status}")
        if status == "updated":
            changed.append(rel)

    # 2. machine-readable readings/ddti-latest.json
    readings = repo / "readings"
    readings.mkdir(exist_ok=True)
    latest = readings / "ddti-latest.json"
    presentation = presentation_projection(index)
    public = {
        "generated_at": index["generated_at"],
        "window": index.get("window"),
        "n_terms": presentation["counts"]["terms"],
        "n_observations": presentation["counts"]["observations"],
        "source_feeds": index.get("source_feeds"),
        # How much of the archive this reading actually saw, and which of the
        # censorship-specific CDT roles arrived. The three-week outage was only ever
        # visible because source_feeds published its own 403s; feed_health makes the
        # next narrowing visible from the reading alone, without reading the ranking.
        "feed_health": index.get("feed_health"),
        "ranked": ranked,
        "citation": "Palimpsest — an open observatory of authoritarian censorship, "
                    "palimpsest.info. DDTI censored-term index, provenance-tracked.",
    }
    if _write_if_changed(latest, json.dumps(public, ensure_ascii=False, indent=2)):
        changed.append("readings/ddti-latest.json")

    # 3. append-only public time-series
    top = ranked[0]
    health = index.get("feed_health") or {}
    row = {
        "generated_at": index["generated_at"],
        "n_terms": presentation["counts"]["terms"],
        "n_new": sum(1 for r in ranked if r.get("is_new")),
        "top_term": top.get("term"),
        "top_threat": top.get("threat"),
        # Coverage travels with the time-series so a narrowing signal is diagnosable
        # from the JSONL alone: a run of rows where days_covered collapses or
        # roles_missing grows is a source outage, not a quiet censor.
        "n_observations": index.get("n_observations_used"),
        "days_covered": health.get("days_covered"),
        "pages_ok": health.get("pages_ok"),
        "roles_missing": health.get("roles_missing"),
    }
    hist = readings / "ddti-history.jsonl"
    prev = hist.read_text(encoding="utf-8") if hist.exists() else ""
    # avoid duplicate consecutive rows (same generated_at)
    if index["generated_at"] not in prev:
        with open(hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        changed.append("readings/ddti-history.jsonl")

    print(f"\nchanged files: {changed if changed else 'none'}")
    print(f"top term: {top.get('term')} (threat {top.get('threat')})")
    return changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()

    changed = publish_index_file(args.index, args.repo)
    # exit 0 if changed, 3 if nothing changed (caller skips commit)
    raise SystemExit(0 if changed else 3)


if __name__ == "__main__":
    main()
