"""INFORMATION ERASURE OBSERVATORY — the unifying runner.

Palimpsest already measures erasure on three surfaces. This runner does NOT
re-measure them; it (1) reads the latest published reading from each surface,
(2) seals each raw reading into the tamper-evident ledger at capture time, and
(3) publishes one composite "Erasure Index" plus the ledger's integrity proof.

The thesis (see docs/ERASURE-OBSERVATORY.md): the field measures ACCESS in the
present tense ("can you reach X now"). We measure ERASURE across time — what was
removed or rewritten — on three layers, and we seal our record so it cannot be
quietly rewritten in turn. That combination is what no access-index competitor has.

    NETWORK erasure  — reachability that disappeared      (OONI GFW index; Censored Planet cross-check)
    NARRATIVE erasure — encyclopedia entries rewritten     (Baike redaction-diff vs the open record)
    MODEL erasure    — answers a model will no longer give (Generative Firewall refusal/party-line index)

Fail loud: a missing surface is reported as ABSENT with a reason, never silently
dropped or zero-filled, and a surface whose newest reading is no longer current is
reported STALE with its true as-of date rather than carried forward as today's value.
The composite is the mean of the layers that actually reported, and we publish exactly
which layers contributed.

stdlib-only, key-less, vantage-insensitive. Runs in GitHub Actions after the
per-surface signals have committed their readings.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import sealed_ledger  # noqa: E402
READINGS = os.path.join(ROOT, "readings")
LEDGER = os.path.join(READINGS, "erasure-ledger.jsonl")
OUT = os.path.join(READINGS, "erasure-observatory-latest.json")
HIST = os.path.join(READINGS, "erasure-observatory-history.jsonl")

# Bumped when the METHOD changes in a way a reader must see, even if the numbers
# do not move. Write-if-changed compares readings, so without this a methodology
# correction that leaves the values identical never reaches the published file and
# the site keeps asserting a method it no longer uses.
#
# 2 (2026-07-31): every input is now bounded for freshness, not just the two that got
# caught — the narrative layer and both cross-checks previously published with no bound
# and no as_of at all. Values are unchanged today because everything is currently fresh,
# which is precisely the case this counter exists for: without the bump the site would
# keep serving a reading that does not carry the freshness fields it now guarantees.
# 3 (2026-08-05): a null narrative reading is also a dated claim. The composite renders
# it STALE or UNAVAILABLE with its reading path and age, rather than leaving it ARMED.
# 4 (2026-08-05): operational collector state is carried into the narrative layer and
# its history state, so disabled, halted, and observed are distinct movements.
METHOD_VERSION = 4


# The Generative Firewall reading refreshes daily (workflow: gfi-refresh.yml) and lands
# in readings/latest.json. A week without a fresh one is a real measurement gap, not a
# hiccup — past that we abstain rather than republish an old number as today's value.
GF_STALE_AFTER_DAYS = 7
# The network layer had no freshness bound at all — the same bug the model layer had, one
# layer over, waiting for OONI's refresh to fail the way CDT's feeds did. A freshness rule
# that guards one layer is a fix; one that guards every layer is the fix.
#
# The bound is NOT a new number. processors/vantage_fusion.py already bounds this exact OONI
# reading at MAX_AGE_HOURS["ooni"] = 36.0 and excludes it from the fusion past that. Two
# places in one repo deciding independently when the same reading goes stale is how the
# definitions drift, so this imports that one rather than picking a second. If the OONI
# cadence changes, it changes in one file.
try:
    from processors.vantage_fusion import MAX_AGE_HOURS as _FUSION_MAX_AGE_HOURS
except Exception:  # noqa: BLE001 — this script must run standalone from a bare checkout
    _FUSION_MAX_AGE_HOURS = {"ooni": 36.0, "censored_planet": 96.0, "net4people": 48.0}
NETWORK_STALE_AFTER_HOURS = float(_FUSION_MAX_AGE_HOURS.get("ooni", 36.0))

# EVERY input this runner reads gets a bound, not just the two that got caught.
# The model layer earned its bound by publishing a 29-day-old number; the network layer had
# none until it was noticed sitting beside it. The narrative layer and BOTH cross-checks
# still had none after that — the same instance-not-class failure, twice more, in the file
# that had just been fixed for it. So the bound now comes from a table with an entry per
# input, and _bounded_reading() below is the single path all of them go through: adding a
# fourth input without an entry is a KeyError at import, not a silent unbounded publish.
#
# censored_planet and net4people were already bounded in vantage_fusion's table and this
# runner simply was not consulting it. Baike's retained reading is checked on the same
# six-hourly composite cadence; a disabled collector does not refresh its timestamp.
INPUT_MAX_AGE_HOURS = {
    "ooni-gfw-latest.json": NETWORK_STALE_AFTER_HOURS,
    "baike-redaction-latest.json": 36.0,
    "censored-planet-latest.json": float(_FUSION_MAX_AGE_HOURS.get("censored_planet", 96.0)),
    "net4people-latest.json": float(_FUSION_MAX_AGE_HOURS.get("net4people", 48.0)),
}
# Candidate GF readings, live file first. The dated
# readings/<date>_generative-firewall-index.json files are FROZEN one-off publications
# kept only as an explicit fallback — they are never "now".
GF_LIVE_SOURCES = ("latest.json", "generative-firewall-index.json")


def _load(name: str) -> dict | None:
    path = os.path.join(READINGS, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def _gf_index(gf: dict | None) -> float | None:
    """The published model-erasure index, whatever the reading calls it.

    The live daily reading publishes `summary.gfi`; the frozen 2026-07-01 reading
    published `summary.generative_firewall_index`. Anything else — missing key, null,
    a string — is unreadable, and unreadable means abstain, never a stand-in number.
    """
    summary = (gf or {}).get("summary") or {}
    for key in ("gfi", "generative_firewall_index"):
        v = summary.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _gf_as_of(gf: dict | None) -> str | None:
    """When the GF reading was actually taken — its own claim, not the file's mtime."""
    summary = (gf or {}).get("summary") or {}
    for key in ("generated_at", "date"):
        v = summary.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _age_days(as_of: str | None, now: datetime) -> float | None:
    """Age of a reading in days, or None when it carries no usable timestamp.
    An undatable reading cannot be shown to be current, so it is treated as unusable."""
    if not as_of:
        return None
    try:
        ts = datetime.fromisoformat(as_of)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


def _reading_as_of(doc: dict | None) -> str | None:
    """When a reading claims it was taken. Top-level first, then inside `summary` — the
    two shapes this repo's readings actually use. The file's mtime is never consulted: a
    republished file is not a fresh measurement, and that distinction is the whole point."""
    for holder in (doc or {}, (doc or {}).get("summary") or {}):
        if not isinstance(holder, dict):
            continue
        for key in ("generated_at", "as_of", "date"):
            v = holder.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _bounded_reading(name: str, now: datetime) -> tuple[dict | None, str | None, float | None, bool]:
    """Load a reading and judge its freshness against its declared bound.

    Returns (doc, as_of, age_hours, fresh). `fresh` is True only when the reading exists,
    carries a usable timestamp, AND is inside its bound — so an undatable reading is never
    treated as current, and neither is a missing one.

    THE POINT OF THE KeyError. INPUT_MAX_AGE_HOURS[name] is a hard lookup on purpose. Every
    freshness bug in this file so far has been an input that nobody remembered to bound, so
    the failure mode for a NEW input must be a loud crash at the moment it is added, not a
    number quietly published as today's."""
    bound = INPUT_MAX_AGE_HOURS[name]
    doc = _load(name)
    as_of = _reading_as_of(doc)
    age_days = _age_days(as_of, now)
    age_hours = age_days * 24.0 if age_days is not None else None
    fresh = bool(doc) and age_hours is not None and age_hours <= bound
    return doc, as_of, age_hours, fresh


def _stale_layer(layer: str, title: str, detail: str, *, reading: str, value: float,
                 as_of: str | None, age_hours: float | None, bound_hours: float) -> dict:
    """A layer we can read but cannot show to be current. Withheld, with its true as-of
    date and the number we are declining to publish, so the abstention is auditable rather
    than a silent gap. Same shape the model layer already uses — one idiom, not two.

    Ages are stated in whichever unit reads honestly: a reading six hours past a 36-hour
    bound is not "0.3 days old"."""
    def _pretty(h: float) -> str:
        return f"{h:.1f} hours" if h < 48 else f"{h / 24:.1f} days"

    if age_hours is None:
        status = "UNDATED"
        reason = (f"the newest {layer} reading ({reading}) carries no usable timestamp, so it "
                  f"cannot be shown to be current — withheld rather than published as live")
    else:
        status = "STALE"
        reason = (f"the newest {layer} reading ({reading}) is {_pretty(age_hours)} old "
                  f"(as of {as_of}) — beyond the {_pretty(bound_hours)} freshness bound, "
                  f"so it is withheld rather than published as live")
    return {"layer": layer, "title": title, "value": None, "status": status,
            "detail": detail, "reason": reason, "reading": reading,
            "as_of": as_of, "withheld_value": round(value, 1)}


def _load_generative_firewall() -> tuple[dict | None, str | None]:
    """The LIVE GF reading, with the dated publications as an explicit fallback.

    The daily instrument writes readings/latest.json; the dated
    <date>_generative-firewall-index.json files are frozen one-off publications. We
    read the live file first and only fall back to a dated one — reading them the
    other way round is how a month-old index gets republished as today's model layer.
    """
    dated = sorted(glob.glob(os.path.join(READINGS, "*_generative-firewall-index.json")))
    order = list(GF_LIVE_SOURCES) + [os.path.basename(p) for p in reversed(dated)]
    for name in order:
        gf = _load(name)
        if gf and _gf_index(gf) is not None:
            return gf, name
    return None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    layers: list[dict] = []
    cross_checks: list[dict] = []
    withheld_cross: list[dict] = []
    sealed: list[dict] = []

    # ---- NETWORK erasure: OONI Great Firewall index (0-100) --------------
    ooni = _load("ooni-gfw-latest.json")
    ooni_detail = "reachability that disappeared — sites, messengers and tools blocked at the wire"
    ooni_index = ooni.get("gfw_index") if ooni else None
    ooni_ok = isinstance(ooni_index, (int, float)) and not isinstance(ooni_index, bool)
    ooni_as_of = _reading_as_of(ooni)
    ooni_age = _age_days(ooni_as_of, now)
    if ooni_ok:
        # seal what OONI last recorded whether or not we publish it — the ledger is a record
        # of what we saw, and withholding a stale value from the index is not a reason to
        # leave the observation out of the chain.
        e = sealed_ledger.append_seal(LEDGER, "ooni-gfw", ooni, now=now)
        if e:
            sealed.append({"source": "ooni-gfw", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    ooni_age_hours = ooni_age * 24.0 if ooni_age is not None else None
    if ooni_ok and ooni_age_hours is not None and ooni_age_hours <= NETWORK_STALE_AFTER_HOURS:
        layers.append({
            "layer": "network",
            "title": "Network erasure",
            "detail": ooni_detail,
            "value": round(float(ooni_index), 1),
            "source": "OONI aggregation (probe_cc=CN)",
            "reading": "ooni-gfw-latest.json",
            "as_of": ooni_as_of,
            "age_days": round(ooni_age, 2),
            "age_hours": round(ooni_age_hours, 1),
        })
    elif ooni_ok:
        layers.append(_stale_layer(
            "network", "Network erasure", ooni_detail, reading="ooni-gfw-latest.json",
            value=float(ooni_index), as_of=ooni_as_of, age_hours=ooni_age_hours,
            bound_hours=NETWORK_STALE_AFTER_HOURS))
    else:
        layers.append({"layer": "network", "title": "Network erasure",
                       "value": None, "status": "ABSENT",
                       "detail": ooni_detail,
                       "reason": "ooni-gfw-latest.json missing or malformed"})

    # ---- MODEL erasure: Generative Firewall index (0-100) ----------------
    # Read the live daily reading, and publish it only while it is actually current.
    # A reading we cannot date, or one older than GF_STALE_AFTER_DAYS, is reported as
    # withheld with its true as-of date — never carried forward as if it were today's.
    gf, gf_name = _load_generative_firewall()
    gf_index = _gf_index(gf)
    gf_as_of = _gf_as_of(gf)
    gf_age = _age_days(gf_as_of, now)
    gf_detail = "answers a state-aligned model will no longer give — refusals and party-line substitution"
    if gf:
        # seal whatever the model instrument last recorded (idempotent on an unchanged payload)
        e = sealed_ledger.append_seal(LEDGER, "generative-firewall", gf, now=now)
        if e:
            sealed.append({"source": "generative-firewall", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    if gf_index is not None and gf_age is not None and gf_age <= GF_STALE_AFTER_DAYS:
        layers.append({
            "layer": "model",
            "title": "Model erasure",
            "detail": gf_detail,
            "value": round(gf_index, 1),
            "source": "Generative Firewall (aligned LLM panel)",
            "reading": gf_name,
            "as_of": gf_as_of,
            "age_days": round(gf_age, 2),
        })
    elif gf_index is not None:
        # readable but not current — abstain loudly, and say exactly how old it is
        if gf_age is None:
            status = "UNDATED"
            reason = (f"the newest generative-firewall reading ({gf_name}) carries no usable "
                      f"timestamp, so it cannot be shown to be current — withheld rather than "
                      f"published as live")
        else:
            status = "STALE"
            reason = (f"the newest generative-firewall reading ({gf_name}) is {gf_age:.1f} days old "
                      f"(as of {gf_as_of}) — beyond the {GF_STALE_AFTER_DAYS}-day freshness bound, "
                      f"so it is withheld rather than published as live")
        layers.append({"layer": "model", "title": "Model erasure",
                       "value": None, "status": status,
                       "detail": gf_detail,
                       "reason": reason,
                       "reading": gf_name,
                       "as_of": gf_as_of,
                       "withheld_value": round(gf_index, 1)})
    else:
        layers.append({"layer": "model", "title": "Model erasure",
                       "value": None, "status": "ABSENT",
                       "detail": gf_detail,
                       "reason": "no generative-firewall reading found (readings/latest.json "
                                 "missing, malformed, or carrying no index)"})

    # ---- NARRATIVE erasure: Baike redaction-diff (0-100) -----------------
    # Baike acquisition is disabled. The retained observation remains visible with its
    # original timestamp, while collector_status says what the current pipeline can do.
    baike, b_as_of, b_age_hours, b_fresh = _bounded_reading("baike-redaction-latest.json", now)
    b_index = (baike or {}).get("rewrite_index")
    b_collector_status = (baike or {}).get("collector_status")
    b_collector_reason = (baike or {}).get("collector_reason")
    b_pipeline_checked_at = (baike or {}).get("pipeline_checked_at")
    # Seal whatever the narrative instrument recorded — a real index OR an honest abstain
    # (both are timestamped provenance of what we saw and tried).
    if baike:
        e = sealed_ledger.append_seal(LEDGER, "baike-redaction", baike, now=now)
        if e:
            sealed.append({"source": "baike-redaction", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    b_readable = isinstance(b_index, (int, float)) and not isinstance(b_index, bool)
    b_series_valid = (baike or {}).get("valid_for_series") is not False
    b_collector_observed = b_collector_status in (None, "observed")
    if b_readable and b_fresh and b_series_valid and b_collector_observed:
        layers.append({
            "layer": "narrative",
            "title": "Narrative erasure",
            "detail": "encyclopedia entries rewritten — sensitive terms excised, biographies truncated, sourcing collapsed to state media",
            "value": round(float(b_index), 1),
            "source": "Baike redaction-diff vs Chinese Wikipedia",
            "reading": "baike-redaction-latest.json",
            "as_of": b_as_of,
            "age_days": round(b_age_hours / 24.0, 2),
            "age_hours": round(b_age_hours, 1),
        })
    elif b_readable and b_series_valid and b_collector_observed:
        # Readable but not current. This branch had never existed: a Baike index that went
        # stale would have published as today's number, which is precisely the bug the model
        # layer was fixed for — sitting unfixed two layers down in the same function.
        layers.append(_stale_layer(
            "narrative", "Narrative erasure",
            "encyclopedia entries rewritten to the state line — sensitive terms excised, "
            "sourcing collapsed to state media",
            reading="baike-redaction-latest.json", value=float(b_index),
            as_of=b_as_of, age_hours=b_age_hours,
            bound_hours=INPUT_MAX_AGE_HOURS["baike-redaction-latest.json"]))
    elif b_readable:
        # A retained number can be inspectable without remaining publishable. This catches
        # quarantined method output and a collector that was disabled after its last look.
        status = "QUARANTINED" if not b_series_valid else "UNAVAILABLE"
        source_reason = (b_collector_reason
                         or (baike or {}).get("reason")
                         or "Baike collection disabled pending authorized access")
        layers.append({
            "layer": "narrative",
            "title": "Narrative erasure",
            "value": None,
            "status": status,
            "detail": ("encyclopedia entries rewritten to the state line; sensitive terms "
                       "excised and sourcing collapsed to state media"),
            "reason": source_reason,
            "reading": "baike-redaction-latest.json",
            "as_of": b_as_of,
            "withheld_value": round(float(b_index), 1),
        })
    else:
        # A null narrative reading is an abstention, not an armed-but-current layer. It may
        # represent a disabled instrument or insufficient comparable evidence; either way,
        # make its source path and age explicit so the composite cannot look healthy by omission.
        reading = "baike-redaction-latest.json"
        path = f"readings/{reading}"
        detail = ("encyclopedia entries rewritten to the state line — sensitive terms excised, "
                  "sourcing collapsed to state media")
        source_reason = ((baike or {}).get("collector_reason")
                         or (baike or {}).get("reason")
                         or "Baike collection disabled pending authorized access")
        if b_age_hours is None:
            status = "UNAVAILABLE"
            reason = (f"Baike narrative layer unavailable: {source_reason}. Source path {path} "
                      "has no usable observation timestamp, so no current value can be claimed")
            layer = {"layer": "narrative", "title": "Narrative erasure", "value": None,
                     "status": status, "detail": detail, "reason": reason, "reading": reading,
                     "as_of": b_as_of}
        elif b_age_hours > INPUT_MAX_AGE_HOURS[reading]:
            status = "STALE"
            age = f"{b_age_hours:.1f} hours" if b_age_hours < 48 else f"{b_age_hours / 24:.1f} days"
            reason = (f"Baike narrative layer unavailable: {source_reason}. Source path {path} "
                      f"is {age} old (as of {b_as_of}), beyond the "
                      f"{INPUT_MAX_AGE_HOURS[reading]:.1f}-hour freshness bound")
            layer = {"layer": "narrative", "title": "Narrative erasure", "value": None,
                     "status": status, "detail": detail, "reason": reason, "reading": reading,
                     "as_of": b_as_of, "age_hours": round(b_age_hours, 1)}
        else:
            status = "UNAVAILABLE"
            reason = (f"Baike narrative layer unavailable: {source_reason}. Source path {path} "
                      f"is current as of {b_as_of}, but it contains no reportable index")
            layer = {"layer": "narrative", "title": "Narrative erasure", "value": None,
                     "status": status, "detail": detail, "reason": reason, "reading": reading,
                     "as_of": b_as_of, "age_hours": round(b_age_hours, 1)}
        layers.append(layer)

    # Operational state is not measurement time. Carry it separately on every narrative
    # shape so JSON, the public dashboard, history, and MCP all tell the same story.
    narrative_layer = layers[-1]
    if b_collector_status:
        narrative_layer["collector_status"] = b_collector_status
    if b_collector_reason:
        narrative_layer["collector_reason"] = b_collector_reason
    if b_pipeline_checked_at:
        narrative_layer["pipeline_checked_at"] = b_pipeline_checked_at

    # ---- cross-checks (different scales, not folded into the composite) ---
    # A cross-check is a corroborating number a reader weighs against the layers, so a stale
    # one is a stale claim — it had no bound at all. Both are dropped rather than shown when
    # they cannot be dated or are past their bound, and the drop is stated so the absence
    # reads as an abstention rather than as the check having been forgotten.
    cp, cp_as_of, cp_age, cp_fresh = _bounded_reading("censored-planet-latest.json", now)
    cp_val = (cp or {}).get("cn_interference_rate_pct")
    if isinstance(cp_val, (int, float)) and not isinstance(cp_val, bool):
        if cp_fresh:
            cross_checks.append({"name": "Censored Planet interference", "value": cp_val,
                                 "unit": "%", "as_of": cp_as_of, "age_hours": round(cp_age, 1),
                                 "note": "independent DNS/HTTP side-channel, confirms network layer"})
        else:
            withheld_cross.append({"name": "Censored Planet interference", "as_of": cp_as_of,
                                   "withheld_value": cp_val, "status": "STALE" if cp_age else "UNDATED"})
        e = sealed_ledger.append_seal(LEDGER, "censored-planet", cp, now=now)
        if e:
            sealed.append({"source": "censored-planet", "seq": e["seq"], "entry_hash": e["entry_hash"]})
    n4p, n4p_as_of, n4p_age, n4p_fresh = _bounded_reading("net4people-latest.json", now)
    n4p_val = (n4p or {}).get("velocity")
    if isinstance(n4p_val, (int, float)) and not isinstance(n4p_val, bool):
        if n4p_fresh:
            cross_checks.append({"name": "Firewall event velocity", "value": n4p_val,
                                 "unit": "x", "as_of": n4p_as_of, "age_hours": round(n4p_age, 1),
                                 "note": "community-logged blocking/circumvention rate vs baseline"})
        else:
            withheld_cross.append({"name": "Firewall event velocity", "as_of": n4p_as_of,
                                   "withheld_value": n4p_val, "status": "STALE" if n4p_age else "UNDATED"})

    # ---- composite: mean of the layers that actually reported ------------
    contributing = [l for l in layers if isinstance(l.get("value"), (int, float))]
    if contributing:
        composite = round(sum(l["value"] for l in contributing) / len(contributing), 1)
    else:
        composite = None

    ledger_summary = sealed_ledger.summary(LEDGER)

    out = {
        "generated_at": now.isoformat(),
        "method_version": METHOD_VERSION,
        "title": "Information Erasure Observatory",
        "thesis": ("The field measures access in the present tense. We measure erasure across "
                   "time — what was removed or rewritten — on three layers, and we seal our own "
                   "record so it cannot be quietly rewritten in turn."),
        "erasure_index": composite,
        "index_scale": "0-100; higher = more of the observed record removed or rewritten",
        "layers_contributing": [l["layer"] for l in contributing],
        "layers": layers,
        "cross_checks": cross_checks,
        # Cross-checks withheld for staleness. An empty list means every check was fresh;
        # a silent absence would read as "we never had that check", which is a different
        # and much more flattering claim than "we had it and it went stale".
        "cross_checks_withheld": withheld_cross,
        "integrity": {
            "ledger": "readings/erasure-ledger.jsonl",
            "entries": ledger_summary["entries"],
            "verified": ledger_summary["verified"],
            "merkle_root": ledger_summary["merkle_root"],
            "head_hash": ledger_summary["head_hash"],
            "first_ts": ledger_summary["first_ts"],
            "head_ts": ledger_summary["head_ts"],
            "by_source": ledger_summary["by_source"],
            "sealed_this_run": sealed,
            "verify_cmd": "python3 scripts/verify_ledger.py",
            "note": ("every source reading above is hash-chained into the ledger at capture time; "
                     "recompute the chain yourself to prove no past reading was altered or dropped"),
        },
        "vs_access_indices": ("Access observatories answer 'is X reachable now'. This answers "
                              "'what did the record lose, and can you prove our measurement of the "
                              "loss was not itself edited afterward'."),
    }

    # Did the answer move? (composite, layer values, or ledger head). This is no
    # longer the gate on writing the reading — it is the gate on the history file
    # and on last_changed_at below.
    prev = _load("erasure-observatory-latest.json") or {}
    def layer_state(layer):
        return (layer.get("layer"), layer.get("value"), layer.get("status"),
                layer.get("collector_status"))

    prev_vals = [layer_state(l) for l in prev.get("layers", [])]
    cur_vals = [layer_state(l) for l in layers]
    changed = (prev.get("erasure_index") != composite
               or prev_vals != cur_vals
               or (prev.get("integrity") or {}).get("head_hash") != ledger_summary["head_hash"])
    # A method correction must reach the published file even when every value is
    # identical, or the site keeps asserting a method it no longer uses.
    changed = changed or prev.get("method_version") != METHOD_VERSION

    # "When did we last look" and "when did the answer last move" are different
    # questions, and a reader has to be able to tell them apart. Write-if-changed
    # answers only the second, so a composite that holds still — which is exactly
    # what a settled erasure regime looks like — stopped refreshing generated_at,
    # and the front page labelled its own healthy hero signal stale past the
    # 12-hour cadence mark. Silence from a censor and silence from a dead runner
    # are not the same claim. So every round publishes its own observation time
    # and last_changed_at carries the movement. Nothing else about the round is
    # invented: an absent layer is still ABSENT, a stale one still withheld, and
    # a round where no layer reported still republishes a null index rather than
    # a number. The history file stays gated on change, so the movement record
    # never fills with heartbeats.
    out["last_changed_at"] = (
        out["generated_at"] if (changed or not prev)
        else (prev.get("last_changed_at") or prev.get("generated_at")))

    os.makedirs(READINGS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if changed or not prev:
        with open(HIST, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "generated_at": out["generated_at"],
                "erasure_index": composite,
                "layers": {l["layer"]: l.get("value") for l in layers},
                "layer_status": {l["layer"]: l.get("status", "REPORTING") for l in layers},
                "collector_status": {
                    l["layer"]: l.get("collector_status") for l in layers
                    if l.get("collector_status")
                },
                "ledger_entries": ledger_summary["entries"],
                "merkle_root": ledger_summary["merkle_root"],
            }, ensure_ascii=False) + "\n")

    idx = f"{composite}" if composite is not None else "n/a (no layer reported)"
    print(f"=== Information Erasure Observatory — index {idx} "
          f"({len(contributing)}/{len(layers)} layers) ===")
    for l in layers:
        v = l.get("value")
        v = f"{v:>5}" if isinstance(v, (int, float)) else f"{l.get('status','ABSENT'):>5}"
        print(f"  {l['layer']:<10} {v}  {l['title']}")
    print(f"  ledger: {ledger_summary['entries']} entries, verified={ledger_summary['verified']}, "
          f"root={ledger_summary['merkle_root'][:16]}…")
    if prev and not changed:
        # Worth saying out loud in the run log, so an operator reading it knows the
        # file was rewritten on purpose and not because something moved.
        print(f"  unchanged since {out['last_changed_at']} — republished with this "
              f"round's observation time, history untouched")
    if not ledger_summary["verified"]:
        print("  !! LEDGER INTEGRITY BROKEN:")
        for p in ledger_summary["problems"]:
            print(f"     - {p}")


if __name__ == "__main__":
    main()
