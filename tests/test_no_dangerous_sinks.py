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
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # a mention in a comment is documentation, not a sink
            if _SINKS.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "dangerous execution sink(s) introduced:\n" + "\n".join(offenders)
