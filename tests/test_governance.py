"""Tests for core.governance — the safety primitives made enforceable.

    PYTHONPATH=. python3 -m pytest tests/test_governance.py -q

These are pure/offline: no network, no real clock dependency (the rate ceiling takes an
injected clock), no external state beyond a tmp_path file.
"""

import os

from core.governance import AuditChain, KillSwitch, RateCeiling


# ── KillSwitch ───────────────────────────────────────────────────────────────

def test_killswitch_default_is_live(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_TEST_UNSET")
    assert ks.is_halted() is False
    ks.require_live()  # must not raise


def test_killswitch_engage_and_release(tmp_path):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_TEST_UNSET")
    ks.engage("unit test")
    assert ks.is_halted() is True
    try:
        ks.require_live()
        assert False, "require_live should raise when halted"
    except RuntimeError:
        pass
    ks.release()
    assert ks.is_halted() is False


def test_killswitch_env_override(tmp_path, monkeypatch):
    ks = KillSwitch(path=str(tmp_path / "halt"), env_var="PALIMPSEST_HALT_TEST_VAR")
    monkeypatch.setenv("PALIMPSEST_HALT_TEST_VAR", "1")
    assert ks.is_halted() is True


# ── RateCeiling ──────────────────────────────────────────────────────────────

def test_rate_ceiling_burst_then_empty():
    rc = RateCeiling(rate=10, capacity=3, clock=lambda: 0.0)  # frozen clock
    assert rc.try_acquire() is True
    assert rc.try_acquire() is True
    assert rc.try_acquire() is True
    assert rc.try_acquire() is False  # capacity exhausted, no time has passed


def test_rate_ceiling_refills_over_time():
    t = {"now": 0.0}
    rc = RateCeiling(rate=2, capacity=2, clock=lambda: t["now"])
    assert rc.try_acquire(2) is True   # drain the bucket
    assert rc.try_acquire() is False
    t["now"] = 1.0                      # 1 second @ 2 tokens/s → 2 tokens back
    assert rc.try_acquire() is True
    assert rc.try_acquire() is True
    assert rc.try_acquire() is False


def test_rate_ceiling_rejects_bad_rate():
    try:
        RateCeiling(rate=0)
        assert False, "rate=0 must raise"
    except ValueError:
        pass


# ── AuditChain ───────────────────────────────────────────────────────────────

def test_audit_chain_verifies_clean(tmp_path):
    ac = AuditChain(path=str(tmp_path / "audit.jsonl"))
    ac.append("kill_switch.engage", {"reason": "drill"})
    ac.append("gazetteer.propose", {"term": "散步"})
    ac.append("gazetteer.ratify", {"term": "散步", "by": "curator"})
    result = ac.verify()
    assert result == {"ok": True, "length": 3, "broken_at": None}


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "audit.jsonl"
    ac = AuditChain(path=str(p))
    ac.append("a", {"v": 1})
    ac.append("b", {"v": 2})
    ac.append("c", {"v": 3})
    # Tamper with the middle entry's detail without recomputing hashes.
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"v": 2', '"v": 999')
    p.write_text("\n".join(lines) + "\n")
    result = ac.verify()
    assert result["ok"] is False
    assert result["broken_at"] == 1


def test_audit_chain_hmac_makes_forgery_fail(tmp_path):
    p = tmp_path / "audit.jsonl"
    keyed = AuditChain(path=str(p), hmac_key=b"secret-key")
    keyed.append("x", {"n": 1})
    keyed.append("y", {"n": 2})
    assert keyed.verify()["ok"] is True
    # A verifier WITHOUT the key cannot validate the keyed chain → detects break at 0.
    unkeyed = AuditChain(path=str(p))
    assert unkeyed.verify()["ok"] is False


if __name__ == "__main__":
    import sys
    sys.exit(__import__("pytest").main([__file__, "-q"]))
