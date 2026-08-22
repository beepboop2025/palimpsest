"""Sealed weekly situation report: fused layers, frozen template, no model prose.

The China Brief is a six-hour CDT digest. The China situation desk is a per-event
interconnection of publisher reports. This report is the missing product: one
ranked answer to "what is the censor working hardest on this week", fused from
independent sealed readings (DDTI, Weibo join, GDELT, GitHub-refuge, board
alarm, forecast ledger, Generative Firewall when present).

Ranking stays on the DDTI threat score. Other layers annotate. A model does not
choose sensitivity, draft the narrative, or break a tie.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.collector_artifact import canonical_json_bytes, sha256_bytes, sha256_file
from processors.cdt_campaigns import cluster_ddti


SCHEMA_VERSION = "palimpsest-weekly-situation.v1"
TEMPLATE_ID = "weekly-situation-v1"
METHOD_VERSION = 1
TOP_N = 12

# Frozen template. Changing these strings is a method change: bump METHOD_VERSION.
TEMPLATE = {
    "id": TEMPLATE_ID,
    "version": METHOD_VERSION,
    "ranking_rule": (
        "DDTI threat descending; GDELT, Weibo and GitHub annotate; they never rerank"
    ),
    "model_policy": (
        "no model drafts the report or decides sensitivity; frozen templates only"
    ),
    "sections": [
        "headline",
        "working_hardest",
        "layer_state",
        "social_differential",
        "campaigns",
        "forecast_skill",
        "abstentions",
        "limitations",
    ],
}

INPUTS = {
    "ddti": "ddti-latest.json",
    "gdelt": "gdelt-latest.json",
    "weibo": "weibo-hotsearch-latest.json",
    "github_refuge": "github-refuge-latest.json",
    "board_alarm": "board-alarm-latest.json",
    "coverage_guard": "coverage-guard-latest.json",
    "forecast_ledger": "forecast-ledger-latest.json",
    "cross_layer": "cross-layer-latest.json",
    "gfi": "latest.json",
}

LIMITATIONS = (
    "The ranking is censor attention allocation from China Digital Times, not a deletion rate.",
    "A missing layer is an abstention, never a zero and never calm.",
    "GDELT and Weibo joins are exact-term matches against the current DDTI list.",
    "The Generative Firewall Index is a named-panel refusal reading, not a motive claim.",
    "Cross-layer coincidence counts elevated layers; it does not identify a common cause.",
    "GitHub-as-Refuge reports pressure against persisted prior-presence baselines only.",
)


class WeeklySituationError(ValueError):
    """The weekly report cannot be sealed from the supplied readings."""


def build_report(
    readings_dir: str | Path,
    *,
    now: datetime | None = None,
    top_n: int = TOP_N,
) -> dict[str, Any]:
    readings_dir = Path(readings_dir)
    loaded, abstentions = _load_inputs(readings_dir)
    generated_at = _iso(now)
    template_sha = sha256_bytes(canonical_json_bytes(TEMPLATE))

    ddti = loaded.get("ddti")
    if ddti is None:
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "template": {**TEMPLATE, "sha256": template_sha},
            "headline": "weekly situation abstains: the content-layer DDTI reading is missing",
            "working_hardest": [],
            "layer_state": _layer_state(loaded, abstentions),
            "social_differential": None,
            "campaigns": {"n_campaigns": 0, "campaigns": []},
            "forecast_skill": _forecast_skill(loaded.get("forecast_ledger")),
            "n_layers_present": _n_present(loaded),
            "trigger": "abstain",
            "inputs": {name: meta for name, meta in abstentions.items() if name in INPUTS}
            | {name: loaded[name]["_meta"] for name in loaded},
            "abstentions": [abstentions[name] for name in INPUTS if name in abstentions],
            "limitations": list(LIMITATIONS),
            "method_version": METHOD_VERSION,
        }
        return _seal(report)

    ranked = [row for row in (ddti.get("ranked") or []) if isinstance(row, Mapping)]
    gdelt_by_term = _gdelt_index(loaded.get("gdelt"))
    weibo_by_term = _weibo_index(loaded.get("weibo"))
    working = []
    for row in ranked[:top_n]:
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        gdelt = gdelt_by_term.get(term)
        weibo = weibo_by_term.get(term)
        layers = ["content"]
        if gdelt is not None:
            layers.append("global-news")
        if weibo is not None:
            layers.append("social")
        working.append(
            {
                "term": term,
                "domain": row.get("domain"),
                "threat": row.get("threat"),
                "attention": row.get("attention"),
                "novelty": row.get("novelty"),
                "is_new": bool(row.get("is_new")),
                "gdelt_label": None if gdelt is None else gdelt.get("label"),
                "weibo_regime": None if weibo is None else weibo.get("regime"),
                "layers": layers,
                "n_layers": len(layers),
                "sample_url": _sample_url(row),
            }
        )

    campaigns = cluster_ddti(ddti)
    social = _social_differential(loaded.get("weibo"))
    board = loaded.get("board_alarm")
    coincidence = None if board is None else board.get("layer_coincidence")
    elevated = [] if board is None else list(board.get("elevated_layers") or [])
    trigger = "cross-layer" if isinstance(coincidence, int) and coincidence >= 2 else "scheduled"
    gfi_clause = _gfi_clause(loaded.get("gfi"), abstentions.get("gfi"))
    coincidence_clause = (
        f"{len(elevated)} layer(s) elevated ({', '.join(elevated) or 'none'})"
        if board is not None
        else "board alarm abstained"
    )
    headline = (
        f"{ddti.get('n_terms', len(ranked))} DDTI terms from "
        f"{ddti.get('n_observations', 'unknown')} CDT observations. "
        f"{_n_present(loaded)} of {len(INPUTS)} fused layers present. "
        f"{coincidence_clause}. {gfi_clause}."
    )
    inputs = {name: loaded[name]["_meta"] for name in loaded}
    inputs.update({name: abstentions[name] for name in abstentions})
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "template": {**TEMPLATE, "sha256": template_sha},
        "headline": headline,
        "working_hardest": working,
        "layer_state": _layer_state(loaded, abstentions),
        "social_differential": social,
        "campaigns": {
            "n_campaigns": campaigns.get("n_campaigns", 0),
            "campaigns": campaigns.get("campaigns", []),
            "method": campaigns.get("method"),
        },
        "forecast_skill": _forecast_skill(loaded.get("forecast_ledger")),
        "n_layers_present": _n_present(loaded),
        "trigger": trigger,
        "inputs": inputs,
        "abstentions": [abstentions[name] for name in INPUTS if name in abstentions],
        "limitations": list(LIMITATIONS),
        "method_version": METHOD_VERSION,
    }
    return _seal(report)


def substance(report: Mapping[str, Any]) -> dict[str, Any]:
    """The sealed payload: everything except the look-time and the seal itself."""
    return {key: value for key, value in report.items() if key not in {"generated_at", "seal"}}


def _seal(report: dict[str, Any]) -> dict[str, Any]:
    digest = sha256_bytes(canonical_json_bytes(substance(report)))
    report["seal"] = {
        "payload_sha256": digest,
        "schema_version": SCHEMA_VERSION,
        "template_id": TEMPLATE_ID,
    }
    return report


def _load_inputs(readings_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    loaded: dict[str, Any] = {}
    abstentions: dict[str, dict[str, Any]] = {}
    for name, filename in INPUTS.items():
        path = readings_dir / filename
        if not path.is_file():
            abstentions[name] = {
                "source": name,
                "path": str(path.name),
                "status": "missing",
                "reason": f"{filename} is not on disk",
            }
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            abstentions[name] = {
                "source": name,
                "path": str(path.name),
                "status": "unreadable",
                "reason": str(exc),
            }
            continue
        if not isinstance(document, Mapping):
            abstentions[name] = {
                "source": name,
                "path": str(path.name),
                "status": "unreadable",
                "reason": "latest file is not a JSON object",
            }
            continue
        meta = {
            "source": name,
            "path": filename,
            "status": "ok",
            "sha256": sha256_file(path),
            "generated_at": _generated_at(document, name),
        }
        record = dict(document)
        record["_meta"] = meta
        loaded[name] = record
    return loaded, abstentions


def _generated_at(document: Mapping[str, Any], name: str) -> str | None:
    if name == "gfi":
        summary = document.get("summary")
        if isinstance(summary, Mapping):
            return summary.get("generated_at") or summary.get("date")
    value = document.get("generated_at")
    return value if isinstance(value, str) else None


def _gdelt_index(gdelt: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if gdelt is None:
        return {}
    out = {}
    for row in gdelt.get("ranked") or []:
        if isinstance(row, Mapping) and row.get("term"):
            out[str(row["term"])] = row
    return out


def _weibo_index(weibo: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if weibo is None:
        return {}
    out = {}
    for row in weibo.get("join") or []:
        if isinstance(row, Mapping) and row.get("term"):
            out[str(row["term"])] = row
    return out


def _sample_url(row: Mapping[str, Any]) -> str | None:
    samples = row.get("samples")
    if not isinstance(samples, list):
        return None
    for sample in samples:
        if isinstance(sample, Mapping):
            url = sample.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
    return None


def _social_differential(weibo: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if weibo is None:
        return None
    regimes = weibo.get("regimes") if isinstance(weibo.get("regimes"), Mapping) else {}
    return {
        "contained_visible": regimes.get("contained_visible"),
        "suppressed_invisible": regimes.get("suppressed_invisible"),
        "window_days": weibo.get("window_days"),
        "generated_at": weibo.get("generated_at"),
        "note": (
            "contained_visible: DDTI term still trends. suppressed_invisible: "
            "DDTI term never appears on the public hot-search board."
        ),
    }


def _forecast_skill(forecast: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if forecast is None:
        return None
    signals = forecast.get("signals") if isinstance(forecast.get("signals"), Mapping) else {}
    rows = []
    for name, payload in signals.items():
        if not isinstance(payload, Mapping):
            continue
        rows.append(
            {
                "signal": name,
                "wis": payload.get("wis"),
                "empirical_coverage": payload.get("empirical_coverage"),
                "beats_baseline": payload.get("beats_baseline"),
                "n_forecasts": payload.get("n_forecasts"),
                "n_misses": payload.get("n_misses"),
            }
        )
    rows.sort(key=lambda item: (item["wis"] is None, item["wis"] if item["wis"] is not None else 0, item["signal"]))
    return {
        "headline": forecast.get("headline"),
        "pooled_empirical_coverage": forecast.get("pooled_empirical_coverage"),
        "nominal_coverage": forecast.get("nominal_coverage"),
        "n_signals_scored": forecast.get("n_signals_scored"),
        "n_beating_baseline": forecast.get("n_beating_baseline"),
        "n_forecasts": forecast.get("n_forecasts"),
        "signals": rows,
    }


def _gfi_clause(gfi: Mapping[str, Any] | None, abstention: Mapping[str, Any] | None) -> str:
    if gfi is None:
        reason = "missing" if abstention is None else abstention.get("status", "missing")
        return f"GFI {reason}, not scored"
    summary = gfi.get("summary") if isinstance(gfi.get("summary"), Mapping) else {}
    value = summary.get("gfi")
    if not isinstance(value, (int, float)):
        return "GFI present but index not reported"
    lo = summary.get("gfi_lo")
    hi = summary.get("gfi_hi")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return f"GFI {value} (interval {lo} to {hi})"
    return f"GFI {value}"


def _layer_state(
    loaded: Mapping[str, Mapping[str, Any]],
    abstentions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    mapping = {
        "content": "ddti",
        "social": "weibo",
        "global-news": "gdelt",
        "platform": "github_refuge",
        "network-board": "board_alarm",
        "model": "gfi",
        "forecast": "forecast_ledger",
    }
    out = {}
    for layer, source in mapping.items():
        if source in loaded:
            out[layer] = {
                "status": "ok",
                "source": source,
                "generated_at": loaded[source]["_meta"].get("generated_at"),
                "sha256": loaded[source]["_meta"].get("sha256"),
            }
        else:
            out[layer] = {
                "status": "abstained",
                "source": source,
                "reason": (abstentions.get(source) or {}).get("reason"),
            }
    return out


def _n_present(loaded: Mapping[str, Any]) -> int:
    return len(loaded)


def _iso(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u2014", " - ").replace("\u2013", "-")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(report: Mapping[str, Any]) -> str:
    """Static HTML for Wayback and citation. Numbers come only from the sealed report."""
    headline = _esc(report.get("headline"))
    generated = _esc(report.get("generated_at"))
    seal = (report.get("seal") or {}).get("payload_sha256") or ""
    trigger = _esc(report.get("trigger"))
    rows = []
    for item in report.get("working_hardest") or []:
        url = item.get("sample_url")
        term_html = _esc(item.get("term"))
        if isinstance(url, str) and url.startswith("https://"):
            term_html = f'<a href="{_esc(url)}">{term_html}</a>'
        rows.append(
            "<tr>"
            f"<td>{term_html}</td>"
            f"<td>{_esc(item.get('domain'))}</td>"
            f"<td>{_esc(item.get('threat'))}</td>"
            f"<td>{_esc(item.get('gdelt_label') or 'no join')}</td>"
            f"<td>{_esc(item.get('weibo_regime') or 'no join')}</td>"
            f"<td>{_esc(', '.join(item.get('layers') or []))}</td>"
            "</tr>"
        )
    table = "\n".join(rows) or '<tr><td colspan="6">No ranked terms. The content layer abstained.</td></tr>'
    abstention_items = "".join(
        f"<li><code>{_esc(item.get('source'))}</code>: {_esc(item.get('reason'))}</li>"
        for item in report.get("abstentions") or []
    ) or "<li>No fused layer abstained.</li>"
    limits = "".join(f"<li>{_esc(item)}</li>" for item in report.get("limitations") or [])
    social = report.get("social_differential") or {}
    forecast = report.get("forecast_skill") or {}
    campaigns = report.get("campaigns") or {}
    campaign_items = "".join(
        f"<li>{_esc(item.get('n_terms'))} terms in "
        f"<a href='{_esc(item.get('url'))}'>{_esc(item.get('title') or item.get('url'))}</a></li>"
        for item in campaigns.get("campaigns") or []
    ) or "<li>No multi-term CDT articles in this window.</li>"
    score_rows = "".join(
        "<tr>"
        f"<td>{_esc(item.get('signal'))}</td>"
        f"<td>{_esc(item.get('wis'))}</td>"
        f"<td>{_esc(item.get('empirical_coverage'))}</td>"
        f"<td>{'yes' if item.get('beats_baseline') else 'no'}</td>"
        f"<td>{_esc(item.get('n_misses'))}</td>"
        "</tr>"
        for item in forecast.get("signals") or []
    ) or '<tr><td colspan="5">Forecast ledger abstained.</td></tr>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly situation report · Palimpsest</title>
<meta name="description" content="Sealed weekly fusion of DDTI, Weibo, GDELT, GitHub-refuge, board alarm and forecast skill. Frozen template. No model prose.">
<link rel="canonical" href="https://palimpsest.info/weekly-situation.html">
<meta name="robots" content="index, follow, max-snippet:-1">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<link rel="stylesheet" href="/dashboards/assets/tikto.css">
<link rel="stylesheet" href="/assets/shell.css">
<style>
  .ws {{ padding-bottom: 72px; }}
  .ws-head {{ padding: clamp(34px, 7vw, 64px) 0 8px; }}
  .ws-lede {{ color: var(--tk-text-2); max-width: 76ch; font-size: 15px; line-height: 1.65; }}
  .ws table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  .ws th, .ws td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--ps-edge-1); vertical-align: top; }}
  .ws th {{ color: var(--tk-text-3); font-weight: 600; }}
  .ws code {{ font-family: var(--tk-font-mono), ui-monospace, Menlo, monospace; font-size: 12px; }}
  .ws a {{ color: var(--tk-live, #06d6e0); }}
  .ws-note {{ padding: 14px 16px; border-left: 3px solid var(--tk-live, #06d6e0); margin: 18px 0; }}
  .ws pre {{ overflow-x: auto; padding: 14px 16px; border: 1px solid var(--ps-edge-1); }}
</style>
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "Dataset",
  "name": "Palimpsest weekly situation report",
  "url": "https://palimpsest.info/weekly-situation.html",
  "dateModified": "{generated}",
  "identifier": "{_esc(seal[:24])}",
  "creator": {{"@type": "Organization", "name": "Palimpsest", "url": "https://palimpsest.info/"}}
}}
</script>
</head>
<body class="ps">
<!--PS_NAV-->
<!--/PS_NAV-->
<main id="main" class="ps-wrap ws">
  <header class="ws-head">
    <p class="ps-kicker">Sealed weekly situation · trigger {trigger}</p>
    <h1 class="ps-h1">What the censor is working hardest on</h1>
    <p class="ws-lede">{headline}</p>
    <p class="ws-lede">Generated {generated}. Seal <code>{_esc(seal)}</code>. Frozen template <code>{TEMPLATE_ID}</code>. This is not a newspaper.</p>
  </header>
  <section>
    <h2 class="ps-section-head">Ranked terms</h2>
    <p class="ws-lede">Order is DDTI threat. Other layers only join. A blank join is a miss, not a negative.</p>
    <table>
      <thead><tr><th>Term</th><th>Domain</th><th>Threat</th><th>GDELT</th><th>Weibo</th><th>Layers</th></tr></thead>
      <tbody>
        {table}
      </tbody>
    </table>
  </section>
  <section>
    <h2 class="ps-section-head">Social differential</h2>
    <p>contained-visible: {_esc(social.get('contained_visible'))}. suppressed-invisible: {_esc(social.get('suppressed_invisible'))}.</p>
  </section>
  <section>
    <h2 class="ps-section-head">CDT campaigns</h2>
    <ul>{campaign_items}</ul>
  </section>
  <section>
    <h2 class="ps-section-head">Forecast scorecard</h2>
    <p class="ws-lede">{_esc(forecast.get('headline'))}</p>
    <table>
      <thead><tr><th>Signal</th><th>WIS (lower better)</th><th>Coverage</th><th>Beats baseline</th><th>Misses</th></tr></thead>
      <tbody>{score_rows}</tbody>
    </table>
  </section>
  <section>
    <h2 class="ps-section-head">Abstentions</h2>
    <ul>{abstention_items}</ul>
  </section>
  <section>
    <h2 class="ps-section-head">Limits</h2>
    <ul>{limits}</ul>
  </section>
  <div class="ws-note">
    <p>Reproduce: <code>python3 scripts/reproduce_all.py</code>. Cite this file: <a href="/cite.html#weekly-situation">cite a specific signal</a>. Challenge a number: <a href="/challenge.html">how to challenge</a>.</p>
  </div>
  <p>Canonical JSON: <a href="/readings/weekly-situation-latest.json"><code>readings/weekly-situation-latest.json</code></a>.</p>
</main>
<script src="/assets/shell.js" defer></script>
</body>
</html>
"""
