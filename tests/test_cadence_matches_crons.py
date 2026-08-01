"""The published readings-to-wall-clock conversion must match the schedule that
actually produces the readings.

`processors/board_alarm.py` states a per-reading guarantee and then converts it
to days with CADENCE_PER_DAY, whose comment promises the numbers are "derived
from the committed refresh crons". Nothing enforced that promise, and
refusal_drift sat at 1.0/day while its workflow ran every six hours — so the
board published a false-alarm interval four times longer than the real one.

This test re-derives the cadence from `.github/workflows/*-refresh.yml`: it
finds the workflow that commits each signal's history file, parses that
workflow's cron expressions, and fails if the declared number has drifted.
stdlib only — no YAML or cron library — because the observatory's tests must
run in a bare interpreter like the code they guard.
"""
import re
from pathlib import Path

import pytest

from processors.board_alarm import CADENCE_PER_DAY, SIGNAL_FILES
from processors.conformal_events import CLOSED_SIGNALS

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

# Signals with no committed cron: curated by hand, so their figure is nominal
# and there is nothing to compare it against. Named here rather than silently
# skipped, so adding a workflow for one of them makes this list wrong on
# purpose.
NO_COMMITTED_CRON = {"bleedthrough_pools", "bleedthrough_capacity"}

CRON_LINE = re.compile(r"^\s*-\s*cron:\s*[\"']?([^\"'#]+?)[\"']?\s*(?:#.*)?$")


def _expand(field: str, lo: int, hi: int) -> list[int]:
    """Expand one cron field to the values it fires on."""
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            step = int(s)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step != 1:  # "5/2" means 5,7,9,... to the top of the range
                end = hi
        out.update(range(start, end + 1, step))
    return sorted(out)


def runs_per_day(expr: str) -> float:
    """Nominal firings per day for a five-field cron expression.

    Day-of-week restriction scales by len(days)/7; day-of-month by
    len(days)/30.44. Both restricted at once is a cron OR and is not modelled —
    no workflow here uses it, and a wrong number would be caught by this very
    test rather than shipped.
    """
    minute, hour, dom, month, dow = expr.split()
    assert month == "*", f"month-restricted cron not modelled: {expr}"
    assert dom == "*" or dow == "*", f"dom+dow cron not modelled: {expr}"
    per_day = len(_expand(minute, 0, 59)) * len(_expand(hour, 0, 23))
    if dow != "*":
        per_day *= len(_expand(dow, 0, 6)) / 7.0
    if dom != "*":
        per_day *= len(_expand(dom, 1, 31)) / 30.44
    return float(per_day)


def workflow_crons(path: Path) -> list[str]:
    return [m.group(1).strip()
            for m in (CRON_LINE.match(line) for line in path.read_text(encoding="utf-8").splitlines())
            if m]


def producing_workflow(history_filename: str) -> Path | None:
    """The refresh workflow that writes/commits this history file, if any."""
    hits = [p for p in sorted(WORKFLOWS.glob("*-refresh.yml"))
            if history_filename in p.read_text(encoding="utf-8")]
    if not hits:
        return None
    assert len(hits) == 1, f"{history_filename} claimed by {[p.name for p in hits]}"
    return hits[0]


# ── the cron parser itself, so a silent parser bug cannot fake agreement ────────

@pytest.mark.parametrize("expr,expected", [
    ("17 */6 * * *", 4.0),
    ("17 */3 * * *", 8.0),
    ("37 */12 * * *", 2.0),
    ("37 5 * * *", 1.0),
    ("0,30 * * * *", 48.0),
    ("23 13 * * 1-5", 5 / 7),
])
def test_runs_per_day_parses_known_expressions(expr, expected):
    assert runs_per_day(expr) == pytest.approx(expected)


# ── the invariant ──────────────────────────────────────────────────────────────

def test_every_open_board_signal_has_a_declared_cadence():
    """CADENCE_PER_DAY covers exactly the signals that can still produce readings:
    every open signal the board consumes (conformal or not), and no closed one — a
    series that will never append again has no readings-per-day, and publishing one
    would convert the guarantee through a fiction."""
    open_board_signals = set(SIGNAL_FILES) - set(CLOSED_SIGNALS)
    assert set(CADENCE_PER_DAY) == open_board_signals


def test_declared_cadence_matches_the_committed_crons():
    checked = []
    for signal, history_filename in sorted(SIGNAL_FILES.items()):
        if signal in CLOSED_SIGNALS:
            continue  # closed series make no cadence claim to verify
        wf = producing_workflow(history_filename)
        if wf is None:
            assert signal in NO_COMMITTED_CRON, (
                f"{signal} has no refresh workflow writing {history_filename}; "
                "its cadence is unverifiable and must not be published as derived")
            continue
        assert signal not in NO_COMMITTED_CRON, (
            f"{signal} now has a workflow ({wf.name}) — drop it from NO_COMMITTED_CRON")
        crons = workflow_crons(wf)
        assert crons, f"{wf.name} produces {history_filename} but declares no cron"
        actual = sum(runs_per_day(c) for c in crons)
        assert CADENCE_PER_DAY[signal] == pytest.approx(actual), (
            f"{signal}: board_alarm declares {CADENCE_PER_DAY[signal]}/day but "
            f"{wf.name} runs {actual}/day ({', '.join(crons)})")
        checked.append(signal)
    # a test that quietly stops checking anything is worse than no test
    assert len(checked) >= len(SIGNAL_FILES) - len(CLOSED_SIGNALS) - len(NO_COMMITTED_CRON)


def test_refusal_churn_cadence_is_the_erasure_workflow_not_daily():
    """Regression, inherited from the v1 signal it replaced: the churn log is
    written inside erasure-refresh.yml, every six hours. Its predecessor was once
    declared 1.0/day, overstating readings-to-false-alarm by exactly 4x on the
    board's published conversion. The churn LOG is a per-run heartbeat, so cron
    cadence == append cadence (unlike the movement-gated findings history)."""
    wf = producing_workflow(SIGNAL_FILES["refusal_churn"])
    assert wf is not None and wf.name == "erasure-refresh.yml"
    assert CADENCE_PER_DAY["refusal_churn"] == 4.0
    assert sum(runs_per_day(c) for c in workflow_crons(wf)) == pytest.approx(4.0)
