"""Guard test: no module ever executes fetched data.

Locks in the input-safety audit. A hostile server can only ever hand us bytes; those bytes
must never reach a code-execution sink. This test scans the source tree and fails if a
dangerous sink is introduced on a collection/processing path. Standard-library only.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Every directory holding first-party code that touches the network or the data it returns.
# mcp/ (the MCP server that serves readings to model clients), demo/ (the ten-second demo the
# README tells a stranger to run) and ops/ (the independent witness) all fetch, so all three
# are in scope. ops/witness is DELIBERATELY a separate from-scratch implementation — it must
# be able to check the observatory without sharing the observatory's code — so it is scanned
# here but never refactored to import from core/.
SCANNED_DIRS = [
    "collectors",
    "processors",
    "core",
    "censorwatch",
    "api",
    "storage",
    "scripts",
    "mcp",
    "demo",
    "ops",
]

# Real call sites, not substrings of longer identifiers. `compile`/`re.compile` are fine and
# excluded; `eval(`/`exec(` as bare calls are not.
_SINKS = re.compile(
    r"(?<![\w.])(eval|exec)\s*\(|"
    r"\bpickle\.(load|loads)\s*\(|"
    r"\bmarshal\.(load|loads)\s*\(|"
    r"\bsubprocess\.|"
    r"\bos\.(system|popen)\s*\(|"
    r"(?<![\w.])__import__\s*\(|"
    r"\byaml\.load\s*\(|"
    r"shell\s*=\s*True"
)


# Narrow, justified exemptions — (file, pattern substring) pairs. Each entry must name
# WHY the sink cannot see fetched bytes. Anything not listed here still fails.
#   anchor_roots.py: invokes the OpenTimestamps client as subprocess.run(["ots", "stamp",
#   <path we constructed>]) — fixed argv, no shell, and the stamped file is written by us
#   from our own chain roots. Fetched data (the Wayback response) never reaches it.
#   verify_public_surface.py: invokes subprocess.run(["git", "-C", <repo root>, "ls-files",
#   "-z"]) to enumerate exactly the files Pages publishes. Fixed argv, no shell, and the
#   argument vector contains no runtime input at all; the only thing it reads is the tree
#   already on disk. It is a pre-publication check and never runs on a collection path.
#   push_data_commit.py: invokes only the fixed git executable, the current Python
#   interpreter with a module name constrained to scripts.<identifier>, and the fixed
#   public-surface verifier. It never places collected bytes in an argv or a shell.
#   network_lane.py: invokes only the pinned cc-downloader, the revision-bundled
#   BLEEDTHROUGH prober, and each tool's fixed --version command. Root-owned plans
#   supply bounded path arguments; fetched bytes never become argv or shell text.
#   run_duckdb_filter.py: invokes the root-owned, hash-pinned DuckDB binary with no
#   argv derived from evidence. Its stdin is deterministic SQL generated from the
#   reviewed institutional-host config and root-owned crawl plan.
_ALLOWED = {
    ("ops/common-crawl/run_duckdb_filter.py", "subprocess."),
    ("ops/network-lane/network_lane.py", "subprocess."),
    ("scripts/anchor_roots.py", "subprocess."),
    ("scripts/push_data_commit.py", "subprocess."),
    ("scripts/verify_public_surface.py", "subprocess."),
}


def _py_files():
    for d in SCANNED_DIRS:
        base = ROOT / d
        if base.exists():
            for p in base.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                yield p


def test_no_code_execution_sinks_on_collection_paths():
    offenders = []
    for p in _py_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = str(p.relative_to(ROOT))
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # a mention in a comment is documentation, not a sink
            m = _SINKS.search(line)
            if m and (rel, "subprocess.") in _ALLOWED and "subprocess." in m.group(0):
                continue
            if m:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "dangerous execution sink(s) introduced:\n" + "\n".join(
        offenders
    )


def test_allowlisted_file_never_uses_shell_or_untrusted_argv():
    """The exemption above stays safe only while the ots call keeps a fixed argv and no
    shell. Pin that shape so a later edit cannot widen the hole quietly."""
    text = (ROOT / "scripts" / "anchor_roots.py").read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert (
        '["ots", "stamp", stamp_path]' in text
    )  # the one permitted invocation, verbatim


def test_data_publisher_keeps_a_fixed_subprocess_boundary():
    text = (ROOT / "scripts" / "push_data_commit.py").read_text(encoding="utf-8")

    assert "shell=True" not in text
    assert '["git", *arguments]' in text
    assert '[sys.executable, "-B", "-m", module]' in text
    assert '[sys.executable, "-B", "scripts/verify_public_surface.py"]' in text
    assert "MODULE_RE.fullmatch(module)" in text


def test_network_lane_keeps_fixed_no_shell_process_boundaries():
    lane = (ROOT / "ops/network-lane/network_lane.py").read_text(encoding="utf-8")
    local_filter = (ROOT / "ops/common-crawl/run_duckdb_filter.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in lane and "shell=True" not in local_filter
    assert '[str(path), "--version"]' in lane
    assert (
        'command = [\n        str(downloader_path),\n        "download",'
        in lane
    )
    assert "command=[str(prober_path)]" in lane
    assert '[str(path), "--version"]' in local_filter
    assert "[str(duckdb_path)]" in local_filter
    assert "input=sql.encode(\"utf-8\")" in local_filter
