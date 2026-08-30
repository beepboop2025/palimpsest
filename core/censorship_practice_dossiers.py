"""Piece-level censorship-practice dossiers from retained public evidence.

The builder is deliberately narrower than a general China-news classifier.  It
publishes one dossier for every *qualifying captured item*, while keeping three
very different claims separate:

* a collector-observed disappearance (for example, a documented social
  tombstone);
* an attributed public report about an information-control practice; and
* a board/archive pattern that is useful for review but does not establish a
  censored post.

It performs no network access, sentiment inference, fuzzy URL joining, actor
inference, or free-form model generation.  An ordinary critical article is not
called censored merely because it is critical, and an article reporting
censorship is not represented as having itself been censored.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from core.china_observation import content_sha256, iso_z, public_text


SCHEMA_VERSION = "palimpsest.censorship-practice-dossiers.v1"
METHOD_VERSION = 1
SITE = "https://palimpsest.info"

READING_URLS = {
    "ddti-latest.json": f"{SITE}/readings/ddti-latest.json",
    "public-deletion-ledgers-latest.json": (
        f"{SITE}/readings/public-deletion-ledgers-latest.json"
    ),
    "social-observations-latest.json": (
        f"{SITE}/readings/social-observations-latest.json"
    ),
    "social-observations-versions.jsonl": (
        f"{SITE}/readings/social-observations-versions.jsonl"
    ),
    "undertext-latest.json": f"{SITE}/readings/undertext-latest.json",
    "wayback-latest.json": f"{SITE}/readings/wayback-latest.json",
    "weibo-hotsearch-latest.json": (
        f"{SITE}/readings/weibo-hotsearch-latest.json"
    ),
}

INPUT_FILES = tuple(READING_URLS)

QUALIFICATION_STATES = frozenset(
    {"observed_disappearance", "peer_reported", "pattern_signal", "review_required"}
)
EVIDENCE_STRENGTHS = frozenset({"strong", "moderate", "context_only"})
ACTOR_ATTRIBUTIONS = frozenset(
    {"not_established", "source_metadata", "peer_source_named"}
)
ACTOR_ROLES = frozenset(
    {
        "not_established",
        "reported_directive_issuer",
        "reported_enforcement_actor",
        "reported_implementing_institution",
        "reported_implementing_surface_class",
        "reported_investigating_authority",
        "source_metadata_actor",
    }
)

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "generated_at",
        "status",
        "source",
        "method",
        "scope",
        "actor_attribution_policy",
        "counts",
        "coverage",
        "dossiers",
    }
)
_DOSSIER_FIELDS = frozenset(
    {
        "dossier_id",
        "qualification",
        "subject",
        "practice",
        "timeline",
        "measurements",
        "evidence",
        "counter_readings",
        "unknowns",
        "cite",
    }
)
_QUALIFICATION_FIELDS = frozenset(
    {"state", "evidence_strength", "basis", "criticality_basis"}
)
_SUBJECT_FIELDS = frozenset(
    {
        "kind",
        "title",
        "excerpt",
        "url",
        "platform",
        "source",
        "language",
        "content_sha256",
        "first_seen",
        "last_seen",
        "last_confirmed_alive",
    }
)
_PRACTICE_FIELDS = frozenset(
    {"mechanisms", "finding", "actor", "interpretation_limit"}
)
_ACTOR_FIELDS = frozenset({"name", "role", "attribution", "basis"})
_TIMELINE_FIELDS = frozenset({"at", "event", "source", "precision"})
_MEASUREMENT_FIELDS = frozenset(
    {
        "measurement_id",
        "reading_id",
        "status",
        "match_kind",
        "source_timestamp",
        "reading_url",
        "input_sha256",
        "metric",
        "value",
        "interpretation_limit",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "evidence_id",
        "relation",
        "source_name",
        "source_url",
        "observed_at",
        "claim",
        "input_sha256",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DOSSIER_ID_RE = re.compile(r"^censorpractice-[0-9a-f]{24}$")
_MEASUREMENT_ID_RE = re.compile(r"^censormeasure-[0-9a-f]{20}$")
_EVIDENCE_ID_RE = re.compile(r"^censorevidence-[0-9a-f]{20}$")

_EXPLICIT_INFORMATION_CONTROL = (
    "censor",
    "404 deleted",
    "deleted content",
    "blocked word",
    "blocked words",
    "sensitive word",
    "sensitive words",
    "cannot speak openly",
    "free expression",
    "freedom of expression",
    "freedom of information",
    "great firewall",
    "book ban",
    "book banning",
    "banned book",
    "discourse censorship",
    "press freedom",
    "detention for reposting",
    "detained for reposting",
    "raid two more independent bookstores",
    "digital transnational repression",
    "minitrue",
    "directive",
    "审查",
    "删除",
    "屏蔽",
    "敏感词",
    "禁言",
    "封号",
    "下架",
    "真理部",
)

_FORBIDDEN_CAUSAL = (
    "this was censored",
    "censored because",
    "ordered the deletion",
    "intended to suppress",
    "cover-up",
    "to silence",
)

_NAMED_ACTOR_RELATIONSHIPS = (
    (
        re.compile(
            r"under investigation by the Wuhan Municipal Bureau of Culture and Tourism",
            re.IGNORECASE,
        ),
        "Wuhan Municipal Bureau of Culture and Tourism",
        "reported_investigating_authority",
        (
            "The retained excerpt explicitly says the subject is under "
            "investigation by this municipal bureau. This names the reported "
            "investigating authority, not a higher-level ordering actor."
        ),
    ),
    (
        re.compile(r"national security police raided", re.IGNORECASE),
        "Hong Kong national security police",
        "reported_enforcement_actor",
        (
            "The retained excerpt explicitly says national security police "
            "conducted the raids and arrests. This is attributed reporting of "
            "the enforcement actor, not independent Palimpsest verification."
        ),
    ),
    (
        re.compile(
            r"notice below was issued by the Hunan Library.*?"
            r"announces the temporary suspension of library Wi-Fi",
            re.IGNORECASE | re.DOTALL,
        ),
        "Hunan Library",
        "reported_implementing_institution",
        (
            "The retained excerpt explicitly says Hunan Library issued the "
            "notice announcing the Wi-Fi suspension. It does not establish who "
            "required or ordered the policy."
        ),
    ),
    (
        re.compile(
            r"keyword-based censorship on Chinese online platforms",
            re.IGNORECASE,
        ),
        "Chinese online platforms (source wording)",
        "reported_implementing_surface_class",
        (
            "The retained excerpt explicitly locates keyword-based censorship "
            "on Chinese online platforms. It names a surface class, not a "
            "specific company or ordering authority."
        ),
    ),
)


class CensorshipDossierError(ValueError):
    """The dossier projection violates its closed public contract."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _given_sha(value: Any) -> str:
    if isinstance(value, str) and _SHA256_RE.fullmatch(value):
        return value
    return ""


def _https(value: Any) -> str:
    text = public_text(value, limit=2_048)
    return text if text.startswith("https://") else ""


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Decode feed entities before bounding so the renderer can safely escape the
    # resulting plain text exactly once. public_text removes any decoded brackets.
    return public_text(html.unescape(value), limit=limit)


def _list_text(value: Any, *, limit: int = 120) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        item = _text(raw, limit)
        if item and item not in result:
            result.append(item)
    return result


def _record_count(payload: Mapping[str, Any] | None) -> int:
    if not payload:
        return 0
    for key in ("observations", "observation_records", "reconstructions", "ranked"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _input_receipts(
    payloads: Mapping[str, Mapping[str, Any] | None],
    social_versions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for filename in INPUT_FILES:
        if filename == "social-observations-versions.jsonl":
            available = bool(social_versions)
            receipts.append(
                {
                    "filename": filename,
                    "available": available,
                    "generated_at": None,
                    "input_sha256": _sha(list(social_versions)) if available else None,
                    "records_reviewed": len(social_versions),
                }
            )
            continue
        payload = payloads.get(filename)
        receipts.append(
            {
                "filename": filename,
                "available": payload is not None,
                "generated_at": iso_z(payload.get("generated_at")) if payload else None,
                "input_sha256": _sha(payload) if payload else None,
                "records_reviewed": _record_count(payload),
            }
        )
    return receipts


def _identity(*parts: str) -> str:
    return content_sha256(*parts)


def _dossier_id(identity: str) -> str:
    return f"censorpractice-{_identity('dossier', identity)[:24]}"


def _measurement_id(identity: str, reading_id: str, match_kind: str) -> str:
    return (
        "censormeasure-"
        f"{_identity('measurement', identity, reading_id, match_kind)[:20]}"
    )


def _evidence_id(identity: str, source_name: str, relation: str) -> str:
    return (
        "censorevidence-"
        f"{_identity('evidence', identity, source_name, relation)[:20]}"
    )


def _actor(observation: Mapping[str, Any]) -> dict[str, str | None]:
    for field in ("actor", "authority", "ordering_authority", "attributed_actor"):
        name = _text(observation.get(field), 160)
        if name:
            return {
                "name": name,
                "role": "source_metadata_actor",
                "attribution": "source_metadata",
                "basis": f"The retained source metadata explicitly names {field}.",
            }
    title = _text(observation.get("title"), 240)
    if "minitrue" in title.casefold() or "真理部" in title:
        return {
            "name": "Minitrue (source label)",
            "role": "reported_directive_issuer",
            "attribution": "peer_source_named",
            "basis": "The peer source title explicitly uses the Minitrue label.",
        }
    retained_text = "\n".join(
        filter(
            None,
            (
                title,
                _text(observation.get("text"), 1_200),
                _text(observation.get("excerpt"), 1_200),
            ),
        )
    )
    for pattern, name, role, basis in _NAMED_ACTOR_RELATIONSHIPS:
        if pattern.search(retained_text):
            return {
                "name": name,
                "role": role,
                "attribution": "peer_source_named",
                "basis": basis,
            }
    return {
        "name": None,
        "role": "not_established",
        "attribution": "not_established",
        "basis": (
            "The retained item does not explicitly identify the ordering actor. "
            "Palimpsest does not infer CCP, state, platform, or local-authority "
            "responsibility from disappearance or topic alone."
        ),
    }


def _criticality_indicators(observation: Mapping[str, Any]) -> list[str]:
    fields: list[tuple[str, str]] = []
    for field in ("title", "text", "excerpt"):
        value = _text(observation.get(field), 1_200)
        if value:
            fields.append((field, value))
    for field in ("tags", "terms", "china_relevance_labels"):
        for value in _list_text(observation.get(field), limit=160):
            fields.append((field, value))

    indicators: list[str] = []
    for phrase in _EXPLICIT_INFORMATION_CONTROL:
        needle = phrase.casefold()
        for field, value in fields:
            if needle in value.casefold():
                label = f"{field}: {phrase}"
                if label not in indicators:
                    indicators.append(label)
                break
    return indicators[:12]


def _ledger_mechanisms(
    observation: Mapping[str, Any], indicators: Sequence[str]
) -> list[str]:
    kind = _text(observation.get("ledger_kind"), 40).casefold()
    if kind == "freeweibo":
        return ["reported_post_removal"]
    if kind == "freewechat":
        return ["reported_article_removal"]

    blob = " ".join(
        [
            _text(observation.get("title"), 600),
            _text(observation.get("text"), 1_200),
            " ".join(_list_text(observation.get("tags"), limit=160)),
            " ".join(_list_text(observation.get("terms"), limit=160)),
        ]
    ).casefold()
    if not indicators:
        return []
    mechanisms: list[str] = []

    def add(mechanism: str) -> None:
        if mechanism not in mechanisms:
            mechanisms.append(mechanism)

    if "minitrue" in blob or "directive" in blob or "真理部" in blob:
        add("reported_editorial_directive")
    if any(
        term in blob
        for term in (
            "404 deleted",
            "deleted content",
            "deleted at the source",
            "censored reflection",
        )
    ):
        add("reported_content_removal")
    if any(term in blob for term in ("blocked word", "sensitive word", "cannot speak openly")):
        add("reported_keyword_filtering")
    if "great firewall" in blob or "internet access" in blob:
        add("reported_network_blocking")
    if any(term in blob for term in ("banned book", "book ban", "book banning")):
        add("reported_publication_restriction")
    if any(
        term in blob
        for term in (
            "detention for reposting",
            "detained for reposting",
            "raid two more independent bookstores",
            "under investigation by the wuhan municipal bureau",
        )
    ):
        add("reported_legal_administrative_pressure")
    if "digital transnational repression" in blob:
        add("reported_digital_repression")
    return mechanisms or ["reported_information_control"]


def _measurement(
    *,
    identity: str,
    filename: str,
    payload: Mapping[str, Any] | None,
    status: str,
    match_kind: str,
    metric: str,
    value: str,
    interpretation_limit: str,
    source_timestamp: str | None = None,
) -> dict[str, Any]:
    normalized_clock = iso_z(source_timestamp)
    if normalized_clock is None and payload:
        normalized_clock = iso_z(payload.get("generated_at"))
    return {
        "measurement_id": _measurement_id(identity, filename, match_kind),
        "reading_id": filename.removesuffix("-latest.json").removesuffix(".jsonl"),
        "status": status,
        "match_kind": match_kind,
        "source_timestamp": normalized_clock,
        "reading_url": READING_URLS[filename],
        "input_sha256": _sha(payload) if payload else None,
        "metric": metric,
        "value": value,
        "interpretation_limit": interpretation_limit,
    }


def _evidence(
    *,
    identity: str,
    source_name: str,
    source_url: str,
    relation: str,
    observed_at: str | None,
    claim: str,
    input_sha256: str,
) -> dict[str, Any]:
    return {
        "evidence_id": _evidence_id(identity, source_name, relation),
        "relation": relation,
        "source_name": source_name,
        "source_url": source_url,
        "observed_at": iso_z(observed_at),
        "claim": claim,
        "input_sha256": input_sha256,
    }


def _timeline(*rows: tuple[str | None, str, str, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for at, event, source, precision in rows:
        stamp = iso_z(at)
        if not stamp:
            continue
        key = (stamp, event, source)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {"at": stamp, "event": event, "source": source, "precision": precision}
        )
    result.sort(key=lambda row: (row["at"], row["event"]))
    return result


def _subject(
    observation: Mapping[str, Any],
    *,
    kind: str,
    fallback_title: str = "",
) -> dict[str, Any]:
    title = _text(observation.get("title"), 240) or fallback_title
    excerpt = _text(observation.get("text") or observation.get("excerpt"), 640)
    url = _https(
        observation.get("url")
        or observation.get("source_url")
        or observation.get("permalink")
    )
    digest = _given_sha(observation.get("content_sha256")) or _identity(
        title, excerpt, url
    )
    return {
        "kind": kind,
        "title": title or "(removed public item; title unavailable)",
        "excerpt": excerpt,
        "url": url,
        "platform": _text(observation.get("platform"), 80),
        "source": _text(
            observation.get("source") or observation.get("source_name"), 120
        ),
        "language": _text(observation.get("language"), 16) or "unknown",
        "content_sha256": digest,
        "first_seen": iso_z(
            observation.get("first_seen")
            or observation.get("first_observed_at")
            or observation.get("published_at")
        ),
        "last_seen": iso_z(
            observation.get("last_seen") or observation.get("first_observed_at")
        ),
        "last_confirmed_alive": iso_z(observation.get("last_confirmed_alive")),
    }


def _citation(dossier: Mapping[str, Any]) -> str:
    subject = dossier["subject"]
    qualification = dossier["qualification"]
    return (
        f"Palimpsest censorship-practice dossier {dossier['dossier_id']}, "
        f"“{subject['title']}”, evidence state {qualification['state']}, "
        f"source clock {dossier['measurements'][0]['source_timestamp'] or 'unknown'}. "
        f"{SITE}/news/china/erasure/#{dossier['dossier_id']}"
    )


def _ledger_dossier(
    observation: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    indicators = _criticality_indicators(observation)
    mechanisms = _ledger_mechanisms(observation, indicators)
    if not mechanisms:
        return None
    url = _https(observation.get("url") or observation.get("source_url"))
    title = _text(observation.get("title"), 240)
    identity = f"ledger:{url or _identity(title)}"
    kind = _text(observation.get("ledger_kind"), 40).casefold()
    source_name = _text(observation.get("source"), 120) or "public deletion ledger"
    actor = _actor(observation)
    clock = iso_z(
        observation.get("detected_at")
        or observation.get("first_seen")
        or payload.get("generated_at")
    )
    if kind in {"freeweibo", "freewechat"}:
        finding = (
            f"The public {kind} ledger reported this item in a removal/recovery "
            "feed. Palimpsest retained the report but did not perform its own "
            "liveness check on the original item."
        )
        basis = "Item came from a feed explicitly declared as a public removal ledger."
        criticality = [f"ledger_kind: {kind}"]
    else:
        finding = (
            "This captured peer article explicitly reports or discusses an "
            "information-control practice. Palimpsest did not observe the peer "
            "article itself being deleted or blocked."
        )
        basis = (
            "The retained title, excerpt, tags, or terms explicitly names an "
            "information-control practice; this is attributed coverage, not a "
            "Palimpsest liveness verdict."
        )
        criticality = indicators
    dossier = {
        "dossier_id": _dossier_id(identity),
        "qualification": {
            "state": "peer_reported",
            "evidence_strength": "moderate",
            "basis": basis,
            "criticality_basis": criticality,
        },
        "subject": _subject(observation, kind="coverage_article" if kind == "cdt" else "public_item"),
        "practice": {
            "mechanisms": mechanisms,
            "finding": finding,
            "actor": actor,
            "interpretation_limit": (
                "The underlying practice remains attributed to the public source. "
                "A ledger entry is not an independent Palimpsest deletion check, "
                "and coverage about censorship is not proof that the coverage "
                "article was censored."
            ),
        },
        "timeline": _timeline(
            (
                observation.get("first_seen") or observation.get("detected_at"),
                "peer item first recorded",
                source_name,
                "source timestamp",
            ),
            (
                observation.get("last_seen") or observation.get("detected_at"),
                "peer item last recorded in this reading",
                source_name,
                "source timestamp",
            ),
        ),
        "measurements": [
            _measurement(
                identity=identity,
                filename="public-deletion-ledgers-latest.json",
                payload=payload,
                status="reported",
                match_kind="source-item",
                metric="public information-control report",
                value=(
                    f"ledger_kind={kind or 'unknown'}; "
                    f"mechanisms={'; '.join(mechanisms)}"
                ),
                interpretation_limit=(
                    "The feed item is an attributed public report, not an "
                    "independent liveness check."
                ),
                source_timestamp=clock,
            )
        ],
        "evidence": [
            _evidence(
                identity=identity,
                source_name=source_name,
                source_url=url,
                relation="attributed_source_report",
                observed_at=clock,
                claim=(
                    "Source metadata supports classification as "
                    f"{'; '.join(mechanisms)}."
                ),
                input_sha256=_sha(payload),
            )
        ],
        "counter_readings": [
            "Palimpsest has no independent item-level liveness result for the peer article."
        ],
        "unknowns": [
            "The ordering actor, motive, geographic reach, audience impact, and complete removal timeline are not established unless an evidence row explicitly says otherwise."
        ],
        "cite": "",
    }
    if actor["attribution"] != "not_established":
        dossier["evidence"].append(
            _evidence(
                identity=identity,
                source_name=source_name,
                source_url=url,
                relation="source_named_actor_relationship",
                observed_at=clock,
                claim=(
                    f"The retained source names {actor['name']} with role "
                    f"{actor['role']}."
                ),
                input_sha256=_sha(payload),
            )
        )
    dossier["cite"] = _citation(dossier)
    return dossier


def _prior_social_version(
    observation_id: str,
    social_versions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    candidates = [
        row
        for row in social_versions
        if row.get("observation_id") == observation_id
        and row.get("state") in {"published", "edited"}
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            iso_z(row.get("first_observed_at")) or "",
            _text(row.get("version_id"), 80),
        )
    )
    return candidates[-1]


def _social_dossier(
    observation: Mapping[str, Any],
    payload: Mapping[str, Any],
    social_versions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if observation.get("state") != "tombstone":
        return None
    observation_id = _text(observation.get("observation_id"), 80)
    if not observation_id:
        return None
    prior = _prior_social_version(observation_id, social_versions)
    subject_source = prior or observation
    identity = f"social:{observation_id}"
    source_name = _text(observation.get("source_name"), 120) or "social source"
    clock = iso_z(payload.get("generated_at"))
    dossier = {
        "dossier_id": _dossier_id(identity),
        "qualification": {
            "state": "observed_disappearance",
            "evidence_strength": "strong",
            "basis": (
                "The bounded social pipeline published a tombstone only after a "
                "documented API signal or reviewed reconciliation; absence alone "
                "does not create a tombstone."
            ),
            "criticality_basis": _list_text(
                subject_source.get("china_relevance_labels"), limit=120
            ),
        },
        "subject": _subject(
            subject_source,
            kind="social_post",
            fallback_title="(removed social post; prior title unavailable)",
        ),
        "practice": {
            "mechanisms": ["observed_social_tombstone"],
            "finding": (
                "The collector recorded this public post as unavailable and "
                "published a content-free tombstone. That establishes an observed "
                "disappearance at this surface, not why the post disappeared."
            ),
            "actor": _actor(observation),
            "interpretation_limit": (
                "A tombstone does not distinguish platform enforcement, author "
                "deletion, account action, legal demand, access restriction, or "
                "another cause. It is not by itself a CCP-attribution finding."
            ),
        },
        "timeline": _timeline(
            (
                subject_source.get("published_at"),
                "post published according to retained metadata",
                source_name,
                "source timestamp",
            ),
            (
                subject_source.get("first_observed_at"),
                "post first observed by bounded collector",
                "Palimpsest social collector",
                "collector timestamp",
            ),
            (
                clock,
                "latest view records content-free tombstone",
                "Palimpsest social collector",
                "reading upper-bound; exact transition time unavailable",
            ),
        ),
        "measurements": [
            _measurement(
                identity=identity,
                filename="social-observations-latest.json",
                payload=payload,
                status="observed",
                match_kind="stable-observation-id",
                metric="social observation state",
                value="tombstone",
                interpretation_limit=(
                    "Observed unavailability is not a finding about cause or actor."
                ),
                source_timestamp=clock,
            )
        ],
        "evidence": [
            _evidence(
                identity=identity,
                source_name="Palimpsest bounded social observations",
                source_url=_https(observation.get("permalink")),
                relation="collector_observed_state",
                observed_at=clock,
                claim="The latest retained state is a content-free tombstone.",
                input_sha256=_sha(payload),
            )
        ],
        "counter_readings": [
            "No retained field identifies the actor or reason for disappearance.",
            "An ordinary edit is not a tombstone and is never promoted to this dossier state.",
        ],
        "unknowns": [
            "The exact transition time, deletion notice, initiating actor, legal basis, and geographic scope are unavailable in the retained latest view."
        ],
        "cite": "",
    }
    if prior:
        dossier["measurements"].append(
            _measurement(
                identity=identity,
                filename="social-observations-versions.jsonl",
                payload=None,
                status="retained",
                match_kind="stable-observation-id-prior-version",
                metric="prior public version retained",
                value=_text(prior.get("version_id"), 80),
                interpretation_limit=(
                    "The version ledger retains bounded metadata and excerpt only; "
                    "it is not a copy of removed media."
                ),
                source_timestamp=iso_z(prior.get("first_observed_at")),
            )
        )
        dossier["measurements"][-1]["input_sha256"] = _sha(list(social_versions))
    dossier["cite"] = _citation(dossier)
    return dossier


def _weibo_pattern_dossier(
    observation: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    regime = _text(
        observation.get("regime")
        or observation.get("deletion_signal")
        or observation.get("event"),
        80,
    )
    if regime not in {"suppressed_invisible", "withdrawal_watch"}:
        return None
    raw_title = _text(observation.get("title"), 240)
    title = re.sub(r"^\[[^]]+\]\s*", "", raw_title) or _text(
        observation.get("text"), 240
    )
    terms = _list_text(observation.get("terms"), limit=120)
    identity = f"weibo:{regime}:{title}:{'|'.join(terms)}"
    if regime == "suppressed_invisible":
        state = "pattern_signal"
        strength = "context_only"
        mechanism = "permitted_attention_suppression"
        finding = (
            "A predeclared sensitive topic was absent from the retained Weibo "
            "hot-search board window while the instrument had a readable board. "
            "This is a permitted-attention pattern, not an observed deleted post."
        )
        basis = (
            "The board instrument emitted suppressed_invisible for a predeclared "
            "topic against its retained archive window."
        )
        value = f"regime=suppressed_invisible; terms={'; '.join(terms) or 'unavailable'}"
    else:
        state = "review_required"
        strength = "context_only"
        mechanism = "hot_search_withdrawal_unconfirmed"
        finding = (
            "The board instrument placed this headline on withdrawal watch after "
            "a retained-board transition. No post URL, takedown notice, or actor "
            "was observed, so the candidate remains unconfirmed."
        )
        basis = (
            "The board instrument emitted withdrawal_watch. Semantic relevance "
            "and a censorship explanation require independent review."
        )
        value = f"regime=withdrawal_watch; terms={'; '.join(terms) or 'unavailable'}"
    synthetic = dict(observation)
    synthetic["title"] = title
    synthetic["source"] = "Weibo hot-search archive"
    dossier = {
        "dossier_id": _dossier_id(identity),
        "qualification": {
            "state": state,
            "evidence_strength": strength,
            "basis": basis,
            "criticality_basis": [f"predeclared term: {term}" for term in terms],
        },
        "subject": _subject(synthetic, kind="topic_or_headline"),
        "practice": {
            "mechanisms": [mechanism],
            "finding": finding,
            "actor": _actor(observation),
            "interpretation_limit": (
                "Board absence can reflect ranking, ordinary lack of attention, "
                "archive coverage, wording drift, or term-sense mismatch. It does "
                "not establish deletion, motive, or CCP/platform action."
            ),
        },
        "timeline": _timeline(
            (
                observation.get("first_seen"),
                "headline or topic first seen",
                "Weibo hot-search archive",
                "archive timestamp",
            ),
            (
                observation.get("last_seen") or payload.get("generated_at"),
                f"board classifier emitted {regime}",
                "Palimpsest Weibo-board instrument",
                "reading timestamp",
            ),
        ),
        "measurements": [
            _measurement(
                identity=identity,
                filename="weibo-hotsearch-latest.json",
                payload=payload,
                status="pattern" if state == "pattern_signal" else "review_required",
                match_kind="declared-board-regime",
                metric="permitted-attention board regime",
                value=value,
                interpretation_limit=(
                    "A board regime is topic/headline-level evidence, not an "
                    "item-level removal confirmation."
                ),
            )
        ],
        "evidence": [
            _evidence(
                identity=identity,
                source_name="Palimpsest Weibo hot-search archive",
                source_url=READING_URLS["weibo-hotsearch-latest.json"],
                relation="board_pattern",
                observed_at=payload.get("generated_at"),
                claim=f"The retained board classifier emitted {regime}.",
                input_sha256=_sha(payload),
            )
        ],
        "counter_readings": [
            "No exact social-post URL or platform deletion notice is attached.",
            "A headline-board transition is not equivalent to removing the underlying post or article.",
        ],
        "unknowns": [
            "Whether the topic was posted elsewhere, why its board state changed, and who caused any change are not established."
        ],
        "cite": "",
    }
    dossier["cite"] = _citation(dossier)
    return dossier


def _add_measurement(dossier: dict[str, Any], measurement: dict[str, Any]) -> None:
    if measurement["measurement_id"] not in {
        row["measurement_id"] for row in dossier["measurements"]
    }:
        dossier["measurements"].append(measurement)


def _add_exact_url_measurements(
    dossiers: list[dict[str, Any]],
    payloads: Mapping[str, Mapping[str, Any] | None],
) -> None:
    by_url = {
        dossier["subject"]["url"]: dossier
        for dossier in dossiers
        if dossier["subject"]["url"]
    }
    if not by_url:
        return

    undertext = payloads.get("undertext-latest.json") or {}
    for observation in undertext.get("observations") or []:
        if not isinstance(observation, dict):
            continue
        url = _https(observation.get("url") or observation.get("source_url"))
        dossier = by_url.get(url)
        if not dossier:
            continue
        signal = _text(
            observation.get("deletion_signal")
            or observation.get("event")
            or observation.get("regime"),
            80,
        ) or "no item-level transition"
        identity = f"joined:{dossier['dossier_id']}"
        _add_measurement(
            dossier,
            _measurement(
                identity=identity,
                filename="undertext-latest.json",
                payload=undertext,
                status="joined",
                match_kind="exact-url-derived-projection",
                metric="UNDERTEXT item signal",
                value=signal,
                interpretation_limit=(
                    "UNDERTEXT is a derived fusion and is not independent "
                    "corroboration of its ledger or archive input."
                ),
                source_timestamp=observation.get("last_seen")
                or undertext.get("generated_at"),
            ),
        )

    wayback = payloads.get("wayback-latest.json") or {}
    for reconstruction in wayback.get("reconstructions") or []:
        if not isinstance(reconstruction, dict):
            continue
        url = _https(reconstruction.get("url"))
        dossier = by_url.get(url)
        if not dossier:
            continue
        event = _text(reconstruction.get("event"), 80) or "unknown"
        identity = f"joined:{dossier['dossier_id']}"
        _add_measurement(
            dossier,
            _measurement(
                identity=identity,
                filename="wayback-latest.json",
                payload=wayback,
                status="archive_context",
                match_kind="exact-url",
                metric="Wayback CDX transition label",
                value=event,
                interpretation_limit=(
                    "A CDX deletion/mutation label is an archive transition, not "
                    "a live deletion or censorship verdict. no_baseline and "
                    "unreachable are archive gaps."
                ),
                source_timestamp=reconstruction.get("last_capture")
                or wayback.get("generated_at"),
            ),
        )

    ddti = payloads.get("ddti-latest.json") or {}
    ddti_hits: dict[str, list[Mapping[str, Any]]] = {}
    for ranked in ddti.get("ranked") or []:
        if not isinstance(ranked, dict):
            continue
        for sample in ranked.get("samples") or []:
            if not isinstance(sample, dict):
                continue
            url = _https(sample.get("url"))
            if url in by_url:
                ddti_hits.setdefault(url, []).append(ranked)
    for url, hits in ddti_hits.items():
        dossier = by_url[url]
        terms = sorted(
            {
                _text(hit.get("term"), 120)
                for hit in hits
                if _text(hit.get("term"), 120)
            }
        )
        identity = f"joined:{dossier['dossier_id']}"
        _add_measurement(
            dossier,
            _measurement(
                identity=identity,
                filename="ddti-latest.json",
                payload=ddti,
                status="reported_attention",
                match_kind="exact-sample-url",
                metric="DDTI terms attached to exact source URL",
                value="; ".join(terms[:24]),
                interpretation_limit=(
                    "DDTI measures attention in collected public reports. It does "
                    "not independently verify deletion, actor, or motive."
                ),
            ),
        )


def _collector_receipts(
    payloads: Mapping[str, Mapping[str, Any] | None]
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    social = payloads.get("social-observations-latest.json") or {}
    coverage = social.get("coverage")
    if isinstance(coverage, dict):
        for row in coverage.get("receipts") or []:
            if not isinstance(row, dict):
                continue
            receipts.append(
                {
                    "family": "social",
                    "source_id": _text(row.get("source_id"), 80),
                    "status": _text(row.get("status"), 40),
                    "accepted": row.get("accepted") if type(row.get("accepted")) is int else 0,
                    "note": (
                        "A failed or not-attempted source is a coverage gap; it is "
                        "not a zero-censorship result."
                    ),
                }
            )
    ledgers = payloads.get("public-deletion-ledgers-latest.json") or {}
    for row in ledgers.get("ledgers") or []:
        if not isinstance(row, dict):
            continue
        receipts.append(
            {
                "family": "public_ledger",
                "source_id": _text(row.get("name"), 80),
                "status": _text(row.get("status"), 40),
                "accepted": row.get("n_observations")
                if type(row.get("n_observations")) is int
                else 0,
                "note": (
                    "A feed may contain reporting about censorship rather than "
                    "items that were themselves removed. Qualification is item-level."
                ),
            }
        )
    return receipts


def _exclusion_counts(
    payloads: Mapping[str, Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    ledgers = payloads.get("public-deletion-ledgers-latest.json") or {}
    for observation in ledgers.get("observations") or []:
        if isinstance(observation, dict) and not _ledger_mechanisms(
            observation, _criticality_indicators(observation)
        ):
            counts["ordinary_peer_coverage_without_explicit_information_control_basis"] += 1
    social = payloads.get("social-observations-latest.json") or {}
    for observation in social.get("observations") or []:
        if isinstance(observation, dict) and observation.get("state") != "tombstone":
            counts["social_published_or_edited_not_a_disappearance"] += 1
    wayback = payloads.get("wayback-latest.json") or {}
    for row in wayback.get("reconstructions") or []:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        if event in {"no_baseline", "unreachable"}:
            counts["archive_gap_or_unreachable_not_censorship"] += 1
        elif event in {"deletion", "mutation"}:
            counts["unmatched_archive_transition_not_piece_level_censorship"] += 1
    weibo = payloads.get("weibo-hotsearch-latest.json") or {}
    for row in weibo.get("observation_records") or []:
        if not isinstance(row, dict):
            continue
        regime = row.get("regime") or row.get("deletion_signal") or row.get("event")
        if regime not in {"suppressed_invisible", "withdrawal_watch"}:
            counts["visible_or_lexical_board_result_not_suppression"] += 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counts.items())
        if count
    ]


def build_document(
    payloads: Mapping[str, Mapping[str, Any] | None],
    *,
    generated_at: str,
    social_versions: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build every qualifying dossier from the supplied retained inputs."""

    clock = iso_z(generated_at)
    if not clock:
        raise CensorshipDossierError("generated_at must be an explicit UTC clock")
    versions = tuple(row for row in social_versions if isinstance(row, Mapping))
    dossiers: list[dict[str, Any]] = []

    ledger = payloads.get("public-deletion-ledgers-latest.json") or {}
    for observation in ledger.get("observations") or ledger.get("observation_records") or []:
        if isinstance(observation, dict):
            dossier = _ledger_dossier(observation, ledger)
            if dossier:
                dossiers.append(dossier)

    social = payloads.get("social-observations-latest.json") or {}
    for observation in social.get("observations") or []:
        if isinstance(observation, dict):
            dossier = _social_dossier(observation, social, versions)
            if dossier:
                dossiers.append(dossier)

    weibo = payloads.get("weibo-hotsearch-latest.json") or {}
    for observation in weibo.get("observation_records") or []:
        if isinstance(observation, dict):
            dossier = _weibo_pattern_dossier(observation, weibo)
            if dossier:
                dossiers.append(dossier)

    unique: dict[str, dict[str, Any]] = {}
    for dossier in dossiers:
        if dossier["dossier_id"] in unique:
            raise CensorshipDossierError("duplicate dossier identity")
        unique[dossier["dossier_id"]] = dossier
    dossiers = list(unique.values())
    _add_exact_url_measurements(dossiers, payloads)

    state_order = {
        "observed_disappearance": 0,
        "peer_reported": 1,
        "pattern_signal": 2,
        "review_required": 3,
    }
    dossiers.sort(
        key=lambda row: (
            state_order[row["qualification"]["state"]],
            -(int((row["subject"]["last_seen"] or "0000")[:4])),
            row["subject"]["title"].casefold(),
            row["dossier_id"],
        )
    )
    state_counts = Counter(row["qualification"]["state"] for row in dossiers)
    receipts = _input_receipts(payloads, versions)
    exclusions = _exclusion_counts(payloads)
    reviewed = sum(row["records_reviewed"] for row in receipts)
    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": clock,
        "status": "live" if dossiers else "coverage_gap",
        "source": (
            "Deterministic projection of retained public deletion-ledger, social "
            "tombstone, Weibo-board, DDTI, UNDERTEXT, and Wayback evidence"
        ),
        "method": (
            "Item-level qualification before presentation; exact URL or stable "
            "observation-ID joins only; every qualifying captured item is emitted. "
            "No network fetch, sentiment model, fuzzy cross-piece join, causal "
            "inference, or default CCP/state/platform attribution. Method v1."
        ),
        "scope": (
            "Every qualifying item in the supplied retained inputs, not every post "
            "on the internet. Critical reporting without censorship evidence is not "
            "called censored. Coverage receipts expose missing and unattempted doors."
        ),
        "actor_attribution_policy": (
            "Name CCP, a PRC authority, a platform, or another actor only when the "
            "retained evidence explicitly names that actor. Otherwise publish "
            "not_established."
        ),
        "counts": {
            "captured_items_reviewed": reviewed,
            "dossiers": len(dossiers),
            "observed_disappearances": state_counts["observed_disappearance"],
            "peer_reported": state_counts["peer_reported"],
            "pattern_signals": state_counts["pattern_signal"],
            "review_required": state_counts["review_required"],
            "excluded_items": sum(row["count"] for row in exclusions),
        },
        "coverage": {
            "selection": "every-captured-qualifying-item",
            "inputs": receipts,
            "collector_receipts": _collector_receipts(payloads),
            "exclusions": exclusions,
        },
        "dossiers": dossiers,
    }
    validate_document(document)
    return document


def _exact_fields(value: Any, expected: frozenset[str], path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise CensorshipDossierError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise CensorshipDossierError(
            f"{path} fields drifted (missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)})"
        )
    return value


def validate_document(document: Mapping[str, Any]) -> None:
    """Validate identities, claim boundaries, and complete count projections."""

    top = _exact_fields(document, _TOP_FIELDS, "document")
    if top["schema_version"] != SCHEMA_VERSION or not iso_z(top["generated_at"]):
        raise CensorshipDossierError("document identity or clock is invalid")
    if top["status"] not in {"live", "coverage_gap"}:
        raise CensorshipDossierError("document status is invalid")
    dossiers = top["dossiers"]
    if type(dossiers) is not list:
        raise CensorshipDossierError("document.dossiers must be an array")
    seen: set[str] = set()
    state_counts: Counter[str] = Counter()
    for index, raw in enumerate(dossiers):
        dossier = _exact_fields(raw, _DOSSIER_FIELDS, f"dossiers[{index}]")
        dossier_id = dossier["dossier_id"]
        if not isinstance(dossier_id, str) or not _DOSSIER_ID_RE.fullmatch(dossier_id):
            raise CensorshipDossierError("dossier identity is invalid")
        if dossier_id in seen:
            raise CensorshipDossierError("dossier identity is duplicated")
        seen.add(dossier_id)
        qualification = _exact_fields(
            dossier["qualification"],
            _QUALIFICATION_FIELDS,
            f"dossiers[{index}].qualification",
        )
        if qualification["state"] not in QUALIFICATION_STATES:
            raise CensorshipDossierError("qualification state is invalid")
        if qualification["evidence_strength"] not in EVIDENCE_STRENGTHS:
            raise CensorshipDossierError("evidence strength is invalid")
        if type(qualification["criticality_basis"]) is not list:
            raise CensorshipDossierError("criticality basis must be an array")
        state_counts[qualification["state"]] += 1

        subject = _exact_fields(
            dossier["subject"], _SUBJECT_FIELDS, f"dossiers[{index}].subject"
        )
        if not subject["title"] or not _SHA256_RE.fullmatch(subject["content_sha256"]):
            raise CensorshipDossierError("dossier subject is incomplete")
        if subject["url"] and not subject["url"].startswith("https://"):
            raise CensorshipDossierError("dossier subject URL is unsafe")

        practice = _exact_fields(
            dossier["practice"], _PRACTICE_FIELDS, f"dossiers[{index}].practice"
        )
        actor = _exact_fields(
            practice["actor"], _ACTOR_FIELDS, f"dossiers[{index}].practice.actor"
        )
        if actor["attribution"] not in ACTOR_ATTRIBUTIONS:
            raise CensorshipDossierError("actor attribution is invalid")
        if actor["role"] not in ACTOR_ROLES:
            raise CensorshipDossierError("actor role is invalid")
        if actor["attribution"] == "not_established":
            if actor["name"] is not None or actor["role"] != "not_established":
                raise CensorshipDossierError("unknown actor cannot have a name or role")
        elif not actor["name"] or actor["role"] == "not_established":
            raise CensorshipDossierError("attributed actor requires a name and role")
        if type(practice["mechanisms"]) is not list or not practice["mechanisms"]:
            raise CensorshipDossierError("practice mechanism is missing")

        for t_index, raw_timeline in enumerate(dossier["timeline"]):
            timeline = _exact_fields(
                raw_timeline,
                _TIMELINE_FIELDS,
                f"dossiers[{index}].timeline[{t_index}]",
            )
            if not iso_z(timeline["at"]):
                raise CensorshipDossierError("timeline clock is invalid")

        measurement_ids: set[str] = set()
        if not dossier["measurements"]:
            raise CensorshipDossierError("dossier must retain a measurement")
        for m_index, raw_measurement in enumerate(dossier["measurements"]):
            measurement = _exact_fields(
                raw_measurement,
                _MEASUREMENT_FIELDS,
                f"dossiers[{index}].measurements[{m_index}]",
            )
            measurement_id = measurement["measurement_id"]
            if (
                not isinstance(measurement_id, str)
                or not _MEASUREMENT_ID_RE.fullmatch(measurement_id)
                or measurement_id in measurement_ids
            ):
                raise CensorshipDossierError("measurement identity is invalid")
            measurement_ids.add(measurement_id)
            if not measurement["reading_url"].startswith("https://"):
                raise CensorshipDossierError("measurement reading URL is unsafe")
            if measurement["source_timestamp"] is not None and not iso_z(
                measurement["source_timestamp"]
            ):
                raise CensorshipDossierError("measurement source clock is invalid")
            digest = measurement["input_sha256"]
            if digest is not None and not _SHA256_RE.fullmatch(digest):
                raise CensorshipDossierError("measurement input digest is invalid")

        evidence_ids: set[str] = set()
        if not dossier["evidence"]:
            raise CensorshipDossierError("dossier must retain evidence")
        for e_index, raw_evidence in enumerate(dossier["evidence"]):
            evidence = _exact_fields(
                raw_evidence,
                _EVIDENCE_FIELDS,
                f"dossiers[{index}].evidence[{e_index}]",
            )
            evidence_id = evidence["evidence_id"]
            if (
                not isinstance(evidence_id, str)
                or not _EVIDENCE_ID_RE.fullmatch(evidence_id)
                or evidence_id in evidence_ids
            ):
                raise CensorshipDossierError("evidence identity is invalid")
            evidence_ids.add(evidence_id)
            if not _SHA256_RE.fullmatch(evidence["input_sha256"]):
                raise CensorshipDossierError("evidence input digest is invalid")
        claim_text = json.dumps(dossier, ensure_ascii=False).casefold()
        if any(phrase in claim_text for phrase in _FORBIDDEN_CAUSAL):
            raise CensorshipDossierError("dossier contains forbidden causal language")

    counts = top["counts"]
    expected_counts = {
        "captured_items_reviewed",
        "dossiers",
        "observed_disappearances",
        "peer_reported",
        "pattern_signals",
        "review_required",
        "excluded_items",
    }
    if type(counts) is not dict or set(counts) != expected_counts:
        raise CensorshipDossierError("dossier counts drifted")
    if counts["dossiers"] != len(dossiers):
        raise CensorshipDossierError("dossier count is inconsistent")
    expected_states = {
        "observed_disappearances": state_counts["observed_disappearance"],
        "peer_reported": state_counts["peer_reported"],
        "pattern_signals": state_counts["pattern_signal"],
        "review_required": state_counts["review_required"],
    }
    if any(counts[field] != value for field, value in expected_states.items()):
        raise CensorshipDossierError("qualification counts are inconsistent")
    if top["status"] == "live" and not dossiers:
        raise CensorshipDossierError("live status requires at least one dossier")
    if top["status"] == "coverage_gap" and dossiers:
        raise CensorshipDossierError("coverage-gap status cannot contain dossiers")


__all__ = [
    "CensorshipDossierError",
    "INPUT_FILES",
    "METHOD_VERSION",
    "READING_URLS",
    "SCHEMA_VERSION",
    "build_document",
    "validate_document",
]
