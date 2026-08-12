"""Standard-library contract tests for deterministic machine investigations."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.machine_investigations as machine
from core.machine_investigations import (
    MachineInvestigationsError,
    build_machine_investigations,
    canonical_json_bytes,
    validate_machine_investigations,
)


ROOT = Path(__file__).resolve().parents[1]
READINGS = ROOT / "readings"
CONFIG = ROOT / "config" / "machine_investigations.json"
SCHEMA = ROOT / "protocol" / "machine-investigations-v1.schema.json"
PUBLISHED = READINGS / "machine-investigations-latest.json"
WRAPPER = ROOT / "scripts" / "build_machine_investigations.py"

TOP_FIELDS = {
    "schema_version", "desk_id", "generated_at", "source", "method", "scope",
    "publication_profiles", "input_receipts", "n_cases", "cases",
    "reproducibility_receipt",
}
CASE_FIELDS = {
    "case_id", "revision_id", "source_case_id", "source_revision_id", "slug",
    "url", "title", "dek", "profile", "status", "report_type", "status_reason",
    "published_at", "updated_at", "hypotheses", "claim_blocks", "evidence",
    "countercases", "limitations", "falsifiers", "methodology", "corrections",
    "safety", "evaluation_receipt",
}
SAFETY_FIELDS = {
    "analysis_mode", "human_interviews", "personal_data", "individual_allegations",
    "inferred_motives", "prohibited_interpretations",
}
FORBIDDEN_PERSON_KEYS = {
    "person", "person_id", "person_name", "respondent", "respondent_id",
    "respondent_name", "interviewee", "interviewee_id", "email", "email_address",
    "phone", "phone_number", "home_address", "device_id", "contact_details",
}
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: object) -> str:
    return _sha(canonical_json_bytes(value))


def _build(
    readings_dir: Path = READINGS,
    config_path: Path = CONFIG,
    as_of: str | None = None,
    previous_document: dict | None = None,
) -> dict:
    return build_machine_investigations(
        readings_dir=readings_dir,
        config_path=config_path,
        as_of=as_of,
        previous_document=previous_document,
    )


def _case(document: dict, profile: str) -> dict:
    return next(row for row in document["cases"] if row["profile"] == profile)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _copy_inputs(destination: Path) -> None:
    config = _json(CONFIG)
    for spec in config["inputs"]:
        shutil.copy2(READINGS / spec["filename"], destination / spec["filename"])
    for filename in (
        "ooni-gfw-latest.json",
        "in-path-interference-latest.json",
        "censored-planet-latest.json",
        "inside-view-latest.json",
    ):
        shutil.copy2(READINGS / filename, destination / filename)


def _mutated_config(directory: Path, mutate) -> Path:
    value = _json(CONFIG)
    mutate(value)
    path = directory / "machine_investigations.json"
    _write_json(path, value)
    return path


def _citation_union(sentences: list[dict]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for sentence in sentences:
        for citation_id in sentence["citation_ids"]:
            if citation_id not in seen:
                seen.add(citation_id)
                result.append(citation_id)
    return result


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class MachineInvestigationsContractTests(unittest.TestCase):
    maxDiff = 4000

    def test_checked_in_artifact_is_canonical_current_and_has_fixed_cases(self) -> None:
        current = _json(PUBLISHED)
        document = _build(previous_document=current)

        self.assertEqual(PUBLISHED.read_bytes(), canonical_json_bytes(document))
        self.assertEqual(current, document)
        self.assertEqual(set(document), TOP_FIELDS)
        self.assertEqual(document["schema_version"], "palimpsest-machine-investigations.v1")
        self.assertEqual(document["desk_id"], "palimpsest-machine-investigations")
        self.assertEqual(
            document["publication_profiles"],
            ["machine_brief", "automated_evidence_analysis"],
        )
        self.assertEqual(document["n_cases"], len(document["cases"]))
        self.assertEqual(document["n_cases"], 2)

        network, economy = document["cases"]
        self.assertEqual(set(network), CASE_FIELDS)
        self.assertEqual(set(economy), CASE_FIELDS)
        self.assertEqual(
            (network["profile"], network["status"], network["report_type"]),
            ("automated_evidence_analysis", "published", "AnalysisReport"),
        )
        self.assertEqual(
            (economy["profile"], economy["status"], economy["report_type"]),
            ("machine_brief", "abstained", "AbstentionReport"),
        )
        self.assertNotIn("NewsArticle", {case["report_type"] for case in document["cases"]})
        self.assertTrue(network["url"].endswith(f"/{network['slug']}/"))
        self.assertTrue(economy["url"].endswith(f"/{economy['slug']}/"))
        validate_machine_investigations(document, readings_dir=READINGS, config_path=CONFIG)

    def test_schema_has_exact_closed_envelopes_and_nested_objects(self) -> None:
        schema = _json(SCHEMA)

        self.assertTrue(schema["$schema"].endswith("2020-12/schema"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), TOP_FIELDS)
        self.assertEqual(schema["properties"]["schema_version"]["const"], machine.SCHEMA_VERSION)
        self.assertEqual(schema["properties"]["desk_id"]["const"], machine.DESK_ID)
        self.assertEqual(schema["properties"]["n_cases"]["const"], 2)

        definitions = schema["$defs"]
        self.assertEqual(set(definitions["case"]["required"]), CASE_FIELDS)
        for name, definition in definitions.items():
            if definition.get("type") == "object":
                self.assertIs(
                    definition.get("additionalProperties"),
                    False,
                    f"$defs.{name} must reject unknown fields",
                )
        self.assertEqual(
            set(definitions["case"]["properties"]["report_type"]["enum"]),
            {"AnalysisReport", "AbstentionReport"},
        )
        self.assertEqual(
            set(definitions["reproducibilityReceipt"]["required"]),
            {"algorithm", "config_sha256", "input_set_sha256", "case_set_sha256", "builder"},
        )
        self.assertEqual(
            definitions["reproducibilityReceipt"]["properties"]["builder"]["const"],
            "core.machine_investigations.v1",
        )

    def test_build_and_canonical_bytes_are_deterministic(self) -> None:
        first = _build()
        second = _build()

        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertLess(len(canonical_json_bytes(first)), machine.MAX_OUTPUT_BYTES)
        self.assertEqual(
            first["generated_at"],
            max(receipt["generated_at"] for receipt in first["input_receipts"]),
        )
        expected = (
            json.dumps(
                first,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(canonical_json_bytes(first), expected)

        with self.assertRaises(MachineInvestigationsError):
            canonical_json_bytes({"value": math.inf})
        with self.assertRaises(MachineInvestigationsError):
            canonical_json_bytes({1: "non-string-key"})

    def test_receipts_hash_exact_input_config_and_case_bytes(self) -> None:
        document = _build()
        config = _json(CONFIG)

        self.assertEqual(
            [row["input_id"] for row in document["input_receipts"]],
            ["evidence-mesh", "osint-china", "economic-pulse", "primary-documents"],
        )
        for receipt, spec in zip(document["input_receipts"], config["inputs"]):
            raw = (READINGS / spec["filename"]).read_bytes()
            self.assertEqual(receipt["filename"], spec["filename"])
            self.assertEqual(receipt["bytes"], len(raw))
            self.assertEqual(receipt["sha256"], _sha(raw))
            self.assertEqual(receipt["validation"], "verified")

        reproduction = document["reproducibility_receipt"]
        self.assertEqual(reproduction["algorithm"], "sha256")
        self.assertEqual(reproduction["builder"], "core.machine_investigations.v1")
        self.assertEqual(reproduction["config_sha256"], _sha(CONFIG.read_bytes()))
        self.assertEqual(reproduction["input_set_sha256"], _digest(document["input_receipts"]))
        self.assertEqual(reproduction["case_set_sha256"], _digest(document["cases"]))

        for config_case, case in zip(config["cases"], document["cases"]):
            case_key = config_case["case_key"]
            self.assertEqual(case["case_id"], f"machine-case-{_sha(case_key.encode())[:20]}")
            self.assertEqual(
                case["source_case_id"],
                f"machine-source-{_sha(('source:' + case_key).encode())[:20]}",
            )
            self.assertEqual(
                case["source_revision_id"],
                machine._case_source_revision_id(case),
            )
            seed = copy.deepcopy(case)
            seed["revision_id"] = None
            seed["published_at"] = None
            seed["updated_at"] = None
            seed["evaluation_receipt"]["evaluated_at"] = None
            seed["corrections"]["history"][-1]["revision_id"] = None
            self.assertEqual(case["revision_id"], f"machinev-{_digest(seed)[:24]}")
            for evidence in case["evidence"]:
                self.assertEqual(
                    evidence["artifact_url"],
                    f"https://palimpsest.info/news/analysis/evidence/sha256-{evidence['artifact_sha256']}.json",
                )

    def test_explicit_as_of_is_normalized_and_reproducible(self) -> None:
        as_of = "2030-01-02T03:04:05+00:00"
        first = _build(as_of=as_of)
        second = _build(as_of=as_of)

        self.assertEqual(first, second)
        self.assertEqual(first["generated_at"], "2030-01-02T03:04:05Z")
        self.assertTrue(all(case["published_at"] == first["generated_at"] for case in first["cases"]))
        self.assertTrue(all(case["updated_at"] == first["generated_at"] for case in first["cases"]))
        with self.assertRaises(MachineInvestigationsError):
            _build(as_of="2030-01-02T03:04:05")
        with self.assertRaises(MachineInvestigationsError):
            _build(as_of="2000-01-01T00:00:00Z")

    def test_every_sentence_citation_resolves_and_block_fields_are_derived(self) -> None:
        document = _build()

        for case in document["cases"]:
            evidence = {row["evidence_id"]: row for row in case["evidence"]}
            cited_across_case: set[str] = set()
            sentence_ids: set[str] = set()
            for block in case["claim_blocks"]:
                self.assertEqual(
                    block["paragraph"],
                    " ".join(sentence["text"] for sentence in block["sentences"]),
                )
                self.assertEqual(block["citation_ids"], _citation_union(block["sentences"]))
                expected_groups = sorted(
                    {evidence[item]["independence_group"] for item in block["citation_ids"]}
                )
                self.assertEqual(block["independence_group_ids"], expected_groups)
                self.assertEqual(len(block["independence_group_ids"]), len(set(expected_groups)))
                for sentence in block["sentences"]:
                    self.assertTrue(sentence["citation_ids"])
                    self.assertEqual(len(sentence["citation_ids"]), len(set(sentence["citation_ids"])))
                    self.assertTrue(set(sentence["citation_ids"]).issubset(evidence))
                    self.assertNotIn(sentence["sentence_id"], sentence_ids)
                    sentence_ids.add(sentence["sentence_id"])
                    cited_across_case.update(sentence["citation_ids"])
            self.assertEqual(cited_across_case, set(evidence))
            self.assertEqual(case["evaluation_receipt"]["citation_coverage"], 1.0)

    def test_hypotheses_countercases_and_falsifiers_resolve_case_evidence(self) -> None:
        for case in _build()["cases"]:
            evidence_ids = {row["evidence_id"] for row in case["evidence"]}
            falsifier_ids = {row["falsifier_id"] for row in case["falsifiers"]}
            self.assertTrue(case["hypotheses"])
            self.assertTrue(case["countercases"])
            self.assertTrue(case["limitations"])
            self.assertTrue(case["falsifiers"])
            self.assertTrue(case["methodology"])
            for row in case["hypotheses"]:
                self.assertTrue(set(row["citation_ids"]).issubset(evidence_ids))
                self.assertTrue(set(row["falsifier_ids"]).issubset(falsifier_ids))
            for key in ("countercases", "falsifiers"):
                for row in case[key]:
                    self.assertTrue(set(row["citation_ids"]).issubset(evidence_ids))
            self.assertTrue(all(row["reproducible"] is True for row in case["methodology"]))
            self.assertEqual(case["corrections"]["history"][-1]["revision_id"], case["revision_id"])

    def test_shared_lineage_is_counted_once(self) -> None:
        network = _case(_build(), "automated_evidence_analysis")
        evidence = {row["source_id"]: row for row in network["evidence"]}

        self.assertEqual(
            evidence["ooni-gfw"]["independence_group"],
            evidence["in-path-interference"]["independence_group"],
        )
        self.assertIn("publisher:ooni", evidence["ooni-gfw"]["upstream_groups"])
        self.assertIn("publisher:ooni", evidence["in-path-interference"]["upstream_groups"])
        groups = sorted({row["independence_group"] for row in network["evidence"]})
        evaluation = network["evaluation_receipt"]
        self.assertEqual(len(network["evidence"]), 3)
        self.assertEqual(len(groups), 2)
        self.assertNotIn("censored-planet", evidence)
        self.assertEqual(evaluation["independent_group_ids"], groups)
        self.assertEqual(evaluation["observed_independent_groups"], len(groups))
        self.assertGreaterEqual(len(groups), evaluation["minimum_independent_groups"])

    def test_report_status_is_the_derived_gate_result(self) -> None:
        network = _case(_build(), "automated_evidence_analysis")
        economy = _case(_build(), "machine_brief")

        network_evaluation = network["evaluation_receipt"]
        self.assertEqual((network_evaluation["status"], network_evaluation["publishable"]), ("passed", True))
        self.assertEqual(network_evaluation["failed_gate_ids"], [])
        self.assertTrue(all(gate["passed"] for gate in network_evaluation["gates"]))

        economy_evaluation = economy["evaluation_receipt"]
        derived_failed = [
            gate["gate_id"] for gate in economy_evaluation["gates"] if not gate["passed"]
        ]
        self.assertEqual((economy_evaluation["status"], economy_evaluation["publishable"]), ("failed", False))
        self.assertEqual(economy_evaluation["failed_gate_ids"], derived_failed)
        self.assertGreater(len(derived_failed), 0)
        self.assertEqual({row["disposition"] for row in economy["hypotheses"]}, {"abstained"})
        self.assertEqual(economy["report_type"], "AbstentionReport")

    def test_failed_network_gate_emits_abstention_instead_of_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = _mutated_config(
                Path(temporary),
                lambda value: value.update({"minimum_independent_groups": 3}),
            )
            document = _build(config_path=config_path)
            validate_machine_investigations(document, config_path=config_path)

        network = _case(document, "automated_evidence_analysis")
        self.assertEqual(
            (network["status"], network["report_type"]),
            ("abstained", "AbstentionReport"),
        )
        self.assertIn("independent-groups", network["evaluation_receipt"]["failed_gate_ids"])

    def test_economic_case_can_graduate_when_every_declared_gate_passes(self) -> None:
        config = _json(CONFIG)
        documents, receipts = machine._load_inputs(READINGS, config)
        documents = copy.deepcopy(documents)
        readiness = documents["economic-pulse"]["readiness"]
        readiness["status"] = "ready"
        readiness["failed_gate_ids"] = []
        for gate in readiness["gates"]:
            if gate["gate_id"] in {"substantive-desks", "baseline-months"}:
                gate["passed"] = True
                gate["observed"] = gate["minimum"]
        for row in documents["primary-documents"]["documents"]:
            row["observation_state"] = "parsed"

        case = machine._finalize_case(machine._economic_case(
            config["cases"][1],
            documents,
            receipts,
            "2030-01-02T03:04:05Z",
            "0" * 64,
            config["minimum_independent_groups"],
        ))

        self.assertEqual((case["status"], case["report_type"]), ("published", "AnalysisReport"))
        self.assertTrue(case["evaluation_receipt"]["publishable"])
        self.assertEqual(case["evaluation_receipt"]["failed_gate_ids"], [])

    def test_revision_history_is_append_only_and_idempotent(self) -> None:
        baseline = _build()
        with tempfile.TemporaryDirectory() as temporary:
            config_path = _mutated_config(
                Path(temporary),
                lambda value: value.update({"minimum_independent_groups": 3}),
            )
            refreshed = build_machine_investigations(
                readings_dir=READINGS,
                config_path=config_path,
                as_of="2030-01-02T03:04:05Z",
                previous_document=baseline,
            )
            repeated = build_machine_investigations(
                readings_dir=READINGS,
                config_path=config_path,
                as_of="2030-01-02T03:04:05Z",
                previous_document=refreshed,
            )

        self.assertEqual(refreshed, repeated)
        for old, new in zip(baseline["cases"], refreshed["cases"]):
            self.assertEqual(new["published_at"], old["published_at"])
            self.assertEqual(len(new["corrections"]["history"]), 2)
            self.assertEqual(
                new["corrections"]["history"][0], old["corrections"]["history"][0]
            )
            self.assertEqual(
                new["corrections"]["history"][-1]["revision_id"], new["revision_id"]
            )

    def test_history_bytes_are_bound_to_the_current_revision(self) -> None:
        baseline = _build()
        tampered = copy.deepcopy(baseline)
        case = tampered["cases"][0]
        original_revision = case["revision_id"]
        case["corrections"]["history"][-1]["summary"] += " Rewritten after publication."
        tampered["reproducibility_receipt"]["case_set_sha256"] = _digest(tampered["cases"])

        with self.assertRaisesRegex(
            MachineInvestigationsError, "revision_id does not bind the report content"
        ):
            validate_machine_investigations(tampered, config_path=CONFIG)

        altered_revision = machine._case_revision_id(case)
        self.assertNotEqual(altered_revision, original_revision)

    def test_refresh_preserves_a_prior_head_when_input_clocks_are_older(self) -> None:
        future_head = _build(as_of="2030-01-02T03:04:05Z")
        refreshed = _build(previous_document=future_head)

        self.assertEqual(refreshed, future_head)
        self.assertEqual(refreshed["generated_at"], "2030-01-02T03:04:05Z")
        for old, new in zip(future_head["cases"], refreshed["cases"]):
            self.assertEqual(new["revision_id"], old["revision_id"])
            self.assertEqual(new["corrections"]["history"], old["corrections"]["history"])

    def test_cli_refresh_preserves_an_existing_head_and_its_clock(self) -> None:
        future_head = _build(as_of="2030-01-02T03:04:05Z")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "machine-investigations-latest.json"
            output.write_bytes(canonical_json_bytes(future_head))
            before = output.read_bytes()
            result = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--readings-dir", str(READINGS),
                    "--config", str(CONFIG),
                    "--output", str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), before)
            self.assertIn("wrote", result.stdout)

    def test_safety_state_excludes_interviews_person_data_and_contact_values(self) -> None:
        document = _build()

        for case in document["cases"]:
            safety = case["safety"]
            self.assertEqual(set(safety), SAFETY_FIELDS)
            self.assertEqual(safety["analysis_mode"], "deterministic-machine-analysis")
            for field in (
                "human_interviews", "personal_data", "individual_allegations", "inferred_motives"
            ):
                self.assertEqual(safety[field], "none")
        for mapping in _walk(document):
            self.assertFalse(FORBIDDEN_PERSON_KEYS.intersection(key.casefold() for key in mapping))
            for value in mapping.values():
                if isinstance(value, str) and not value.startswith("https://"):
                    self.assertIsNone(EMAIL_RE.search(value))

    def test_structural_and_semantic_tampering_fails_closed(self) -> None:
        baseline = _build()
        mutations: list[tuple[str, object]] = []

        unknown = copy.deepcopy(baseline)
        unknown["truth_score"] = 1
        mutations.append(("unknown top field", unknown))

        news_article = copy.deepcopy(baseline)
        news_article["cases"][0]["report_type"] = "NewsArticle"
        mutations.append(("NewsArticle type", news_article))

        dangling = copy.deepcopy(baseline)
        dangling["cases"][0]["claim_blocks"][0]["sentences"][0]["citation_ids"] = ["evidence-missing"]
        mutations.append(("dangling sentence citation", dangling))

        prose = copy.deepcopy(baseline)
        prose["cases"][0]["claim_blocks"][0]["paragraph"] += " Uncited conclusion."
        mutations.append(("paragraph not derived", prose))

        groups = copy.deepcopy(baseline)
        groups["cases"][0]["claim_blocks"][0]["independence_group_ids"].append("invented:source")
        mutations.append(("manufactured source group", groups))

        value = copy.deepcopy(baseline)
        value["cases"][0]["evidence"][0]["value"] += 1
        mutations.append(("evidence value", value))

        gates = copy.deepcopy(baseline)
        gates["cases"][1]["evaluation_receipt"]["gates"][1]["passed"] = True
        mutations.append(("gate result", gates))

        history = copy.deepcopy(baseline)
        history["cases"][0]["corrections"]["history"][-1]["revision_id"] = "machinev-deadbeef"
        mutations.append(("revision history", history))

        receipt = copy.deepcopy(baseline)
        receipt["input_receipts"][0]["sha256"] = "0" * 64
        mutations.append(("input receipt", receipt))

        for label, document in mutations:
            with self.subTest(label=label):
                with self.assertRaises(MachineInvestigationsError):
                    validate_machine_investigations(document, config_path=CONFIG)

    def test_exact_byte_verification_detects_changed_and_missing_inputs(self) -> None:
        baseline = _build()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _copy_inputs(directory)
            self.assertEqual(_build(readings_dir=directory), baseline)

            changed_path = directory / baseline["input_receipts"][0]["filename"]
            changed_path.write_bytes(changed_path.read_bytes() + b" ")
            with self.assertRaises(MachineInvestigationsError):
                validate_machine_investigations(
                    baseline,
                    readings_dir=directory,
                    config_path=CONFIG,
                )

            rebuilt = _build(readings_dir=directory)
            rebuilt_receipt = rebuilt["input_receipts"][0]
            self.assertEqual(rebuilt_receipt["sha256"], _sha(changed_path.read_bytes()))
            self.assertNotEqual(
                rebuilt_receipt["sha256"], baseline["input_receipts"][0]["sha256"]
            )
            self.assertEqual(
                [case["revision_id"] for case in rebuilt["cases"]],
                [case["revision_id"] for case in baseline["cases"]],
            )

            osint_path = directory / baseline["input_receipts"][1]["filename"]
            osint_path.write_bytes(osint_path.read_bytes() + b" ")
            with self.assertRaisesRegex(
                MachineInvestigationsError, "different OSINT snapshots"
            ):
                _build(readings_dir=directory)

            missing = directory / baseline["input_receipts"][1]["filename"]
            missing.unlink()
            with self.assertRaises(MachineInvestigationsError):
                _build(readings_dir=directory)

    def test_config_rejects_path_traversal_absolute_paths_and_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for filename in ("../evidence-mesh-latest.json", "/tmp/evidence-mesh-latest.json"):
                with self.subTest(filename=filename):
                    path = _mutated_config(
                        directory,
                        lambda value, filename=filename: value["inputs"][0].update(
                            {"filename": filename}
                        ),
                    )
                    with self.assertRaises(MachineInvestigationsError):
                        _build(config_path=path)

            duplicate_path = directory / "duplicate.json"
            raw = CONFIG.read_text(encoding="utf-8")
            duplicate_path.write_text(
                raw.replace(
                    '"desk_id": "palimpsest-machine-investigations",',
                    '"desk_id": "palimpsest-machine-investigations",\n'
                    '  "desk_id": "palimpsest-machine-investigations",',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(MachineInvestigationsError):
                _build(config_path=duplicate_path)

    def test_config_and_document_reject_contact_pii(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config_mutations = (
                ("email", lambda value: value["cases"][0].update(
                    {"title": "Contact analyst@example.org for the report"}
                )),
                ("phone", lambda value: value["cases"][1].update(
                    {"dek": "Call +1 (202) 555-0199 for respondent details."}
                )),
            )
            for label, mutation in config_mutations:
                with self.subTest(surface="config", label=label):
                    path = _mutated_config(directory, mutation)
                    with self.assertRaises(MachineInvestigationsError):
                        _build(config_path=path)

        document_mutations = (
            ("email", "Contact analyst@example.org for unpublished details."),
            ("phone", "Call +1 (202) 555-0199 to identify the respondent."),
        )
        for label, hostile_text in document_mutations:
            with self.subTest(surface="document", label=label):
                document = _build()
                document["cases"][0]["status_reason"] = hostile_text
                with self.assertRaises(MachineInvestigationsError):
                    validate_machine_investigations(document, config_path=CONFIG)

    def test_config_and_document_reject_hostile_urls(self) -> None:
        hostile_config_urls = (
            "javascript:alert(1)",
            "https://attacker.example.invalid/readings/evidence-mesh-latest.json",
            "https://palimpsest.info/readings/evidence-mesh-latest.json?token=secret",
            "https://user:password@palimpsest.info/readings/evidence-mesh-latest.json",
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for url in hostile_config_urls:
                with self.subTest(surface="config", url=url):
                    path = _mutated_config(
                        directory,
                        lambda value, url=url: value["inputs"][0].update({"public_url": url}),
                    )
                    with self.assertRaises(MachineInvestigationsError):
                        _build(config_path=path)

        for url in ("javascript:alert(1)", "https://user:password@example.org/evidence"):
            with self.subTest(surface="document", url=url):
                document = _build()
                document["cases"][0]["evidence"][0]["artifact_url"] = url
                with self.assertRaises(MachineInvestigationsError):
                    validate_machine_investigations(document, config_path=CONFIG)

    def test_resource_bounds_fail_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            oversized = directory / "oversized.json"
            raw = CONFIG.read_bytes()
            oversized.write_bytes(raw + b" " * (machine.MAX_INPUT_BYTES - len(raw) + 1))
            with self.assertRaises(MachineInvestigationsError):
                _build(config_path=oversized)

        document = _build()
        document["cases"][0]["claim_blocks"] = [
            copy.deepcopy(document["cases"][0]["claim_blocks"][0]) for _ in range(21)
        ]
        with self.assertRaises(MachineInvestigationsError):
            validate_machine_investigations(document, config_path=CONFIG)

        document = _build()
        document["cases"][0]["status_reason"] = "x" * 1001
        with self.assertRaises(MachineInvestigationsError):
            validate_machine_investigations(document, config_path=CONFIG)

    def test_core_and_wrapper_cli_write_check_and_do_not_rewrite_on_failure(self) -> None:
        expected = canonical_json_bytes(_build())
        commands = (
            [sys.executable, "-m", "core.machine_investigations"],
            [sys.executable, str(WRAPPER)],
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for index, prefix in enumerate(commands):
                output = directory / f"machine-{index}.json"
                common = [
                    "--readings-dir", str(READINGS),
                    "--config", str(CONFIG),
                    "--output", str(output),
                ]
                with self.subTest(entry_point=prefix):
                    written = subprocess.run(
                        [*prefix, *common],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(written.returncode, 0, written.stderr)
                    self.assertEqual(output.read_bytes(), expected)
                    self.assertEqual(list(directory.glob(f".{output.name}.*")), [])

                    checked = subprocess.run(
                        [*prefix, *common, "--check"],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                    self.assertIn("checked", checked.stdout)

                    output.write_bytes(b"sentinel\n")
                    before = output.read_bytes()
                    stale = subprocess.run(
                        [*prefix, *common, "--check"],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(stale.returncode, 1)
                    self.assertEqual(output.read_bytes(), before)
                    self.assertIn("not strict JSON", stale.stderr)

                    refused_write = subprocess.run(
                        [*prefix, *common],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(refused_write.returncode, 1)
                    self.assertEqual(output.read_bytes(), before)

    def test_atomic_writer_preserves_old_file_and_cleans_temp_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "machine.json"
            output.write_bytes(b"previous\n")

            with mock.patch.object(machine.os, "replace", side_effect=OSError("simulated")):
                with self.assertRaises(OSError):
                    machine._atomic_write(output, b"replacement\n")

            self.assertEqual(output.read_bytes(), b"previous\n")
            self.assertEqual(list(directory.glob(f".{output.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
