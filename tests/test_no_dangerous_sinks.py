"""Guard test: no module ever executes fetched data.

Locks in the input-safety audit. A hostile server can only ever hand us bytes; those bytes
must never reach a code-execution sink. This test scans the source tree and fails if a
dangerous sink is introduced on a collection/processing path. Standard-library only.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED_DIRS = ["collectors", "processors", "core", "censorwatch", "api", "storage", "scripts"]

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
_ALLOWED = {
    ("scripts/anchor_roots.py", "subprocess."),
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
    assert not offenders, "dangerous execution sink(s) introduced:\n" + "\n".join(offenders)


def test_allowlisted_file_never_uses_shell_or_untrusted_argv():
    """The exemption above stays safe only while the ots call keeps a fixed argv and no
    shell. Pin that shape so a later edit cannot widen the hole quietly."""
    text = (ROOT / "scripts" / "anchor_roots.py").read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert '["ots", "stamp", stamp_path]' in text  # the one permitted invocation, verbatim
