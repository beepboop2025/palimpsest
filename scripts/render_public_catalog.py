"""Render the checked public catalog as readable, citable HTML.

Only the publication-filtered projection is accepted. These pages do not read
raw feeds, the economic ledger, or private stores. JavaScript enhances the
dated snapshot; a failed fetch must not remove it.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from scripts import site_nav

SITE = "https://www.palimpsest.info"
BEGIN = "<!-- PUBLIC_DATASETS -->"
END = "<!-- /PUBLIC_DATASETS -->"
INDEXABLE = {"fresh", "partial", "stale", "historical"}
STATES = {
    "fresh": "Within its collection window at this assessment; check the reading's reference period before use.",
    "partial": "Some evidence is available; the source reading documents incomplete coverage.",
    "stale": "Last retained reading; the expected collection window has passed.",
    "gated": "Values are withheld or collection requires permission or review. No value download is advertised.",
    "abstained": "The collector published its inability to measure; this is not a zero observation.",
    "warming": "Coverage is still developing. Inspect the dated reading for what was actually collected.",
    "disabled": "Collection is disabled; retained documentation does not establish current coverage.",
    "private-node": "Retained on a private research node; row data are not publicly distributed.",
}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def link(url, label) -> str:
    parsed = urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return esc(label)
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def route(item: dict) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", item["id"]):
        raise ValueError("invalid dataset slug")
    return f'/datasets/{item["id"]}/'


def available(item: dict) -> bool:
    return item.get("publication_allowed") is not False and item["artifacts"].get("latest_available") is True


def distributions(item: dict) -> list[dict]:
    if not available(item):
        return []
    result = [{"name": "Current JSON", "url": item["urls"]["latest"], "mediatype": "application/json"}]
    if item["artifacts"].get("history_available"):
        result.append({"name": "History JSONL", "url": item["urls"]["history"], "mediatype": "application/x-ndjson"})
    result.extend(row for row in item.get("distributions", []) if row.get("available"))
    return result


def metadata(item: dict) -> str:
    artifact = item["artifacts"]
    state = artifact["evidence_state"]
    history = "No public history distribution"
    if available(item) and artifact.get("history_available"):
        history = f'{artifact["history_rows"]:,} retained records'
    elif any("history" in row["name"].lower() for row in distributions(item)):
        history = "Available in the history download above"
    fields = [
        ("Evidence state", state),
        ("Reading timestamp", artifact.get("observed_at") or "No public reading timestamp"),
        ("Collection target", item["cadence"]),
        ("Sources", " · ".join(item["sources"]) or "See method"),
        ("Geography", " · ".join(item["geography"]) or "See scope"),
        ("History", history),
    ]
    if available(item):
        fields.extend((name.removeprefix("coverage.").replace("_", " "), f"{value:,}") for name, value in artifact.get("counts", {}).items())
    return '<dl class="dataset__meta">' + ''.join(f'<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>' for k, v in fields) + '</dl>'


def dataset_body(item: dict) -> str:
    state = item["artifacts"]["evidence_state"]
    downloads = ' · '.join(link(row["url"], row["name"]) for row in distributions(item))
    rights = item["license"]
    return (f'<p class="dataset__description">{esc(item["description"])}</p>'
            f'<p>{esc(STATES.get(state, "No assessed current observation; inspect the source and method."))}</p>'
            f'<p class="dataset__files">{downloads or "No public value download for this dataset."}</p>'
            f'<p>{link(item["urls"]["landing_page"], "Open research view")} · '
            f'{link(item["urls"]["method"], "Read the method")}</p>'
            + metadata(item)
            + f'<p>Reuse terms: {link(rights["url"], rights["name"])}. Source rights remain attached to observations.</p>')


def render_directory(catalog: dict) -> str:
    rows = []
    for index, item in enumerate(catalog["datasets"], 1):
        state = item["artifacts"]["evidence_state"]
        search = ' '.join([item["id"], item["name"], item["description"], item["layer"], item["collection_mode"], state, *item["geography"], *item["sources"]]).lower()
        rows.append(f'<details class="dataset" id="dataset-{esc(item["id"])}" data-id="{esc(item["id"])}" data-layer="{esc(item["layer"])}" data-mode="{esc(item["collection_mode"])}" data-state="{esc(state)}" data-search="{esc(search)}">'
                    f'<summary><span class="dataset__index">{index:02}</span><span class="dataset__title"><b>{esc(item["name"])}</b><code>{esc(item["id"])}</code></span>'
                    f'<span class="dataset__facet dataset__facet--layer">{esc(item["layer"])}</span><span class="dataset__facet dataset__facet--source">{esc(" · ".join(item["sources"]))}</span>'
                    f'<span class="dataset__facet dataset__facet--mode">{esc(item["collection_mode"])}</span><span class="dataset__state">{esc(state)}</span></summary>'
                    f'<div class="dataset__body"><div>{dataset_body(item)}<p><a href="{route(item)}">Dataset details and citation →</a></p></div></div></details>')
    return '\n'.join(rows)


def jsonld(item: dict) -> dict:
    result = {
        "@context": "https://schema.org", "@type": "Dataset", "@id": SITE + route(item) + "#dataset",
        "name": item["name"], "description": item["description"], "url": SITE + route(item),
        "identifier": item["id"], "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "Palimpsest", "url": SITE + "/"},
        "license": item["license"]["url"], "spatialCoverage": item["geography"],
        "measurementTechnique": item["collection_mode"],
        "isBasedOn": item["urls"]["method"],
        "includedInDataCatalog": {"@type": "DataCatalog", "name": "Palimpsest Evidence Atlas", "url": SITE + "/data.html"},
        "distribution": [{"@type": "DataDownload", "name": row["name"], "contentUrl": row["url"], "encodingFormat": row["mediatype"]} for row in distributions(item)],
    }
    if item["artifacts"].get("observed_at"):
        result["dateModified"] = item["artifacts"]["observed_at"]
    return result


def render_dataset(item: dict, clock: str) -> str:
    indexable = available(item) and item["artifacts"]["evidence_state"] in INDEXABLE
    structured = json.dumps(jsonld(item), ensure_ascii=False).replace("<", "\\u003c") if indexable else ""
    citation = f'Palimpsest. {item["name"]}. Reading: {item["artifacts"].get("observed_at") or "not published"}. {SITE + route(item)}'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(item["name"])} · China data and evidence · Palimpsest</title>
<meta name="description" content="{esc(item["description"])}"><meta name="robots" content="{"index,follow,max-snippet:-1" if indexable else "noindex,follow"}">
<link rel="canonical" href="{SITE + route(item)}">{site_nav.HEAD}
<link rel="stylesheet" href="/assets/data-catalog.css"><script type="application/ld+json">{structured or "{}"}</script></head>
<body class="ps tk atlas">{site_nav.render(route(item))}<main id="main" class="ps-wrap ps-wrap--wide dataset-page">
<p><a href="/data.html">China data and evidence directory</a> / {esc(item["layer"])}</p><h1>{esc(item["name"])}</h1>
<p>Dataset <code>{esc(item["id"])}</code> · Assessed {esc(clock)}</p>{dataset_body(item)}
<section><h2>How to use this evidence</h2><p>The reading timestamp records the instrument's evidence clock. The economic reference period or event date is defined inside the source record. A recent collection does not turn an old observation into a current one. Counts describe this dataset's coverage, not all activity in China.</p></section>
<section><h2>Cite this dataset</h2><p>{esc(citation)}</p><p>When citing a number, include the exact JSON or CSV record, its reference period, units and upstream source. Retain a copy of the reading; a latest URL changes as new editions are published.</p></section>
<p><a href="/china/economy/">Detailed economic tables</a> · <a href="/china/evidence/">China evidence observatory</a> · <a href="/developers.html">API and MCP access</a> · <a href="/challenge.html">Report a correction</a></p>
</main>{site_nav.FOOT}</body></html>'''


def replace_id(text: str, identifier: str, content: str) -> str:
    pattern = re.compile(r'(<(?:span|p)\b[^>]*\bid="' + re.escape(identifier) + r'"[^>]*>)[\s\S]*?(</(?:span|p)>)')
    text, count = pattern.subn(lambda m: m[1] + content + m[2], text)
    if count != 1:
        raise ValueError(f"catalog template missing unique {identifier}")
    return text


def write_home(root: Path, catalog: dict) -> None:
    path = root / "index.html"
    if not path.exists():
        return
    text = path.read_text()

    def put(attribute: str, value: str) -> None:
        nonlocal text
        pattern = r'(<(?P<tag>span|strong|code)\b[^>]*\b' + re.escape(attribute) + r'(?:="[^"]*")?[^>]*>)[\s\S]*?(</(?P=tag)>)'
        text = re.sub(pattern, lambda m: m[1] + esc(value) + m[3], text)

    summary = catalog["summary"]
    put("data-home-osint-label", "Fresh at assessment")
    put("data-home-osint-live", str(summary["states"].get("fresh", 0)))
    put("data-home-osint-total", str(summary["datasets"]))
    put("data-home-osint-state", "Assessed " + catalog["generated_at"] + ". See each dataset for its coverage and reference period.")
    registry = next((item for item in catalog["datasets"] if item.get("latest") == "readings/eval-registry-latest.json" and available(item)), None)
    if registry:
        runs = registry["artifacts"].get("counts", {}).get("runs")
        if type(runs) is int:
            put("data-home-registry-runs", str(runs))
            put("data-home-registry-root", "Registry reading " + str(registry["artifacts"].get("observed_at") or "undated"))
    else:
        put("data-home-registry-runs", "No public assessment")
        put("data-home-registry-root", "Inspect the registry receipt")
    feed = root / "news/feed.json"
    put("data-home-wire-events", "See source index")
    put("data-home-wire-source-state", "Open individual reports for their publisher and date.")
    if feed.exists():
        document = json.loads(feed.read_text())
        if document.get("version") == "https://jsonfeed.org/version/1.1":
            reports = [item for item in document.get("items", []) if item.get("_palimpsest", {}).get("kind") == "publisher_source_record"]
            put("data-home-wire-events", str(len(reports)))
            put("data-home-wire-source-state", "Published edition " + catalog["generated_at"] + ". Attributed publisher records; source coverage is not inferred.")
    text = text.replace('data-feed-state="loading"', 'data-feed-state="snapshot"')
    path.write_text(text)


def write_surfaces(root: Path, catalog: dict) -> None:
    if catalog.get("schema") != "palimpsest-data-catalog/v1" or not catalog.get("availability_semantics"):
        raise ValueError("renderer requires the publication-checked catalog")
    template = root / "data.html"
    if not template.exists():  # Minimal collector/contract fixtures have no site.
        return
    text = template.read_text()
    if BEGIN not in text:
        text, count = re.subn(r'(<div class="dataset-list" id="dataset-list"[^>]*>)[\s\S]*?(\n    </div>)', lambda m: m[1] + BEGIN + END + m[2], text)
        if count != 1:
            raise ValueError("catalog directory container missing")
    text = re.sub(re.escape(BEGIN) + r'[\s\S]*?' + re.escape(END), lambda _: BEGIN + render_directory(catalog) + END, text)
    summary = catalog["summary"]
    for identifier, value in {"stat-datasets": summary["datasets"], "stat-fresh": summary["states"].get("fresh", 0), "stat-rows": summary["history_rows"], "result-count": summary["datasets"]}.items():
        text = replace_id(text, identifier, f"{value:,}")
    text = replace_id(text, "stat-bytes", f'{summary["published_bytes"] / 1048576:,.1f} MiB')
    text = replace_id(text, "catalog-asof", f'Assessed {esc(catalog["generated_at"])}. States describe this dated edition; source periods and publication limits remain attached.')
    text = text.replace('fresh public readings</small>', 'fresh at assessment</small>')
    text = text.replace('This explorer needs JavaScript for local filtering.', 'The complete dataset directory is readable above without JavaScript. Enable JavaScript for local filtering.')
    template.write_text(text)
    write_home(root, catalog)
    sitemap = []
    for item in catalog["datasets"]:
        path = root / route(item).lstrip("/") / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_dataset(item, catalog["generated_at"]))
        if available(item) and item["artifacts"]["evidence_state"] in INDEXABLE:
            clock = item["artifacts"].get("observed_at")
            sitemap.append(f'<url><loc>{SITE + route(item)}</loc>' + (f'<lastmod>{esc(clock)}</lastmod>' if clock else '') + '</url>')
    (root / "datasets/sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(sitemap) + '</urlset>\n')
    # The composite OSINT bundle may contain a withheld economic family. Keep
    # the independent, permission-checked signal directory usable in that case.
    board = root / "osint-china.html"
    if board.exists():
        board_text = board.read_text()
        start, end = '<!-- PUBLIC_SIGNAL_COVERAGE -->', '<!-- /PUBLIC_SIGNAL_COVERAGE -->'
        section = (start + f'<section class="public-signal-coverage" data-assessed-at="{esc(catalog["generated_at"])}" aria-labelledby="public-signal-title"><h2 id="public-signal-title">Published signal coverage</h2>'
                   + f'<p>Assessed {esc(catalog["generated_at"])}. These independent readings remain available when a combined score cannot be published. Open a dataset for its sources, coverage, dates and downloads.</p>'
                   + render_directory(catalog) + '</section>' + end)
        if start in board_text:
            board_text = re.sub(re.escape(start) + r'[\s\S]*?' + re.escape(end), lambda _: section, board_text)
        else:
            board_text = board_text.replace('</main>', section + '\n</main>', 1)
            board_text = board_text.replace('</head>', '<link rel="stylesheet" href="/assets/data-catalog.css">\n</head>', 1)
        board.write_text(board_text)
