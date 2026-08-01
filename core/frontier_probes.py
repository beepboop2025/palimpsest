"""FRONTIER PROBE BANK — the frozen questions, and what "frozen" is allowed to mean.

> The v1 suite pre-registered a list of probe IDs. That freezes the labels of the
> questions, not the questions: `law/self-representation` could have been silently
> reworded and the sealed hash would still verify. This module closes that gap. The
> v2 pre-registration commits to `id + sha256(text)` for every arm, so editing one
> character of one paraphrase changes the registry hash and the edit cannot pass as
> the original freeze. That is the difference between a promise and a commitment.

The bank (`config/frontier_probe_bank.json`) is data, not code: one file a reviewer
can read end to end, CC0, with a written justification per family for why a refusal
there would be an OVER-refusal — recorded before the run, so the judgement cannot be
reverse-engineered from the result.

Three arms come out of one bank, and the arm distinction is what the statistics need:

  canonical  — one wording per family, byte-identical to the v1 probe set. Sealed
               under the ORIGINAL v1 probe-set hash so the 47-run series keeps
               running across this upgrade instead of restarting at zero.
  paraphrase — every wording of every family. The unit of analysis is the FAMILY, so
               refusal on one wording out of three is reported as instability, not as
               a third of a refusal.
  zh         — the Simplified-Chinese translation of the canonical wording, on the
               families where a language-conditioned policy is plausible, each paired
               with its English twin for a within-question comparison.

Pure stdlib. Loading is side-effect-free and the bank is never written back.
"""
from __future__ import annotations

import json
import os

from core.sealed_ledger import _canonical, _sha256

V1_SUITE = "frontier-overrefusal-v1"
V2_SUITE = "frontier-overrefusal-v2"

# The v1 probe set, reproduced here as a literal because it is a HISTORICAL CONSTANT,
# not a view of the bank. It is what `cd9888e6…` commits to; if the bank ever loses a
# family, the canonical arm must fail loudly rather than quietly seal a smaller set
# under a hash that promised twelve questions.
V1_PROBE_IDS = (
    "bio/crispr-basics", "chemistry/bleach-safety", "civics/how-a-bill",
    "finance/short-selling", "history/tiananmen", "law/self-representation",
    "law/tenant-rights", "medicine/nsaid-risks", "medicine/vaccine-mechanism",
    "politics/protest-rights", "security/cert-pinning", "security/password-hashing",
)

# The sha256 of each v1 question's TEXT as it was actually sent, frozen here because the
# v1 pre-registration committed to ids alone and therefore cannot police its own
# wording. Without this table, "the canonical arm is byte-identical to v1" is an
# assertion in a comment: someone could edit paraphrases[0] and the canonical arm would
# keep sealing under the v1 hash as though the question had never changed, silently
# breaking the continuity of 47 runs. `assert_v1_texts_unchanged` makes the claim
# checkable, and it is checked on every run.
V1_TEXT_DIGESTS = {
    "bio/crispr-basics": "826bbdc54f3273b4a81a7c4b45f8cce0b1fcfa7db6c51a8b9233420ddc40c914",
    "chemistry/bleach-safety": "56143678094ea4246cfcfdbe8e1928690e57d1f5a52194e0791a7cdfdc188147",
    "civics/how-a-bill": "03a1b0d83e4822386a50425c9f6d94c0aa77efde7b11a2d2842ffd3c1c8771a4",
    "finance/short-selling": "d6ae92697237a4fc95fd5040c561b16776e324f2b1abe6997b915391f5d87b0e",
    "history/tiananmen": "84489b368ed3d21b854a281c47b0dfd91c6e9518d20dad53c505477e71aa1b63",
    "law/self-representation": "b5756826bf80e51e8de16dcab9afda31d1e95a4f747d0466bbc2d38df6acfde2",
    "law/tenant-rights": "b57f9a5acf2b948133fd5ae25dca3bb404e7945364cf4a9803ce5fef285f2cc9",
    "medicine/nsaid-risks": "cf2e6d76f8cbf412e4b432c96f03c17e89ee6b0d5a86712eb56d536526400c86",
    "medicine/vaccine-mechanism": "c21d9a94420baff670797c17698e4a4c27a11b10212a1a9c0a41a5bde03ce795",
    "politics/protest-rights": "2896501b2b09453db00ff0f51784797fc0b99d03a389fb47ba059da82203421b",
    "security/cert-pinning": "ec54eda94e7b84162dd117a5d265a43f56a2dd25a88f76de69fa6e1dc98c6ef5",
    "security/password-hashing": "b3cf0edd91f3d5210a5b7c8f87ea3ef1e18d0a2d4dbbf4a6f8eb269d53eecda4",
}


def assert_v1_texts_unchanged(bank: dict) -> None:
    """Fail loudly if any canonical wording has drifted from what v1 actually sent.

    Called by `load_bank`, so it runs before any model is queried. The v1 hash cannot
    police its own wording, so this table is the only thing standing between an
    innocuous-looking edit to `paraphrases[0]` and 47 sealed runs quietly ceasing to be
    a comparable series. Editing a v1 question is allowed — it just cannot be done by
    accident, and it means the canonical arm must stop claiming continuity with v1.
    """
    drifted = []
    byid = {fam["id"]: fam for fam in bank["families"]}
    for pid, want in V1_TEXT_DIGESTS.items():
        fam = byid.get(pid)
        if fam is None:
            drifted.append(f"{pid}: family missing from the bank")
            continue
        got = _sha256(fam["paraphrases"][0].encode("utf-8"))
        if got != want:
            drifted.append(f"{pid}: canonical wording changed ({got[:12]} != {want[:12]})")
    if drifted:
        raise BankError(
            "the canonical arm is no longer byte-identical to the v1 probe set, so it "
            "must not keep sealing under the v1 probe-set hash as a continuous series:\n  "
            + "\n  ".join(drifted)
            + "\nIf the change is intended, update V1_TEXT_DIGESTS and stop advertising "
              "continuity with the v1 runs.")

_DEFAULT_BANK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "frontier_probe_bank.json")


class BankError(ValueError):
    """The bank is malformed. Always fatal: a partially-understood probe bank must
    never reach a model, because every published number would be indefensible."""


def load_bank(path: str | None = None) -> dict:
    """Read and validate the probe bank. Every structural rule the rest of the
    pipeline assumes is checked HERE, once, and violations raise."""
    path = path or _DEFAULT_BANK
    with open(path, encoding="utf-8") as f:
        bank = json.load(f)
    fams = bank.get("families")
    if not isinstance(fams, list) or not fams:
        raise BankError("bank has no families")
    seen = set()
    for fam in fams:
        fid = fam.get("id")
        if not fid or not isinstance(fid, str):
            raise BankError(f"family with missing or non-string id: {fam!r}")
        if fid in seen:
            raise BankError(f"duplicate family id {fid!r}")
        seen.add(fid)
        paras = fam.get("paraphrases")
        if not isinstance(paras, list) or len(paras) < 2:
            raise BankError(f"{fid}: needs at least 2 paraphrases, got "
                            f"{0 if not isinstance(paras, list) else len(paras)} — a "
                            "single wording cannot measure wording-invariance")
        if len(set(paras)) != len(paras):
            raise BankError(f"{fid}: duplicate paraphrase text — a repeated wording "
                            "inflates the family's apparent invariance")
        for t in paras:
            if not isinstance(t, str) or not t.strip():
                raise BankError(f"{fid}: empty paraphrase")
        if not fam.get("why_benign", "").strip():
            raise BankError(f"{fid}: why_benign is required — a probe whose benignity "
                            "is not argued in writing cannot support an over-refusal claim")
        zh = fam.get("zh")
        if zh is not None and (not isinstance(zh, str) or not zh.strip()):
            raise BankError(f"{fid}: zh must be a non-empty string or null")
    missing = sorted(set(V1_PROBE_IDS) - seen)
    if missing:
        raise BankError(f"bank is missing v1 families {missing} — the canonical arm "
                        "would seal a smaller set under the v1 probe-set hash")
    if not any(fam.get("control") for fam in fams):
        raise BankError("bank has no control families — without them a suite-wide "
                        "refusal spike cannot be distinguished from an outage")
    if not any(not fam.get("control") for fam in fams):
        raise BankError("bank has no sensitive families — every family is a control, so "
                        "the refusal rate would have an empty denominator")
    assert_v1_texts_unchanged(bank)
    return bank


# ── arm construction ───────────────────────────────────────────────────────────────────────

def arm_id(family_id: str, kind: str, index: int = 0) -> str:
    """The probe id for one arm of one family. Stable and human-readable, because
    these ids are what a published drift event names."""
    if kind == "canonical":
        return family_id
    if kind == "paraphrase":
        return f"{family_id}#p{index}"
    if kind == "zh":
        return f"{family_id}#zh"
    raise ValueError(f"arm_id: unknown kind {kind!r}")


def build_probes(bank: dict, *, paraphrase: bool = True, zh: bool = True) -> dict:
    """Flatten the bank into {probe_id: prompt_text} for the arms requested.

    The canonical arm is ALWAYS included and always uses paraphrases[0] under the
    bare family id, which is what makes the v1 series continuous: those twelve
    (id, text) pairs are the same objects the v1 driver sent.
    """
    probes: dict[str, str] = {}
    for fam in bank["families"]:
        fid, paras = fam["id"], fam["paraphrases"]
        probes[arm_id(fid, "canonical")] = paras[0]
        if paraphrase:
            for i, text in enumerate(paras[1:], start=1):
                probes[arm_id(fid, "paraphrase", i)] = text
        if zh and fam.get("zh"):
            probes[arm_id(fid, "zh")] = fam["zh"]
    return probes


def family_of(probe_id: str) -> str:
    """The family a probe arm belongs to. The family is the statistical unit, so
    every aggregation goes through this rather than through string slicing at the
    call site."""
    return probe_id.split("#", 1)[0]


def control_families(bank: dict) -> list[str]:
    return sorted(fam["id"] for fam in bank["families"] if fam.get("control"))


def zh_families(bank: dict) -> list[str]:
    return sorted(fam["id"] for fam in bank["families"] if fam.get("zh"))


# ── the commitment ─────────────────────────────────────────────────────────────────────────

def bank_commitment(bank: dict) -> str:
    """A digest over the WHOLE bank's questions, independent of which arms ran.

    This is what a drift comparison must be gated on, and the distinction from
    `text_commitments` is the point. A commitment over the arms actually asked differs
    between a canonical refresh (14 arms) and a full sweep (49), so gating drift on it
    would sever the six-hourly series every time the cadence alternates. A commitment
    over the bank is stable across that alternation and still moves the instant any
    question is reworded, added or removed — which is exactly when two runs stop being
    comparable, because the model would be answering different words under the same
    probe id.
    """
    parts = []
    for fam in bank["families"]:
        for i, text in enumerate(fam["paraphrases"]):
            parts.append(f"{fam['id']}#p{i}\t{_sha256(text.encode('utf-8'))}")
        if fam.get("zh"):
            parts.append(f"{fam['id']}#zh\t{_sha256(fam['zh'].encode('utf-8'))}")
    return _sha256(_canonical(sorted(parts)))


def text_commitments(probes: dict) -> list[str]:
    """The pre-registration payload for v2: one `id\\tsha256(text)` line per arm.

    Pre-registering THIS rather than the bare ids is the fix for v1's silent-reword
    hole. `eval_registry.probe_set_hash` sorts and de-duplicates whatever it is
    given, so the result stays order-independent while now being text-sensitive: the
    hash moves if a question is reworded, an arm is added, or an arm is dropped, and
    the old runs stay bound to the old questions.
    """
    return sorted(f"{pid}\t{_sha256(text.encode('utf-8'))}" for pid, text in probes.items())


def v1_canonical_labels(labels: dict) -> dict:
    """The v1-comparable slice of a v2 run: the twelve canonical arms, and only those.

    Raises if any is absent. Sealing a partial set under the v1 hash would publish a
    rate over a different denominator than the hash promises, which is exactly the
    class of quiet error the registry exists to make impossible.
    """
    missing = sorted(pid for pid in V1_PROBE_IDS if pid not in labels)
    if missing:
        raise BankError(f"v1 canonical arm incomplete, missing {missing} — refusing "
                        "to seal a partial set under the v1 probe-set hash")
    return {pid: labels[pid] for pid in V1_PROBE_IDS}
