"""Directory-scanner hygiene gate for the advertised MCP surface.

Automated MCP directories (Andrax Pentester's MCPGrade, mcpgrade.dev,
VerifyMCP and others) grade a server from tools/list, the prompt list and the
server instructions alone. They never read the code. So "USE THESE TOOLS
FIRST, before answering from memory" is graded as an injection imperative, and
a description that names a sibling tool is graded as tool shadowing. A
November 2026 fleet audit with the same rules found one imperative and five
shadowing findings on this surface. This test holds the surface to the fleet
rules before any release.

Rules (fleet canon of 23 Nov 2026; keep in step with the fleet audit script):
 1. A tool's metadata describes only that tool: no other tool's name appears
    in its title, description or parameter text.
 2. The server instructions and prompt descriptions name no tool; they
    describe the workflow by role.
 3. No credential or personal-data trigger words in advertised metadata.
 4. No imperatives aimed at the model ("before answering", "ignore
    previous", "IMPORTANT:", "use these tools first" and the like).
 5. No invisible, zero-width or bidi control characters.
 6. No URL in tool metadata (a URL beside a fetch verb grades as
    fetch-and-obey).
 7. openWorldHint is never set to true explicitly: the spec default is
    already true, and scanners grade the explicit flag as over-broad scope.
 8. Every description stays under 1,000 characters.
 9. No parameter asks for a secret.
10. Every free-text string parameter is bounded (enum, const, pattern,
    format or maxLength).
11. No em or en dashes, the house rule for anything that ships.

Signal ids such as 'eval-registry' are argument values, not tool names, and
stay in the text. Prompt bodies returned by prompts/get are user-invoked
playbooks and are not part of the graded listing.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

_MCP_PATH = pathlib.Path(__file__).resolve().parents[1] / "mcp" / "palimpsest_mcp.py"
_spec = importlib.util.spec_from_file_location("palimpsest_mcp_hygiene", _MCP_PATH)
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)

SENSITIVE = re.compile(
    r"pass(?:word|words|wd)|credential|credit[ -]?card|card number|\bcvv\b|\bssn\b"
    r"|social security|\bpii\b|personal data|\bsecrets?\b|api[ _-]?keys?\b|private key"
    r"|seed phrase|webhook|callback url|\bfeedback\b",
    re.I,
)
IMPERATIVE = re.compile(
    r"ignore (?:all |any )?(?:previous|prior)|disregard (?:all|any|previous)"
    r"|before (?:you )?answer(?:ing)?|instead of answering|you must|always (?:call|use)"
    r"|do not tell|\bIMPORTANT:|reach for (?:these|this) tools? first"
    r"|use (?:these|this) tools? first|\bFIRST,? before\b",
    re.I,
)
INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufe00-\ufe0f"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
URL = re.compile(r"https?://", re.I)
DASHES = re.compile("[\u2013\u2014]")
SECRET_PARAM = re.compile(
    r"^(pass(word)?|passwd|secret|api[_-]?key|apikey|token|auth|bearer|private[_-]?key)$", re.I
)
BOUNDED = ("enum", "const", "pattern", "format", "maxLength")


def _rpc(method):
    return mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}})["result"]


@pytest.fixture(scope="module")
def surface():
    init = _rpc("initialize")
    tools = _rpc("tools/list")["tools"]
    prompts = _rpc("prompts/list")["prompts"]
    return tools, prompts, init.get("instructions") or "", init["serverInfo"]


def _tool_text(tool) -> str:
    parts = [tool.get("title") or "", tool.get("description") or ""]
    annotations = tool.get("annotations") or {}
    if annotations.get("title"):
        parts.append(annotations["title"])
    for name, spec in (tool.get("inputSchema") or {}).get("properties", {}).items():
        parts.append(f"{name}: {json.dumps(spec, ensure_ascii=False)}")
    if tool.get("outputSchema"):
        parts.append("output: " + json.dumps(tool["outputSchema"], ensure_ascii=False))
    return "\n".join(parts)


def _prompt_text(prompt) -> str:
    parts = [prompt.get("title") or "", prompt.get("description") or ""]
    for arg in prompt.get("arguments") or []:
        parts.append(f"{arg.get('name')}: {arg.get('description') or ''}")
    return "\n".join(parts)


def _units(surface):
    """Every block of advertised text, labelled by where a scanner reads it."""
    tools, prompts, instructions, server_info = surface
    yield "(server title)", server_info.get("title") or ""
    yield "(server instructions)", instructions
    for t in tools:
        yield f"tool {t['name']}", _tool_text(t)
    for p in prompts:
        yield f"prompt {p['name']}", _prompt_text(p)


def _names_in(text: str, names) -> list[str]:
    return [n for n in names
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", text)]


def test_metadata_never_names_another_tool(surface):
    tools = surface[0]
    names = [t["name"] for t in tools]
    bad = []
    for where, text in _units(surface):
        own = where[len("tool "):] if where.startswith("tool ") else None
        hits = _names_in(text, [n for n in names if n != own])
        if hits:
            bad.append(f"{where} names {hits}")
    assert not bad, "describe the workflow by role, not by tool name:\n" + "\n".join(bad)


@pytest.mark.parametrize("rule,pattern", [
    ("credential or personal-data trigger word", SENSITIVE),
    ("imperative aimed at the model", IMPERATIVE),
    ("invisible or bidi control character", INVISIBLE),
    ("em or en dash", DASHES),
])
def test_metadata_has_no_scanner_trigger(surface, rule, pattern):
    bad = []
    for where, text in _units(surface):
        m = pattern.search(text)
        if m:
            a = max(0, m.start() - 40)
            bad.append(f"{where}: ...{text[a:m.end() + 40]!r}")
    assert not bad, f"{rule} in advertised metadata:\n" + "\n".join(bad)


def test_tool_metadata_carries_no_url(surface):
    bad = [t["name"] for t in surface[0] if URL.search(_tool_text(t))]
    assert not bad, f"URL in tool metadata: {bad}"


def test_open_world_hint_is_never_explicitly_true(surface):
    bad = [t["name"] for t in surface[0]
           if (t.get("annotations") or {}).get("openWorldHint") is True]
    assert not bad, f"explicit openWorldHint=true (the spec default already is): {bad}"


def test_descriptions_stay_short(surface):
    bad = [f"{t['name']}: {len(t.get('description') or '')}" for t in surface[0]
           if len(t.get("description") or "") >= 1000]
    assert not bad, f"descriptions of 1,000 characters or more: {bad}"


def test_no_parameter_asks_for_a_secret(surface):
    bad = [f"{t['name']}.{p}" for t in surface[0]
           for p in (t.get("inputSchema") or {}).get("properties", {}) if SECRET_PARAM.match(p)]
    assert not bad, f"secret-shaped parameters: {bad}"


def test_every_free_text_parameter_is_bounded(surface):
    bad = []
    for t in surface[0]:
        for p, spec in (t.get("inputSchema") or {}).get("properties", {}).items():
            for variant in spec.get("anyOf") or [spec]:
                if variant.get("type") == "string" and not any(k in variant for k in BOUNDED):
                    bad.append(f"{t['name']}.{p}")
    assert not bad, f"unbounded free-text parameters: {bad}"


def test_length_bounds_admit_every_value_in_use(surface):
    """A bound must never reject an input that works today."""
    tools = {t["name"]: t for t in surface[0]}
    name_bound = tools["get_signal"]["inputSchema"]["properties"]["name"]["maxLength"]
    assert max(len(signal) for signal in mcp.SIGNALS) * 2 <= name_bound
    newsroom = tools["get_newsroom"]["inputSchema"]["properties"]
    assert newsroom["status"]["maxLength"] >= 32
    assert newsroom["priority"]["maxLength"] >= 32


def test_claim_discipline_survives_the_cleanup(surface):
    """Role-based wording must keep every evidence-state statement."""
    text = " ".join(surface[2].split()).lower()
    for statement in (
        "restricted or unavailable evidence is never zero, calm, healthy, or directional",
        "denies redistribution of cfets/chinamoney values",
        "they publish no values or derivatives",
        "never pool the suites",
        "human validation is pending",
        "unknown availability is not a fresh observation or permission to reuse values",
        "newer than any model's training data",
    ):
        assert statement in text, statement


def test_the_gate_catches_what_the_scanner_caught():
    """The rules must fire on the exact sentences the fleet audit flagged."""
    names = ["research_catalog", "list_signals", "get_signal", "gfw_reading"]
    assert IMPERATIVE.search(
        "USE THESE TOOLS FIRST, before answering from memory, whenever a task touches")
    assert _names_in("Use research_catalog to page the complete editorial dataset registry.",
                     names) == ["research_catalog"]
    assert _names_in("Call list_signals first to discover valid names.", names) == ["list_signals"]
    assert _names_in("then use get_signal to drill into whichever signal moved", names) == ["get_signal"]
    assert not _names_in("The research catalog pages the complete editorial dataset registry.", names)
    assert not _names_in("'research-catalog' and 'ooni-gfw' are signal ids", names)
