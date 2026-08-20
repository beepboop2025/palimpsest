"""Archive Save Page Now lookups: snapshot only when the API named one."""

from __future__ import annotations

from collectors.archive_capture import (
    attach_new_url_captures,
    parse_wayback_snapshot,
    previous_urls_from_reading,
    request_wayback_save,
)


def test_parse_wayback_snapshot_requires_a_timestamped_ia_url():
    body = 'See https://web.archive.org/web/20260820120000/https://www.gov.cn/ for the capture.'
    assert parse_wayback_snapshot(body) == (
        "https://web.archive.org/web/20260820120000/https://www.gov.cn/"
    )
    assert parse_wayback_snapshot("saved, but no snapshot URL in this body") is None
    assert parse_wayback_snapshot("") is None


def test_request_wayback_save_does_not_invent_a_snapshot():
    capture = request_wayback_save("https://www.gov.cn/", fetch=lambda url: "queued")
    assert capture["save_requested"] is True
    assert capture["wayback_snapshot"] is None
    assert capture["archive_today_lookup"] == "https://archive.today/https://www.gov.cn/"


def test_attach_only_requests_saves_for_new_https_urls():
    seen = {"https://www.gov.cn/"}
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        return "https://web.archive.org/web/20260820120000/https://www.news.cn/"

    rows = attach_new_url_captures(
        [
            {"url": "https://www.gov.cn/", "archive": {}},
            {"url": "https://www.news.cn/", "archive": {}},
        ],
        previous_urls=seen,
        fetch=fetch,
        limit=8,
    )
    assert len(requested) == 1
    assert "save/" in requested[0]
    assert rows[0]["archive"].get("wayback_snapshot") is None
    assert rows[1]["archive"]["wayback_snapshot"] == (
        "https://web.archive.org/web/20260820120000/https://www.news.cn/"
    )


def test_previous_urls_from_missing_reading_are_empty(tmp_path):
    assert previous_urls_from_reading(tmp_path / "missing.json") == set()
    (tmp_path / "reading.json").write_text(
        '{"observations":[{"url":"https://www.gov.cn/"}]}',
        encoding="utf-8",
    )
    assert previous_urls_from_reading(tmp_path / "reading.json") == {"https://www.gov.cn/"}
