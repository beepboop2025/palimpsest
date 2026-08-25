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
import io
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
from core.safe_fetch import (  # noqa: E402
    FetchError,
    SafeFetchResponse,
    safe_fetch_response,
)

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
WAYBACK_CAPTURE_VERSION = "3"
WAYBACK_SAVE_URL = "https://web.archive.org/save/"
WAYBACK_STATUS_URL = "https://web.archive.org/save/status/"
WAYBACK_RESPONSE_LIMIT = 1024 * 1024
WAYBACK_REPLAY_LIMIT = 64 * 1024 * 1024
UA = "palimpsest-anchor/1.0 (+https://palimpsest.info)"


class _BufferedResponse(io.BytesIO):
    """urllib-shaped view over one already bounded safe-fetch response."""

    def __init__(self, response: SafeFetchResponse):
        super().__init__(response.body)
        self.status = response.status
        self._url = response.url
        # safe_fetch has already decoded gzip/deflate through its output cap.
        self.headers = {
            name: value
            for name, value in response.headers.items()
            if name.casefold() not in {"content-encoding", "content-length"}
        }

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


def _wayback_url_policy(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FetchError("Wayback URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "web.archive.org"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise FetchError("Wayback URL is outside the reviewed HTTPS authority")
    allowed_path = (
        parsed.path == "/cdx/search/cdx"
        or parsed.path == "/save/"
        or parsed.path == "/save/status/"
        or parsed.path.startswith("/web/")
    )
    if not allowed_path:
        raise FetchError("Wayback URL is outside the reviewed API paths")


def _open_wayback(
    request,
    *,
    opener,
    timeout: int,
    max_bytes: int,
):
    """Use an injected offline opener or the hardened production transport."""
    if opener is not None:
        return opener(request, timeout=timeout)
    method = request.get_method()
    try:
        response = safe_fetch_response(
            request.full_url,
            method=method,
            body=request.data,
            headers=dict(request.header_items()),
            max_bytes=max_bytes,
            timeout=timeout,
            max_redirects=3,
            url_policy=_wayback_url_policy,
            return_redirect_response=method == "POST",
        )
    except FetchError as exc:
        raise OSError("Wayback transport failed") from exc
    buffered = _BufferedResponse(response)
    if not 200 <= response.status < 400:
        raise urllib.error.HTTPError(
            response.url,
            response.status,
            "bounded Wayback HTTP response",
            buffered.headers,
            buffered,
        )
    return buffered


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


def _wayback_cdx_snapshots(capture_target: str, *, opener,
                           timeout: int) -> list[str]:
    """Return newest-first exact successful captures indexed by Wayback CDX."""
    query = urllib.parse.urlencode([
        ("url", capture_target),
        ("output", "json"),
        ("fl", "timestamp,original,statuscode"),
        ("filter", "statuscode:200"),
        # A negative CDX limit selects from the end of the time-ordered index.
        # Sorting locally as well makes the newest-first contract independent
        # of the order in which a CDX node serializes those rows.
        ("limit", "-5"),
    ])
    request = urllib.request.Request(
        f"https://web.archive.org/cdx/search/cdx?{query}",
        headers={"User-Agent": UA, "Accept-Encoding": "identity"},
    )
    with _open_wayback(
        request,
        opener=opener,
        timeout=timeout,
        max_bytes=64 * 1024,
    ) as response:
        raw = response.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("Wayback CDX response is too large")
    payload = json.loads(raw)
    if not isinstance(payload, list) or not payload:
        return []
    header = payload[0]
    if header != ["timestamp", "original", "statuscode"]:
        raise ValueError("Wayback CDX response has an unexpected schema")
    timestamps = set()
    for row in payload[1:]:
        if (not isinstance(row, list) or len(row) != 3
                or row[1] != capture_target or row[2] != "200"
                or re.fullmatch(r"\d{14}", row[0]) is None):
            continue
        timestamps.add(row[0])
    return [
        f"https://web.archive.org/web/{timestamp}/{capture_target}"
        for timestamp in sorted(timestamps, reverse=True)
    ]


def _wayback_cdx_snapshot(capture_target: str, *, opener,
                          timeout: int) -> str | None:
    """Return the newest exact successful capture indexed by Wayback CDX."""
    snapshots = _wayback_cdx_snapshots(
        capture_target, opener=opener, timeout=timeout
    )
    return snapshots[0] if snapshots else None


def _wayback_replay_evidence(snapshot: str, *, expected_bytes: int, opener,
                             timeout: int, replay_attempts: int, sleeper) -> dict:
    if replay_attempts < 1:
        raise ValueError("Wayback replay attempts must be positive")
    if (
        type(expected_bytes) is not int
        or expected_bytes < 0
        or expected_bytes > WAYBACK_REPLAY_LIMIT
    ):
        raise ValueError("Wayback expected replay size is outside its ceiling")
    raw_snapshot = _raw_wayback_url(snapshot)
    replay_req = urllib.request.Request(
        raw_snapshot,
        headers={"User-Agent": UA, "Accept-Encoding": "identity"},
    )
    for attempt in range(replay_attempts):
        try:
            with _open_wayback(
                replay_req,
                opener=opener,
                timeout=timeout,
                max_bytes=max(1, expected_bytes + 64 * 1024),
            ) as replay:
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


def _evidence_matches(evidence: dict, *, expected_sha256: str,
                      expected_bytes: int) -> bool:
    return (
        evidence.get("snapshot_sha256") == expected_sha256
        and evidence.get("snapshot_bytes") == expected_bytes
    )


def _matching_cdx_snapshot(capture_target: str, *, expected_sha256: str,
                           expected_bytes: int, opener, timeout: int,
                           replay_attempts: int, sleeper) -> tuple[str, dict] | None:
    """Find an indexed capture whose replay is the exact expected artifact."""
    for snapshot in _wayback_cdx_snapshots(
        capture_target, opener=opener, timeout=timeout
    ):
        try:
            evidence = _wayback_replay_evidence(
                snapshot,
                expected_bytes=expected_bytes,
                opener=opener,
                timeout=timeout,
                replay_attempts=replay_attempts,
                sleeper=sleeper,
            )
        except (OSError, TimeoutError, ValueError):
            # One corrupt, unavailable, or not-yet-replicated replay must not
            # hide another valid capture returned by the same CDX query.
            continue
        if _evidence_matches(
            evidence,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
        ):
            return snapshot, evidence
    return None


def _eventual_matching_cdx_snapshot(
        capture_target: str, *, expected_sha256: str, expected_bytes: int,
        opener, timeout: int, attempts: int, replay_attempts: int,
        sleeper) -> tuple[str, dict] | None:
    """Poll boundedly until CDX exposes a byte-matching accepted capture."""
    if attempts < 1:
        raise ValueError("Wayback CDX attempts must be positive")
    for attempt in range(attempts):
        match = _matching_cdx_snapshot(
            capture_target,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
            opener=opener,
            timeout=timeout,
            replay_attempts=replay_attempts,
            sleeper=sleeper,
        )
        if match is not None:
            return match
        if attempt + 1 < attempts:
            sleeper(min(2 ** attempt, 4))
    return None


def _wayback_auth_headers(access_key: str | None,
                          secret_key: str | None) -> dict[str, str]:
    """Build SPN2's LOW auth header without ever logging either credential."""
    if access_key is None:
        access_key = os.environ.get("PALIMPSEST_WAYBACK_ACCESS_KEY", "")
    if secret_key is None:
        secret_key = os.environ.get("PALIMPSEST_WAYBACK_SECRET_KEY", "")
    if bool(access_key) != bool(secret_key):
        raise ValueError(
            "both PALIMPSEST_WAYBACK_ACCESS_KEY and "
            "PALIMPSEST_WAYBACK_SECRET_KEY are required together"
        )
    if not access_key:
        return {}
    if (
        type(access_key) is not str
        or type(secret_key) is not str
        or len(access_key) > 2_048
        or len(secret_key) > 2_048
        or any(
            ord(char) < 0x20 or ord(char) == 0x7f
            for char in access_key + secret_key
        )
    ):
        raise ValueError("Wayback credentials are invalid or too large")
    return {"Authorization": f"LOW {access_key}:{secret_key}"}


def _bounded_body(response, limit: int = WAYBACK_RESPONSE_LIMIT) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Wayback response is too large")
    return raw


def _json_object(raw: bytes) -> dict | None:
    if not raw.strip():
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r}")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _snapshot_from_url(candidate: str | None) -> str | None:
    if not candidate:
        return None
    try:
        _raw_wayback_url(candidate)
    except ValueError:
        return None
    return candidate


def _job_id_from_url(candidate: str | None) -> str | None:
    if not candidate:
        return None
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname != "web.archive.org":
        return None
    prefix = "/save/status/"
    if not parsed.path.startswith(prefix):
        return None
    job_id = parsed.path[len(prefix):].strip("/")
    return job_id or None


def _valid_job_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) else None


def _poll_wayback_job(job_id: str, capture_target: str, *, auth_headers: dict,
                      opener, timeout: int, attempts: int, sleeper) -> str:
    """Poll SPN2's authenticated job endpoint for its exact replay URL."""
    if attempts < 1:
        raise ValueError("Wayback status attempts must be positive")
    for attempt in range(attempts):
        body = urllib.parse.urlencode({"job_id": job_id}).encode()
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            **auth_headers,
        }
        request = urllib.request.Request(
            WAYBACK_STATUS_URL, data=body, headers=headers, method="POST"
        )
        with _open_wayback(
            request,
            opener=opener,
            timeout=timeout,
            max_bytes=WAYBACK_RESPONSE_LIMIT,
        ) as response:
            payload = _json_object(_bounded_body(response))
        if payload is None:
            raise ValueError("Wayback status returned a non-JSON response")
        if payload.get("status") == "pending":
            if attempt + 1 < attempts:
                sleeper(3)
                continue
            raise TimeoutError("Wayback capture job remained pending")
        timestamp = payload.get("timestamp")
        original_url = payload.get("original_url")
        if (isinstance(timestamp, str) and re.fullmatch(r"\d{14}", timestamp)
                and original_url == capture_target):
            return f"https://web.archive.org/web/{timestamp}/{original_url}"
        message = payload.get("message")
        if isinstance(message, str) and message:
            raise ValueError(f"Wayback capture failed: {message}")
        raise ValueError("Wayback status omitted the completed capture URL")
    raise AssertionError("unreachable Wayback status loop")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4097)
    except OSError:
        raw = b""
    if len(raw) > 4096:
        raw = raw[:4096]
    payload = _json_object(raw)
    if payload is not None and isinstance(payload.get("message"), str):
        return payload["message"][:400]
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:400] or str(exc.reason)


def wayback_save(url: str, *, expected_sha256: str, expected_bytes: int,
                 opener=None, timeout: int = 90,
                 replay_attempts: int = 3, cdx_attempts: int = 4,
                 status_attempts: int = 20, sleeper=time.sleep,
                 access_key: str | None = None,
                 secret_key: str | None = None) -> dict:
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
    try:
        auth_headers = _wayback_auth_headers(access_key, secret_key)
        try:
            indexed = _matching_cdx_snapshot(
                capture_target,
                expected_sha256=expected_sha256,
                expected_bytes=expected_bytes,
                opener=opener,
                timeout=timeout,
                replay_attempts=replay_attempts,
                sleeper=sleeper,
            )
        except (OSError, TimeoutError, ValueError):
            indexed = None
        if indexed is not None:
            snapshot, replay_evidence = indexed
            result.update({
                "snapshot": snapshot,
                "capture_source": "cdx",
                "http": None,
                **replay_evidence,
                "ok": True,
            })
            return result

        form = urllib.parse.urlencode({"url": capture_target}).encode()
        save_url = WAYBACK_SAVE_URL + "?" + urllib.parse.urlencode(
            {"url": capture_target}
        )
        save_headers = {
            "User-Agent": UA,
            "Accept": "application/json" if auth_headers else
                      "text/html,application/xhtml+xml,application/xml",
            "Content-Type": "application/x-www-form-urlencoded",
            **auth_headers,
        }
        request = urllib.request.Request(
            save_url, data=form, headers=save_headers, method="POST"
        )
        http_status = None
        save_error = None
        status_error = None
        candidates: list[tuple[str, str]] = []
        job_id = None
        try:
            with _open_wayback(
                request,
                opener=opener,
                timeout=timeout,
                max_bytes=WAYBACK_RESPONSE_LIMIT,
            ) as response:
                http_status = getattr(response, "status", None)
                headers = getattr(response, "headers", None)
                location = (
                    headers.get("Content-Location") or headers.get("Location")
                    if headers
                    else None
                )
                response_url = response.geturl()
                raw = _bounded_body(response)
            payload = _json_object(raw)
            if payload is not None:
                job_id = _valid_job_id(payload.get("job_id"))
            job_id = job_id or _valid_job_id(_job_id_from_url(location))
            job_id = job_id or _valid_job_id(_job_id_from_url(response_url))
            for candidate in (
                urllib.parse.urljoin("https://web.archive.org", location)
                if location else None,
                response_url,
            ):
                snapshot = _snapshot_from_url(candidate)
                if snapshot is not None:
                    candidates.append((snapshot, "save"))
        except urllib.error.HTTPError as exc:
            http_status = exc.code
            if exc.code == 401:
                raise
            if exc.code not in {404, 429, 500, 502, 503, 504}:
                raise
            save_error = exc
            snapshot = _snapshot_from_url(exc.geturl())
            if snapshot is not None:
                candidates.append((snapshot, "save"))

        if job_id is not None:
            try:
                status_snapshot = _poll_wayback_job(
                    job_id,
                    capture_target,
                    auth_headers=auth_headers,
                    opener=opener,
                    timeout=timeout,
                    attempts=status_attempts,
                    sleeper=sleeper,
                )
                candidates.append((status_snapshot, "status"))
            except (OSError, TimeoutError, ValueError) as exc:
                status_error = exc

        mismatch = None
        for snapshot, capture_source in candidates:
            try:
                replay_evidence = _wayback_replay_evidence(
                    snapshot,
                    expected_bytes=expected_bytes,
                    opener=opener,
                    timeout=timeout,
                    replay_attempts=replay_attempts,
                    sleeper=sleeper,
                )
            except (OSError, TimeoutError, ValueError):
                continue
            if _evidence_matches(
                replay_evidence,
                expected_sha256=expected_sha256,
                expected_bytes=expected_bytes,
            ):
                result.update({
                    "snapshot": snapshot,
                    "capture_source": capture_source,
                    "http": http_status,
                    **replay_evidence,
                    "ok": True,
                })
                return result
            mismatch = snapshot, capture_source, replay_evidence

        indexed = _eventual_matching_cdx_snapshot(
            capture_target,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
            opener=opener,
            timeout=timeout,
            attempts=cdx_attempts,
            replay_attempts=replay_attempts,
            sleeper=sleeper,
        )
        if indexed is not None:
            snapshot, replay_evidence = indexed
            result.update({
                "snapshot": snapshot,
                "capture_source": "cdx",
                "http": http_status,
                **replay_evidence,
                "ok": True,
            })
            return result
        if mismatch is not None:
            snapshot, capture_source, replay_evidence = mismatch
            result.update({
                "snapshot": snapshot,
                "capture_source": capture_source,
                "http": http_status,
                **replay_evidence,
                "ok": False,
                "reason": "Wayback replay does not match the served artifact",
            })
            return result
        if status_error is not None:
            raise status_error
        if save_error is not None:
            raise save_error
        raise ValueError(
            "Wayback accepted the save request but no byte-matching snapshot appeared"
        )
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        if exc.code == 401:
            reason = (
                "Wayback authentication required (HTTP 401): " + detail
                + "; configure PALIMPSEST_WAYBACK_ACCESS_KEY and "
                  "PALIMPSEST_WAYBACK_SECRET_KEY"
            )
        else:
            reason = f"HTTPError {exc.code}: {detail}"
        result.update({"ok": False, "reason": reason})
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


def summarize_anchor_record(record: dict) -> dict:
    """Return the stable public summary for one append-only anchor record."""
    roots = record["roots"]
    wayback = record["wayback"]
    ots = record["ots"]
    latest = {
        "ts": record["ts"],
        "registry_root": roots["registry_root"],
        "erasure_root": roots["erasure_root"],
        "readings_root": roots["readings_root"],
        "readings_chain": (
            "broken" if roots.get("readings_problems") else "verified"
        ),
        "readings_problems": roots.get("readings_problems", []),
        "wayback_ok": sum(1 for item in wayback if item["ok"]),
        "wayback_snapshots": [
            item.get("snapshot") for item in wayback if item["ok"]
        ],
        "wayback_reused": sum(
            1 for item in wayback if item.get("reused") is True
        ),
        "ots": ots.get("proof") if ots["ok"] else None,
        "ots_status": "stamped" if ots["ok"] else ots.get("reason", "failed"),
        "ots_reused": ots.get("reused") is True,
    }
    if record.get("retry_of"):
        latest["retry_of"] = record["retry_of"]
    return latest


def serialize_anchor_summary(record: dict) -> str:
    """Serialize a summary exactly as ``anchors-latest.json`` is published."""
    return json.dumps(
        summarize_anchor_record(record), ensure_ascii=False, indent=1
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def anchor_state_at(log_path, as_of: datetime) -> dict | None:
    """Reconstruct the append-only anchor state that existed at ``as_of``.

    Anchor timestamps are required to be monotonic because the historical file state is
    a byte prefix, not an unordered set.  The returned sizes therefore describe the exact
    public summary and JSONL prefix available at that publication clock.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("anchor history clock must include a timezone")
    cutoff = as_of.astimezone(timezone.utc)
    selected = None
    selected_at = None
    last_at = None
    history_bytes = 0
    history_rows = 0
    with open(log_path, "rb") as history:
        for line_number, raw_line in enumerate(history, start=1):
            if not raw_line.strip():
                raise ValueError(
                    f"anchor history line {line_number} is empty"
                )
            try:
                record = json.loads(
                    raw_line, parse_constant=_reject_json_constant
                )
                text = record["ts"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("timestamp is absent")
                normalized = text.strip()
                if normalized.endswith(("Z", "z")):
                    normalized = normalized[:-1] + "+00:00"
                record_at = datetime.fromisoformat(normalized)
                if record_at.tzinfo is None or record_at.utcoffset() is None:
                    raise ValueError("timestamp has no timezone")
                record_at = record_at.astimezone(timezone.utc)
            except (
                KeyError,
                TypeError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise ValueError(
                    f"anchor history line {line_number} is invalid"
                ) from exc
            if last_at is not None and record_at < last_at:
                raise ValueError("anchor history timestamps are not monotonic")
            last_at = record_at
            if record_at <= cutoff:
                selected = record
                selected_at = record_at
                history_bytes += len(raw_line)
                history_rows += raw_line.count(b"\n")

    if selected is None:
        return None
    summary_text = serialize_anchor_summary(selected)
    return {
        "record": selected,
        "recorded_at": selected_at,
        "summary": summarize_anchor_record(selected),
        "summary_bytes": summary_text.encode("utf-8"),
        "history_bytes": history_bytes,
        "history_rows": history_rows,
    }


def anchor(*, dry_run: bool = False, opener=None,
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
    # Each Wayback witness is bound to one artifact's exact digest and length,
    # so an unchanged artifact remains valid even when another root moves.
    prior_wayback = reusable_wayback(prev, expectations)
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
            "wayback_targets": missing_wayback,
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
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(serialize_anchor_summary(record))

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
