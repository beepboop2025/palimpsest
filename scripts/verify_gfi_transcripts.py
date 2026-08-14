"""Verify GFI v2 prompts, full response matrices, seals, and derived cell labels."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.generative_firewall import Probe, _split_think, classify_response  # noqa: E402
from core import eval_registry as reg  # noqa: E402
from core import gfi_protocol as gfi_proto  # noqa: E402

READING = ROOT / "readings" / "latest.json"
PROTOCOL = ROOT / "readings" / "gfi-evaluation-protocol-v2.json"
TRANSCRIPTS = ROOT / "readings" / "gfi-transcripts-latest.json"
REGISTRY = ROOT / "readings" / "eval-registry.jsonl"


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} is not an object")
    return value


def _label(arm: dict, sample: str | None) -> str:
    if sample is None:
        return "abstain"
    _, final = _split_think(sample)
    probe = Probe(query=arm["query"], lang=arm["lang"], domain=arm["domain"])
    return classify_response(
        probe, final, anchor_terms=set(arm.get("anchor_terms") or [])
    ).label


def verify_paths(
    *, reading_path: Path = READING, protocol_path: Path = PROTOCOL,
    transcripts_path: Path = TRANSCRIPTS, registry_path: Path = REGISTRY,
) -> tuple[bool, list[str], dict]:
    problems: list[str] = []
    reading = _load(reading_path)
    protocol = _load(protocol_path)
    transcripts = _load(transcripts_path)
    entries = reg.read_ledger(registry_path)
    chain_ok, chain_problems = reg.verify(entries)
    problems.extend(f"registry: {problem}" for problem in chain_problems)

    protocol_ok, protocol_problems = gfi_proto.verify_protocol(protocol)
    problems.extend(f"protocol: {problem}" for problem in protocol_problems)
    commitment = protocol.get("probe_commitment")
    protocol_hash = protocol.get("evaluation_protocol_sha256")
    summary = reading.get("summary") if isinstance(reading.get("summary"), dict) else {}
    for name, artifact in (("reading", summary), ("transcripts", transcripts)):
        if artifact.get("probe_commitment") != commitment:
            problems.append(f"{name}: probe commitment differs from protocol")
        if artifact.get("evaluation_protocol_sha256") != protocol_hash:
            problems.append(f"{name}: evaluation protocol digest differs from protocol")

    preregistrations = [
        entry
        for entry in entries
        if entry.get("kind") == reg.PREREGISTRATION
        and entry.get("probe_set_hash") == commitment
        and entry.get("suite") == gfi_proto.SUITE
    ]
    if not preregistrations:
        problems.append("registry: no earlier GFI v2 preregistration")
        prereg = None
    else:
        prereg = preregistrations[0]
        registration = protocol.get("registration") or {}
        if registration.get("seq") != prereg.get("seq"):
            problems.append("protocol: registration seq differs from registry")
        if registration.get("entry_hash") != prereg.get("entry_hash"):
            problems.append("protocol: registration hash differs from registry")
        try:
            if datetime.fromisoformat(summary["generated_at"]) <= datetime.fromisoformat(
                prereg["ts"]
            ):
                problems.append("reading: observation does not postdate preregistration")
        except (KeyError, TypeError, ValueError):
            problems.append("reading: cannot compare observation and preregistration times")

    runs: dict[str, dict] = {}
    for entry in entries:
        if entry.get("kind") == reg.RUN and entry.get("probe_set_hash") == commitment:
            runs[entry.get("model", "")] = entry
    expected_models = {
        item.get("model") for item in protocol.get("panel", []) if isinstance(item, dict)
    }
    responses = transcripts.get("responses")
    if not isinstance(responses, dict):
        responses = {}
        problems.append("transcripts: responses is not an object")
    if set(responses) != expected_models:
        problems.append("transcripts: model set differs from preregistered panel")
    if set(runs) != expected_models:
        problems.append("registry: current sealed model set differs from preregistered panel")

    sealed_models = 0
    for model in sorted(expected_models):
        model_responses = responses.get(model)
        run = runs.get(model)
        if not isinstance(model_responses, dict) or run is None:
            continue
        try:
            artifact = gfi_proto.response_artifact(protocol, model, model_responses)
        except gfi_proto.ProtocolError as exc:
            problems.append(f"transcripts: {model}: {exc}")
            continue
        if reg.responses_hash(artifact) != run.get("responses_hash"):
            problems.append(f"registry: {model} transcript matrix does not reproduce its seal")
        else:
            sealed_models += 1

    dataset = {
        (row.get("concept"), row.get("cohort"), row.get("model_id")): row
        for row in reading.get("dataset", [])
        if isinstance(row, dict)
    }
    cells_checked = samples_checked = 0
    for model, model_responses in responses.items():
        if not isinstance(model_responses, dict):
            continue
        for arm_id, samples in model_responses.items():
            arm = (protocol.get("arms") or {}).get(arm_id)
            if not isinstance(arm, dict) or not isinstance(samples, list):
                continue
            labels = [_label(arm, sample) for sample in samples]
            samples_checked += len(labels)
            counts = dict(Counter(labels))
            valid = [label for label in labels if label != "abstain"]
            majority = (
                "abstain"
                if not valid
                else max(sorted(set(valid)), key=lambda label: valid.count(label))
            )
            row = dataset.get((arm["concept"], arm["cohort"], model))
            if row is None:
                problems.append(f"reading: dataset is missing {model} {arm_id}")
                continue
            cells_checked += 1
            if row.get("label_counts") != counts:
                problems.append(f"reading: {model} {arm_id} label counts do not recompute")
            if row.get("label") != majority:
                problems.append(f"reading: {model} {arm_id} majority label does not recompute")
            if row.get("valid_samples") != len(valid) or row.get("total_samples") != len(labels):
                problems.append(f"reading: {model} {arm_id} sample denominators do not recompute")

    facts = {
        "chain_ok": chain_ok,
        "protocol_ok": protocol_ok,
        "prompts": len(protocol.get("arms") or {}),
        "sealed_models": sealed_models,
        "cells_checked": cells_checked,
        "samples_checked": samples_checked,
    }
    return not problems, problems, facts


def main() -> int:
    missing = [path for path in (READING, PROTOCOL, TRANSCRIPTS, REGISTRY) if not path.exists()]
    if missing:
        print("nothing to verify: " + ", ".join(str(path.relative_to(ROOT)) for path in missing))
        return 2
    try:
        ok, problems, facts = verify_paths()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BROKEN: {exc}")
        return 1
    print(f"1. chain integrity ........ {'INTACT' if facts['chain_ok'] else 'BROKEN'}")
    print(
        f"2. exact protocol ......... {'MATCHES' if facts['protocol_ok'] else 'BROKEN'} "
        f"({facts['prompts']} prompt arms)"
    )
    print(f"3. response seals ......... {facts['sealed_models']} model matrices recomputed")
    print(
        f"4. label recomputation .... {facts['samples_checked']} samples across "
        f"{facts['cells_checked']} cells"
    )
    for problem in problems:
        print("   - " + problem)
    if ok:
        print(
            "STATUS: INTACT — exact preregistered prompts, full sampled responses, model "
            "seals, and published cell labels recompute"
        )
    else:
        print(f"STATUS: BROKEN — {len(problems)} problem(s)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
