"""Least-privilege and supply-chain contract for CodeQL analysis."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
ACTION_SHA = "db488ddef3bf6cb639b32c2e9a7c0a7ea8271d28"
CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def test_codeql_scans_change_and_time_boundaries_without_privileged_triggers():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in text
    assert "pull_request_target" not in text
    assert "push:" in text and "- main" in text
    assert "schedule:" in text and 'cron: "31 3 * * 2"' in text
    assert "timeout-minutes: 30" in text
    assert "cancel-in-progress: true" in text


def test_codeql_has_only_the_permissions_needed_to_upload_findings():
    text = WORKFLOW.read_text(encoding="utf-8")
    permissions = text[text.index("permissions:") : text.index("concurrency:")]

    assert permissions.splitlines() == [
        "permissions:",
        "  contents: read",
        "  security-events: write",
        "",
    ]
    assert "contents: write" not in text
    assert "actions: write" not in text
    assert "packages: write" not in text
    assert "id-token: write" not in text
    assert "secrets." not in text


def test_codeql_actions_are_exactly_pinned_and_python_scoped():
    text = WORKFLOW.read_text(encoding="utf-8")
    action_uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", text, re.MULTILINE)

    assert action_uses == [
        f"actions/checkout@{CHECKOUT_SHA}",
        f"github/codeql-action/init@{ACTION_SHA}",
        f"github/codeql-action/analyze@{ACTION_SHA}",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in action_uses)
    assert "languages: python" in text
    assert "build-mode: none" in text
    assert "queries: security-and-quality" in text
    assert "category: /language:python" in text
