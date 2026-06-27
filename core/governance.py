"""Governance — safety as executable code, not just prose.

SAFETY.md states the project's hard rules. This module makes three of them
*enforceable at runtime*, so an auditor (e.g. an Open Technology Fund code review)
can verify them by reading code and running tests rather than trusting a promise:

  KillSwitch   — a single on-disk file can halt all active collection instantly,
                 with no redeploy. The default posture is "off": any collector that
                 reaches out is expected to consult the switch first.

  RateCeiling  — a token-bucket limiter that bounds outbound request rate, so polite,
                 non-abusive collection is structural rather than aspirational.

  AuditChain   — a hash-chained, append-only log of privileged actions. Each entry
                 commits to the previous one, so any later tampering (editing or
                 deleting a past record) is detectable by recomputing the chain.

All three are standard-library only and have no external state beyond plain files, so
they are trivial to inspect, test, and reason about. Nothing here collects data; this
is the layer that constrains the code that does.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ── KillSwitch ───────────────────────────────────────────────────────────────

class KillSwitch:
    """A file-gated global halt. If the kill file exists (or env override is set), the
    system is HALTED and all active collection must abstain.

    Deliberately fail-safe in the cautious direction: any error reading the gate is
    treated as "halted", so a permissions glitch stops collection rather than letting it
    run unchecked. Engaging the switch is one filesystem write and needs no redeploy.
    """

    def __init__(self, path: str = None, env_var: str = "PALIMPSEST_HALT"):
        self.path = Path(path or os.getenv("PALIMPSEST_KILLFILE", "./.palimpsest_halt"))
        self.env_var = env_var

    def is_halted(self) -> bool:
        if os.getenv(self.env_var, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
        try:
            return self.path.exists()
        except OSError:
            return True  # fail safe: if we cannot tell, assume halted

    def engage(self, reason: str = "") -> None:
        """Halt the system. Best-effort; raises only if the file truly cannot be written."""
        self.path.write_text(
            f"halted_at={datetime.now(timezone.utc).isoformat()}\nreason={reason}\n",
            encoding="utf-8",
        )

    def release(self) -> None:
        """Resume the system by removing the gate (no-op if already absent)."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def require_live(self) -> None:
        """Raise if halted — the one-liner a collector calls before any outbound work."""
        if self.is_halted():
            raise RuntimeError("Palimpsest is halted by the kill switch; collection refused.")


# ── RateCeiling ──────────────────────────────────────────────────────────────

class RateCeiling:
    """Thread-safe token-bucket rate limiter.

    Bounds sustained request rate to `rate` per second with a short burst of `capacity`.
    `acquire()` blocks until a token is available (cooperative politeness); `try_acquire()`
    returns immediately with a bool for callers that prefer to skip rather than wait.

    A monotonic clock is injectable for deterministic testing — the default uses
    time.monotonic so wall-clock changes never grant or revoke tokens spuriously.
    """

    def __init__(self, rate: float, capacity: float = None, *, clock=time.monotonic):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, *, sleep=time.sleep) -> None:
        while not self.try_acquire(tokens):
            with self._lock:
                deficit = tokens - self._tokens
            sleep(max(deficit / self.rate, 0.001))


# ── AuditChain ───────────────────────────────────────────────────────────────

@dataclass
class AuditEntry:
    index: int
    at: str
    action: str
    detail: dict
    prev_hash: str
    hash: str


class AuditChain:
    """Append-only, hash-chained audit log of privileged actions.

    Each entry's hash commits to (index, timestamp, action, detail, prev_hash). Because
    every entry chains to the one before it, editing or deleting any past entry breaks the
    chain from that point on — `verify()` recomputes the whole chain and reports the first
    broken link. An optional HMAC key makes the chain not just tamper-EVIDENT but
    tamper-resistant (an attacker without the key cannot forge a valid continuation).

    Storage is newline-delimited JSON (one entry per line) for trivial inspection. This is
    accountability infrastructure: it records WHAT the system did (e.g. "engaged kill
    switch", "promoted gazetteer candidate"), never anything about a surveilled person.
    """

    GENESIS = "0" * 64

    def __init__(self, path: str = "./audit.log.jsonl", *, hmac_key: bytes = None):
        self.path = Path(path)
        self._hmac_key = hmac_key
        self._lock = threading.Lock()

    def _digest(self, index: int, at: str, action: str, detail: dict, prev_hash: str) -> str:
        payload = json.dumps(
            {"index": index, "at": at, "action": action, "detail": detail, "prev_hash": prev_hash},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if self._hmac_key:
            return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
        return hashlib.sha256(payload).hexdigest()

    def _read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def append(self, action: str, detail: dict = None) -> AuditEntry:
        """Append one tamper-evident record and return it."""
        with self._lock:
            rows = self._read_all()
            index = len(rows)
            prev_hash = rows[-1]["hash"] if rows else self.GENESIS
            at = datetime.now(timezone.utc).isoformat()
            detail = detail or {}
            h = self._digest(index, at, action, detail, prev_hash)
            entry = AuditEntry(index, at, action, detail, prev_hash, h)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
            return entry

    def verify(self) -> dict:
        """Recompute the chain. Returns {"ok": bool, "length": int, "broken_at": int|None}.

        broken_at is the index of the first entry whose stored hash or prev-link does not
        match a clean recomputation — i.e. the first sign of tampering.
        """
        rows = self._read_all()
        prev_hash = self.GENESIS
        for i, row in enumerate(rows):
            if row.get("index") != i or row.get("prev_hash") != prev_hash:
                return {"ok": False, "length": len(rows), "broken_at": i}
            recomputed = self._digest(i, row["at"], row["action"], row.get("detail", {}), prev_hash)
            if recomputed != row.get("hash"):
                return {"ok": False, "length": len(rows), "broken_at": i}
            prev_hash = row["hash"]
        return {"ok": True, "length": len(rows), "broken_at": None}


if __name__ == "__main__":  # offline smoke test
    import tempfile
    d = tempfile.mkdtemp()
    ks = KillSwitch(path=os.path.join(d, "halt"))
    print("halted initially:", ks.is_halted())
    ks.engage("manual test"); print("halted after engage:", ks.is_halted()); ks.release()

    rc = RateCeiling(rate=5, capacity=2)
    print("burst of 2 then empty:", rc.try_acquire(), rc.try_acquire(), rc.try_acquire())

    ac = AuditChain(path=os.path.join(d, "audit.jsonl"))
    ac.append("kill_switch.release", {"by": "test"})
    ac.append("gazetteer.propose", {"term": "散步"})
    print("verify clean:", ac.verify())
    # tamper and re-verify
    p = Path(d) / "audit.jsonl"
    lines = p.read_text().splitlines(); lines[0] = lines[0].replace("test", "mallory")
    p.write_text("\n".join(lines) + "\n")
    print("verify tampered:", ac.verify())
