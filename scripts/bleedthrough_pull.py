"""BLEEDTHROUGH runner — one injector-tomography round, published to
readings/bleedthrough-latest.json (+ history).

UNLIKE the passive signals (OONI, GDELT, DDTI) this one ACTIVELY PROBES China, so it must
NOT run from GitHub Actions or any shared CI IP — those get burned instantly and it is the
wrong place for rotating probers. It is built to run from a DEPLOYMENT-CONTROLLED prober
(a rotating VPS outside China), and it is triple-gated:

  1. env BLEEDTHROUGH_LIVE must be truthy (default OFF — a bare run does nothing),
  2. the kill switch (core/governance) must be released, and
  3. the target file must be a CURATED list, not the shipped placeholder example.

Direct targets ride the direct transport (fleet size); resolver targets ride the
open-resolver fallback (pool / rotation / regional signal that survives inbound decay). A
disk baseline (JsonFleetStore) remembers each vantage across runs so rotation/capacity/
silence events fall out. Honesty guard: if nothing injected this round, abstain rather than
publish a hollow board. Standard-library only.

    BLEEDTHROUGH_LIVE=1 BLEEDTHROUGH_TARGETS=config/bleedthrough_targets.json \\
        python -m scripts.bleedthrough_pull
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from collectors.bleedthrough import (
    JsonFleetStore,
    FleetBaselineStore,
    REGIONAL_FIREWALL,
    _udp_transport,
    distinct_region_pools,
    load_targets,
    open_resolver_transport,
    run_round,
)
from core.claim_support import looks_sampled
from core.governance import KillSwitch, RateCeiling

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READINGS = os.getenv("BLEEDTHROUGH_READINGS", os.path.join(ROOT, "readings"))
OUT = os.getenv("BLEEDTHROUGH_OUT", os.path.join(READINGS, "bleedthrough-latest.json"))
HIST = os.getenv(
    "BLEEDTHROUGH_HIST", os.path.join(READINGS, "bleedthrough-history.jsonl")
)

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
METHOD_VERSION = 2

STORE_DIR = os.getenv(
    "BLEEDTHROUGH_STORE",
    os.path.join(ROOT, "data", "bleedthrough_baselines"),
)
PENDING = os.getenv(
    "BLEEDTHROUGH_PENDING",
    os.path.join(STORE_DIR, ".pending-publication.json"),
)
PENDING_VERSION = 1
MAX_PENDING_BYTES = 8 * 1024 * 1024

TARGETS = os.getenv(
    "BLEEDTHROUGH_TARGETS", os.path.join(ROOT, "config", "bleedthrough_targets.json")
)
RATE_PER_SEC = float(
    os.getenv("BLEEDTHROUGH_RATE", "5")
)  # polite default; deployment tunes
BURST = int(os.getenv("BLEEDTHROUGH_BURST", "24"))
WAIT_S = float(
    os.getenv("BLEEDTHROUGH_WAIT", "1.2")
)  # listen window per query, recorded

# Provenance of the prober itself. Deliberately COARSE: the exact host is operator-only. A
# reading that named the box would bind this prober to the public api.seiche.info A record,
# which is exactly the linkage ops/bleedthrough_prober.sh refuses by default.
VANTAGE_KIND = os.getenv(
    "BLEEDTHROUGH_VANTAGE_KIND", "single fixed-IP VPS outside China"
)
VANTAGE_COUNTRY = os.getenv("BLEEDTHROUGH_VANTAGE_COUNTRY") or None

# A round is one prober, however many targets it probes. Recorded so no reader can mistake
# the target count for a count of observation points.
VANTAGE_COUNT = 1
DEPLOYED_COMMIT_FILE = os.getenv(
    "PALIMPSEST_DEPLOYED_COMMIT_FILE", "/etc/palimpsest/deployed-commit"
)
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")

# Publication is a separate trust boundary from collection.  The fleet store deliberately
# keys longitudinal baselines by the exact target tag, but those keys must never cross into
# latest/history: an IPv4 hash would be cheaply enumerable and therefore is not an honest
# anonymisation scheme.  Public events retain only allow-listed province/ASN scope.
_PUBLIC_REGIONS = frozenset(
    {
        "CN",
        "CN-AH",
        "CN-BJ",
        "CN-CQ",
        "CN-FJ",
        "CN-GD",
        "CN-GS",
        "CN-GX",
        "CN-GZ",
        "CN-HA",
        "CN-HB",
        "CN-HE",
        "CN-HI",
        "CN-HK",
        "CN-HL",
        "CN-HN",
        "CN-JL",
        "CN-JS",
        "CN-JX",
        "CN-LN",
        "CN-MO",
        "CN-NM",
        "CN-NX",
        "CN-QH",
        "CN-SC",
        "CN-SD",
        "CN-SH",
        "CN-SN",
        "CN-SX",
        "CN-TJ",
        "CN-TW",
        "CN-XJ",
        "CN-XZ",
        "CN-YN",
        "CN-ZJ",
    }
)
_PUBLIC_ASNS = frozenset({"AS4134", "AS4808", "AS4812", "AS4837", "AS9808", "AS17623"})
_PUBLIC_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_PUBLIC_EVENT_KINDS = frozenset(
    {
        "pool_rotation",
        "capacity_shift",
        "injector_silent",
        "regional_firewall_candidate",
    }
)
_PUBLIC_VANTAGE_KINDS = {
    "single fixed-ip vps outside china": "single fixed-IP VPS outside China",
    "single vps outside china": "single VPS outside China",
    "rotating vps outside china": "rotating VPS outside China",
}


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _public_probe_domain(value: str) -> str | None:
    """Return one publishable DNS name, never an IP, URL, contact, or local path."""
    domain = (value or "").strip().rstrip(".")
    if not domain or len(domain) > 253 or any(c in domain for c in "@/:\\?#%"):
        return None
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not _PUBLIC_DOMAIN_LABEL.fullmatch(label) for label in labels
    ):
        return None
    return domain.lower()


def _public_vantage_kind(value: str) -> str:
    """Map operator input onto a finite coarse vocabulary; never echo arbitrary env text."""
    normalized = " ".join((value or "").strip().lower().split())
    return _PUBLIC_VANTAGE_KINDS.get(normalized, "controlled external VPS")


def _public_country(value: str | None) -> str | None:
    country = (value or "").strip().upper()
    return country if re.fullmatch(r"[A-Z]{2}", country) else None


def _public_vantage_scope(vantage_tag: str) -> str:
    """Reduce ``target-ip@province/asn`` to validated aggregate geography.

    Malformed labels fail closed to the national bucket.  In particular, this function
    does not hash the target address: the IPv4 space is small enough for an observer to
    reverse an unkeyed digest by enumeration.
    """
    raw_scope = str(vantage_tag or "").rsplit("@", 1)[-1]
    raw_region, separator, raw_asn = raw_scope.partition("/")
    region = raw_region.strip().upper()
    asn = raw_asn.strip().upper() if separator else ""
    if region not in _PUBLIC_REGIONS:
        return "CN"
    if asn in _PUBLIC_ASNS:
        return f"{region}/{asn}"
    return region


def _bounded_process_count(value) -> int | None:
    # bool is an int subclass, but it is not a defensible process-count observation.
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 1_000_000
    ):
        return value
    return None


def _public_event(event) -> dict | None:
    """Build a public event from allow-listed semantics, never from free-form event text."""
    kind = str(getattr(event, "kind", ""))
    if kind not in _PUBLIC_EVENT_KINDS:
        return None

    if kind == "capacity_shift":
        before = _bounded_process_count(
            event.a.get("process_count") if isinstance(event.a, dict) else None
        )
        after = _bounded_process_count(
            event.b.get("process_count") if isinstance(event.b, dict) else None
        )
        detail = (
            f"injector-process floor changed from {before} to {after}"
            if before is not None and after is not None
            else "injector-process floor changed"
        )
    elif kind == "pool_rotation":
        detail = "forged-IP pool rotated"
    elif kind == "injector_silent":
        detail = "previously injecting target became silent"
    else:
        detail = "regional forged-IP pool diverged from the shared baseline"

    vantage = _public_vantage_scope(getattr(event, "vantage_tag", ""))
    if kind == "regional_firewall_candidate" and vantage.split("/", 1)[0] == "CN":
        return None
    return {
        "kind": kind,
        "vantage": vantage,
        "detail": detail,
        "severity": event.severity(),
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _atomic_write(path: str, payload: bytes, *, mode: int = 0o644) -> None:
    """Durably replace one public artifact while preserving its last-good version."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _atomic_write_json(path: str, value: dict) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _atomic_write(path, payload)


def _atomic_append_jsonl(path: str, value: dict) -> None:
    """Append through atomic replacement; a crash cannot leave a torn JSONL record."""
    destination = Path(path)
    try:
        previous = destination.read_bytes()
    except FileNotFoundError:
        previous = b""
    if previous and not previous.endswith(b"\n"):
        raise ValueError(f"refusing to append to truncated history: {destination}")
    row = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    _atomic_write(path, previous + row)


def _atomic_append_jsonl_once(path: str, value: dict) -> None:
    """Append one exact row unless a prior transaction already committed it."""
    destination = Path(path)
    try:
        previous = destination.read_bytes()
    except FileNotFoundError:
        previous = b""
    if previous and not previous.endswith(b"\n"):
        raise ValueError(f"refusing to append to truncated history: {destination}")
    for line in previous.splitlines():
        try:
            existing = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"refusing to append to invalid history: {destination}"
            ) from exc
        if existing == value:
            return
        if existing.get("generated_at") == value.get("generated_at"):
            raise ValueError(
                f"refusing conflicting history rows at {value.get('generated_at')}"
            )
    row = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    _atomic_write(path, previous + row)


class _StagedFleetStore:
    """Read durable baselines while holding this round's mutations in memory."""

    def __init__(self, durable: JsonFleetStore):
        self.durable = durable
        self.updates: dict[str, dict] = {}

    def get(self, tag: str):
        if tag in self.updates:
            return self.updates[tag]
        return self.durable.get(tag)

    def put(self, tag: str, baseline: dict) -> None:
        self.updates[tag] = dict(baseline)

    def journal_updates(self) -> list[dict]:
        return [
            {"tag": tag, "baseline": baseline}
            for tag, baseline in sorted(self.updates.items())
        ]


def _commit_baseline_updates(updates: list[dict]) -> None:
    durable = JsonFleetStore(STORE_DIR)
    for update in updates:
        durable.put(update["tag"], update["baseline"])


def _validate_pending(value) -> dict:
    """Validate the private recovery record before it can write any destination."""
    if not isinstance(value, dict) or set(value) != {
        "pending_version",
        "latest",
        "history_row",
        "baseline_updates",
    }:
        raise ValueError("BLEEDTHROUGH pending publication has an invalid envelope")
    if value["pending_version"] != PENDING_VERSION:
        raise ValueError("BLEEDTHROUGH pending publication version is unsupported")
    if not isinstance(value["latest"], dict):
        raise ValueError("BLEEDTHROUGH pending latest document is invalid")
    if value["history_row"] is not None and not isinstance(value["history_row"], dict):
        raise ValueError("BLEEDTHROUGH pending history row is invalid")
    updates = value["baseline_updates"]
    if not isinstance(updates, list) or len(updates) > 10_000:
        raise ValueError("BLEEDTHROUGH pending baseline update list is invalid")
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"tag", "baseline"}:
            raise ValueError("BLEEDTHROUGH pending baseline update is invalid")
        if not isinstance(update["tag"], str) or not 1 <= len(update["tag"]) <= 512:
            raise ValueError("BLEEDTHROUGH pending baseline tag is invalid")
        baseline = update["baseline"]
        if not isinstance(baseline, dict) or set(baseline) != {
            "pool_hash",
            "cycle_signature",
            "process_count",
            "observed_at",
        }:
            raise ValueError("BLEEDTHROUGH pending baseline value is invalid")
    return value


def _write_pending(value: dict) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_PENDING_BYTES:
        raise ValueError("BLEEDTHROUGH pending publication exceeds its byte cap")
    _atomic_write(PENDING, payload, mode=0o600)


def _clear_pending() -> None:
    pending = Path(PENDING)
    pending.unlink()
    _fsync_directory(pending.parent)


def _recover_pending_publication() -> bool:
    """Replay one durable publication exactly once, before any fresh network probe."""
    pending = Path(PENDING)
    try:
        payload = pending.read_bytes()
    except FileNotFoundError:
        return False
    if len(payload) > MAX_PENDING_BYTES:
        raise ValueError("BLEEDTHROUGH pending publication exceeds its byte cap")
    try:
        transaction = _validate_pending(json.loads(payload))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("BLEEDTHROUGH pending publication is invalid JSON") from exc

    _commit_baseline_updates(transaction["baseline_updates"])
    if transaction["history_row"] is not None:
        _atomic_append_jsonl_once(HIST, transaction["history_row"])
    _atomic_write_json(OUT, transaction["latest"])
    _clear_pending()
    print(
        "BLEEDTHROUGH: recovered the pending publication before starting a new probe."
    )
    return True


def _code_version() -> str | None:
    """Best-effort commit id, read straight off .git — no subprocess (a flagged sink), and
    then from the root-owned deployment receipt used by exported production trees."""
    git = os.path.join(ROOT, ".git")
    try:
        head = open(os.path.join(git, "HEAD"), encoding="utf-8").read().strip()
        if not head.startswith("ref:"):
            candidate = head[:40]
            return candidate if _COMMIT_RE.fullmatch(candidate) else None
        ref = head.split(":", 1)[1].strip()
        try:
            candidate = (
                open(os.path.join(git, ref), encoding="utf-8").read().strip()[:40]
            )
            return candidate if _COMMIT_RE.fullmatch(candidate) else None
        except OSError:
            for line in open(os.path.join(git, "packed-refs"), encoding="utf-8"):
                if line.rstrip().endswith(" " + ref):
                    candidate = line.split(" ", 1)[0][:40]
                    return candidate if _COMMIT_RE.fullmatch(candidate) else None
    except OSError:
        pass
    try:
        candidate = open(DEPLOYED_COMMIT_FILE, encoding="utf-8").read().strip()
    except OSError:
        return None
    return candidate if _COMMIT_RE.fullmatch(candidate) else None


def _refuse(msg: str) -> None:
    print(f"BLEEDTHROUGH: refusing to run — {msg}")


def main() -> None:
    # A previous round may already have advanced the private fleet state.  Finish its exact
    # publication and stop: probing again in the same invocation would immediately replace
    # the recovered event document with an event-free heartbeat.
    if _recover_pending_publication():
        return

    # ── gate 1: explicit opt-in ────────────────────────────────────────────────────────
    if not _truthy(os.getenv("BLEEDTHROUGH_LIVE")):
        _refuse(
            "BLEEDTHROUGH_LIVE is not set. This runner actively probes China and must be "
            "launched deliberately from a controlled prober, never from CI."
        )
        return
    # ── gate 2: kill switch ────────────────────────────────────────────────────────────
    if KillSwitch().is_halted():
        _refuse("the kill switch is engaged.")
        return
    # ── gate 3: not the placeholder example ────────────────────────────────────────────
    if not os.path.exists(TARGETS):
        _refuse(
            f"no target file at {TARGETS}. Curate one with curate_dark_ips / "
            f"curate_resolvers; the shipped file is an example only."
        )
        return
    try:
        raw = json.load(open(TARGETS, encoding="utf-8"))
    except (OSError, ValueError) as e:
        _refuse(f"target file unreadable ({e}).")
        return
    if raw.get("_meta", {}).get("placeholder"):
        _refuse(
            "the target file is the shipped placeholder (RFC 5737 documentation IPs). "
            "Replace it with a curated list before probing."
        )
        return

    conf = load_targets(TARGETS)
    probe, dark, resolver = conf["probe"], conf["dark"], conf["resolver"]
    public_probe_domain = _public_probe_domain(probe.domain)
    if public_probe_domain is None:
        _refuse("the configured probe is not a publishable DNS hostname.")
        return
    rate = RateCeiling(rate=RATE_PER_SEC)
    # Armed PER PROBE, not merely checked once above: a round is thousands of datagrams over
    # roughly an hour, and a startup-only gate leaves that whole window unstoppable.
    kill = KillSwitch()
    durable_store = JsonFleetStore(STORE_DIR)
    staged_store = _StagedFleetStore(durable_store)
    store = FleetBaselineStore(store=staged_store)

    fingerprints, events = [], []
    # direct-injection round (fleet size) over dark IPs
    if dark:

        def direct_transport(domain, target_ip):
            return _udp_transport(domain, target_ip, wait=WAIT_S)

        r = run_round(
            probe,
            dark,
            transport=direct_transport,
            store=store,
            kill_switch=kill,
            rate_ceiling=rate,
            burst=BURST,
        )
        fingerprints += r["fingerprints"]
        events += r["events"]
    # open-resolver fallback round (pool / regional) over live resolvers
    if resolver:
        rt = open_resolver_transport(
            clean_answers=conf.get("clean_answers"), wait=WAIT_S
        )
        r = run_round(
            probe,
            resolver,
            transport=rt,
            store=store,
            kill_switch=kill,
            rate_ceiling=rate,
            burst=BURST,
        )
        fingerprints += r["fingerprints"]
        events += r["events"]

    injecting = [fp for fp in fingerprints if fp.pool_hash]
    # Honesty guard 1: no injection observed anywhere → abstain (channel may be down / list stale)
    if not injecting:
        print(
            "BLEEDTHROUGH: no injection observed on any target this round "
            "(channel down / list stale / all silent) — abstaining, not publishing."
        )
        return

    # Honesty guard 2: when per-target pool hashes are near-unique, the censor's forged-IP
    # pool is being SAMPLED rather than enumerated at this burst. Per-target pools are then
    # not comparable to each other and any regional reading is sampling noise, so strip the
    # regional claims and record that we did. This is the failure mode a single prober over
    # many dark targets actually produces; regional_divergence guards it too, and this is the
    # second layer in case a future caller bypasses that one.
    sampled_pools = looks_sampled(
        len({fp.pool_hash for fp in injecting}), len(injecting)
    )
    if sampled_pools:
        dropped = sum(1 for e in events if e.kind == REGIONAL_FIREWALL)
        events = [e for e in events if e.kind != REGIONAL_FIREWALL]
        print(
            f"BLEEDTHROUGH: pool hashes are near-unique across targets — the pool is "
            f"sampled, not enumerated, at burst {BURST}; regional divergence is not "
            f"identifiable this round. Dropped {dropped} regional event(s)."
        )

    # transports: `ran: False` with a null count, never 0 — "did not run" is not "measured zero"
    transports = {
        "direct": {"ran": bool(dark), "targets": len(dark) or None},
        "open_resolver": {"ran": bool(resolver), "targets": len(resolver) or None},
    }
    legs = [
        name
        for name, key in (
            ("direct injection over dark IPs", "direct"),
            ("open-resolver fallback", "open_resolver"),
        )
        if transports[key]["ran"]
    ]
    projected_events = [
        public for event in events if (public := _public_event(event)) is not None
    ]
    # Multiple private targets can observe the same coarse apparatus transition.
    # Publishing one identical row per target would leak panel shape and inflate
    # apparent event volume, so the public boundary keeps one deterministic row
    # per kind/scope/detail/severity tuple.
    public_events = [
        dict(zip(("kind", "vantage", "detail", "severity"), key, strict=True))
        for key in sorted(
            {
                (
                    event["kind"],
                    event["vantage"],
                    event["detail"],
                    event["severity"],
                )
                for event in projected_events
            }
        )
    ]

    now = datetime.now(timezone.utc)
    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "signal": "bleedthrough",
        "title": "GFW injector fleet",
        "scope": (
            "apparatus-layer tomography of the Great Firewall's DNS-injector fleet: "
            "forged-IP pools, a floor on parallel injector responses, and operational "
            "events, measured from outside China"
        ),
        # Computed from what actually ran this round — a hardcoded sentence would misdescribe
        # the first round whose transport mix differs.
        "method": (
            "the censor as sensor — benign stateless UDP DNS probes provoke the GFW's "
            "own injectors to answer; we fingerprint the fleet from the forgeries. "
            f"Transport this round: {' + '.join(legs)}. "
            "Injector count is a FLOOR, not a census: each injector answers a given "
            "query at most once, so a silent injector is undercounted."
        ),
        "probe_domain": public_probe_domain,
        # NB: these count TARGET IPs probed, not observation points. There is one prober;
        # see provenance.vantage_count.
        "vantages_probed": len(fingerprints),
        "vantages_injecting": len(injecting),
        "distinct_pools": distinct_region_pools(injecting),
        "distinct_pools_basis": "union of forged IPs per region, not per-target samples",
        "max_process_count": max((fp.process_count for fp in injecting), default=0),
        "process_count_semantics": "floor",
        "pool_sampling_suspected": sampled_pools,
        "provenance": {
            "vantage_count": VANTAGE_COUNT,
            "vantage_kind": _public_vantage_kind(VANTAGE_KIND),
            "vantage_country": _public_country(VANTAGE_COUNTRY),
            "flow_id_policy": "ephemeral source port per query (paths not pinned)",
            "burst": BURST,
            "rate_per_sec": RATE_PER_SEC,
            "wait_s": WAIT_S,
            "queries_attempted": sum(fp.n_probes for fp in fingerprints),
            "transports": transports,
            "code_version": _code_version(),
            "authorization": {
                "live_opt_in": True,
                "fixed_box_opt_in": _truthy(os.getenv("BLEEDTHROUGH_ALLOW_BOX")),
            },
            "caveat": (
                "single-vantage round: observed censorship varies with network path, "
                "so cross-region comparisons from one prober are not identifiable "
                "(cf. arXiv:2406.19304)."
            ),
        },
        "events": public_events,
    }

    os.makedirs(READINGS, exist_ok=True)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except (ValueError, OSError):
            prev = {}
    sig_keys = (
        "method",
        "vantages_probed",
        "vantages_injecting",
        "distinct_pools",
        "max_process_count",
        "pool_sampling_suspected",
        "events",
    )
    provenance_keys = (
        "vantage_count",
        "burst",
        "rate_per_sec",
        "wait_s",
        "transports",
    )
    # method_version is part of the comparison so a methodology correction reaches
    # the published file even when every value is identical.
    changed = (
        any(prev.get(key) != out.get(key) for key in sig_keys)
        or any(
            (prev.get("provenance") or {}).get(key) != out["provenance"].get(key)
            for key in provenance_keys
        )
        or prev.get("method_version") != METHOD_VERSION
    )

    # "When did we last look" and "when did the answer last move" are different
    # questions, and a reader has to be able to tell them apart. Write-if-changed
    # answers only the second, so a fleet that holds still — which is exactly what
    # a stable injector deployment looks like — stopped refreshing generated_at,
    # and the observatory ended up labelling its own healthy signal stale. A quiet
    # apparatus and a dead prober are not the same claim. So every round that gets
    # past the honesty guards publishes its own observation time, and
    # last_changed_at carries the movement. The history file stays gated on change,
    # so the movement record never fills with heartbeats and no false sense of
    # rotation is manufactured. Falling back to the previous generated_at lets a
    # file published before this field existed backfill honestly.
    out["last_changed_at"] = (
        out["generated_at"]
        if (changed or not prev)
        else (prev.get("last_changed_at") or prev.get("generated_at"))
    )

    history_row = None
    if changed or not prev:
        history_row = {
            "generated_at": out["generated_at"],
            # denominator alongside the numerator, so a later baseline cannot compare
            # rounds of different sizes as though they were the same measurement
            "vantages_probed": out["vantages_probed"],
            "vantages_injecting": out["vantages_injecting"],
            "distinct_pools": out["distinct_pools"],
            "max_process_count": out["max_process_count"],
            "pool_sampling_suspected": out["pool_sampling_suspected"],
            "vantage_count": VANTAGE_COUNT,
            "burst": BURST,
            "method_version": METHOD_VERSION,
            "rate_per_sec": RATE_PER_SEC,
            "wait_s": WAIT_S,
            "direct_targets": transports["direct"]["targets"] or 0,
            "open_resolver_targets": transports["open_resolver"]["targets"] or 0,
            "n_events": len(public_events),
        }
    else:
        print(
            f"BLEEDTHROUGH: fleet unchanged since {out['last_changed_at']} — "
            f"republished with this round's observation time, history untouched"
        )

    # Persist the complete intended result before advancing the private baselines.  A later
    # failure can then replay the event-bearing document even though a new comparison would
    # no longer emit the transition.  The pending file is private (0600) because baseline
    # tags contain exact curated targets; only the sanitized latest/history are public.
    transaction = {
        "pending_version": PENDING_VERSION,
        "latest": out,
        "history_row": history_row,
        "baseline_updates": staged_store.journal_updates(),
    }
    _write_pending(transaction)
    _commit_baseline_updates(transaction["baseline_updates"])
    if history_row is not None:
        _atomic_append_jsonl_once(HIST, history_row)
    _atomic_write_json(OUT, out)
    _clear_pending()

    print(
        f"=== BLEEDTHROUGH — {len(injecting)}/{len(fingerprints)} target IPs injecting "
        f"from {VANTAGE_COUNT} prober, {out['distinct_pools']} distinct pool(s), "
        f"injector floor >={out['max_process_count']} ==="
    )
    for event in public_events:
        print(
            f"  [{event['severity']}] {event['kind']} — "
            f"{event['vantage']}: {event['detail']}"
        )


if __name__ == "__main__":
    main()
