"""CensorLab-style predeploy stress lane for source promotion.

Each enabled source is run through deterministic, no-network stress checks before
it is promoted into the continuous collection schedule.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from censorwatch.classifier import classify_state
from censorwatch.config import CensorwatchSettings, get_settings
from censorwatch.registry import enabled_sources, get_collector
from censorwatch.interfaces import LivenessState

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _source_requires_proxy(source_name: str) -> bool:
    return source_name in {"xueqiu", "weibo_search"}


def _simulate_classifier_checks(source_name: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    st, _ = classify_state(200, "请完成安全验证后继续访问")
    checks.append(CheckResult("captcha_wall_unknown", st == LivenessState.UNKNOWN, f"state={st}"))
    st, _ = classify_state(404, "")
    checks.append(CheckResult("not_found_is_gone", st == LivenessState.GONE, f"state={st}"))

    # Source-specific deletion marker route test.
    marker = "该帖子可能已被删除" if source_name == "eastmoney_guba" else "根据相关法律法规"
    st, _ = classify_state(200, "正文内容..." + marker, extra_markers=(marker,))
    checks.append(CheckResult("source_marker_is_gone", st == LivenessState.GONE, f"state={st}"))
    return checks


def _simulate_collector_checks(source_name: str, settings: CensorwatchSettings) -> list[CheckResult]:
    checks: list[CheckResult] = []
    collector = get_collector(source_name)
    if collector is None:
        checks.append(CheckResult("collector_load", False, "collector unavailable"))
        return checks

    checks.append(CheckResult("collector_load", True, "ok"))

    # Liveness route quality gate: must expose at least one control post.
    try:
        controls = collector.control_posts()
    except Exception as e:  # pragma: no cover
        controls = []
        checks.append(CheckResult("control_posts", False, f"error:{type(e).__name__}"))
    else:
        checks.append(CheckResult("control_posts", len(controls) > 0, f"count={len(controls)}"))

    # Parser/validator route smoke test.
    try:
        df = pd.DataFrame([{"post_id": "smoke", "url": "https://example.com/1", "full_text": "test"}])
        ok = bool(collector.validate(df))
        checks.append(CheckResult("validate_smoke", ok, "validate returned truthy"))
    except Exception as e:
        checks.append(CheckResult("validate_smoke", False, f"{type(e).__name__}:{e}"))

    # Proxy route gate for known anti-bot sources.
    if _source_requires_proxy(source_name):
        checks.append(
            CheckResult(
                "proxy_ready",
                bool(settings.proxy_url),
                "proxy configured" if settings.proxy_url else "missing CENSORWATCH_PROXY_URL/HTTPS_PROXY",
            )
        )
    else:
        checks.append(CheckResult("proxy_ready", True, "not required"))

    return checks


def evaluate_source(source_name: str, settings: CensorwatchSettings | None = None) -> dict:
    settings = settings or get_settings()
    checks = []
    checks.extend(_simulate_classifier_checks(source_name))
    checks.extend(_simulate_collector_checks(source_name, settings))
    passed = all(c.ok for c in checks)
    return {
        "source": source_name,
        "passed": passed,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }


def run_emulation(settings: CensorwatchSettings | None = None, now: datetime | None = None) -> dict:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    srcs = enabled_sources()
    results = [evaluate_source(s, settings=settings) for s in srcs]
    promoted = [r["source"] for r in results if r["passed"]]
    blocked = [r["source"] for r in results if not r["passed"]]
    payload = {
        "generated_at": now.isoformat(),
        "mode": "strict" if settings.promotion_gate_enabled else "advisory",
        "total_enabled_sources": len(srcs),
        "promoted_sources": promoted,
        "blocked_sources": blocked,
        "results": results,
    }

    out_dir = Path("./data/censorwatch/emulation")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    try:
        import redis

        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        r.set("censorwatch:emulation:latest", json.dumps(payload, ensure_ascii=False), ex=3600)
        r.close()
    except Exception:
        pass

    return {
        "status": "ok",
        "mode": payload["mode"],
        "promoted_sources": promoted,
        "blocked_sources": blocked,
    }


def promoted_sources_for_schedule(settings: CensorwatchSettings | None = None) -> list[str]:
    """Sources eligible for scheduling under the current promotion policy."""
    settings = settings or get_settings()
    srcs = enabled_sources()
    if not settings.promotion_gate_enabled:
        return srcs
    promoted = []
    for s in srcs:
        if evaluate_source(s, settings=settings)["passed"]:
            promoted.append(s)
    return promoted

