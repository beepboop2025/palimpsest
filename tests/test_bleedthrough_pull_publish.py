"""BLEEDTHROUGH — publication timing.

tests/test_bleedthrough.py covers what the round measures. These tests cover the
separate question the publisher has to answer honestly: when did we last look, as
against when did the fleet last move. A stable injector deployment is the normal
state of the apparatus — pools rotate on the censor's schedule, not ours — so a
round that finds the same fleet is a finding, not a fault, and it must not read on
the site as a prober that died.

Offline: the UDP transport is replaced by a canned one, so the only thing that
runs is the fingerprinting and the writer. Nothing leaves the machine, which is
the same discipline the runner itself enforces with its three live gates.

    PYTHONPATH=. python3 -m pytest tests/test_bleedthrough_pull_publish.py -q
"""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from collectors.bleedthrough import ApparatusEvent, POOL_ROTATION, RawInjection

import scripts.bleedthrough_pull as pull
import scripts.import_bleedthrough_snapshot as relay


# Three targets in one province, all drawing the same forged-IP pool. Three and not
# two because looks_sampled() calls one distinct pool across two targets a sample
# (1 >= 0.5 * 2) and would strip the regional layer; at three the round reads as an
# enumeration, which keeps these tests about publication timing and nothing else.
TARGET_IPS = ("203.0.113.10", "203.0.113.11", "203.0.113.12")

POOL_A = ("1.2.3.4", "5.6.7.8")
POOL_B = (
    "9.9.9.9",
    "8.8.8.8",
)  # a rotated pool: the movement this signal exists to see


@pytest.fixture
def publish(tmp_path, monkeypatch):
    """Run main() against a temp readings dir with the probe path stubbed."""
    # The kill switch is file-gated and fails safe, so point it at a path that does
    # not exist rather than trusting the repo checkout to be free of a halt file.
    monkeypatch.delenv("PALIMPSEST_HALT", raising=False)
    monkeypatch.setenv("PALIMPSEST_KILLFILE", str(tmp_path / "no_such_halt_file"))
    monkeypatch.setenv("BLEEDTHROUGH_LIVE", "1")
    monkeypatch.setenv("BLEEDTHROUGH_ALLOW_BOX", "1")
    monkeypatch.setattr(pull, "VANTAGE_KIND", relay.VANTAGE_KIND)
    monkeypatch.setattr(pull, "VANTAGE_COUNTRY", relay.VANTAGE_COUNTRY)
    receipt = tmp_path / "deployed-commit"
    receipt.write_text("a" * 40 + "\n", encoding="utf-8")
    # Production is an exported, read-only tree without `.git`; model that
    # topology explicitly so CI checkout style cannot override the receipt.
    monkeypatch.setattr(pull, "ROOT", str(tmp_path / "export-without-git"))
    monkeypatch.setattr(pull, "DEPLOYED_COMMIT_FILE", str(receipt))

    targets = tmp_path / "targets.json"
    targets.write_text(
        json.dumps(
            {
                "probe": {
                    "domain": "torproject.org",
                    "qtype": 1,
                    "ddti": "CIRCUMVENTION",
                },
                "targets": [
                    {"ip": ip, "province": "CN-HA", "asn": "AS4134", "kind": "dark"}
                    for ip in TARGET_IPS
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pull, "TARGETS", str(targets))
    monkeypatch.setattr(pull, "READINGS", str(tmp_path))
    monkeypatch.setattr(pull, "OUT", str(tmp_path / "bleedthrough-latest.json"))
    monkeypatch.setattr(pull, "HIST", str(tmp_path / "bleedthrough-history.jsonl"))
    # The disk baseline has to survive across rounds inside one test — that is how a
    # rotation becomes an event — but must not leak into the repo's data dir.
    monkeypatch.setattr(pull, "STORE_DIR", str(tmp_path / "baselines"))
    monkeypatch.setattr(
        pull, "PENDING", str(tmp_path / "baselines" / ".pending-publication.json")
    )
    # Two queries per target is enough to fingerprint a canned fleet, and it keeps the
    # rate ceiling from ever having to sleep.
    monkeypatch.setattr(pull, "BURST", 2)

    def run(
        pool=POOL_A, *, silent_targets=(), observed_waits=None, observed_targets=None
    ):
        # Every injector answers every probe with the same forged pool, so the
        # fingerprint is a function of `pool` alone and an unchanged round is
        # genuinely unchanged rather than accidentally so.
        def transport(domain, target_ip, *, wait):
            if observed_waits is not None:
                observed_waits.append(wait)
            if observed_targets is not None:
                observed_targets.append(target_ip)
            if target_ip in silent_targets:
                return []
            return [RawInjection(ip, rr_ttl=64) for ip in pool]

        monkeypatch.setattr(pull, "_udp_transport", transport)
        pull.main()
        out = tmp_path / "bleedthrough-latest.json"
        # None, not an exception: an abstaining round legitimately publishes nothing,
        # and the tests below need to say so out loud.
        if not out.exists():
            return None
        with open(out, encoding="utf-8") as f:
            return json.load(f)

    return run, tmp_path


def _history(tmp_path):
    path = tmp_path / "bleedthrough-history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a_repeated_finding_still_refreshes_the_observation_time(publish):
    """The bug this guards: a fleet that holds still never moved the reading, so the
    file stopped being rewritten and the board called a working prober stale."""
    run, tmp_path = publish
    first = run(POOL_A)
    second = run(POOL_A)

    assert second["generated_at"] > first["generated_at"], (
        "an unchanged fleet must still publish this round's observation time"
    )


def test_producer_latest_and_history_satisfy_the_strict_relay_contract(publish):
    run, tmp_path = publish
    reading = run(POOL_A)
    checked_at = (
        datetime.fromisoformat(
            reading["generated_at"].replace("Z", "+00:00")
        ).timestamp()
        + 1
    )

    validated = relay.validate_document(reading, now=checked_at)
    assert validated["method_version"] == pull.METHOD_VERSION
    assert validated["provenance"]["code_version"] == "a" * 40
    rows = _history(tmp_path)
    assert rows
    for index, row in enumerate(rows):
        validated_row = relay._validate_history_row(row, index, now=checked_at)
        assert validated_row["generated_at"].endswith("Z")
        assert validated_row["method_version"] == pull.METHOD_VERSION
        assert validated_row["vantages_probed"] == row["vantages_probed"]


def test_a_repeated_finding_does_not_move_last_changed_at(publish):
    run, tmp_path = publish
    first = run(POOL_A)
    second = run(POOL_A)

    assert second["last_changed_at"] == first["last_changed_at"]
    assert first["last_changed_at"] == first["generated_at"]


def test_a_repeated_finding_appends_no_history(publish):
    """History is the rotation record. Heartbeats belong in the reading, not here,
    or a record of what the censor changed fills up with what it did not."""
    run, tmp_path = publish
    run(POOL_A)
    run(POOL_A)
    run(POOL_A)

    assert len(_history(tmp_path)) == 1


def test_target_denominator_change_moves_history_even_when_signal_values_hold(publish):
    run, tmp_path = publish
    first = run(POOL_A)
    targets = tmp_path / "targets.json"
    payload = json.loads(targets.read_text(encoding="utf-8"))
    extra = "203.0.113.13"
    payload["targets"].append(
        {
            "ip": extra,
            "province": "CN-HA",
            "asn": "AS4134",
            "kind": "dark",
        }
    )
    targets.write_text(json.dumps(payload), encoding="utf-8")

    expanded = run(POOL_A, silent_targets={extra})

    assert expanded["vantages_probed"] == first["vantages_probed"] + 1
    assert expanded["vantages_injecting"] == first["vantages_injecting"]
    assert expanded["last_changed_at"] == expanded["generated_at"]
    assert len(_history(tmp_path)) == 2


def test_recorded_wait_is_applied_to_the_direct_transport(publish, monkeypatch):
    run, _tmp_path = publish
    monkeypatch.setattr(pull, "WAIT_S", 2.75)
    observed = []

    reading = run(POOL_A, observed_waits=observed)

    assert observed and set(observed) == {2.75}
    assert reading["provenance"]["wait_s"] == 2.75


def test_recorded_wait_is_applied_to_the_resolver_transport(publish, monkeypatch):
    run, tmp_path = publish
    targets = tmp_path / "targets.json"
    payload = json.loads(targets.read_text(encoding="utf-8"))
    for target in payload["targets"]:
        target["kind"] = "resolver"
    targets.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(pull, "WAIT_S", 2.5)
    observed = []

    def resolver_factory(*, clean_answers, wait):
        observed.append((clean_answers, wait))

        def transport(_domain, _target_ip):
            return [RawInjection(ip, rr_ttl=64) for ip in POOL_A]

        return transport

    monkeypatch.setattr(pull, "open_resolver_transport", resolver_factory)

    reading = run(POOL_A)

    assert observed == [(payload.get("clean_answers"), 2.5)]
    assert reading["provenance"]["wait_s"] == 2.5
    assert reading["provenance"]["transports"]["open_resolver"]["ran"] is True


def test_transport_or_burst_change_moves_method_history(publish, monkeypatch):
    run, tmp_path = publish
    first = run(POOL_A)

    monkeypatch.setattr(pull, "BURST", 3)
    changed = run(POOL_A)

    assert changed["provenance"]["burst"] == 3
    assert changed["last_changed_at"] == changed["generated_at"]
    assert changed["last_changed_at"] != first["last_changed_at"]
    rows = _history(tmp_path)
    assert len(rows) == 2
    assert rows[-1]["burst"] == 3


def test_a_rotated_pool_moves_last_changed_at_and_appends(publish):
    run, tmp_path = publish
    run(POOL_A)
    moved = run(POOL_B)

    assert moved["last_changed_at"] == moved["generated_at"]
    assert len(_history(tmp_path)) == 2
    assert any(e["kind"] == "pool_rotation" for e in moved["events"]), (
        "the round that moved the reading should say what moved"
    )


def test_history_replace_failure_recovers_exact_event_without_reprobing(
    publish, monkeypatch
):
    run, tmp_path = publish
    previous = run(POOL_A)
    real_atomic_write = pull._atomic_write
    failed = False

    def fail_history_once(path, payload, *, mode=0o644):
        nonlocal failed
        if path == pull.HIST and not failed:
            failed = True
            raise OSError("injected history replacement failure")
        return real_atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(pull, "_atomic_write", fail_history_once)
    with pytest.raises(OSError, match="history replacement"):
        run(POOL_B)

    pending_path = tmp_path / "baselines" / ".pending-publication.json"
    transaction = json.loads(pending_path.read_text(encoding="utf-8"))
    intended = transaction["latest"]
    assert intended["generated_at"] > previous["generated_at"]
    assert any(event["kind"] == "pool_rotation" for event in intended["events"])
    assert json.loads((tmp_path / "bleedthrough-latest.json").read_text()) == previous
    assert len(_history(tmp_path)) == 1

    probed = []
    recovered = run(POOL_B, observed_targets=probed)

    assert probed == [], "recovery must complete before any new network probe"
    assert recovered == intended
    assert len(_history(tmp_path)) == 2
    assert not pending_path.exists()


def test_latest_replace_failure_recovery_does_not_duplicate_history(
    publish, monkeypatch
):
    run, tmp_path = publish
    previous = run(POOL_A)
    real_atomic_write = pull._atomic_write
    failed = False

    def fail_latest_once(path, payload, *, mode=0o644):
        nonlocal failed
        if path == pull.OUT and not failed:
            failed = True
            raise OSError("injected latest replacement failure")
        return real_atomic_write(path, payload, mode=mode)

    monkeypatch.setattr(pull, "_atomic_write", fail_latest_once)
    with pytest.raises(OSError, match="latest replacement"):
        run(POOL_B)

    pending_path = tmp_path / "baselines" / ".pending-publication.json"
    transaction = json.loads(pending_path.read_text(encoding="utf-8"))
    intended = transaction["latest"]
    assert len(_history(tmp_path)) == 2, "history landed before latest failed"
    assert json.loads((tmp_path / "bleedthrough-latest.json").read_text()) == previous

    probed = []
    recovered = run(POOL_B, observed_targets=probed)

    assert probed == [], "recovery must not probe over the pending transition"
    assert recovered == intended
    assert len(_history(tmp_path)) == 2, "replay must recognize the committed row"
    assert not pending_path.exists()


def test_public_event_keeps_coarse_scope_and_semantics_without_target_identity(publish):
    run, tmp_path = publish
    run(POOL_A)
    moved = run(POOL_B)

    rotations = [event for event in moved["events"] if event["kind"] == "pool_rotation"]
    assert rotations
    assert len(rotations) == 1, "private targets must not duplicate one public event"
    assert {event["vantage"] for event in rotations} == {"CN-HA/AS4134"}
    assert {event["detail"] for event in rotations} == {"forged-IP pool rotated"}
    assert {event["severity"] for event in rotations} == {"low"}
    assert all("target_id" not in event for event in rotations), (
        "an unkeyed digest of IPv4 space would be reversible by enumeration"
    )

    public_bytes = (tmp_path / "bleedthrough-latest.json").read_text(
        encoding="utf-8"
    ) + (tmp_path / "bleedthrough-history.jsonl").read_text(encoding="utf-8")
    for address in (*TARGET_IPS, *POOL_A, *POOL_B):
        assert address not in public_bytes


def test_public_artifacts_discard_hostile_event_and_operator_metadata(
    publish, monkeypatch
):
    run, tmp_path = publish
    malicious = (
        "admin@example.com /Users/operator/private /var/lib/palimpsest/secret "
        "198.51.100.77 198%2E51%2E100%2E77 %2Fhome%2Foperator%2Fstate"
    )
    monkeypatch.setattr(pull, "VANTAGE_KIND", malicious)
    monkeypatch.setattr(pull, "VANTAGE_COUNTRY", "DE/admin@example.com")

    def hostile_observe(_store, fingerprint):
        # Collection events are deliberately treated as private/untrusted at the writer.
        return ApparatusEvent(
            POOL_ROTATION,
            f"{fingerprint.vantage_tag.split('@', 1)[0]}@CN-HA/AS3325256781",
            malicious,
            {"private_path": "/home/operator/state"},
            {"host": "198.51.100.77"},
        )

    monkeypatch.setattr(pull.FleetBaselineStore, "observe", hostile_observe)
    reading = run(POOL_A)

    assert reading["events"]
    assert {event["vantage"] for event in reading["events"]} == {"CN-HA"}
    assert {event["detail"] for event in reading["events"]} == {
        "forged-IP pool rotated"
    }
    assert reading["provenance"]["vantage_kind"] == "controlled external VPS"
    assert reading["provenance"]["vantage_country"] is None

    public_bytes = (tmp_path / "bleedthrough-latest.json").read_text(
        encoding="utf-8"
    ) + (tmp_path / "bleedthrough-history.jsonl").read_text(encoding="utf-8")
    forbidden = (
        "admin@example.com",
        "/Users/operator/private",
        "/var/lib/palimpsest/secret",
        "/home/operator/state",
        "%2Fhome%2Foperator%2Fstate",
        "198.51.100.77",
        "198%2E51%2E100%2E77",
        "AS3325256781",
        *TARGET_IPS,
        *POOL_A,
    )
    for secret in forbidden:
        assert secret not in public_bytes


def test_exported_deployment_uses_a_validated_root_owned_commit_receipt(
    tmp_path, monkeypatch
):
    receipt = tmp_path / "deployed-commit"
    receipt.write_text("b" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(pull, "ROOT", str(tmp_path / "export-without-git"))
    monkeypatch.setattr(pull, "DEPLOYED_COMMIT_FILE", str(receipt))

    assert pull._code_version() == "b" * 40

    receipt.write_text("not-a-commit\n", encoding="utf-8")
    assert pull._code_version() is None


@pytest.mark.parametrize(
    "unsafe_probe",
    [
        "198.51.100.77",
        "admin@example.com",
        "/Users/operator/private/probe",
        "https://example.com/probe?host=198.51.100.77",
        "198%2E51%2E100%2E77.example",
    ],
)
def test_unsafe_probe_identity_is_rejected_before_collection(
    publish, monkeypatch, unsafe_probe
):
    run, tmp_path = publish
    targets = tmp_path / "targets.json"
    payload = json.loads(targets.read_text(encoding="utf-8"))
    payload["probe"]["domain"] = unsafe_probe
    targets.write_text(json.dumps(payload), encoding="utf-8")

    called = False

    def transport_must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(pull, "_udp_transport", transport_must_not_run)
    pull.main()

    assert called is False
    assert not (tmp_path / "bleedthrough-latest.json").exists()
    assert not (tmp_path / "bleedthrough-history.jsonl").exists()


def test_last_changed_at_survives_a_file_written_before_the_field_existed(publish):
    """Backfill path: the published file predates last_changed_at, so the previous
    generated_at is the honest answer for when the fleet last moved."""
    run, tmp_path = publish
    first = run(POOL_A)

    legacy = dict(first)
    legacy.pop("last_changed_at")
    with open(tmp_path / "bleedthrough-latest.json", "w", encoding="utf-8") as f:
        json.dump(legacy, f)

    second = run(POOL_A)
    assert second["last_changed_at"] == first["generated_at"]


def test_a_silent_round_still_abstains(publish):
    """The heartbeat is for rounds that produced a reading. A round where no target
    injected has measured nothing — the channel may be down or the list stale — and
    it must leave no file behind rather than republish a hollow board."""
    run, tmp_path = publish

    assert run(()) is None  # no forged answers anywhere
    assert not (tmp_path / "bleedthrough-latest.json").exists()
    assert _history(tmp_path) == []


def test_a_silent_round_does_not_overwrite_a_good_reading(publish):
    """And when a reading is already published, an abstaining round must not touch
    it — neither to refresh its timestamp nor to blank it."""
    run, tmp_path = publish
    good = run(POOL_A)

    assert run(()) == good, (
        "an abstaining round must leave the last reading exactly as it was"
    )
    assert len(_history(tmp_path)) == 1
