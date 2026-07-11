#!/usr/bin/env python3
"""PALIMPSEST WITNESS — an independent observer of the published sealed chains.

Runs on infrastructure separate from the publish pipeline (the Hetzner box),
fetches the chains the world sees at palimpsest.info, and holds Palimpsest to
its own guarantee. It is deliberately a SEPARATE IMPLEMENTATION: it shares no
code with the repository it watches, so a bug or a backdoor in the publisher
cannot also blind the witness.

Each run it:
  1. fetches readings/eval-registry.jsonl and readings/erasure-ledger.jsonl
  2. re-verifies both hash chains from scratch (including the eval registry's
     pre-registration rule)
  3. checks PREFIX CONSISTENCY against every observation in its local log:
     the chain as served today must still contain, unchanged, the exact head
     this witness recorded on every earlier day. A rewrite, reorder, or
     truncation of history breaks that and raises an alert. This is the
     split-view / retroactive-rewrite detector.
  4. appends today's observation (length, head, root) to its own append-only
     witness log

Alerts print to stdout/stderr and, when TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are set, go to Telegram. Exit codes: 0 = consistent,
2 = ALERT (verification failure or history rewrite), 3 = could not fetch.

Pure stdlib. State lives in ~/.palimpsest-witness/ by default
(PALIMPSEST_WITNESS_DIR overrides).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SITE = os.environ.get("PALIMPSEST_SITE", "https://palimpsest.info")
CHAINS = {
    "eval-registry": f"{SITE}/readings/eval-registry.jsonl",
    "erasure-ledger": f"{SITE}/readings/erasure-ledger.jsonl",
}
STATE_DIR = os.environ.get(
    "PALIMPSEST_WITNESS_DIR",
    os.path.join(os.path.expanduser("~"), ".palimpsest-witness"))
GENESIS = "0" * 64
UA = "palimpsest-witness/1.0 (independent chain observer)"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def fetch_chain(url: str, opener=urllib.request.urlopen) -> list[dict]:
    req = urllib.request.Request(f"{url}?witness={int(datetime.now().timestamp())}",
                                 headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    with opener(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def verify_erasure(entries: list[dict]) -> list[str]:
    """Independent re-implementation of the erasure ledger rules."""
    problems, prev = [], GENESIS
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: broken prev link")
            recomputed = _sha256(_canonical({
                "seq": e["seq"], "ts": e["ts"], "source": e["source"],
                "payload_sha256": e["payload_sha256"], "prev_hash": e["prev_hash"]}))
            if recomputed != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed ({exc})")
            prev = e.get("entry_hash", prev)
    return problems


def verify_registry(entries: list[dict]) -> list[str]:
    """Independent re-implementation of the eval registry rules, including
    the pre-registration constraint (no answers before frozen questions)."""
    problems, prev, registered = [], GENESIS, set()
    for i, e in enumerate(entries):
        try:
            if e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: non-contiguous")
            if e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: broken prev link")
            core = {k: v for k, v in e.items() if k != "entry_hash"}
            if _sha256(_canonical(core)) != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute")
            if e["kind"] == "preregistration":
                registered.add(e["probe_set_hash"])
            elif e["kind"] == "run":
                if e["probe_set_hash"] not in registered:
                    problems.append(f"seq {e.get('seq')}: run probe set never pre-registered")
            else:
                problems.append(f"seq {e.get('seq')}: unknown kind {e.get('kind')!r}")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed ({exc})")
            prev = e.get("entry_hash", prev)
    return problems


def merkle_root(entries: list[dict]) -> str:
    if not entries:
        return GENESIS
    level = [e["entry_hash"] for e in entries]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_sha256((level[i] + level[i + 1]).encode("utf-8"))
                 for i in range(0, len(level), 2)]
    return level[0]


def load_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def prefix_alerts(chain: str, entries: list[dict], observations: list[dict]) -> list[str]:
    """The core witness property: everything this witness saw before must still
    be there, byte-identical. For each past observation of n entries with head
    h, today's chain must be at least n long and its entry n-1 must hash to h."""
    alerts = []
    for obs in observations:
        n, h = obs["n"], obs["head"]
        if len(entries) < n:
            alerts.append(f"{chain}: HISTORY SHRANK — witnessed {n} entries on "
                          f"{obs['ts'][:10]}, now only {len(entries)}")
        elif n > 0 and entries[n - 1].get("entry_hash") != h:
            alerts.append(f"{chain}: HISTORY REWRITTEN — entry {n - 1} no longer matches "
                          f"the head witnessed on {obs['ts'][:10]} "
                          f"({h[:16]}… -> {entries[n - 1].get('entry_hash', '?')[:16]}…)")
    return alerts


def telegram(msg: str, opener=urllib.request.urlopen) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        opener(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30)
    except Exception as exc:  # noqa: BLE001 — an alert about the alert, not a crash
        print(f"telegram alert failed: {exc}", file=sys.stderr)


def main(opener=urllib.request.urlopen) -> int:
    os.makedirs(STATE_DIR, exist_ok=True)
    verifiers = {"eval-registry": verify_registry, "erasure-ledger": verify_erasure}
    all_alerts, fetched_any = [], False

    for chain, url in CHAINS.items():
        log_path = os.path.join(STATE_DIR, f"{chain}.witness.jsonl")
        try:
            entries = fetch_chain(url, opener=opener)
        except Exception as exc:  # noqa: BLE001
            print(f"{chain}: FETCH FAILED ({exc}) — cannot witness this run", file=sys.stderr)
            continue
        fetched_any = True

        problems = verifiers[chain](entries)
        observations = load_log(log_path)
        alerts = ([f"{chain}: {p}" for p in problems]
                  + prefix_alerts(chain, entries, observations))
        all_alerts.extend(alerts)

        obs = {"ts": datetime.now(timezone.utc).isoformat(), "n": len(entries),
               "head": entries[-1]["entry_hash"] if entries else GENESIS,
               "root": merkle_root(entries), "alerts": len(alerts)}
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
        print(f"{chain}: {len(entries)} entries, root {obs['root'][:16]}…, "
              + ("CONSISTENT with all "
                 f"{len(observations)} prior observations" if not alerts else "ALERTS BELOW"))

    if not fetched_any:
        return 3
    if all_alerts:
        body = "PALIMPSEST WITNESS ALERT\n" + "\n".join(all_alerts)
        print(body, file=sys.stderr)
        telegram(body, opener=opener)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
