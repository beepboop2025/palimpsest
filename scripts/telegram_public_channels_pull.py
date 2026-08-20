"""Pull public Telegram channel previews and land ScamShield inbox capsules.

Primary path is keyless ``t.me/s/`` HTML. Optional Bot API ``getChat`` runs only
when a token is already in the environment (never invented, never committed).
``getUpdates`` is not used.

ScamShield: drain ``var/scamshield-inbox`` through the existing sanitized feed
so capsules actually land on the beat. Public whispers / telegram-watch stay
review-gated. This runner never writes ``telegram-watch-latest.json``.

Usage:  PYTHONPATH=. python -m scripts.telegram_public_channels_pull
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from collectors.archive_capture import attach_new_url_captures
from collectors.telegram_public_channels import (
    collect_channels,
    load_channels,
    load_join_index,
)
from core.china_observation import iso_z, serialize_observation
from core.governance import KillSwitch, RateCeiling
from core.safe_fetch import FetchError, safe_fetch
from evidence.capsule import CapsuleError
from scripts.scamshield_feed import publication_candidate


ROOT = Path(__file__).resolve().parent.parent
READINGS = ROOT / "readings"
OUT = READINGS / "telegram-public-channels-latest.json"
HIST = READINGS / "telegram-public-channels-history.jsonl"
STATE = ROOT / "data" / "telegram-public-channels" / "state.json"
INBOX = ROOT / "var" / "scamshield-inbox"
USER_AGENT = (
    "Palimpsest/0.2 (+https://palimpsest.info; open-source censorship "
    "research; public Telegram channel preview only)"
)
JOIN_FILES = (
    "official-first-seen-latest.json",
    "public-deletion-ledgers-latest.json",
    "weibo-hotsearch-latest.json",
    "wayback-latest.json",
)


def _http_fetch(url: str) -> tuple[int, str]:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    try:
        body = safe_fetch(
            url,
            max_bytes=1024 * 1024,
            timeout=25,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            proxy=proxy,
        )
        return 200, body
    except FetchError as exc:
        message = str(exc)
        if message.startswith("http status "):
            token = message.rsplit(" ", 1)[-1]
            if token.isdigit():
                return int(token), ""
        raise OSError(message) from exc


def _save_text(url: str) -> str:
    proxy = os.getenv("PALIMPSEST_PROXY", "").strip() or None
    return safe_fetch(
        url,
        max_bytes=512 * 1024,
        timeout=25,
        headers={"User-Agent": USER_AGENT},
        proxy=proxy,
    )


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _join_readings(readings_dir: Path) -> dict[str, dict]:
    return {name: _load_json(readings_dir / name) for name in JOIN_FILES}


def optional_bot_token() -> str:
    """Reuse a token already in the environment. Never invent or commit one."""

    return (
        os.getenv("PALIMPSEST_TELEGRAM_COLLECT_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )


def bot_get_chat(handle: str, *, token: str, fetch) -> dict | None:
    """Public-channel reachability only. Never getUpdates / DMs / private chats."""

    if not token or not handle:
        return None
    api = f"https://api.telegram.org/bot{token}/getChat?chat_id=@{handle}"
    try:
        status, body = fetch(api)
    except OSError:
        return None
    if status != 200:
        return {"ok": False, "http_status": status}
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") if payload.get("ok") else None
    if not isinstance(result, dict):
        return {"ok": False, "http_status": status}
    kind = result.get("type")
    if kind != "channel":
        return {"ok": False, "reason": "not-a-channel"}
    return {
        "ok": True,
        "type": "channel",
        "title": result.get("title") if isinstance(result.get("title"), str) else None,
        "username": result.get("username") if isinstance(result.get("username"), str) else None,
    }


def drain_scamshield_inbox(inbox: Path) -> dict:
    """Run the existing sanitized feed over the local inbox. Never publishes."""

    if not inbox.is_dir():
        return {
            "status": "inbox-absent",
            "n_capsules": 0,
            "n_candidates": 0,
            "n_echo_family": 0,
            "families": [],
            "automatic_publication": False,
        }
    n_capsules = 0
    n_candidates = 0
    n_echo = 0
    families: Counter[str] = Counter()
    for path in sorted(inbox.glob("*.json"))[:1000]:
        if path.is_symlink():
            continue
        n_capsules += 1
        try:
            capsule = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(capsule, dict):
                continue
            record = publication_candidate(capsule, include_typology_matches=False)
        except (OSError, ValueError, TypeError, CapsuleError):
            continue
        n_candidates += 1
        for hyp in record.get("hypotheses") or []:
            if isinstance(hyp, dict) and hyp.get("typology_id"):
                families[str(hyp["typology_id"])] += 1
        threat = record.get("threat_assessment") if isinstance(record.get("threat_assessment"), dict) else {}
        labels = list(threat.get("families") or []) + list(record.get("families") or [])
        blob = " ".join(str(item) for item in labels)
        if any(token in blob.lower() for token in ("echo", "archiv", "delet", "censor")):
            n_echo += 1
    return {
        "status": "drained",
        "n_capsules": n_capsules,
        "n_candidates": n_candidates,
        "n_echo_family": n_echo,
        "families": [{"id": key, "n": n} for key, n in families.most_common(12)],
        "automatic_publication": False,
    }


def main(
    *,
    fetch=None,
    now: datetime | None = None,
    state_path: Path | None = None,
    readings_dir: Path | None = None,
    inbox: Path | None = None,
    bot_fetch=None,
) -> dict | None:
    kill = KillSwitch()
    if kill.is_halted():
        print("telegram-public-channels: halted by kill switch — abstaining")
        return None

    state_file = state_path or STATE
    previous = _load_json(state_file)
    live_fetch = fetch or _http_fetch
    readings = _join_readings(readings_dir or READINGS)
    result = collect_channels(
        channels=load_channels(),
        fetch=live_fetch,
        join_index=load_join_index(readings),
        previous=previous,
        kill_switch=kill,
        rate_ceiling=None if fetch is not None else RateCeiling(rate=0.4, capacity=2.0),
        now=now or datetime.now(timezone.utc),
    )
    if result["n_channels_ok"] == 0 and result["n_observations"] == 0:
        print(
            "telegram-public-channels: every public preview silent or login-walled — abstaining "
            f"(channels={[row['status'] for row in result['channels']]})"
        )
        return None

    prior_urls = {
        url for url, row in (previous.get("posts") or {}).items()
        if isinstance(row, dict) and row.get("content_sha256")
    }
    observations = attach_new_url_captures(
        [serialize_observation(obs) for obs in result["observations"]],
        previous_urls=prior_urls,
        fetch=_save_text if fetch is None else None,
        limit=6,
    )

    bot_note = {"status": "unused", "n_ok": 0}
    token = optional_bot_token() if fetch is None or bot_fetch is not None else ""
    if token:
        ok = 0
        for channel in load_channels():
            info = bot_get_chat(channel["handle"], token=token, fetch=bot_fetch or live_fetch)
            if info and info.get("ok"):
                ok += 1
        bot_note = {"status": "getChat-only", "n_ok": ok, "n_channels": result["n_channels"]}

    scamshield = drain_scamshield_inbox(inbox or INBOX)
    generated = iso_z(result["generated_at"]) or iso_z(datetime.now(timezone.utc))
    out = {
        "generated_at": generated,
        "method_version": 1,
        "source": "Public Telegram channel HTML previews (Dragon Den)",
        "scope": (
            "Fat warehouse records for named public channels already in-tree. "
            "Full public text, hashes, first-seen, outbound links, gazetteer, "
            "and joins to official/ledger/Weibo/Wayback when a real record matches. "
            "Public whispers and telegram-watch stay review-gated. No DMs, bots, "
            "private groups, phones, or IOCs."
        ),
        "method": (
            "Keyless GET of https://t.me/s/{handle}; parse tgme_widget_message; "
            "optional Bot API getChat if a token is already present; ScamShield "
            "inbox drained through the existing sanitized feed."
        ),
        "n_channels": result["n_channels"],
        "n_channels_ok": result["n_channels_ok"],
        "n_observations": len(observations),
        "n_mainland_echo": result["n_mainland_echo"],
        "channels": result["channels"],
        "bot_api": bot_note,
        "scamshield": scamshield,
        "observations": observations,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"generated_at": generated, "posts": result["posts"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    READINGS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HIST.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": generated,
            "n_channels_ok": out["n_channels_ok"],
            "n_observations": out["n_observations"],
            "n_mainland_echo": out["n_mainland_echo"],
            "scamshield_candidates": scamshield["n_candidates"],
        }, ensure_ascii=False) + "\n")
    print(
        f"telegram-public-channels: {out['n_channels_ok']}/{out['n_channels']} channels, "
        f"{out['n_observations']} posts, {out['n_mainland_echo']} mainland-echo, "
        f"scamshield={scamshield['status']}:{scamshield['n_candidates']}"
    )
    return out


if __name__ == "__main__":
    main()
