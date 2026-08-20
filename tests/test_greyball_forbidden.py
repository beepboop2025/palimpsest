"""Greyball modules have no path for the forbidden techniques."""

from __future__ import annotations

import pathlib
import re

from collectors.browser_capture import refuse_history_export
from collectors.multi_node_panel import rotate_identity, rotate_residential_path
from collectors.public_endpoint import probe_hidden_object
from core.observer_class import FORBIDDEN_TECHNIQUES, ForbiddenTechniqueError, refuse_forbidden
from processors.search_differential import discover_blocked_terms
from processors.synthetic_calibration import run_calibration


ROOT = pathlib.Path(__file__).resolve().parent.parent
GREYBALL_PATHS = [
    ROOT / "core/visibility_event.py",
    ROOT / "core/observer_class.py",
    ROOT / "collectors/browser_capture.py",
    ROOT / "collectors/donation_ingest.py",
    ROOT / "collectors/public_endpoint.py",
    ROOT / "collectors/multi_node_panel.py",
    ROOT / "collectors/public_account_panel.py",
    ROOT / "collectors/deletion_report_agg.py",
    ROOT / "processors/search_differential.py",
    ROOT / "processors/event_cluster_sidecar.py",
    ROOT / "processors/synthetic_calibration.py",
]

_IMPLEMENTED = re.compile(
    r"def (solve_captcha|rotate_proxy|scrape_login|infiltrate_group|"
    r"load_leaked_db|deanonymize|discover_blocked)\b"
)


def test_forbidden_list_is_complete():
    required = {
        "captcha_solving",
        "stolen_credentials",
        "shared_credentials",
        "private_group_infiltration",
        "leaked_social_db",
        "fake_account_network",
        "residential_proxy_rotation",
        "login_wall_scrape",
        "covert_in_china_collection",
        "deanonymization",
        "identity_linkage",
        "automated_blocked_term_discovery",
    }
    assert required <= FORBIDDEN_TECHNIQUES


def test_refuse_gates_are_hard_fails():
    for fn in (
        refuse_history_export,
        rotate_identity,
        rotate_residential_path,
        probe_hidden_object,
        discover_blocked_terms,
        lambda: refuse_forbidden("stolen_credentials"),
        lambda: refuse_forbidden("covert_in_china_collection"),
    ):
        try:
            fn()
        except ForbiddenTechniqueError:
            pass
        else:
            raise AssertionError(f"{fn} did not refuse")


def test_greyball_modules_do_not_implement_forbidden_helpers():
    for path in GREYBALL_PATHS:
        text = path.read_text(encoding="utf-8")
        assert _IMPLEMENTED.search(text) is None, path
        assert "PALIMPSEST_LIVE=1" not in text


def test_calibration_does_not_mint_a_censorship_label():
    assert run_calibration()["censorship_label_emitted"] is None
