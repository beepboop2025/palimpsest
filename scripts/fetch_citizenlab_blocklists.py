"""Acquire Citizen Lab's published client blocklists into a local cache.

ACQUISITION IS DELIBERATELY OUT-OF-TREE. collectors/blocklist_archaeology.py is handed bytes
and never fetches: its default loader refuses an http(s) source_ref outright, so the
inert/no-network property holds by construction. This script is the injected acquisition
step, and it is the only place in the repo that reaches for this corpus.

WHAT IS FETCHED
  github.com/citizenlab/chat-censorship — LINE/raw-block-lists/line-v*.txt, the exhaustive
  keyword blocklists reverse-engineered from successive LINE client releases. These are
  already-published research artifacts. No client is decompiled here, nothing is decrypted,
  no DRM is touched: we read files Citizen Lab has already put on the public web.

WHY THIS CORPUS IS WORTH THE TROUBLE
  Every other Palimpsest leg INFERS sensitivity from deletions. A shipped client blocklist is
  the censor's own trigger list, pre-labelled. A term newly present in version N+1 is a dated
  directive with no inference at all — the cleanest novelty input the system can take.

LICENSING — READ BEFORE PUBLISHING ANYTHING DERIVED FROM THIS
  The repository carries NO licence file and its README states no terms, so the corpus is
  all-rights-reserved by default. This script therefore writes only into data/, which is
  gitignored: the corpus is CACHED, never redistributed by us. Whether any specific keyword
  may be republished as evidence in a public reading is a separate decision, taken in
  scripts/blocklist_pull.py and deliberately conservative there. Attribution to Citizen Lab
  travels with every derived artifact.

    PYTHONPATH=. python3 scripts/fetch_citizenlab_blocklists.py
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "citizenlab", "line")
MANIFEST = os.path.join(CACHE, "manifest.json")

REPO = "citizenlab/chat-censorship"
DIR = "LINE/raw-block-lists"
API = f"https://api.github.com/repos/{REPO}/contents/{DIR}"
RAW = f"https://raw.githubusercontent.com/{REPO}/master/{DIR}"
ATTRIBUTION = ("The Citizen Lab, University of Toronto — chat-censorship corpus "
               "(https://github.com/citizenlab/chat-censorship). Asia Chats: "
               "Investigating Regionally-based Keyword Censorship in LINE (2013-2014).")

USER_AGENT = "Mozilla/5.0 (Palimpsest/0.2; open-source censorship research)"
MAX_BYTES = 4 * 1024 * 1024   # these lists are single-digit KB; a 4 MiB cap is generous


def _get(url: str, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RuntimeError(f"{url} exceeds the {MAX_BYTES} byte cap — refusing")
    return raw


def _list_versions() -> list:
    """Discover line-v*.txt from the public contents API, so a new release is picked up."""
    payload = json.loads(_get(API, "application/vnd.github+json").decode("utf-8"))
    out = []
    for entry in payload:
        name = entry.get("name", "")
        if entry.get("type") == "file" and name.startswith("line-v") and name.endswith(".txt"):
            stem = name[len("line-v"):-len(".txt")]
            if stem.isdigit():
                out.append({"name": name, "version": f"v{stem}", "order": int(stem)})
    out.sort(key=lambda a: a["order"])
    return out


def _first_commit_date(name: str, previous: dict) -> tuple:
    """Earliest commit touching this file — a BOUND on when the version was observed, not the
    client's true release date. Falls back to whatever a previous run recorded rather than
    inventing a date; returns (iso_or_None, how_it_was_derived)."""
    url = (f"https://api.github.com/repos/{REPO}/commits"
           f"?path={DIR}/{name}&per_page=100")
    try:
        commits = json.loads(_get(url, "application/vnd.github+json").decode("utf-8"))
        dates = [c["commit"]["committer"]["date"] for c in commits
                 if c.get("commit", {}).get("committer", {}).get("date")]
        if dates:
            return min(dates), "earliest-commit-touching-file"
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError):
        pass
    prior = (previous.get("files") or {}).get(name) or {}
    if prior.get("observed_at"):
        return prior["observed_at"], prior.get("date_basis", "carried-from-previous-run")
    return None, "unavailable"


def main() -> dict:
    os.makedirs(CACHE, exist_ok=True)
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, json.JSONDecodeError):
        previous = {}

    versions = _list_versions()
    if not versions:
        raise SystemExit("FATAL: no line-v*.txt found — refusing to write an empty manifest.")

    files, changed = {}, []
    for v in versions:
        raw = _get(f"{RAW}/{v['name']}")
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(CACHE, v["name"])
        prior_digest = ((previous.get("files") or {}).get(v["name"]) or {}).get("sha256")
        if prior_digest and prior_digest != digest:
            # A published historical artifact changing under us is itself a finding.
            changed.append({"file": v["name"], "was": prior_digest, "now": digest})
        with open(path, "wb") as fh:
            fh.write(raw)
        observed_at, basis = _first_commit_date(v["name"], previous)
        files[v["name"]] = {
            "version": v["version"],
            "order": v["order"],
            "bytes": len(raw),
            "sha256": digest,
            "observed_at": observed_at,
            "date_basis": basis,
            "source_url": f"{RAW}/{v['name']}",
            "local_path": os.path.relpath(path, ROOT),
        }
        print(f"  {v['name']:<22} {len(raw):>6}b  {digest[:12]}  {observed_at or 'UNDATED'}")

    manifest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "repo": REPO,
        "directory": DIR,
        "attribution": ATTRIBUTION,
        "licence": ("NO licence file in the upstream repository and no terms stated in its "
                    "README — treated as all-rights-reserved. Cached under data/ (gitignored), "
                    "never redistributed from this repo."),
        "app": "line",
        "completeness": "exhaustive",
        "completeness_basis": ("upstream README: the LINE lists were reverse engineered from "
                               "the application and are exhaustive, unlike the sample-tested "
                               "WeChat/QQMail/Apple/Bing lists"),
        "n_files": len(files),
        "content_changed_since_last_fetch": changed,
        "files": files,
    }
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return manifest


if __name__ == "__main__":
    m = main()
    print(f"\ncached {m['n_files']} artifact(s) -> {os.path.relpath(CACHE, ROOT)}")
    if m["content_changed_since_last_fetch"]:
        print("  ! upstream content changed since the last fetch:")
        for c in m["content_changed_since_last_fetch"]:
            print(f"      {c['file']}  {c['was'][:12]} -> {c['now'][:12]}")
