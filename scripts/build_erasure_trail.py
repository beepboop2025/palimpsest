"""Journalist erasure trail — flatten public observations into citeable rows.

Offline fusion of committed readings. The clock is the newest input
``generated_at``, never wall-clock. Public deletion ledgers are included only
when a committed file exists; the builder does not invent a live ledger round.

A journalist who is not the operator should be able to find a public deletion
or reconstruction, see first-seen / last-seen / snapshots / hashes / source
URLs, export the row, and cite it. The desk states what is captured (public
posts, deletions, archives, GFW injector telemetry from separate instruments)
and what is not (private WeChat, classified systems, in-country accounts).

Usage:
    PYTHONPATH=. python3 -m scripts.build_erasure_trail
    PYTHONPATH=. python3 -m scripts.build_erasure_trail --check
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.china_observation import content_sha256, iso_z, public_text
from scripts import site_nav


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
PAGE_DIR = ROOT / "news" / "china" / "erasure"
JSON_OUT = READINGS / "erasure-trail-latest.json"
CSV_OUT = READINGS / "erasure-trail.csv"
HIST = READINGS / "erasure-trail-history.jsonl"
HTML_OUT = PAGE_DIR / "index.html"
SITE = "https://palimpsest.info"
METHOD_VERSION = 1
SCHEMA_VERSION = "palimpsest-erasure-trail.v1"

INPUT_FILES = (
    "undertext-latest.json",
    "wayback-latest.json",
    "weibo-hotsearch-latest.json",
    "ddti-latest.json",
    "public-deletion-ledgers-latest.json",
)

ROW_FIELDS = (
    "key",
    "title",
    "excerpt",
    "source",
    "source_url",
    "first_seen",
    "last_seen",
    "last_confirmed_alive",
    "deletion_signal",
    "content_sha256",
    "wayback_lookup",
    "wayback_snapshot",
    "archive_today_lookup",
    "gazetteer",
    "collector",
    "cite",
)

HONESTY_CAPTURES = (
    "Public posts that were already published, public deletion and blocking "
    "ledgers, Wayback reconstructions of those public URLs, and GFW injector "
    "telemetry from separate instruments (OONI, Censored Planet, Inside View, "
    "Bleedthrough)."
)
HONESTY_DOES_NOT = (
    "Private WeChat, classified systems, in-country accounts, follower graphs, "
    "comments, locations, media binaries, or consumer profiles."
)
HONESTY_LIVE = (
    "This desk never fabricates a live reading. A missing ledger or silent "
    "collector is a coverage gap, not proof of calm and not a zero."
)

_TOP_FIELDS = frozenset({
    "schema_version",
    "generated_at",
    "source",
    "method",
    "scope",
    "safety",
    "honesty",
    "n_rows",
    "inputs",
    "rows",
})
_ROW_FIELDS = frozenset(ROW_FIELDS)


def _load_json(name: str, readings_dir: Path) -> dict[str, Any] | None:
    path = readings_dir / name
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _wayback_timestamp(value: Any) -> str | None:
    """Accept a CDX 14-digit stamp or an ISO timestamp. Never invent one."""

    if isinstance(value, str) and len(value) == 14 and value.isdigit():
        return (
            f"{value[0:4]}-{value[4:6]}-{value[6:8]}T"
            f"{value[8:10]}:{value[10:12]}:{value[12:14]}Z"
        )
    return iso_z(value)


def _parse_generated_at(value: Any) -> datetime | None:
    stamp = iso_z(value)
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def fusion_clock(payloads: Mapping[str, Mapping[str, Any] | None]) -> datetime | None:
    """Newest committed input timestamp. Never invents a later clock."""

    newest: datetime | None = None
    for payload in payloads.values():
        if not payload:
            continue
        parsed = _parse_generated_at(payload.get("generated_at"))
        if parsed is None:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _https(url: Any) -> str:
    text = public_text(url, limit=2048)
    return text if text.startswith("https://") else ""


def _sha(value: Any) -> str:
    if isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    ):
        return value
    return ""


def _archive_url(archive: Any, key: str) -> str:
    if not isinstance(archive, dict):
        return ""
    return _https(archive.get(key))


def _gazetteer_label(hits: Any) -> str:
    labels: list[str] = []
    if not isinstance(hits, list):
        return ""
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        zh = public_text(hit.get("zh"), limit=80)
        en = public_text(hit.get("en"), limit=160)
        label = " — ".join(part for part in (zh, en) if part)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 6:
            break
    return "; ".join(labels)


def _cite(row: Mapping[str, str]) -> str:
    url = row["source_url"] or f"{SITE}/news/china/erasure/#{row['key']}"
    first = row["first_seen"] or "unknown"
    last = row["last_seen"] or "unknown"
    digest = row["content_sha256"] or "none"
    return (
        f"Palimpsest erasure trail ({row['key']}), “{row['title']}”, "
        f"first seen {first}, last seen {last}, SHA-256 {digest}. "
        f"Public source: {url}. "
        f"Desk: {SITE}/news/china/erasure/#{row['key']}"
    )


def _row(
    *,
    title: str,
    excerpt: str,
    source: str,
    source_url: str,
    first_seen: str | None,
    last_seen: str | None,
    last_confirmed_alive: str | None,
    deletion_signal: str,
    content_sha256_value: str,
    wayback_lookup: str,
    wayback_snapshot: str,
    archive_today_lookup: str,
    gazetteer: str,
    collector: str,
) -> dict[str, str]:
    key = content_sha256(collector, source, source_url, title)[:32]
    row = {
        "key": key,
        "title": public_text(title, limit=240) or "(untitled public record)",
        "excerpt": public_text(excerpt, limit=400),
        "source": public_text(source, limit=80) or "unknown",
        "source_url": source_url,
        "first_seen": iso_z(first_seen) or "",
        "last_seen": iso_z(last_seen) or "",
        "last_confirmed_alive": iso_z(last_confirmed_alive) or "",
        "deletion_signal": public_text(deletion_signal, limit=80),
        "content_sha256": content_sha256_value,
        "wayback_lookup": wayback_lookup,
        "wayback_snapshot": wayback_snapshot,
        "archive_today_lookup": archive_today_lookup,
        "gazetteer": gazetteer,
        "collector": public_text(collector, limit=40) or "unknown",
        "cite": "",
    }
    row["cite"] = _cite(row)
    return row


def _from_observation(obs: Mapping[str, Any], *, collector: str) -> dict[str, str] | None:
    title = public_text(obs.get("title"), limit=240)
    url = _https(obs.get("url") or obs.get("source_url"))
    if not title and not url:
        return None
    archive = obs.get("archive") if isinstance(obs.get("archive"), dict) else {}
    digest = _sha(obs.get("content_sha256"))
    if not digest:
        digest = content_sha256(title, public_text(obs.get("text"), limit=400), url)
    return _row(
        title=title,
        excerpt=public_text(obs.get("text") or obs.get("detail") or obs.get("note"), limit=400),
        source=public_text(obs.get("source"), limit=80) or collector,
        source_url=url,
        first_seen=iso_z(obs.get("first_seen") or obs.get("detected_at")),
        last_seen=iso_z(obs.get("last_seen") or obs.get("detected_at")),
        last_confirmed_alive=iso_z(obs.get("last_confirmed_alive")),
        deletion_signal=public_text(
            obs.get("deletion_signal") or obs.get("event"), limit=80
        ),
        content_sha256_value=digest,
        wayback_lookup=_archive_url(archive, "wayback_lookup"),
        wayback_snapshot=_archive_url(archive, "wayback_snapshot"),
        archive_today_lookup=_archive_url(archive, "archive_today_lookup"),
        gazetteer=_gazetteer_label(obs.get("gazetteer_hits")),
        collector=collector,
    )


def _from_wayback(rec: Mapping[str, Any]) -> dict[str, str] | None:
    title = public_text(rec.get("term") or rec.get("url"), limit=240)
    url = _https(rec.get("url"))
    if not title and not url:
        return None
    event = public_text(rec.get("event"), limit=80) or "unknown"
    first = _wayback_timestamp(rec.get("first_capture"))
    last = _wayback_timestamp(rec.get("last_capture"))
    snapshot = _https(rec.get("last_live_snapshot") or rec.get("wayback_snapshot"))
    lookup = f"https://web.archive.org/web/*/{url}" if url else ""
    return _row(
        title=f"[wayback:{event}] {title}" if title else f"[wayback:{event}]",
        excerpt=public_text(rec.get("detail") or rec.get("note") or event, limit=400),
        source="wayback:reconstruction",
        source_url=url,
        first_seen=first or last,
        last_seen=last or first,
        last_confirmed_alive=first,
        deletion_signal=event,
        content_sha256_value=content_sha256("wayback", event, url, title),
        wayback_lookup=lookup,
        wayback_snapshot=snapshot,
        archive_today_lookup=f"https://archive.today/{url}" if url else "",
        gazetteer=title,
        collector="wayback",
    )


def _richer(existing: Mapping[str, str], candidate: Mapping[str, str]) -> bool:
    score = lambda row: (
        1 if row.get("wayback_snapshot") else 0,
        1 if row.get("source_url") else 0,
        1 if row.get("content_sha256") else 0,
        1 if row.get("last_confirmed_alive") else 0,
        len(row.get("excerpt") or ""),
    )
    return score(candidate) > score(existing)


def _merge_gazetteer(*labels: str) -> str:
    seen: list[str] = []
    for label in labels:
        for part in (label or "").split("; "):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
            if len(seen) >= 8:
                return "; ".join(seen)
    return "; ".join(seen)


def _collect_rows(payloads: Mapping[str, Mapping[str, Any] | None]) -> list[dict[str, str]]:
    collected: dict[str, dict[str, str]] = {}

    def add(row: dict[str, str] | None) -> None:
        if not row:
            return
        key = row["key"]
        if key in collected:
            keep = dict(row if _richer(collected[key], row) else collected[key])
            keep["gazetteer"] = _merge_gazetteer(
                collected[key].get("gazetteer", ""),
                row.get("gazetteer", ""),
            )
            collected[key] = keep
            return
        collected[key] = row

    undertext = payloads.get("undertext-latest.json") or {}
    for obs in undertext.get("observations") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="undertext"))

    wayback = payloads.get("wayback-latest.json") or {}
    for rec in wayback.get("reconstructions") or []:
        if isinstance(rec, dict):
            add(_from_wayback(rec))
    for obs in wayback.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="wayback"))

    weibo = payloads.get("weibo-hotsearch-latest.json") or {}
    for obs in weibo.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="weibo-hotsearch"))

    ddti = payloads.get("ddti-latest.json") or {}
    for obs in ddti.get("observation_records") or []:
        if isinstance(obs, dict):
            add(_from_observation(obs, collector="ddti"))

    ledgers = payloads.get("public-deletion-ledgers-latest.json")
    if ledgers:
        for obs in ledgers.get("observations") or ledgers.get("observation_records") or []:
            if isinstance(obs, dict):
                add(_from_observation(obs, collector="public-deletion-ledgers"))

    rows = list(collected.values())
    rows.sort(key=lambda row: (
        row.get("last_seen") or "",
        row.get("first_seen") or "",
        row.get("key") or "",
    ), reverse=True)
    return rows


def _input_receipts(payloads: Mapping[str, Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    receipts = []
    for name in INPUT_FILES:
        payload = payloads.get(name)
        receipts.append({
            "filename": name,
            "available": payload is not None,
            "generated_at": iso_z(payload.get("generated_at")) if payload else None,
        })
    return receipts


def build_document(
    *,
    readings_dir: Path | None = None,
) -> dict[str, Any] | None:
    readings_dir = readings_dir or READINGS
    payloads = {name: _load_json(name, readings_dir) for name in INPUT_FILES}
    clock = fusion_clock(payloads)
    if clock is None:
        return None
    rows = _collect_rows(payloads)
    if not rows:
        return None
    generated = iso_z(clock)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated,
        "source": (
            "Offline journalist projection of committed UNDERTEXT, Wayback, "
            "Weibo-board, DDTI, and optional public-deletion-ledger readings"
        ),
        "method": (
            "Deterministic flatten of already-published observation records and "
            "Wayback reconstructions. Clock is the newest input generated_at. "
            "No network fetch. No invented live ledger."
        ),
        "scope": (
            "Already-public China posts, deletions, and archive reconstructions "
            "that Palimpsest has already published. Not a census of all censorship."
        ),
        "safety": (
            "Watch the censor, never the censored. Public data only. Nobody "
            "inside China is asked to act."
        ),
        "honesty": {
            "captures": HONESTY_CAPTURES,
            "does_not_capture": HONESTY_DOES_NOT,
            "live_claim": HONESTY_LIVE,
        },
        "n_rows": len(rows),
        "inputs": _input_receipts(payloads),
        "rows": rows,
    }
    extra = set(document) - _TOP_FIELDS
    if extra:
        raise ValueError(f"erasure-trail document has unexpected keys: {sorted(extra)}")
    for row in rows:
        extra_row = set(row) - _ROW_FIELDS
        if extra_row:
            raise ValueError(f"erasure-trail row has unexpected keys: {sorted(extra_row)}")
    return document


def render_csv(document: Mapping[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(ROW_FIELDS),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in document["rows"]:
        writer.writerow({field: row.get(field, "") for field in ROW_FIELDS})
    return buffer.getvalue()


def _h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url: str, label: str | None = None) -> str:
    if not url.startswith("https://"):
        return _h(label or "—")
    return f'<a href="{_h(url)}">{_h(label or url)}</a>'


def render_html(document: Mapping[str, Any]) -> str:
    rows_html = []
    for row in document["rows"]:
        rows_html.append(
            "<tr id=\"" + _h(row["key"]) + "\">"
            f"<td><strong>{_h(row['title'])}</strong>"
            f"<div class=\"et-excerpt\">{_h(row['excerpt'] or '—')}</div></td>"
            f"<td>{_h(row['deletion_signal'] or '—')}</td>"
            f"<td>{_link(row['source_url'], row['source'])}</td>"
            f"<td><time>{_h(row['first_seen'] or 'unknown')}</time></td>"
            f"<td><time>{_h(row['last_seen'] or 'unknown')}</time></td>"
            f"<td>{_link(row['wayback_snapshot'], 'snapshot') if row['wayback_snapshot'] else _link(row['wayback_lookup'], 'lookup')}</td>"
            f"<td><code>{_h(row['content_sha256'][:16] + '…' if row['content_sha256'] else '—')}</code></td>"
            f"<td><button type=\"button\" class=\"et-cite\" data-cite=\"{_h(row['cite'])}\">Copy cite</button></td>"
            "</tr>"
        )
    table = "\n".join(rows_html) if rows_html else (
        "<tr><td colspan=\"8\">No public rows in the committed inputs. "
        "Absence is a coverage gap, not a live zero.</td></tr>"
    )
    nav = site_nav.render("/news/china/erasure/")
    generated = _h(document["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Find a deleted post · evidence trail · Palimpsest</title>
<meta name="description" content="Journalist desk for public China deletions and reconstructions: first-seen, last-seen, snapshots, hashes, source URLs, CSV export and a citation line. Public data only.">
<link rel="canonical" href="{SITE}/news/china/erasure/">
<link rel="icon" type="image/svg+xml" href="/brand/palimpsest-icon.svg">
<meta name="robots" content="index, follow, max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Palimpsest">
<meta property="og:title" content="Find a deleted post · evidence trail">
<meta property="og:description" content="First-seen, last-seen, snapshots, hashes and source URLs for already-public China posts the state later hid.">
<meta property="og:url" content="{SITE}/news/china/erasure/">
<meta property="og:image" content="{SITE}/brand/og-site.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="/dashboards/assets/tikto.css">
<link rel="stylesheet" href="/assets/shell.css">
<style>
.et-hero{{padding:clamp(36px,7vw,72px) 0 24px}}
.et-title{{font-size:clamp(40px,7vw,84px);font-weight:800;line-height:.9;letter-spacing:-.04em;margin:0;max-width:900px}}
.et-deck{{font-size:clamp(16px,2vw,21px);line-height:1.5;color:var(--tk-text-2);max-width:760px;margin:18px 0 0}}
.et-actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}
.et-honesty{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 28px}}
.et-honesty article{{padding:16px 18px}}
.et-honesty h2{{margin:0 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase}}
.et-honesty p{{margin:0;color:var(--tk-text-2);font-size:14px;line-height:1.55}}
.et-meta{{font:600 11px/1.4 var(--tk-font-mono);color:var(--tk-text-3);margin:0 0 14px}}
.et-table{{width:100%;border-collapse:collapse;font-size:13px}}
.et-table th{{text-align:left;font:600 10px/1.3 var(--tk-font-mono);letter-spacing:.1em;text-transform:uppercase;color:var(--tk-text-4);padding:10px 8px;border-bottom:1px solid var(--tk-line-2)}}
.et-table td{{padding:12px 8px;border-bottom:1px solid var(--tk-line-1);vertical-align:top}}
.et-excerpt{{color:var(--tk-text-3);font-size:12px;margin-top:6px;max-width:36ch}}
.et-table code{{font-size:11px}}
.et-cite{{min-height:36px;padding:6px 10px;border:1px solid var(--tk-line-2);background:transparent;color:var(--tk-text-2);font:600 10px/1 var(--tk-font-mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer}}
.et-cite:hover{{color:var(--tk-text-0);border-color:var(--tk-line-3)}}
.et-how{{margin:28px 0;max-width:760px}}
.et-how ol{{margin:8px 0 0;padding-left:1.2em;color:var(--tk-text-2);line-height:1.6}}
@media(max-width:900px){{.et-honesty{{grid-template-columns:1fr}}.et-wrap{{overflow-x:auto}}}}
</style>
</head>
<body class="ps">
{nav}
<main id="main" class="ps-wrap ps-wrap--wide">
  <header class="et-hero">
    <p class="ps-kicker">Journalist desk · public record only</p>
    <h1 class="et-title">Find a deleted post.<br>See the trail. Export it.</h1>
    <p class="et-deck">Each row is an already-public China post, deletion ledger item, or archive reconstruction. The trail is first-seen, last-seen, snapshots, hashes and source URLs — not a private-message feed and not a live claim about anyone inside China.</p>
    <div class="et-actions">
      <a class="ps-btn" href="/readings/erasure-trail.csv">Download CSV</a>
      <a class="ps-btn ps-btn--ghost" href="/readings/erasure-trail-latest.json">Download JSON</a>
      <a class="ps-btn ps-btn--ghost" href="/docs/FOR-JOURNALISTS.md">How to cite</a>
      <a class="ps-btn ps-btn--ghost" href="/osint-china.html">Signal board</a>
    </div>
  </header>

  <section class="et-honesty" aria-label="What this desk captures">
    <article class="ps-p2">
      <h2>Captured</h2>
      <p>{_h(document["honesty"]["captures"])}</p>
    </article>
    <article class="ps-p2">
      <h2>Not captured</h2>
      <p>{_h(document["honesty"]["does_not_capture"])}</p>
    </article>
    <article class="ps-p2">
      <h2>Live readings</h2>
      <p>{_h(document["honesty"]["live_claim"])}</p>
    </article>
  </section>

  <p class="et-meta">{_h(document["n_rows"])} public rows · clock {generated} from committed inputs · method v{METHOD_VERSION}</p>
  <div class="et-wrap ps-p1" tabindex="0" role="region" aria-label="Public deletion and reconstruction trail, scrolls horizontally">
    <table class="et-table">
      <thead>
        <tr>
          <th>Record</th>
          <th>Signal</th>
          <th>Source URL</th>
          <th>First seen</th>
          <th>Last seen</th>
          <th>Snapshot</th>
          <th>SHA-256</th>
          <th>Cite</th>
        </tr>
      </thead>
      <tbody>
        {table}
      </tbody>
    </table>
  </div>

  <section class="et-how" id="cite">
    <p class="ps-kicker">How to cite</p>
    <h2 class="ps-section-head">Export the row. Quote the trail. Do not upgrade the claim.</h2>
    <ol>
      <li>Find the row by title, gazetteer term, or source URL.</li>
      <li>Copy first-seen, last-seen, the snapshot or lookup URL, and the SHA-256.</li>
      <li>Download the <a href="/readings/erasure-trail.csv">CSV</a> or <a href="/readings/erasure-trail-latest.json">JSON</a> if you need the full table.</li>
      <li>Cite Palimpsest as the observatory that recorded a public disappearance, not as a witness inside China and not as proof of motive.</li>
    </ol>
    <p>Method: <a href="/docs/CHINA-CAPTURE.md">China capture</a> · <a href="/docs/FOR-JOURNALISTS.md">Journalist guide</a> · <a href="/protocol/china-observation-v1.schema.json">Observation schema</a>.</p>
  </section>
</main>
{site_nav.FOOT}
<script>
document.querySelectorAll(".et-cite").forEach(function (button) {{
  button.addEventListener("click", function () {{
    var text = button.getAttribute("data-cite") || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(function () {{
        button.textContent = "Copied";
      }}).catch(function () {{
        button.textContent = "Copy failed";
      }});
    }}
  }});
}});
</script>
</body>
</html>
"""


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_outputs(document: Mapping[str, Any], *, history: bool = True) -> None:
    READINGS.mkdir(parents=True, exist_ok=True)
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(_canonical_json(document), encoding="utf-8")
    CSV_OUT.write_text(render_csv(document), encoding="utf-8")
    HTML_OUT.write_text(render_html(document), encoding="utf-8")
    if history:
        with HIST.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "generated_at": document["generated_at"],
                "n_rows": document["n_rows"],
            }, ensure_ascii=False) + "\n")


def check_outputs(document: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    expected = {
        JSON_OUT: _canonical_json(document),
        CSV_OUT: render_csv(document),
        HTML_OUT: render_html(document),
    }
    for path, payload in expected.items():
        if not path.is_file():
            problems.append(f"missing {path.relative_to(ROOT)}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != payload:
            problems.append(f"stale {path.relative_to(ROOT)}")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate committed outputs")
    parser.add_argument("--readings-dir", type=Path, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    readings_dir = args.readings_dir or READINGS
    document = build_document(readings_dir=readings_dir)
    if document is None:
        print("erasure-trail: no dated public inputs — abstaining")
        return 2
    if args.check:
        problems = check_outputs(document)
        if problems:
            print("erasure-trail --check failed:\n  " + "\n  ".join(problems))
            return 1
        print(f"erasure-trail: current · {document['n_rows']} rows · {document['generated_at']}")
        return 0
    write_outputs(document)
    print(f"erasure-trail: {document['n_rows']} row(s) · {document['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
