"""Every scheduled runner that builds a governance-gated collector must actually wire the
governance in.

The kill switch is the project's one-file, no-redeploy halt: SAFETY's promise is that
setting PALIMPSEST_HALT (or touching the kill file) stops all active collection. That
promise is only real if each runner *passes* a KillSwitch into the collector it drives — a
collector constructed with `kill_switch=None` consults nothing and keeps reading straight
through a halt. The same goes for the rate ceiling and the politeness promise.

This is exactly the defect that shipped in scripts/github_refuge_pull.py and
scripts/baike_redaction_pull.py: both drove a collector whose __init__ accepts
`kill_switch` / `rate_ceiling` and neither passed either, so a CI-scheduled job outran the
halt. The bug is invisible in a normal test run (nothing raises; it just quietly does not
halt), so it is guarded structurally here instead.

Method: read the AST — no imports, no network, no execution.
  1. Discover the governance-gated collector classes: any class in collectors/ whose
     __init__ accepts BOTH kill_switch and rate_ceiling. Discovery is automatic, so a new
     gated collector is covered on the day it lands.
  2. Walk scripts/*_pull.py for constructions of those classes and require both keywords.

Stdlib only.
"""
from __future__ import annotations

import ast
import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTORS = os.path.join(ROOT, "collectors")
SCRIPTS = os.path.join(ROOT, "scripts")

GOVERNANCE_KWARGS = ("kill_switch", "rate_ceiling")


def _parse(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _accepts_governance(fn: ast.FunctionDef) -> bool:
    args = fn.args
    names = {a.arg for a in list(args.args) + list(args.kwonlyargs)}
    return all(k in names for k in GOVERNANCE_KWARGS)


def gated_collector_classes() -> dict:
    """{class_name: relative_path} for every collector class whose __init__ takes both
    governance knobs. That signature IS the declaration "I reach the network"."""
    found = {}
    for path in sorted(glob.glob(os.path.join(COLLECTORS, "*.py"))):
        try:
            tree = _parse(path)
        except SyntaxError:  # pragma: no cover - a broken collector fails its own tests
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if (isinstance(item, ast.FunctionDef) and item.name == "__init__"
                        and _accepts_governance(item)):
                    found[node.name] = os.path.relpath(path, ROOT)
    return found


def _call_name(call: ast.Call) -> str:
    """Bare name of the callee: `Foo(...)` -> 'Foo', `mod.Foo(...)` -> 'Foo'."""
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def gated_constructions() -> list:
    """[(script_rel_path, class_name, lineno, {kwargs})] for every construction of a gated
    collector class inside a scheduled *_pull.py runner."""
    gated = gated_collector_classes()
    out = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "*_pull.py"))):
        tree = _parse(path)
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in gated:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            has_splat = any(kw.arg is None for kw in node.keywords)
            out.append((rel, name, node.lineno, kwargs, has_splat))
    return out


def test_discovery_is_not_vacuous():
    """Guard the guard: if the AST walk ever stops finding gated classes or their uses, the
    assertions below would pass on an empty list and the check would rot silently."""
    gated = gated_collector_classes()
    assert len(gated) >= 5, f"expected the known governance-gated collectors, found {gated}"
    assert "GitHubRefugeCollector" in gated
    assert "BaikeRedactionWatch" in gated
    assert "WaybackVantagePoint" in gated

    constructions = gated_constructions()
    assert constructions, "no gated collector construction found in any scripts/*_pull.py"


@pytest.mark.parametrize("kwarg", GOVERNANCE_KWARGS)
def test_every_pull_runner_wires_governance(kwarg):
    """A runner that builds a network-reaching collector must hand it the halt and the
    rate ceiling. Without the kill switch, PALIMPSEST_HALT does not stop a scheduled job."""
    missing = [
        f"{rel}:{lineno} {name}(...) is missing {kwarg}="
        for rel, name, lineno, kwargs, has_splat in gated_constructions()
        if kwarg not in kwargs and not has_splat
    ]
    assert not missing, (
        "governance-gated collectors constructed without governance:\n  "
        + "\n  ".join(missing)
    )
