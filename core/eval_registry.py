"""VERIFIABLE EVAL REGISTRY — tamper-evident, pre-registered AI model evaluations.

> AI evaluation has a trust problem: labs grade their own homework, and an eval can be
> quietly re-run, cherry-picked, or revised after the fact until the number looks right.
> This registry makes that impossible to hide. You FREEZE the probe set first (a
> pre-registration sealed into a hash chain), and only then submit the run. Anyone can
> recompute the chain and prove (a) the questions were fixed before the answers existed,
> and (b) no past result was altered, reordered, or dropped.

It generalizes `core.sealed_ledger` from "Palimpsest's own erasure readings" to "any
model evaluation, by anyone". The unit is an *attestation*, of two kinds:

  preregistration  — freezes a probe set. Seals `probe_set_hash` = sha256 of the
                     canonicalized, sorted probe list, before the model is ever queried.
  run              — a result. Seals the model id, the number of probes, a
                     `responses_hash` over the full results, and a small metrics dict.
                     It MUST reference a `probe_set_hash` that was pre-registered EARLIER
                     in the chain. A run whose questions were never frozen first fails
                     verification. That is the anti-p-hacking property.

Why this matters for AI safety (the reason it exists beyond censorship): as models mediate
more of what people can know, third parties need to audit them and PROVE the audit was not
gamed. A shared, append-only, independently verifiable substrate for evals is governance
infrastructure — "evals you can prove weren't rewritten after the fact." Palimpsest's own
model-erasure readings are simply the first thing anchored into it; the registry is model-
and topic-agnostic (any frontier model, any probe suite, Chinese or Western).

Pure stdlib, offline-verifiable, no keys, no chain except the hashes themselves. Reuses the
canonicalization and Merkle machinery of `core.sealed_ledger` so the two records verify the
same way.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.sealed_ledger import (
    GENESIS_PREV,
    _canonical,
    _sha256,
    atomic_replace_bytes,
    ledger_lock,
    merkle_root,
    payload_digest,
    read_ledger,
    read_ledger_snapshot,
)

PREREGISTRATION = "preregistration"
RUN = "run"
DIGEST_RECEIPT_COMMITMENT = "exact_canonical_receipt_v1"
MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA = "palimpsest.myquant-eval-preregistration.v1"
MYQUANT_RUN_RECEIPT_SCHEMA = "palimpsest.myquant-eval-run.v1"
MYQUANT_DIGEST_SUITE = "myquant-digest-evaluation-v1"

REGISTRY_TITLE = "Verifiable Eval Registry"
REGISTRY_WHAT = (
    "tamper-evident, pre-registered AI model evaluations — the questions are "
    "frozen before the model is queried, and every result is hash-chained so "
    "it cannot be quietly revised. Any model, any suite; this is the record."
)
REGISTRY_PUBLIC_PATH = "readings/eval-registry.jsonl"
REGISTRY_VERIFY_COMMAND = "python3 scripts/verify_eval_registry.py"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_PREREGISTRATION_KEYS = frozenset(
    {
        "seq",
        "prev_hash",
        "ts",
        "kind",
        "probe_set_hash",
        "n_probes",
        "suite",
        "note",
        "commitment",
        "receipt_schema",
        "preregistration_receipt_sha256",
        "evaluation_id",
        "model_artifact_sha256",
        "evaluation_protocol_sha256",
        "preregistration_issued_at",
        "entry_hash",
    }
)
_DIGEST_RUN_KEYS = frozenset(
    {
        "seq",
        "prev_hash",
        "ts",
        "kind",
        "probe_set_hash",
        "model",
        "model_artifact_sha256",
        "responses_hash",
        "metrics",
        "suite",
        "commitment",
        "receipt_schema",
        "result_receipt_sha256",
        "result_artifact_sha256",
        "preregistration_receipt_sha256",
        "evaluation_protocol_sha256",
        "evaluation_id",
        "run_id",
        "run_started_at",
        "run_completed_at",
        "entry_hash",
    }
)
_RESERVED_DIGEST_FIELDS = frozenset(
    {
        "commitment",
        "receipt_schema",
        "preregistration_receipt_sha256",
        "evaluation_id",
        "model_artifact_sha256",
        "evaluation_protocol_sha256",
        "preregistration_issued_at",
        "result_receipt_sha256",
        "result_artifact_sha256",
        "run_id",
        "run_started_at",
        "run_completed_at",
    }
)


def _uses_reserved_digest_namespace(entry: dict) -> bool:
    receipt_schema = entry.get("receipt_schema")
    return (
        entry.get("suite") == MYQUANT_DIGEST_SUITE
        or any(field in entry for field in _RESERVED_DIGEST_FIELDS)
        or (
            type(receipt_schema) is str
            and receipt_schema.startswith("palimpsest.myquant-eval-")
        )
    )


def probe_set_hash(probes) -> str:
    """sha256 of the canonicalized, de-duplicated, sorted probe set. Order-independent,
    so the same questions always freeze to the same hash regardless of listing order."""
    canon = sorted({str(p) for p in probes})
    return _sha256(_canonical(canon))


def responses_hash(responses) -> str:
    """sha256 of the full results object (probe -> response, or any run artifact). This is
    what a run commits to; publishing the raw responses alongside lets anyone recompute it."""
    return payload_digest(responses if isinstance(responses, dict) else {"_": responses})


def _entry_hash(core: dict) -> str:
    return payload_digest(core)


def registry_lock(
    path: str | Path,
    *,
    exclusive: bool = True,
    create: bool = True,
):
    """The one cross-process lock domain for registry writers and verifiers."""
    return ledger_lock(path, exclusive=exclusive, create=create)


def _verified_snapshot(path: str | Path) -> tuple[list[dict], bytes]:
    entries, raw = read_ledger_snapshot(path)
    ok, problems = verify(entries)
    if not ok:
        raise ValueError("refusing to extend a broken eval registry: " + "; ".join(problems))
    return entries, raw


def _append_locked(path: str | Path, core: dict) -> dict:
    entries, raw = _verified_snapshot(path)
    seq = len(entries)
    prev = entries[-1]["entry_hash"] if entries else GENESIS_PREV
    core = {"seq": seq, "prev_hash": prev, **core}
    entry = {**core, "entry_hash": _entry_hash(core)}
    encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False).encode("utf-8")
    atomic_replace_bytes(path, raw + encoded + b"\n")
    return entry


def _append(path: str, core: dict, *, _lock_held: bool = False) -> dict:
    if _lock_held:
        return _append_locked(path, core)
    with registry_lock(path):
        return _append_locked(path, core)


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase 64-character sha256")
    return value


def _parse_aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def _verified_entries(path: str) -> list[dict]:
    entries, _ = _verified_snapshot(path)
    return entries


def preregister_digest(
    path: str,
    *,
    probe_set_hash: str,
    n_probes: int,
    suite: str,
    preregistration_receipt_sha256: str,
    evaluation_id: str,
    model_artifact_sha256: str,
    evaluation_protocol_sha256: str,
    issued_at: str,
    receipt_schema: str = MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA,
    now: datetime | None = None,
    _lock_held: bool = False,
) -> dict:
    """Append a digest-only preregistration whose questions stay private.

    This is deliberately narrower than :func:`preregister`: the caller supplies an
    already-computed probe-set commitment and an exact canonical receipt commitment,
    but no prompt, label, path, URL, or free-form metadata can enter the registry.
    Duplicate evaluation ids and receipt digests are rejected here; an importer may
    recognize an *exact* existing entry as an idempotent retry before calling us.
    """
    _require_sha256(probe_set_hash, "probe_set_hash")
    _require_sha256(preregistration_receipt_sha256, "preregistration_receipt_sha256")
    _require_sha256(evaluation_id, "evaluation_id")
    _require_sha256(model_artifact_sha256, "model_artifact_sha256")
    _require_sha256(evaluation_protocol_sha256, "evaluation_protocol_sha256")
    if type(n_probes) is not int or n_probes <= 0:
        raise ValueError("n_probes must be a positive integer")
    if suite != MYQUANT_DIGEST_SUITE:
        raise ValueError(f"suite must be {MYQUANT_DIGEST_SUITE}")
    if receipt_schema != MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA:
        raise ValueError("unknown digest preregistration receipt schema")
    def append_once() -> dict:
        # For production calls this timestamp is sampled only after the writer lock
        # is held, so a process paused while waiting cannot backdate its registry slot.
        import_time = now or datetime.now(timezone.utc)
        if _parse_aware_timestamp(issued_at, "issued_at") > _parse_aware_timestamp(
            import_time.isoformat(), "now"
        ):
            raise ValueError("issued_at cannot be later than the registry append")

        entries = _verified_entries(path)
        for entry in entries:
            if entry.get("commitment") != DIGEST_RECEIPT_COMMITMENT:
                continue
            if entry.get("evaluation_id") == evaluation_id:
                raise ValueError("evaluation_id is already preregistered")
            if entry.get("preregistration_receipt_sha256") == preregistration_receipt_sha256:
                raise ValueError("preregistration receipt is already registered")

        return _append(path, {
            "ts": import_time.isoformat(),
            "kind": PREREGISTRATION,
            "probe_set_hash": probe_set_hash,
            "n_probes": n_probes,
            "suite": suite,
            "note": "",
            "commitment": DIGEST_RECEIPT_COMMITMENT,
            "receipt_schema": receipt_schema,
            "preregistration_receipt_sha256": preregistration_receipt_sha256,
            "evaluation_id": evaluation_id,
            "model_artifact_sha256": model_artifact_sha256,
            "evaluation_protocol_sha256": evaluation_protocol_sha256,
            "preregistration_issued_at": issued_at,
        }, _lock_held=True)

    if _lock_held:
        return append_once()
    with registry_lock(path):
        return append_once()


def submit_receipt_run(
    path: str,
    *,
    probe_set_hash: str,
    model_artifact_sha256: str,
    suite: str,
    result_receipt_sha256: str,
    result_artifact_sha256: str,
    preregistration_receipt_sha256: str,
    evaluation_protocol_sha256: str,
    evaluation_id: str,
    run_id: str,
    run_started_at: str,
    run_completed_at: str,
    receipt_schema: str = MYQUANT_RUN_RECEIPT_SCHEMA,
    now: datetime | None = None,
    _lock_held: bool = False,
) -> dict:
    """Append one run that commits to the exact canonical result receipt.

    ``responses_hash`` intentionally equals ``result_receipt_sha256``.  The private
    result artifact may have its own digest for provenance, but that digest is never
    substituted for the public receipt commitment.  A second result for an evaluation,
    run id, receipt, or result artifact is rejected to make replay/cherry-picking loud.
    """
    for field, value in (
        ("probe_set_hash", probe_set_hash),
        ("model_artifact_sha256", model_artifact_sha256),
        ("result_receipt_sha256", result_receipt_sha256),
        ("result_artifact_sha256", result_artifact_sha256),
        ("preregistration_receipt_sha256", preregistration_receipt_sha256),
        ("evaluation_protocol_sha256", evaluation_protocol_sha256),
        ("evaluation_id", evaluation_id),
        ("run_id", run_id),
    ):
        _require_sha256(value, field)
    if suite != MYQUANT_DIGEST_SUITE:
        raise ValueError(f"suite must be {MYQUANT_DIGEST_SUITE}")
    if receipt_schema != MYQUANT_RUN_RECEIPT_SCHEMA:
        raise ValueError("unknown digest run receipt schema")
    started = _parse_aware_timestamp(run_started_at, "run_started_at")
    completed = _parse_aware_timestamp(run_completed_at, "run_completed_at")
    if started >= completed:
        raise ValueError("run_completed_at must be strictly later than run_started_at")

    def append_once() -> dict:
        import_time = now or datetime.now(timezone.utc)
        appended_at = _parse_aware_timestamp(import_time.isoformat(), "now")
        if completed > appended_at:
            raise ValueError("run_completed_at cannot be later than the registry append")

        entries = _verified_entries(path)
        preregistration = next(
            (
                entry
                for entry in entries
                if entry.get("kind") == PREREGISTRATION
                and entry.get("commitment") == DIGEST_RECEIPT_COMMITMENT
                and entry.get("preregistration_receipt_sha256")
                == preregistration_receipt_sha256
            ),
            None,
        )
        if preregistration is None:
            raise ValueError("result references a preregistration receipt not registered earlier")
        expected = {
            "probe_set_hash": probe_set_hash,
            "evaluation_id": evaluation_id,
            "model_artifact_sha256": model_artifact_sha256,
            "evaluation_protocol_sha256": evaluation_protocol_sha256,
            "suite": suite,
        }
        for field, value in expected.items():
            if preregistration.get(field) != value:
                raise ValueError(f"result {field} does not match its preregistration")
        preregistered_at = _parse_aware_timestamp(
            preregistration.get("ts"), "preregistration ts"
        )
        if preregistered_at >= started:
            raise ValueError("preregistration must reach the registry strictly before run start")

        for entry in entries:
            if entry.get("kind") != RUN or entry.get("commitment") != DIGEST_RECEIPT_COMMITMENT:
                continue
            if entry.get("evaluation_id") == evaluation_id:
                raise ValueError("evaluation already has a registered result")
            if entry.get("run_id") == run_id:
                raise ValueError("run_id is already registered")
            if entry.get("result_receipt_sha256") == result_receipt_sha256:
                raise ValueError("result receipt is already registered")
            if entry.get("result_artifact_sha256") == result_artifact_sha256:
                raise ValueError("result artifact is already registered under another run")

        return _append(path, {
            "ts": import_time.isoformat(),
            "kind": RUN,
            "probe_set_hash": probe_set_hash,
            "model": "sha256:" + model_artifact_sha256,
            "model_artifact_sha256": model_artifact_sha256,
            "responses_hash": result_receipt_sha256,
            "metrics": {},
            "suite": suite,
            "commitment": DIGEST_RECEIPT_COMMITMENT,
            "receipt_schema": receipt_schema,
            "result_receipt_sha256": result_receipt_sha256,
            "result_artifact_sha256": result_artifact_sha256,
            "preregistration_receipt_sha256": preregistration_receipt_sha256,
            "evaluation_protocol_sha256": evaluation_protocol_sha256,
            "evaluation_id": evaluation_id,
            "run_id": run_id,
            "run_started_at": run_started_at,
            "run_completed_at": run_completed_at,
        }, _lock_held=True)

    if _lock_held:
        return append_once()
    with registry_lock(path):
        return append_once()


def preregister(path: str, probes, *, suite: str = "", note: str = "",
                now: datetime | None = None) -> dict:
    """Freeze a probe set BEFORE running any model. Returns the sealed attestation
    (its `probe_set_hash` is what a later run must reference)."""
    ph = probe_set_hash(probes)
    with registry_lock(path):
        ts = (now or datetime.now(timezone.utc)).isoformat()
        return _append(path, {
            "ts": ts, "kind": PREREGISTRATION, "probe_set_hash": ph,
            "n_probes": len(sorted({str(p) for p in probes})),
            "suite": suite, "note": note,
        }, _lock_held=True)


def submit_run(path: str, *, probe_set_hash: str, model: str, responses,
               metrics: dict | None = None, suite: str = "", now: datetime | None = None) -> dict:
    """Record an evaluation run. The probe_set_hash MUST already be pre-registered in this
    registry (verify() enforces it). `responses` is hashed, not necessarily stored here —
    publish it alongside so anyone can recompute `responses_hash`."""
    digest = responses_hash(responses)
    with registry_lock(path):
        ts = (now or datetime.now(timezone.utc)).isoformat()
        return _append(path, {
            "ts": ts, "kind": RUN, "probe_set_hash": probe_set_hash, "model": model,
            "responses_hash": digest,
            "metrics": metrics or {}, "suite": suite,
        }, _lock_held=True)


def verify(entries: list[dict]) -> tuple[bool, list[str]]:
    """Recompute the chain and enforce the pre-registration rule. Reports EVERY break:
    non-contiguous seq, broken prev link, altered entry, or a RUN whose probe set was
    never frozen earlier (answers before the questions)."""
    problems: list[str] = []
    prev = GENESIS_PREV
    registered: set[str] = set()
    receipt_preregistrations: dict[str, dict] = {}
    receipt_evaluations: set[str] = set()
    result_evaluations: set[str] = set()
    receipt_runs: set[str] = set()
    result_receipts: set[str] = set()
    result_artifacts: set[str] = set()
    for i, e in enumerate(entries):
        if type(e) is not dict:
            problems.append(f"position {i}: malformed attestation (expected an object)")
            continue
        try:
            if type(e.get("seq")) is not int or e["seq"] != i:
                problems.append(f"seq {e.get('seq')} at position {i}: reordered / non-contiguous")
            if type(e.get("prev_hash")) is not str or e["prev_hash"] != prev:
                problems.append(f"seq {e.get('seq')}: prev_hash does not link to the previous entry")
            if type(e.get("entry_hash")) is not str:
                problems.append(f"seq {e.get('seq')}: entry_hash is not a string")
            if type(e.get("kind")) is not str:
                problems.append(f"seq {e.get('seq')}: kind is not a string")
            try:
                _parse_aware_timestamp(e.get("ts"), "ts")
            except ValueError as exc:
                problems.append(f"seq {e.get('seq')}: {exc}")
            core = {k: e[k] for k in e if k != "entry_hash"}
            if _entry_hash(core) != e["entry_hash"]:
                problems.append(f"seq {e.get('seq')}: entry_hash does not recompute — altered after sealing")
            if e["kind"] == PREREGISTRATION:
                registered.add(e["probe_set_hash"])
                commitment = e.get("commitment")
                reserved = _uses_reserved_digest_namespace(e)
                if reserved and commitment != DIGEST_RECEIPT_COMMITMENT:
                    problems.append(
                        f"seq {e.get('seq')}: reserved MyQuant fields require the exact "
                        "digest receipt commitment"
                    )
                if commitment is not None and commitment != DIGEST_RECEIPT_COMMITMENT:
                    problems.append(
                        f"seq {e.get('seq')}: unknown digest receipt commitment {commitment!r}"
                    )
                if commitment == DIGEST_RECEIPT_COMMITMENT:
                    if frozenset(e) != _DIGEST_PREREGISTRATION_KEYS:
                        problems.append(
                            f"seq {e.get('seq')}: digest preregistration fields do not match "
                            "the closed schema"
                        )
                    if e.get("receipt_schema") != MYQUANT_PREREGISTRATION_RECEIPT_SCHEMA:
                        problems.append(
                            f"seq {e.get('seq')}: unknown digest preregistration receipt schema"
                        )
                    if e.get("suite") != MYQUANT_DIGEST_SUITE:
                        problems.append(f"seq {e.get('seq')}: unknown digest evaluation suite")
                    if e.get("note") != "":
                        problems.append(
                            f"seq {e.get('seq')}: digest preregistration must not carry a note"
                        )
                    if type(e.get("n_probes")) is not int or e["n_probes"] <= 0:
                        problems.append(
                            f"seq {e.get('seq')}: digest preregistration probe count is invalid"
                        )
                    digest_fields = (
                        "probe_set_hash",
                        "preregistration_receipt_sha256",
                        "evaluation_id",
                        "model_artifact_sha256",
                        "evaluation_protocol_sha256",
                    )
                    for field in digest_fields:
                        if type(e.get(field)) is not str or not _SHA256.fullmatch(e[field]):
                            problems.append(
                                f"seq {e.get('seq')}: {field} is not a lowercase sha256"
                            )
                    try:
                        issued_at = _parse_aware_timestamp(
                            e.get("preregistration_issued_at"),
                            "preregistration_issued_at",
                        )
                        appended_at = _parse_aware_timestamp(e.get("ts"), "ts")
                        if issued_at > appended_at:
                            problems.append(
                                f"seq {e.get('seq')}: preregistration was issued after its "
                                "registry append"
                            )
                    except ValueError as exc:
                        problems.append(f"seq {e.get('seq')}: {exc}")
                    receipt = e.get("preregistration_receipt_sha256")
                    evaluation = e.get("evaluation_id")
                    if receipt in receipt_preregistrations:
                        problems.append(
                            f"seq {e.get('seq')}: duplicate digest preregistration receipt"
                        )
                    if evaluation in receipt_evaluations:
                        problems.append(f"seq {e.get('seq')}: duplicate digest evaluation_id")
                    if type(receipt) is str:
                        receipt_preregistrations[receipt] = e
                    if type(evaluation) is str:
                        receipt_evaluations.add(evaluation)
            elif e["kind"] == RUN:
                if e["probe_set_hash"] not in registered:
                    problems.append(f"seq {e.get('seq')}: RUN references a probe set never pre-registered "
                                    f"earlier — result cannot be trusted (answers before frozen questions)")
                commitment = e.get("commitment")
                reserved = _uses_reserved_digest_namespace(e)
                if reserved and commitment != DIGEST_RECEIPT_COMMITMENT:
                    problems.append(
                        f"seq {e.get('seq')}: reserved MyQuant fields require the exact "
                        "digest receipt commitment"
                    )
                if commitment is not None and commitment != DIGEST_RECEIPT_COMMITMENT:
                    problems.append(
                        f"seq {e.get('seq')}: unknown digest receipt commitment {commitment!r}"
                    )
                if commitment == DIGEST_RECEIPT_COMMITMENT:
                    if frozenset(e) != _DIGEST_RUN_KEYS:
                        problems.append(
                            f"seq {e.get('seq')}: digest run fields do not match the closed schema"
                        )
                    if e.get("receipt_schema") != MYQUANT_RUN_RECEIPT_SCHEMA:
                        problems.append(f"seq {e.get('seq')}: unknown digest run receipt schema")
                    if e.get("suite") != MYQUANT_DIGEST_SUITE:
                        problems.append(f"seq {e.get('seq')}: unknown digest evaluation suite")
                    if e.get("metrics") != {}:
                        problems.append(
                            f"seq {e.get('seq')}: digest run must not carry metrics or labels"
                        )
                    digest_fields = (
                        "probe_set_hash",
                        "model_artifact_sha256",
                        "result_receipt_sha256",
                        "result_artifact_sha256",
                        "preregistration_receipt_sha256",
                        "evaluation_protocol_sha256",
                        "evaluation_id",
                        "run_id",
                    )
                    for field in digest_fields:
                        if type(e.get(field)) is not str or not _SHA256.fullmatch(e[field]):
                            problems.append(
                                f"seq {e.get('seq')}: {field} is not a lowercase sha256"
                            )
                    try:
                        run_started = _parse_aware_timestamp(
                            e.get("run_started_at"), "run_started_at"
                        )
                        run_completed = _parse_aware_timestamp(
                            e.get("run_completed_at"), "run_completed_at"
                        )
                        appended_at = _parse_aware_timestamp(e.get("ts"), "ts")
                        if run_started >= run_completed:
                            problems.append(
                                f"seq {e.get('seq')}: run_completed_at is not later than run_started_at"
                            )
                        if run_completed > appended_at:
                            problems.append(
                                f"seq {e.get('seq')}: result was appended before run completion"
                            )
                    except ValueError as exc:
                        run_started = None
                        problems.append(f"seq {e.get('seq')}: {exc}")
                    if e.get("responses_hash") != e.get("result_receipt_sha256"):
                        problems.append(
                            f"seq {e.get('seq')}: responses_hash does not commit to the exact result receipt"
                        )
                    preregistration = receipt_preregistrations.get(
                        e.get("preregistration_receipt_sha256")
                    )
                    if preregistration is None:
                        problems.append(
                            f"seq {e.get('seq')}: digest result references a preregistration receipt "
                            "not registered earlier"
                        )
                    else:
                        try:
                            preregistered_at = _parse_aware_timestamp(
                                preregistration.get("ts"), "preregistration ts"
                            )
                            if run_started is not None and preregistered_at >= run_started:
                                problems.append(
                                    f"seq {e.get('seq')}: preregistration did not reach the "
                                    "registry strictly before run start"
                                )
                        except ValueError as exc:
                            problems.append(f"seq {e.get('seq')}: {exc}")
                        for field in (
                            "probe_set_hash",
                            "evaluation_id",
                            "model_artifact_sha256",
                            "evaluation_protocol_sha256",
                            "suite",
                        ):
                            expected = (
                                "sha256:" + e[field]
                                if field == "model_artifact_sha256"
                                else e[field]
                            )
                            actual = (
                                e.get("model")
                                if field == "model_artifact_sha256"
                                else e.get(field)
                            )
                            if field == "model_artifact_sha256":
                                preregistered = "sha256:" + str(preregistration.get(field, ""))
                            else:
                                preregistered = preregistration.get(field)
                            if actual != expected or actual != preregistered:
                                problems.append(
                                    f"seq {e.get('seq')}: digest result {field} does not match "
                                    "its preregistration"
                                )
                    duplicate_fields = (
                        ("evaluation_id", result_evaluations),
                        ("run_id", receipt_runs),
                        ("result_receipt_sha256", result_receipts),
                        ("result_artifact_sha256", result_artifacts),
                    )
                    for field, seen in duplicate_fields:
                        value = e.get(field)
                        if value in seen:
                            problems.append(
                                f"seq {e.get('seq')}: duplicate/replayed digest result {field}"
                            )
                        if type(value) is str:
                            seen.add(value)
            else:
                problems.append(f"seq {e.get('seq')}: unknown kind {e.get('kind')!r}")
            prev = e["entry_hash"]
        except (KeyError, TypeError) as exc:
            problems.append(f"position {i}: malformed attestation ({exc})")
            prev = e.get("entry_hash", prev)
    return (not problems), problems


def summarize(entries: list[dict]) -> dict:
    """Build the public registry summary from already-verified/read entries."""
    ok, problems = verify(entries)
    runs = [e for e in entries if e.get("kind") == RUN]
    return {
        "attestations": len(entries),
        "preregistrations": sum(1 for e in entries if e.get("kind") == PREREGISTRATION),
        "runs": len(runs),
        "models": sorted({e.get("model", "") for e in runs if e.get("model")}),
        "verified": ok,
        "problems": problems,
        "merkle_root": merkle_root(entries),
        "head_hash": entries[-1]["entry_hash"] if entries else GENESIS_PREV,
        "first_ts": entries[0]["ts"] if entries else None,
        "head_ts": entries[-1]["ts"] if entries else None,
        "recent_runs": [
            {"ts": e["ts"], "model": e.get("model"), "suite": e.get("suite", ""),
             "metrics": e.get("metrics", {}), "probe_set_hash": e["probe_set_hash"][:16],
             "responses_hash": e.get("responses_hash", "")[:16]}
            for e in runs[-8:]
        ],
    }


def summary_document(entries: list[dict]) -> dict:
    """Deterministic public projection of one registry snapshot.

    ``generated_at`` is the chain head timestamp, not wall-clock refresh time.  A
    no-op writer therefore emits identical bytes and cannot make stale evidence look
    newly generated merely because a scheduled job ran.
    """
    if not entries:
        raise ValueError("cannot publish an eval registry summary for an empty registry")
    return {
        "generated_at": entries[-1]["ts"],
        "title": REGISTRY_TITLE,
        "what": REGISTRY_WHAT,
        "registry": REGISTRY_PUBLIC_PATH,
        "verify_cmd": REGISTRY_VERIFY_COMMAND,
        **summarize(entries),
    }


def write_summary(path: str | Path, entries: list[dict]) -> dict:
    """Atomically and durably publish a summary from already-read entries."""
    document = summary_document(entries)
    encoded = (
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    atomic_replace_bytes(path, encoded)
    return document


def refresh_summary(registry_path: str | Path, output_path: str | Path) -> dict:
    """Read and project one registry snapshot while excluding concurrent writers."""
    with registry_lock(registry_path):
        entries, _ = _verified_snapshot(registry_path)
        return write_summary(output_path, entries)


def summary(path: str) -> dict:
    return summarize(read_ledger(path))
