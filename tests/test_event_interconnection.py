"""Named-key fat-object interconnection: join on exact keys, miss abstains."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import event_analysis, event_interconnection
from tests.test_event_analysis_v2 import EVENT_ID, OFFICIAL_URL, _event, _feed, _wire


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests/fixtures/peer-warehouse"


def _warehouses(**overrides: dict | None) -> dict[str, dict | None]:
    loaded = {slot: None for slot in event_interconnection.SLOT_IDS}
    loaded.update(overrides)
    return loaded


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_same_host_official_and_greatfire_join() -> None:
    official = _load_fixture("official-first-seen-warehouse.json")
    greatfire = _load_fixture("greatfire-warehouse.json")
    event = _event()
    block = event_interconnection.build_interconnection(
        event,
        _warehouses(**{"official-first-seen": official, "greatfire": greatfire}),
    )

    joined = {row["peer_id"]: row for row in block["peers"] if row["status"] == "joined"}
    assert set(joined) >= {"official-first-seen", "greatfire"}
    assert "host" in joined["official-first-seen"]["join_keys"]
    assert "host" in joined["greatfire"]["join_keys"]
    assert joined["greatfire"]["citation"] == "GreatFire, 2026-08-20"
    assert joined["official-first-seen"]["citation"] == "official-first-seen, 2026-08-20"
    assert joined["greatfire"]["count"] == 12
    assert joined["greatfire"]["denominator_label"] == "GreatFire probe set"
    assert joined["official-first-seen"]["count"] == 1
    assert joined["greatfire"]["denominator_value"] != joined["official-first-seen"]["denominator_value"]
    assert block["meets_quality_bar"] is False
    assert block["independent_source_groups"] == 1
    assert "exact host" in joined["greatfire"]["why_joined"]


def test_same_term_board_and_cdt_join() -> None:
    cdt = _load_fixture("cdt-warehouse.json")
    board = _load_fixture("public-board-warehouse.json")
    event = _event()
    block = event_interconnection.build_interconnection(
        event, _warehouses(cdt=cdt, **{"public-board": board})
    )

    joined = {row["peer_id"]: row for row in block["peers"] if row["status"] == "joined"}
    assert set(joined) >= {"cdt", "public-board"}
    assert joined["cdt"]["join_keys"] == ["term"]
    assert joined["public-board"]["join_keys"] == ["term"]
    assert joined["cdt"]["citation"] == "China Digital Times, 2026-08-20"
    assert joined["public-board"]["citation"] == "public board, 2026-08-20"
    assert "nbs releases july figures" in joined["cdt"]["why_joined"]


def test_miss_without_an_exact_key_abstains() -> None:
    stranger = event_interconnection.warehouse_fixture(
        "greatfire",
        records=[
            event_interconnection.peer_record(
                "other-host",
                hosts=["example.net"],
                observed_at="2026-08-20T04:00:00Z",
                count=3,
                count_label="GreatFire blocked samples",
                denominator_label="GreatFire probe set",
                denominator_value=9,
            )
        ],
    )
    block = event_interconnection.build_interconnection(
        _event(), _warehouses(greatfire=stranger)
    )

    greatfire = next(row for row in block["peers"] if row["peer_id"] == "greatfire")
    assert greatfire["status"] == "skipped"
    assert greatfire["skip_reason"] == "no_key"
    assert "no exact" in greatfire["why_skipped"]
    assert block["joined_count"] == 0
    assert block["meets_quality_bar"] is False


def test_same_host_outside_the_24h_window_is_not_a_story() -> None:
    late = event_interconnection.warehouse_fixture(
        "greatfire",
        records=[
            event_interconnection.peer_record(
                "late-host",
                hosts=["stats.gov.cn"],
                observed_at="2026-08-22T06:00:00Z",
                count=12,
                count_label="GreatFire blocked samples",
                denominator_label="GreatFire probe set",
                denominator_value=40,
            )
        ],
    )
    block = event_interconnection.build_interconnection(
        _event(), _warehouses(greatfire=late)
    )

    greatfire = next(row for row in block["peers"] if row["peer_id"] == "greatfire")
    assert greatfire["status"] == "skipped"
    assert greatfire["skip_reason"] == "window_missed"
    assert "window" in greatfire["why_skipped"]
    assert "cross-day" in greatfire["why_skipped"]
    assert block["joined_count"] == 0


def test_silent_and_warming_up_warehouses_are_recorded() -> None:
    silent = event_interconnection.warehouse_fixture("ooni", status="silent")
    warming = event_interconnection.warehouse_fixture("bleedthrough", status="warming_up")
    block = event_interconnection.build_interconnection(
        _event(), _warehouses(ooni=silent, bleedthrough=warming)
    )
    by_id = {row["peer_id"]: row for row in block["peers"] if row["record_id"] is None}
    assert by_id["ooni"]["skip_reason"] == "silent"
    assert by_id["bleedthrough"]["skip_reason"] == "warming_up"
    assert by_id["wayback"]["skip_reason"] == "silent"
    assert all(row["status"] == "skipped" for row in block["peers"])


def test_event_analysis_cites_peer_name_and_date_without_collapsing_denominators() -> None:
    official = _load_fixture("official-first-seen-warehouse.json")
    greatfire = _load_fixture("greatfire-warehouse.json")
    event = _event()
    analysis = event_analysis.build_event_analysis(
        event,
        wire=_wire(event),
        feed=_feed(),
        peer_warehouses=_warehouses(
            **{"official-first-seen": official, "greatfire": greatfire}
        ),
    )

    event_analysis.validate_event_analysis(analysis, event=event)
    blob = " ".join(
        [analysis["position"]]
        + [item["text"] for item in analysis["brief"]["lead"]["sentences"]]
    )
    assert "GreatFire, 2026-08-20" in blob
    assert "12 GreatFire blocked samples over 40 GreatFire probe set" in blob
    assert "1 official pages first seen over 1 official-first-seen watchlist" in blob
    assert "59.3%" not in blob
    assert analysis["interconnection"]["joined_count"] == 2
    assert analysis["publication_receipt"]["automatic_publication"] is False
    assert analysis["evidence_assessment"]["independent_groups"] == 1


def test_fixtures_and_generated_block_conform_to_public_schemas() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    warehouse_schema = json.loads(
        (ROOT / "protocol/peer-warehouse-v1.schema.json").read_text()
    )
    join_schema = json.loads(
        (ROOT / "protocol/event-interconnection-v1.schema.json").read_text()
    )
    for name in (
        "official-first-seen-warehouse.json",
        "greatfire-warehouse.json",
        "cdt-warehouse.json",
        "public-board-warehouse.json",
    ):
        jsonschema.Draft202012Validator(warehouse_schema).validate(_load_fixture(name))
    block = event_interconnection.build_interconnection(
        _event(),
        _warehouses(
            **{
                "official-first-seen": _load_fixture("official-first-seen-warehouse.json"),
                "greatfire": _load_fixture("greatfire-warehouse.json"),
                "cdt": _load_fixture("cdt-warehouse.json"),
                "public-board": _load_fixture("public-board-warehouse.json"),
            }
        ),
    )
    jsonschema.Draft202012Validator(join_schema).validate(block)


def test_asn_joins_only_when_both_sides_carry_the_same_asn() -> None:
    event = _event()
    event["asns"] = [4808]
    bleed = event_interconnection.warehouse_fixture(
        "bleedthrough",
        records=[
            event_interconnection.peer_record(
                "bt-as4808",
                asns=[4808],
                observed_at="2026-08-20T03:00:00Z",
                count=4,
                count_label="injector vantage rows",
                denominator_label="Bleedthrough vantage set",
                denominator_value=4,
            )
        ],
    )
    block = event_interconnection.build_interconnection(
        event, _warehouses(bleedthrough=bleed)
    )
    joined = next(row for row in block["peers"] if row["peer_id"] == "bleedthrough")
    assert joined["status"] == "joined"
    assert joined["join_keys"] == ["asn"]
    assert "AS4808" in joined["why_joined"]


def test_url_path_join_is_exact() -> None:
    official = event_interconnection.warehouse_fixture(
        "official-first-seen",
        records=[
            event_interconnection.peer_record(
                "official-path",
                hosts=["stats.gov.cn"],
                url_paths=["/sj/zxfb/202608/t20260820_1.html"],
                observed_at="2026-08-20T02:00:00Z",
                count=1,
                count_label="official pages first seen",
                denominator_label="official-first-seen watchlist",
                denominator_value=1,
            )
        ],
    )
    block = event_interconnection.build_interconnection(
        _event(), _warehouses(**{"official-first-seen": official})
    )
    joined = next(row for row in block["peers"] if row["peer_id"] == "official-first-seen")
    assert joined["status"] == "joined"
    assert "url_path" in joined["join_keys"]


def test_live_ooni_projection_exposes_hosts_without_inventing_a_join(
    tmp_path: Path,
) -> None:
    from core import peer_warehouse_live

    (tmp_path / "ooni-gfw-latest.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-20T07:12:09Z",
                "top_blocked": [
                    {
                        "domain": "www.hrw.org",
                        "anomaly_count": 38,
                        "measurement_count": 44,
                        "completed_measurement_count": 38,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    projected = peer_warehouse_live.project_live_warehouses(tmp_path)
    assert projected["ooni"]["status"] == "live"
    assert projected["ooni"]["peers"][0]["hosts"] == ["hrw.org"]
    loaded = event_interconnection.load_optional_peer_warehouses(tmp_path)
    event = _event()
    block = event_interconnection.build_interconnection(event, loaded)
    ooni = next(row for row in block["peers"] if row["peer_id"] == "ooni")
    assert ooni["status"] == "skipped"
    assert ooni["skip_reason"] == "no_key"


def test_wrong_schema_file_in_warehouse_name_stays_silent(tmp_path: Path) -> None:
    (tmp_path / "greatfire-warehouse.json").write_text(
        json.dumps({"schema_version": "palimpsest-peer-context.v1", "hosts": []}),
        encoding="utf-8",
    )
    loaded = event_interconnection.load_optional_peer_warehouses(
        tmp_path, project_live=False
    )
    assert loaded["greatfire"] is None


def test_loader_leaves_missing_slots_silent(tmp_path: Path) -> None:
    loaded = event_interconnection.load_optional_peer_warehouses(
        tmp_path, project_live=False
    )
    assert set(loaded) == set(event_interconnection.SLOT_IDS)
    assert all(value is None for value in loaded.values())
    (tmp_path / "greatfire-warehouse.json").write_text(
        (FIXTURES / "greatfire-warehouse.json").read_text(), encoding="utf-8"
    )
    loaded = event_interconnection.load_optional_peer_warehouses(
        tmp_path, project_live=False
    )
    assert loaded["greatfire"]["warehouse_id"] == "greatfire"
    assert loaded["ooni"] is None
