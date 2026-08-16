"""Anchor the sealed chains OUTSIDE our own infrastructure.

A hash chain proves internal consistency, but a chain the operator serves is
only tamper-evident to someone who already holds an old copy. This script
closes that gap by depositing each new Merkle root with parties we do not
control, so rewriting history would require defeating them too:

  1. Internet Archive — a Wayback Machine snapshot of the published chain
     files. A dated, third-party copy of the exact bytes, held by a
     library. Pure stdlib.
  2. OpenTimestamps — the roots are stamped into Bitcoin via the standard
     `ots` client when it is installed (CI installs it; local runs skip
     loudly). The resulting .ots files are committed and verify with the
     standard client against the Bitcoin blockchain, not against us.

Idempotent by the house convention: if none of the three roots moved and the
last anchor record completed every external deposit, nothing is anchored and
nothing grows. An incomplete external attempt is resumed selectively: proven
Wayback snapshots and a present OpenTimestamps proof are reused, while only
the missing evidence is retried. All three roots are compared, because a root
that is published but never re-anchored goes stale against the chain it claims
to fingerprint.

A broken chain is never anchored, because anchoring a bad root would launder
it, but the three chains do not fail together:

  * eval-registry and erasure-ledger are the observatory's own attestations,
    written by this workflow. A break in either aborts with exit 1.
  * readings-ledger sweeps 31 files written by 30 other workflows, so its
    corruption surface is somebody else's truncated JSON far more often than
    it is our tampering. A break there is printed loudly and its root is
    WITHHELD from the record, while the other two chains still reach Wayback
    and Bitcoin. Fail-closed here would let one bad line in a file we do not
    write keep the established chains off the blockchain, and, since the
    anchor step runs before the commit step, out of the repository entirely.

Every attempt (success or failure) is recorded in readings/anchors.jsonl and
summarized in readings/anchors-latest.json for the site. An anchoring failure
is a visible gap in the log, never a fabricated success.

    python3 scripts/anchor_roots.py            # anchor if roots moved
    python3 scripts/anchor_roots.py --dry-run  # show what would be anchored
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import eval_registry as reg  # noqa: E402
from core import sealed_ledger as led  # noqa: E402

READINGS = os.path.join(ROOT, "readings")
REGISTRY = os.path.join(READINGS, "eval-registry.jsonl")
ERASURE = os.path.join(READINGS, "erasure-ledger.jsonl")
# Every published reading, sealed by scripts/seal_readings.py. Anchored here so
# the readings record reaches Bitcoin on the same footing as the other two
# chains; a seal nobody anchors is only a promise we made to ourselves.
READINGS_LEDGER = os.path.join(READINGS, "readings-ledger.jsonl")
ANCHOR_LOG = os.path.join(READINGS, "anchors.jsonl")
ANCHOR_LATEST = os.path.join(READINGS, "anchors-latest.json")
ANCHOR_DIR = os.path.join(READINGS, "anchors")

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
WAYBACK_TARGETS = (
    f"{SITE}/readings/eval-registry.jsonl",
    f"{SITE}/readings/erasure-ledger.jsonl",
    f"{SITE}/readings/readings-ledger.jsonl",
)
ROOT_KEYS = ("registry_root", "erasure_root", "readings_root")
WAYBACK_CAPTURE_VERSION = "2"
UA = "palimpsest-anchor/1.0 (+https://palimpsest.info)"


def current_roots() -> dict:
    """Verify the three chains and return the roots we are entitled to anchor.

    Our own two attestations fail closed; the readings sweep fails open with
    its root withheld. See the module docstring for why the coupling is
    deliberately asymmetric.
    """
    reg_entries = reg.read_ledger(REGISTRY)
    led_entries = led.read_ledger(ERASURE)
    reg_ok, reg_problems = reg.verify(reg_entries)
    led_ok, led_problems = led.verify(led_entries)
    if not (reg_ok and led_ok):
        for p in reg_problems + led_problems:
            print(f"BROKEN: {p}")
        raise SystemExit(1)
    roots = {
        "registry_root": led.merkle_root(reg_entries),
        "registry_head": reg_entries[-1]["entry_hash"] if reg_entries else led.GENESIS_PREV,
        "registry_entries": len(reg_entries),
        "erasure_root": led.merkle_root(led_entries),
        "erasure_head": led_entries[-1]["entry_hash"] if led_entries else led.GENESIS_PREV,
        "erasure_entries": len(led_entries),
    }
    try:
        rdg_entries = led.read_ledger(READINGS_LEDGER)
        rdg_ok, rdg_problems = led.verify(rdg_entries)
        if not rdg_entries:
            # read_ledger returns [] for a missing file and verify([]) is
            # vacuously true, so an emptied or deleted ledger would otherwise
            # anchor the GENESIS root and read as a healthy chain. A chain that
            # sealed 31 readings yesterday and is empty today is the loudest
            # thing this script can be told; it is not a fresh start.
            rdg_ok = False
            rdg_problems = ["readings ledger is empty or missing: expected "
                            "seals for every published reading"]
    except (OSError, ValueError) as exc:  # a half-written line is unparseable
        rdg_entries, rdg_ok, rdg_problems = [], False, ["ledger unreadable"]
        print(f"readings ledger unreadable: {exc}")
    if rdg_ok:
        roots["readings_root"] = led.merkle_root(rdg_entries)
        roots["readings_head"] = (rdg_entries[-1]["entry_hash"] if rdg_entries
                                  else led.GENESIS_PREV)
        roots["readings_entries"] = len(rdg_entries)
    else:
        for p in rdg_problems:
            print(f"BROKEN readings chain: {p}")
        roots["readings_root"] = None
        roots["readings_problems"] = rdg_problems
    return roots


def last_anchor(path: str = ANCHOR_LOG) -> dict | None:
    if not os.path.exists(path):
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line)
    return last


def _file_evidence(path: str) -> dict:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def wayback_expectations() -> dict[str, dict]:
    """Bind each public target to the exact local bytes it must replay."""
    sources = (REGISTRY, ERASURE, READINGS_LEDGER)
    return {
        target: _file_evidence(source)
        for target, source in zip(WAYBACK_TARGETS, sources, strict=True)
    }


def _raw_wayback_url(snapshot: str) -> str:
    """Convert a human Wayback replay URL to its unmodified byte replay."""
    parsed = urllib.parse.urlsplit(snapshot)
    if parsed.scheme != "https" or parsed.hostname != "web.archive.org":
        raise ValueError("Wayback returned a snapshot outside web.archive.org")
    parts = parsed.path.split("/", 3)
    if len(parts) != 4 or parts[1] != "web":
        raise ValueError("Wayback returned an unrecognized snapshot path")
    marker = re.fullmatch(r"(\d{14})(?:[a-z_]+)?", parts[2])
    if marker is None:
        raise ValueError("Wayback returned an invalid snapshot timestamp")
    raw_path = f"/web/{marker.group(1)}id_/{parts[3]}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, raw_path, parsed.query, "")
    )


def _hash_stream(stream, expected_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if size > expected_bytes:
            break
    return digest.hexdigest(), size


def _hash_replay(response, expected_bytes: int) -> tuple[str, int, str]:
    headers = getattr(response, "headers", None)
    encoding = (headers.get("Content-Encoding", "") if headers else "").casefold()
    if encoding in {"", "identity"}:
        digest, size = _hash_stream(response, expected_bytes)
        return digest, size, encoding or "identity"
    if encoding not in {"gzip", "x-gzip"}:
        raise ValueError(f"unsupported Wayback content encoding: {encoding}")
    with gzip.GzipFile(fileobj=response, mode="rb") as decoded:
        digest, size = _hash_stream(decoded, expected_bytes)
    return digest, size, "gzip"


def _wayback_cdx_snapshot(capture_target: str, *, opener, timeout: int) -> str | None:
    """Return the newest exact successful capture indexed by Wayback CDX."""
    query = urllib.parse.urlencode([
        ("url", capture_target),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode"),
        ("filter", "statuscode:200"),
        ("limit", "5"),
    ])
    request = urllib.request.Request(
        f"https://web.archive.org/cdx/search/cdx?{query}",
        headers={"User-Agent": UA, "Accept-Encoding": "identity"},
    )
    with opener(request, timeout=timeout) as response:
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("Wayback CDX response is too large")
    payload = json.loads(raw)
    if not isinstance(payload, list) or not payload:
        return None
    header = payload[0]
    if header != ["timestamp", "original", "statuscode"]:
        raise ValueError("Wayback CDX response has an unexpected schema")
    for row in reversed(payload[1:]):
        if (not isinstance(row, list) or len(row) != 3
                or row[1] != capture_target or row[2] != "200"
                or re.fullmatch(r"\d{14}", row[0]) is None):
            continue
        return f"https://web.archive.org/web/{row[0]}/{row[1]}"
    return None


def _eventual_cdx_snapshot(capture_target: str, *, opener, timeout: int,
                           attempts: int, sleeper) -> str | None:
    """Poll boundedly for a newly accepted Save Page Now capture.

    The save endpoint can return HTTP 200 on a queue/status URL before any
    replay URL exists. Only the exact hash-qualified target is queried, and a
    later byte-for-byte replay check still decides whether it counts.
    """
    if attempts < 1:
        raise ValueError("Wayback CDX attempts must be positive")
    for attempt in range(attempts):
        snapshot = _wayback_cdx_snapshot(
            capture_target, opener=opener, timeout=timeout
        )
        if snapshot is not None:
            return snapshot
        if attempt + 1 < attempts:
            sleeper(min(2 ** attempt, 4))
    return None


def _wayback_replay_evidence(snapshot: str, *, expected_bytes: int, opener,
                             timeout: int, replay_attempts: int, sleeper) -> dict:
    if replay_attempts < 1:
        raise ValueError("Wayback replay attempts must be positive")
    raw_snapshot = _raw_wayback_url(snapshot)
    replay_req = urllib.request.Request(
        raw_snapshot,
        headers={"User-Agent": UA, "Accept-Encoding": "identity"},
    )
    for attempt in range(replay_attempts):
        try:
            with opener(replay_req, timeout=timeout) as replay:
                replay_http = getattr(replay, "status", None)
                actual_sha256, actual_bytes, replay_encoding = _hash_replay(
                    replay, expected_bytes
                )
            return {
                "raw_snapshot": raw_snapshot,
                "replay_http": replay_http,
                "replay_content_encoding": replay_encoding,
                "snapshot_sha256": actual_sha256,
                "snapshot_bytes": actual_bytes,
            }
        except urllib.error.HTTPError as exc:
            transient = exc.code in {404, 429, 503}
            if not transient or attempt + 1 == replay_attempts:
                raise
            sleeper(min(2 ** attempt, 4))
    raise AssertionError("unreachable Wayback replay loop")


def wayback_save(url: str, *, expected_sha256: str, expected_bytes: int,
                 opener=urllib.request.urlopen, timeout: int = 90,
                 replay_attempts: int = 3, cdx_attempts: int = 4,
                 sleeper=time.sleep) -> dict:
    """Deposit and byte-verify one artifact with the Internet Archive.

    Save Page Now may redirect to an older replay while still returning HTTP
    200. The content digest, not that status code, decides whether the witness
    is valid.
    """
    separator = "&" if "?" in url else "?"
    capture_target = (
        f"{url}{separator}"
        + urllib.parse.urlencode([
            ("palimpsest_sha256", expected_sha256),
            ("palimpsest_capture_version", WAYBACK_CAPTURE_VERSION),
        ])
    )
    result = {
        "target": url,
        "capture_target": capture_target,
        "expected_sha256": expected_sha256,
        "expected_bytes": expected_bytes,
    }
    req = urllib.request.Request(
        f"https://web.archive.org/save/{capture_target}",
        headers={"User-Agent": UA},
    )
    try:
        try:
            snapshot = _wayback_cdx_snapshot(
                capture_target, opener=opener, timeout=timeout
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            snapshot = None
        if snapshot is not None:
            capture_source = "cdx"
            http_status = None
        else:
            capture_source = "save"
            try:
                with opener(req, timeout=timeout) as resp:
                    http_status = getattr(resp, "status", None)
                    headers = getattr(resp, "headers", None)
                    content_location = (
                        headers.get("Content-Location") if headers else None
                    )
                    snapshot = (
                        urllib.parse.urljoin(
                            "https://web.archive.org", content_location
                        )
                        if content_location else resp.geturl()
                    )
            except urllib.error.HTTPError as exc:
                # Save Page Now redirects to the new replay before every replay
                # node necessarily sees it. urllib follows that redirect and may
                # expose only the final 404; its URL still identifies the capture.
                if exc.code not in {404, 429, 500, 502, 503, 504}:
                    raise
                candidate = exc.geturl()
                try:
                    _raw_wayback_url(candidate)
                    snapshot = candidate
                except ValueError:
                    snapshot = _wayback_cdx_snapshot(
                        capture_target, opener=opener, timeout=timeout
                    )
                    if snapshot is None:
                        raise
                    capture_source = "cdx"
                http_status = exc.code
        try:
            _raw_wayback_url(snapshot)
        except ValueError as path_error:
            indexed_snapshot = _eventual_cdx_snapshot(
                capture_target,
                opener=opener,
                timeout=timeout,
                attempts=cdx_attempts,
                sleeper=sleeper,
            )
            if indexed_snapshot is None:
                raise path_error
            snapshot = indexed_snapshot
            capture_source = "cdx"
        try:
            replay_evidence = _wayback_replay_evidence(
                snapshot,
                expected_bytes=expected_bytes,
                opener=opener,
                timeout=timeout,
                replay_attempts=replay_attempts,
                sleeper=sleeper,
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 429, 503}:
                raise
            indexed_snapshot = _wayback_cdx_snapshot(
                capture_target, opener=opener, timeout=timeout
            )
            if indexed_snapshot is None or indexed_snapshot == snapshot:
                raise
            snapshot = indexed_snapshot
            capture_source = "cdx"
            replay_evidence = _wayback_replay_evidence(
                snapshot,
                expected_bytes=expected_bytes,
                opener=opener,
                timeout=timeout,
                replay_attempts=replay_attempts,
                sleeper=sleeper,
            )
        actual_sha256 = replay_evidence["snapshot_sha256"]
        actual_bytes = replay_evidence["snapshot_bytes"]
        result.update({
            "snapshot": snapshot,
            "capture_source": capture_source,
            "http": http_status,
            **replay_evidence,
        })
        if actual_sha256 != expected_sha256 or actual_bytes != expected_bytes:
            result.update({
                "ok": False,
                "reason": "Wayback replay does not match the served artifact",
            })
            return result
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 — anchoring must degrade loudly, not crash
        result.update({
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
        })
        return result


def ots_stamp(roots: dict, ts: str, run=subprocess.run) -> dict:
    """Write the roots to a canonical text file and stamp it into Bitcoin with
    the standard OpenTimestamps client, if installed. The .ots proof commits to
    the repo and verifies with `ots verify` against Bitcoin, not against us."""
    if shutil.which("ots") is None:
        return {"ok": False, "skipped": True,
                "reason": "ots client not installed (pip install opentimestamps-client)"}
    os.makedirs(ANCHOR_DIR, exist_ok=True)
    stamp_name = f"roots-{ts.replace(':', '').replace('-', '').split('.')[0]}Z.txt"
    stamp_path = os.path.join(ANCHOR_DIR, stamp_name)
    # Only values we actually verified go into the stamp. A withheld root is
    # absent rather than stamped as the string "None", because Bitcoin should
    # commit to what we can stand behind and nothing else; the break itself is
    # recorded in the anchor log beside it.
    body = "".join(f"{k} {roots[k]}\n" for k in sorted(roots)
                   if isinstance(roots[k], (str, int))) + f"anchored_at {ts}\n"
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(body)
    try:
        proc = run(["ots", "stamp", stamp_path], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and os.path.exists(stamp_path + ".ots"):
            return {"ok": True, "file": f"readings/anchors/{stamp_name}",
                    "proof": f"readings/anchors/{stamp_name}.ots"}
        return {"ok": False, "reason": (proc.stderr or proc.stdout or "ots stamp failed").strip()[:400]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def reusable_wayback(prev: dict | None,
                     expectations: dict[str, dict]) -> dict[str, dict]:
    """Return snapshots already byte-verified for the current artifacts."""
    if not isinstance(prev, dict):
        return {}
    reusable = {}
    for item in prev.get("wayback", []):
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        snapshot = item.get("snapshot")
        expected = expectations.get(target)
        if (expected and item.get("ok") is True
                and isinstance(snapshot, str) and snapshot
                and item.get("snapshot_sha256") == expected["sha256"]
                and item.get("snapshot_bytes") == expected["bytes"]):
            reusable[target] = dict(item)
    return reusable


def reusable_ots(prev: dict | None) -> dict | None:
    """Return the prior stamp only when its referenced proof still exists."""
    if not isinstance(prev, dict) or not isinstance(prev.get("ots"), dict):
        return None
    ots = prev["ots"]
    proof = ots.get("proof")
    if ots.get("ok") is not True or not isinstance(proof, str) or not proof:
        return None
    proof_path = proof if os.path.isabs(proof) else os.path.join(ROOT, proof)
    return dict(ots) if os.path.isfile(proof_path) else None


def anchor(*, dry_run: bool = False, opener=urllib.request.urlopen,
           run=subprocess.run, log_path: str = ANCHOR_LOG,
           latest_path: str = ANCHOR_LATEST) -> dict | None:
    roots = current_roots()
    expectations = wayback_expectations()
    prev = last_anchor(log_path)
    # Every root we publish is compared. Leaving readings_root out meant a
    # refresh where the erasure inputs and the eval registry both sat still,
    # while the other readings moved, anchored nothing and kept republishing a
    # readings_root that no longer fingerprinted readings-ledger.jsonl, for as
    # many consecutive quiet rounds as it took.
    same_roots = bool(prev) and all(
        prev.get("roots", {}).get(key) == roots.get(key) for key in ROOT_KEYS
    )
    prior_wayback = reusable_wayback(prev, expectations) if same_roots else {}
    prior_ots = reusable_ots(prev) if same_roots else None
    missing_wayback = [target for target in WAYBACK_TARGETS
                       if target not in prior_wayback]
    if same_roots and not missing_wayback and prior_ots is not None:
        print("roots unchanged since last anchor — nothing to do")
        return None
    if dry_run:
        action = "would_retry" if same_roots else "would_anchor"
        print(json.dumps({action: {
            "roots": roots,
            "wayback_targets": missing_wayback if same_roots else list(WAYBACK_TARGETS),
            "opentimestamps": "reuse" if prior_ots is not None else "stamp",
        }}, indent=2))
        return None
    ts = datetime.now(timezone.utc).isoformat()

    record = {
        "ts": ts,
        "roots": roots,
        "wayback": [],
    }
    if same_roots:
        record["retry_of"] = prev.get("ts")
    for target in WAYBACK_TARGETS:
        if target in prior_wayback:
            reused = dict(prior_wayback[target])
            reused["reused"] = True
            record["wayback"].append(reused)
        else:
            expected = expectations[target]
            record["wayback"].append(wayback_save(
                target,
                expected_sha256=expected["sha256"],
                expected_bytes=expected["bytes"],
                opener=opener,
            ))
    if prior_ots is not None:
        record["ots"] = dict(prior_ots)
        record["ots"]["reused"] = True
    else:
        record["ots"] = ots_stamp(roots, ts, run=run)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    ok_wayback = sum(1 for w in record["wayback"] if w["ok"])
    latest = {
        "ts": ts,
        "registry_root": roots["registry_root"],
        "erasure_root": roots["erasure_root"],
        "readings_root": roots["readings_root"],
        "readings_chain": "broken" if roots.get("readings_problems") else "verified",
        "readings_problems": roots.get("readings_problems", []),
        "wayback_ok": ok_wayback,
        "wayback_snapshots": [w.get("snapshot") for w in record["wayback"] if w["ok"]],
        "wayback_reused": sum(1 for w in record["wayback"] if w.get("reused") is True),
        "ots": record["ots"].get("proof") if record["ots"]["ok"] else None,
        "ots_status": "stamped" if record["ots"]["ok"]
                      else record["ots"].get("reason", "failed"),
        "ots_reused": record["ots"].get("reused") is True,
    }
    if record.get("retry_of"):
        latest["retry_of"] = record["retry_of"]
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)

    readings_line = (f"readings {roots['readings_root'][:16]}… "
                     f"({roots['readings_entries']} entries)"
                     if roots["readings_root"]
                     else f"readings WITHHELD ({len(roots['readings_problems'])} breaks)")
    print(f"anchored     : registry {roots['registry_root'][:16]}… / "
          f"erasure {roots['erasure_root'][:16]}… / " + readings_line)
    print(f"wayback      : {ok_wayback}/{len(WAYBACK_TARGETS)} snapshots")
    print(f"opentimestamps: {'stamped -> ' + record['ots']['proof'] if record['ots']['ok'] else record['ots'].get('reason')}")
    return record


if __name__ == "__main__":
    anchor(dry_run="--dry-run" in sys.argv[1:])
