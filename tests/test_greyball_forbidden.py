"""Greyball modules have no path for the forbidden techniques."""

from __future__ import annotations

import pathlib
import re

from collectors.greyball_browser import refuse_history_export
from collectors.greyball_observers import rotate_identity, rotate_residential_path
from collectors.greyball_endpoint import probe_hidden_object
from core.observer_class import FORBIDDEN_TECHNIQUES, ForbiddenTechniqueError, refuse_forbidden
from collectors.greyball_serp import discover_blocked_terms
from processors.greyball_missingness import run_calibration


ROOT = pathlib.Path(__file__).resolve().parent.parent
GREYBALL_PATHS = [
    ROOT / "core/visibility_event.py",
    ROOT / "core/observer_class.py",
    ROOT / "collectors/greyball_browser.py",
    ROOT / "collectors/greyball_donation.py",
    ROOT / "collectors/greyball_endpoint.py",
    ROOT / "collectors/greyball_observers.py",
    ROOT / "collectors/greyball_serp.py",
    ROOT / "collectors/greyball_panel.py",
    ROOT / "collectors/public_deletion_ledgers.py",
    ROOT / "processors/event_cluster_sidecar.py",
    ROOT / "processors/greyball_missingness.py",
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
