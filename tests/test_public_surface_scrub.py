"""Guard: the check that reads the tree before it is served does what it claims.

Pages publishes every tracked file, so the default for anything committed here is "served at
palimpsest.info". This runs the same code the workflows run, over the same file list Pages
publishes, so a reintroduction fails on someone's laptop before it fails in public.

The rule strings themselves are deliberately absent from this file and from the script: a
test that asserts "the string must not appear" has to contain the string. The assertions
below work on a synthetic rule instead.

Every fixture is assembled from pieces rather than written out, because this file is
published too and a test containing a forbidden shape fails its own check.

Standard library, offline.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.verify_public_surface import (  # noqa: E402
    SELF,
    _findings_in,
    check,
    identity_rules,
    render,
    tracked_files,
)

WORKFLOWS = ROOT / ".github" / "workflows"


def test_the_published_tree_is_clean_today():
    rules, _ = identity_rules()
    problems = [p for p in check(rules=rules) if p.blocking]
    assert not problems, "\n".join(render(p) for p in problems)


def test_an_internal_annotation_with_a_handle_is_caught():
    line = "    " + "TODO" + "(someone): decide what this means and return it."
    assert _findings_in("collectors/x.py", line, ())


def test_a_local_machine_hostname_is_caught():
    """Git author metadata of the form <user>@<user>s-laptop.local carries a username."""
    host = "someone@someones-laptop" + ".local"
    assert _findings_in("docs/x.md", f"committed by {host}", ())


def test_an_address_on_a_domain_this_project_does_not_own_is_caught():
    address = "someone@" + "somemailprovider.com"
    assert _findings_in("docs/x.md", f"write to {address}", ())


def test_the_project_s_own_addresses_are_not_flagged():
    """desk@ and bot@ are published on purpose; a check that cries wolf on them gets turned
    off within the week."""
    assert not _findings_in("docs/x.md", "contact desk@palimpsest.info", ())
    assert not _findings_in("docs/x.md", "committed as bot@palimpsest.info", ())


def test_one_allowed_address_does_not_allow_its_whole_domain():
    """The allow-list names single addresses. A neighbour at the same domain is a different
    mailbox and probably a person, so it stays a finding."""
    allowed = "jubao@" + "eastmoney.com"
    assert not _findings_in("docs/x.md", f"reports go to {allowed}", ())

    neighbour = "someperson@" + "eastmoney.com"
    assert _findings_in("docs/x.md", f"reports go to {neighbour}", ()), (
        "an allowed address widened into an allowed domain"
    )


def test_an_identity_rule_matches_without_being_echoed():
    """The failure output is as public as the site, so a match reports a rule NUMBER. If it
    printed the rule, a red CI log would publish the string the rule exists to hide."""
    secret = "a-string-that-must-not-be-published"
    found = _findings_in("docs/x.md", f"measured from {secret}", (secret,))
    assert found, "the identity rule did not fire"
    assert all(secret not in render(f) for f in found), "the output echoed the secret"
    assert "#1" in found[0].why


def test_the_checker_reads_what_pages_serves():
    """git ls-files is not a proxy for the published set here, it IS the published set. A
    checker that walked only docs/ would miss an annotation in collectors/."""
    files = tracked_files()
    assert "collectors/inside_view.py" in files
    assert "docs/MEASURING-GFW-DNS-INJECTION.md" in files


# ── a rule of several words survives being wrapped ────────────────────────────


def test_a_multi_word_rule_is_caught_across_a_line_break():
    """Most rules are two or three words, and any formatter that wraps prose can put a
    newline in the middle of one. Matching line by line looks for the words in a place they
    need not be, and the wrapped copy is exactly the copy that ships."""
    rule = "Some Assembled Name"
    inline = _findings_in("docs/x.md", f"measured by {rule} in July", (rule,))
    assert inline, "the rule did not fire on one line"

    wrapped = _findings_in(
        "docs/x.md", "measured by Some\nAssembled Name in July", (rule,)
    )
    assert wrapped, (
        "a wrapped rule went unmatched, and a wrap is invisible when published"
    )

    indented = _findings_in(
        "docs/x.md", "measured by Some Assembled\n   Name in July", (rule,)
    )
    assert indented, "the leading indent of the next line defeated the match"


def test_a_wrapped_match_is_reported_on_the_line_it_starts_on():
    """The line number is the only part of the report that is actionable, so it has to name
    the line someone opens, not the line the last word happened to land on."""
    rule = "Some Assembled Name"
    text = "one\ntwo\nmeasured by Some\nAssembled Name in July\n"
    found = _findings_in("docs/x.md", text, (rule,))
    assert found and found[0].line == 3, [f.line for f in found]


# ── the check blocks a leak and does not block a corpus ───────────────────────


def test_a_shape_in_a_collected_corpus_warns_rather_than_blocks():
    """readings/ and the fixture trees hold pages and transcripts collected from the world.
    A third party's address inside one is the subject matter. Blocking on it would stop every
    data refresh until someone hand-listed each address, and a gate that stops routine work
    is a gate that gets deleted."""
    address = "someperson@" + "somemailprovider.com"
    line = f"reports go to {address}"

    corpus = _findings_in("readings/refusal-drift-transcripts.json", line, ())
    assert corpus, "a collected corpus was not read at all"
    assert not any(f.blocking for f in corpus), (
        "a scraped address blocked the push path"
    )

    fixture = _findings_in("censorwatch/tests/fixtures/guba_list.html", line, ())
    assert fixture and not any(f.blocking for f in fixture)

    authored = _findings_in("docs/x.md", line, ())
    assert authored and all(f.blocking for f in authored), (
        "an address in an authored page has to block: that is the leak this exists for"
    )


GENERATED_FROM_THE_CDT_FEED = (
    "china-brief.html",
    "demo/data/cdt_history.json",
    "dashboards/ddti_dashboard.html",
    "dashboards/ddti_observatory.html",
)


@pytest.mark.parametrize("path", GENERATED_FROM_THE_CDT_FEED)
def test_a_scraped_shape_in_a_generated_artifact_warns_rather_than_blocks(path):
    """These four are written from the China Digital Times feed and committed by
    china-brief-refresh.yml and ddti-refresh.yml. They are not under readings/, so they used
    to be treated as authored. One address-shaped or annotation-shaped string in one scraped
    article title would then exit 1 in the step before `git push` and kill that cron on every
    tick, silently, with nothing published to show for it."""
    address = "someperson@" + "somemailprovider.com"
    for line in (
        f"<li>reports go to {address}</li>",
        "<li>" + "TODO" + "(someone): a headline shaped like an annotation</li>",
    ):
        found = _findings_in(path, line, ())
        assert found, f"{path} was not read at all"
        assert not any(f.blocking for f in found), (
            f"a scraped shape in {path} blocks the refresh push path"
        )


@pytest.mark.parametrize("path", GENERATED_FROM_THE_CDT_FEED)
def test_an_identity_rule_still_blocks_in_a_generated_artifact(path):
    """Only the shape rules are downgraded. A name reaching a generated page is published
    exactly as hard as a name reaching an authored one, so the rule list keeps blocking."""
    secret = "a-string-that-must-not-be-published"
    found = _findings_in(path, f"measured from {secret}", (secret,))
    assert found and all(f.blocking for f in found), (
        f"the identity rules stopped blocking on {path}"
    )
    assert all(secret not in render(f) for f in found)


@pytest.mark.parametrize("path", GENERATED_FROM_THE_CDT_FEED)
def test_each_generated_artifact_is_one_a_refresh_workflow_stages(path):
    """The list above is hand-written, so it is only worth anything while it names the files
    the crons actually commit. A rename that lands in the workflow and not here puts the
    outage straight back."""
    staged = "".join(
        (WORKFLOWS / name).read_text(encoding="utf-8")
        for name in ("china-brief-refresh.yml", "ddti-refresh.yml")
    )
    assert path in staged, f"no refresh workflow commits {path} any more"


def test_a_rule_match_blocks_even_in_a_collected_corpus():
    """The corpus exemption is for shapes, which are heuristics. A rule-list match is not a
    heuristic, and a name that reached a scraped file is published just the same."""
    secret = "a-string-that-must-not-be-published"
    found = _findings_in("readings/history.jsonl", f"seen at {secret}", (secret,))
    assert found and all(f.blocking for f in found)


# ── the checker does not exempt itself from the rules that matter ─────────────


def test_the_checker_is_not_exempt_from_the_identity_rules(tmp_path, monkeypatch):
    """The script is a tracked file and is therefore published like any other. Waiving the
    shape rules for it is necessary, because it spells those shapes out; waiving the rule
    list for it would leave one published file where a name may appear."""
    secret = "a-string-that-must-not-be-published"
    target = tmp_path / SELF
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# measured from {secret}\n", encoding="utf-8")
    monkeypatch.setattr("scripts.verify_public_surface.ROOT", str(tmp_path))

    found = check(paths=[SELF], rules=(secret,))
    assert found, "the checker exempted itself from the rule list"
    assert all(secret not in render(f) for f in found)


def test_a_file_the_checker_cannot_decode_is_reported_not_skipped(
    tmp_path, monkeypatch
):
    """A published byte string this checker cannot read is not a pass: a name survives any
    encoding the checker declines to open. Silence here is how an unscanned file ships."""
    target = tmp_path / "docs" / "opaque.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe\x00identity\x00")
    monkeypatch.setattr("scripts.verify_public_surface.ROOT", str(tmp_path))

    found = check(paths=["docs/opaque.txt"], rules=())
    assert found, "a non-UTF-8 tracked file passed unreported"
    assert "not checked" in found[0].why


# ── the check has to run before the thing that publishes, in the same job ─────


def _jobs(text: str) -> list:
    """The workflow's jobs, split on the two-space keys under `jobs:`. A YAML parser is not
    available to the stdlib-only suite, and the indentation in this repository is uniform."""
    body = text.split("\njobs:", 1)[-1]
    lines = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    jobs, current = [], None
    for line in lines:
        if (
            len(line) > 2
            and line[:2] == "  "
            and line[2] != " "
            and line.rstrip().endswith(":")
        ):
            current = [line]
            jobs.append(current)
        elif current is not None:
            current.append(line)
    return ["\n".join(j) for j in jobs]


def _workflows_that_push():
    """Both suffixes. GitHub reads .yaml exactly as it reads .yml, so a checker that globs
    one of them leaves a workflow that can push without ever being looked at."""
    files = list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    publish_markers = ("git push", "python scripts/push_data_commit.py")
    return sorted(
        (
            path
            for path in files
            if any(
                marker in path.read_text(encoding="utf-8") for marker in publish_markers
            )
        ),
        key=lambda path: path.name,
    )


def test_there_are_workflows_that_push():
    """If this empties out, the gate test below stops testing anything."""
    assert _workflows_that_push()


@pytest.mark.parametrize("wf", _workflows_that_push(), ids=lambda p: p.name)
def test_the_scrub_runs_before_the_push_that_publishes(wf):
    """Pages serves this repository from the branch, so the push IS the deploy. A check that
    runs after it finds the match in public.

    Compared job by job rather than by file line number: two steps in two different jobs run
    in whatever order the runner picks, and a scrub in job A does nothing for a push in job
    B, yet the scrub's line number in the file is lower either way.
    """
    jobs = _jobs(wf.read_text(encoding="utf-8"))
    publish_markers = ("git push", "python scripts/push_data_commit.py")
    pushers = [job for job in jobs if any(marker in job for marker in publish_markers)]
    assert pushers, f"{wf.name} was selected for pushing and no job in it pushes"
    for job in pushers:
        name = job.splitlines()[0].strip()
        scrub = [
            i
            for i, line in enumerate(job.splitlines())
            if "verify_public_surface.py" in line
        ]
        push = [
            i
            for i, line in enumerate(job.splitlines())
            if any(marker in line for marker in publish_markers)
        ]
        assert scrub, (
            f"{wf.name} job {name} pushes to a published branch and never runs the scrub"
        )
        assert min(scrub) < min(push), (
            f"{wf.name} job {name} runs the scrub after the push, so a match is found "
            "after publication"
        )


def test_the_publish_path_does_not_depend_on_a_secret():
    """A required rule list may guard retention, but must never gate publication.

    The acquisition recovery artifact is skipped when its explicit rules are
    absent, while the later publication scrub keeps the established report-only
    behavior. Any other use of --require-rules would stop a scheduled refresh.
    """
    for wf in _workflows_that_push():
        text = wf.read_text(encoding="utf-8")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "verify_public_surface.py" in line and not line.lstrip().startswith("#"):
                if "--require-rules" not in line:
                    continue
                step = "\n".join(lines[max(0, index - 8): index + 1])
                assert "- name: Screen acquisition before artifact retention" in step
                assert "id: acquisition_scrub" in step
                assert "continue-on-error: true" in step
                assert "if: steps.acquisition_scrub.outcome == 'success'" in text
