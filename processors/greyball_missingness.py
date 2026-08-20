"""Greyball missingness calibration — distinguish eight processes, or stay silent.

Before Palimpsest interprets real observations it must show, offline, that it
can tell these cases apart:

1. random deletion
2. topic-selective deletion
3. cascade deletion
4. ranking suppression
5. temporary outage
6. login-wall conversion
7. rate limiting
8. burst deletion during an event

If it cannot, it must not emit a censorship label. The discriminator uses only
observables (control fate, topic concentration, graph order, HTTP status,
recovery, rank-with-presence). Ground-truth labels are for scoring, never for
classification. Missing is not censorship. ``confirmed_removal`` is withheld.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PACK = ROOT / "config" / "greyball_missingness_cases.json"
SCHEMA_VERSION = "palimpsest-greyball-missingness.v1"
METHOD_VERSION = 1

CASES = (
    "random_deletion",
    "topic_selective_deletion",
    "cascade_deletion",
    "ranking_suppression",
    "temporary_outage",
    "login_wall_conversion",
    "rate_limiting",
    "burst_deletion_during_event",
)

CENSORSHIP_LABEL = "confirmed_removal"


def _post(i: int, topic: str, *, parent: int | None = None) -> dict[str, Any]:
    return {
        "id": f"p{i}",
        "topic": topic,
        "parent": None if parent is None else f"p{parent}",
        "present": True,
        "rank": i + 1,
        "http_status": 200,
        "recovered": False,
        "t": i,
    }


def generate_world(case: str, *, seed: int = 0, n: int = 40) -> dict[str, Any]:
    """One synthetic panel. Treatment topic ``T``, control topic ``C``."""

    rng = random.Random(seed)
    posts = [_post(i, "T" if i < n // 2 else "C", parent=(i - 1 if 0 < i < n // 2 else None)) for i in range(n)]
    event_t = n // 3
    if case == "random_deletion":
        doomed = set(rng.sample(range(n), n // 5))
        for i in doomed:
            posts[i]["present"] = False
            posts[i]["http_status"] = 404
    elif case == "topic_selective_deletion":
        for post in posts:
            if post["topic"] == "T":
                post["present"] = False
                post["http_status"] = 404
    elif case == "cascade_deletion":
        t_ids = [i for i, post in enumerate(posts) if post["topic"] == "T"]
        wave = t_ids[: max(3, len(t_ids) // 2)]
        for i in wave:
            posts[i]["present"] = False
            posts[i]["http_status"] = 404
    elif case == "ranking_suppression":
        for post in posts:
            if post["topic"] == "T":
                post["rank"] = post["rank"] + 50
                post["present"] = True
                post["http_status"] = 200
    elif case == "temporary_outage":
        for post in posts:
            post["http_status"] = 503
            post["present"] = False
            post["recovered"] = True
            post["t_fail"] = event_t
            post["t_recover"] = event_t + 3
    elif case == "login_wall_conversion":
        for post in posts:
            post["http_status"] = 403
            post["present"] = False
            post["body"] = "请登录 passport.example.com"
    elif case == "rate_limiting":
        for post in posts:
            post["http_status"] = 429
            post["present"] = True
            post["retry_after"] = 30
    elif case == "burst_deletion_during_event":
        for post in posts:
            if post["topic"] == "T" and abs(post["t"] - event_t) <= 3:
                post["present"] = False
                post["http_status"] = 404
    else:
        raise ValueError(f"unknown synthetic case {case!r}")
    return {
        "case": case,
        "posts": posts,
        "control_topic": "C",
        "treatment_topic": "T",
        "event_t": event_t,
    }


def load_fixture_pack(path: Path | str = FIXTURE_PACK) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rates(posts: list[dict[str, Any]], topic: str) -> float:
    group = [p for p in posts if p["topic"] == topic]
    if not group:
        return 0.0
    gone = sum(1 for p in group if not p["present"])
    return gone / len(group)


def _statuses(posts: list[dict[str, Any]]) -> set[int]:
    return {int(p["http_status"]) for p in posts if isinstance(p.get("http_status"), int)}


def _prefix_wave(posts: list[dict[str, Any]]) -> bool:
    """Gone T ids are a prefix of the T chain — a cascade, not a mid-window burst."""

    t_ids = [int(p["id"][1:]) for p in posts if p["topic"] == "T"]
    gone = [int(p["id"][1:]) for p in posts if p["topic"] == "T" and not p["present"]]
    if len(gone) < 3 or not t_ids:
        return False
    gone_sorted = sorted(gone)
    prefix = t_ids[: len(gone_sorted)]
    rate = len(gone_sorted) / len(t_ids)
    return gone_sorted == prefix and 0.2 <= rate <= 0.85


def _mid_burst(posts: list[dict[str, Any]]) -> bool:
    gone_t = [p["t"] for p in posts if p["topic"] == "T" and not p["present"]]
    if len(gone_t) < 3:
        return False
    return (max(gone_t) - min(gone_t)) <= 8 and min(gone_t) > 2 and not _prefix_wave(posts)


def classify_world(world: Mapping[str, Any]) -> dict[str, Any]:
    """Observables only. Does not read ``world['case']``."""

    posts = list(world.get("posts") or [])
    t_rate = _rates(posts, "T")
    c_rate = _rates(posts, "C")
    statuses = _statuses(posts)
    recovered = all(p.get("recovered") for p in posts) and posts
    present_t = [p for p in posts if p["topic"] == "T" and p.get("present")]
    ranks_t = [p.get("rank") for p in present_t]
    ranks_c = [p.get("rank") for p in posts if p["topic"] == "C" and p.get("present")]
    bodies = " ".join(str(p.get("body") or "") for p in posts)
    prefix = _prefix_wave(posts)
    burst = _mid_burst(posts)

    label: str
    visibility: str | None
    if 429 in statuses and all(p.get("http_status") == 429 for p in posts):
        label = "rate_limiting"
        visibility = "rate_limit"
    elif recovered and (503 in statuses or 502 in statuses or 500 in statuses):
        label = "temporary_outage"
        visibility = "outage"
    elif ("请登录" in bodies or 403 in statuses) and t_rate > 0.8 and c_rate > 0.8:
        label = "login_wall_conversion"
        visibility = "login_wall"
    elif present_t and ranks_t and ranks_c and min(ranks_t) > max(ranks_c) and t_rate == 0 and c_rate == 0:
        label = "ranking_suppression"
        visibility = "ranking_suppression"
    elif t_rate > 0.9 and c_rate < 0.15:
        label = "topic_selective_deletion"
        visibility = "visibility_anomaly"
    elif prefix and c_rate < 0.15:
        label = "cascade_deletion"
        visibility = "visibility_anomaly"
    elif burst and c_rate < 0.15:
        label = "burst_deletion_during_event"
        visibility = "visibility_anomaly"
    elif 0.05 < t_rate < 0.6 and abs(t_rate - c_rate) < 0.25:
        label = "random_deletion"
        visibility = "visibility_anomaly"
    else:
        label = "unknown"
        visibility = None

    return {
        "predicted_case": label,
        "visibility_label": visibility,
        "censorship_label": None,
        "observables": {
            "treatment_gone_rate": round(t_rate, 4),
            "control_gone_rate": round(c_rate, 4),
            "http_statuses": sorted(statuses),
            "prefix_wave": prefix,
            "burst": burst,
            "recovered": bool(recovered),
        },
    }


def run_calibration(
    *,
    seed: int = 0,
    fixture_pack: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score the eight-case fixture pack. One misclassification fails closed."""

    pack = dict(fixture_pack) if fixture_pack is not None else (
        load_fixture_pack() if FIXTURE_PACK.exists() else None
    )
    distinguished: dict[str, bool] = {}
    predictions: dict[str, str] = {}
    labels: dict[str, str | None] = {}
    worlds: list[Mapping[str, Any]]
    if pack and isinstance(pack.get("cases"), list) and pack["cases"]:
        worlds = list(pack["cases"])
    else:
        worlds = [generate_world(case, seed=seed + i * 17) for i, case in enumerate(CASES)]
    if len(worlds) != 8:
        raise ValueError("greyball missingness fixture pack must contain eight cases")
    for world in worlds:
        truth = str(world.get("case") or "")
        # Classifier never receives the ground-truth key.
        observables = {k: v for k, v in world.items() if k != "case"}
        result = classify_world(observables)
        predictions[truth] = result["predicted_case"]
        labels[truth] = result["visibility_label"]
        distinguished[truth] = result["predicted_case"] == truth
        if result["predicted_case"] != truth:
            distinguished[truth] = False

    all_ok = all(distinguished.get(case) for case in CASES) and set(distinguished) == set(CASES)
    predicted_set = set(predictions.values())
    collision = len(predicted_set) < len(CASES)
    return {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "cases": list(CASES),
        "distinguished": distinguished,
        "predictions": predictions,
        "visibility_labels": labels,
        "all_distinguished": all_ok,
        "may_emit_censorship_label": False,
        "censorship_label_emitted": None,
        "collision": collision,
        "note": (
            "If all_distinguished is false, Palimpsest must not emit a "
            "censorship label. Even when true, this harness emits only "
            "visibility_anomaly / login_wall / rate_limit / outage / "
            "ranking_suppression — never confirmed_removal from synthetic data. "
            "Missing is not censorship."
        ),
    }


def censorship_label_if_calibrated(calibration: Mapping[str, Any]) -> str | None:
    """Fail closed: synthetic success still does not mint a censorship label."""

    if not calibration.get("all_distinguished"):
        return None
    if calibration.get("may_emit_censorship_label"):
        return None
    return None
