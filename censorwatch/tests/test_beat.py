from __future__ import annotations


def test_build_schedule_includes_consolidate_and_dynamic_collectors(monkeypatch):
    import censorwatch.emulation as emu
    from censorwatch.beat import build_censorwatch_schedule

    monkeypatch.setattr(emu, "promoted_sources_for_schedule", lambda settings=None: ["eastmoney_guba", "xueqiu"])
    sched = build_censorwatch_schedule()
    assert "cw-collect-eastmoney_guba" in sched
    assert "cw-collect-xueqiu" in sched
    assert "cw-consolidate" in sched
    assert "cw-emulate" in sched
    assert "cw-fusion" in sched
    assert sched["cw-consolidate"]["task"] == "censorwatch.tasks.cw_consolidate"


def test_build_schedule_fallback_when_no_enabled_sources(monkeypatch):
    import censorwatch.emulation as emu
    from censorwatch.beat import build_censorwatch_schedule

    monkeypatch.setattr(emu, "promoted_sources_for_schedule", lambda settings=None: [])
    sched = build_censorwatch_schedule()
    assert "cw-collect-eastmoney_guba" in sched
