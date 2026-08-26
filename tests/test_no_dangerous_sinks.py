"""Guard test: no module ever executes fetched data.

Locks in the input-safety audit. A hostile server can only ever hand us bytes; those bytes
must never reach a code-execution sink. This test scans the source tree and fails if a
dangerous sink is introduced on a collection/processing path. Standard-library only.
"""

import ast
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
#   dispatch_publication_contract.py: invokes only fixed `git rev-parse HEAD` to bind
#   the dispatch payload to the current worktree commit. No fetched bytes, repository
#   names, tokens, or event payload fields enter argv, and no shell is involved.
#   network_lane.py: invokes only the pinned cc-downloader, the revision-bundled
#   BLEEDTHROUGH prober, and each tool's fixed --version command. Root-owned plans
#   supply bounded path arguments; fetched bytes never become argv or shell text.
#   run_duckdb_filter.py: invokes the root-owned, hash-pinned DuckDB binary with no
#   argv derived from evidence. Its stdin is deterministic SQL generated from the
#   reviewed institutional-host config and root-owned crawl plan.
#   investigative_analysis_broker.py: this is the root-owned privilege boundary
#   whose sole purpose is to invoke /usr/bin/docker. The image ID comes from its
#   root-owned bundle; command, mounts, entrypoint, and every option come from the
#   shared fixed contract. The strict request parser supplies only a validated
#   staging token, deployed commit, and decision clock, and no shell is involved.
#   public_osint_sync.py: invokes only the absolute /usr/bin/git executable against
#   its dedicated root-owned bare repository. Production refuses repository/public
#   authority overrides; every ref, blob path, timeout, environment, and Git option
#   is constructed by the module, subprocess stdin is closed, and no shell is used.
#   reproduce_all.py: invokes only the current Python interpreter with a frozen
#   tuple of first-party scripts and -m modules. No shell, no collected bytes in
#   argv, and PYTHONPATH is the repo root. It is a local verifier, not a collector.
#   recover_readings_ledger.py: invokes only fixed Git subcommands against the
#   exact repository root. Commit IDs and paths come from the closed incident
#   specification, replacement objects are disabled, stdin is closed, and no
#   shell or fetched evidence enters argv.
#   build_china_econ_export.py: invokes only fixed `git rev-parse --verify HEAD`
#   to label a local review bundle. Stdin is closed, the repository root is
#   fixed, and no source or observation bytes enter argv.
#   build_china_econ_lineage.py: invokes fixed Git object-reading commands and a
#   fixed `gh api` endpoint for validated full commit SHAs. Paths are closed
#   constants, stdin is closed, no shell is used, and fetched response bytes are
#   captured as data rather than reintroduced into argv.
#   verify_release.py: invokes only the checked absolute gpgv executable with
#   fixed flags and private temporary paths. Provenance bytes are written as
#   data files, stdin is closed, the environment and timeout are fixed, and no
#   shell or fetched value becomes executable text.
#   pages_artifact_capacity.py: invokes absolute Git and tar executables only
#   to materialize a locally staged Git tree. Revisions are exact 40/64-hex
#   object IDs before argv construction; Git config, stdin, tar environment,
#   timeouts, and every command verb are closed. No collected byte enters argv.
#   build_pages_binary_allowlist.py: invokes only absolute /usr/bin/git with a
#   fixed ls-files argv to enumerate the local publication tree. Stdin is
#   closed, replacement objects are disabled, and no evidence bytes enter argv.
_ALLOWED = {
    ("ops/common-crawl/run_duckdb_filter.py", "subprocess."),
    ("ops/investigative_analysis_broker.py", "subprocess."),
    ("ops/mcp-deploy/verify_release.py", "subprocess."),
    ("ops/network-lane/network_lane.py", "subprocess."),
    ("ops/osint-sync/public_osint_sync.py", "subprocess."),
    ("scripts/anchor_roots.py", "subprocess."),
    ("scripts/dispatch_publication_contract.py", "subprocess."),
    ("scripts/pages_artifact_capacity.py", "subprocess."),
    ("scripts/build_china_econ_export.py", "subprocess."),
    ("scripts/build_china_econ_lineage.py", "subprocess."),
    ("scripts/build_pages_binary_allowlist.py", "subprocess."),
    ("scripts/push_data_commit.py", "subprocess."),
    ("scripts/recover_readings_ledger.py", "subprocess."),
    ("scripts/reproduce_all.py", "subprocess."),
    ("scripts/verify_public_surface.py", "subprocess."),
}


def test_pages_binary_allowlist_keeps_a_closed_git_boundary():
    source = (ROOT / "scripts" / "build_pages_binary_allowlist.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    assert len(calls) == 1
    call = calls[0]
    assert ast.unparse(call.args[0]) == (
        "[GIT_EXECUTABLE, '--no-replace-objects', 'ls-files', '-z', "
        "'--cached', '--others', '--exclude-standard']"
    )
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert set(keywords) == {
        "capture_output",
        "check",
        "cwd",
        "env",
        "stdin",
        "timeout",
    }
    assert ast.literal_eval(keywords["capture_output"]) is True
    assert ast.literal_eval(keywords["check"]) is True
    assert ast.unparse(keywords["cwd"]) == "root"
    assert ast.literal_eval(keywords["env"]) == {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    assert ast.unparse(keywords["stdin"]) == "subprocess.DEVNULL"
    assert ast.literal_eval(keywords["timeout"]) == 120
    assert 'GIT_EXECUTABLE = "/usr/bin/git"' in source
    assert "shell=True" not in source


def test_mcp_release_verifier_keeps_a_closed_gpgv_boundary():
    source = (ROOT / "ops" / "mcp-deploy" / "verify_release.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    subprocess_imports = [
        (alias.name, alias.asname)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "subprocess"
    ]
    assert subprocess_imports == [("subprocess", None)]
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "subprocess"
        for node in ast.walk(tree)
    )
    subprocess_attributes = sorted(
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "subprocess"
    )
    assert subprocess_attributes == ["DEVNULL", "SubprocessError", "run"]
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]
    assert len(calls) == 1
    call = calls[0]
    assert ast.unparse(call.args[0]) == (
        "[str(gpgv_path), '--homedir', str(directory), '--status-fd', '1', "
        "'--keyring', str(keyring_path), str(signature_path), str(payload_path)]"
    )
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert set(keywords) == {"capture_output", "check", "env", "stdin", "timeout"}
    assert ast.literal_eval(keywords["check"]) is False
    assert ast.literal_eval(keywords["capture_output"]) is True
    assert ast.literal_eval(keywords["timeout"]) == 20
    assert ast.unparse(keywords["stdin"]) == "subprocess.DEVNULL"
    assert ast.literal_eval(keywords["env"]) == {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
    }
    assert 'DEFAULT_GPGV = Path("/usr/bin/gpgv")' in source
    assert "if not gpgv_path.is_file() or gpgv_path.is_symlink():" in source


def test_china_economic_git_helpers_keep_a_closed_subprocess_boundary():
    export = (ROOT / "scripts" / "build_china_econ_export.py").read_text(
        encoding="utf-8"
    )
    lineage = (ROOT / "scripts" / "build_china_econ_lineage.py").read_text(
        encoding="utf-8"
    )

    for text in (export, lineage):
        assert "shell=True" not in text
        assert "stdin=subprocess.DEVNULL" in text
        assert "timeout=" in text
        assert "cwd=ROOT" in text
        assert "env=" in text
    assert 'GIT_EXECUTABLE = "/usr/bin/git"' in export
    assert '[GIT_EXECUTABLE, "--no-replace-objects", "rev-parse"' in export
    assert 'GIT_EXECUTABLE = "/usr/bin/git"' in lineage
    assert 'GH_EXECUTABLE = "/usr/bin/gh"' in lineage
    assert lineage.count("_git(") == 6  # one definition plus five reviewed call sites
    assert lineage.count("_gh(") == 2  # one definition plus one reviewed API call
    assert '_git("ls-tree", "-z", commit_sha, "--", path)' in lineage
    assert '_git("cat-file", "blob", entry["object_sha"])' in lineage
    assert '"repos/{PRODUCER_REPOSITORY}/commits/{commit_sha}?per_page=1"' in lineage
    assert 'revision != "HEAD"' in lineage
    assert 're.fullmatch(r"[0-9a-f]{40}", revision)' in lineage
    assert "--max-count={MAX_WDI_LINEAGE_NODES + 1}" in lineage


def test_pages_capacity_guard_keeps_a_closed_staged_tree_process_boundary():
    source = (ROOT / "scripts" / "pages_artifact_capacity.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in source
    assert 'GIT_EXECUTABLE = "/usr/bin/git"' in source
    assert 'SYSTEM_TAR_EXECUTABLE = "/usr/bin/tar"' in source
    assert '[GIT_EXECUTABLE, "--no-replace-objects", *arguments]' in source
    assert (
        '[GIT_EXECUTABLE, "--no-replace-objects", "archive", "--format=tar", tree]'
        in source
    )
    assert '[SYSTEM_TAR_EXECUTABLE, "-xf", "-", "-C", str(destination)]' in source
    assert "OBJECT_ID_RE.fullmatch(revision)" in source
    assert "COMMIT_SHA_RE.fullmatch(publication_sha)" in source
    assert 'tree = _git(repo, "write-tree")' in source
    assert "wire_archive.build(stage, publication_sha)" in source
    assert "wire_archive.verify(stage, publication_sha)" in source
    assert "stdin=subprocess.DEVNULL" in source
    assert "env=GIT_ENVIRONMENT" in source
    assert "env=TAR_ENVIRONMENT" in source
    assert "timeout=PROCESS_TIMEOUT_SECONDS" in source

    tree = ast.parse(source)
    calls: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                calls.append(f"{function.name}:{node.func.attr}")
    assert sorted(calls) == [
        "_create_action_tar:run",
        "_extract_tree:Popen",
        "_extract_tree:run",
        "_git:run",
        "_gnu_tar:run",
    ], "every Pages capacity subprocess call must stay inside its reviewed boundary"


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


def test_contract_dispatch_keeps_a_fixed_head_lookup_boundary():
    text = (ROOT / "scripts" / "dispatch_publication_contract.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in text
    assert '["git", "rev-parse", "HEAD"]' in text


def test_readings_ledger_recovery_keeps_a_fixed_no_shell_git_boundary():
    text = (ROOT / "scripts" / "recover_readings_ledger.py").read_text(encoding="utf-8")

    assert "shell=True" not in text
    assert '["git", "-C", os.fspath(repo), *args]' in text
    assert 'env["GIT_NO_REPLACE_OBJECTS"] = "1"' in text
    assert "stdin=subprocess.DEVNULL" in text
    assert text.count("repo = _require_repo(repo)") >= 2
    assert "_HEX_40.fullmatch" in text


def test_network_lane_keeps_fixed_no_shell_process_boundaries():
    lane = (ROOT / "ops/network-lane/network_lane.py").read_text(encoding="utf-8")
    local_filter = (ROOT / "ops/common-crawl/run_duckdb_filter.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in lane and "shell=True" not in local_filter
    assert '[str(path), "--version"]' in lane
    assert 'command = [\n        str(downloader_path),\n        "download",' in lane
    assert "command=[str(prober_path)]" in lane
    assert '[str(path), "--version"]' in local_filter
    assert "[str(duckdb_path)]" in local_filter
    assert 'input=sql.encode("utf-8")' in local_filter


def test_public_osint_sync_keeps_fixed_no_shell_git_boundary():
    sync = (ROOT / "ops" / "osint-sync" / "public_osint_sync.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in sync
    assert '"/usr/bin/git"' in sync
    assert "stdin=subprocess.DEVNULL" in sync
    assert "env=_git_environment(" in sync
    assert "config.repository_url != REPOSITORY_URL" in sync
    assert '"core.hooksPath=/dev/null"' in sync
    assert '"protocol.file.allow=never"' in sync

    tree = ast.parse(sync)
    calls: list[tuple[int, str]] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                calls.append((node.lineno, f"{function.name}:{node.func.attr}"))
    assert [name for _line, name in sorted(calls)] == [
        "_run_git:run",
        "_git_is_ancestor:run",
        "_prepare_repository:run",
        "_git_blob:run",
    ], "every subprocess call site must be individually reviewed and inventoried"


def test_reproduce_all_keeps_a_fixed_no_shell_python_boundary():
    """reproduce_all may spawn first-party verifiers, never a shell or fetched argv."""
    text = (ROOT / "scripts" / "reproduce_all.py").read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "result = subprocess.run(" in text
    assert "cwd=ROOT" in text
    assert '"PYTHONPATH": str(ROOT)' in text
    tree = ast.parse(text)
    calls: list[str] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                calls.append(f"{function.name}:{node.func.attr}")
    assert calls == ["_run:run"], (
        "reproduce_all may only invoke subprocess inside _run, with the frozen command list"
    )


def test_analysis_broker_keeps_a_fixed_no_shell_docker_boundary():
    broker = (ROOT / "ops" / "investigative_analysis_broker.py").read_text(
        encoding="utf-8"
    )
    contract = (ROOT / "core" / "investigative_container_contract.py").read_text(
        encoding="utf-8"
    )

    assert "shell=True" not in broker and "shell=True" not in contract
    assert 'Path("/usr/bin/docker")' in broker
    assert "command = docker_command(" in broker
    assert 'CONTAINER_NAME = "palimpsest-investigative-analysis"' in contract
    assert '"--network",\n        "none"' in contract
    assert '"--pull",\n        "never"' in contract
    assert '"--entrypoint",\n        "/usr/local/bin/python3"' in contract
