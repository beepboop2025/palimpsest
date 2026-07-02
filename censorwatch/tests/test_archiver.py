"""Tests for the archiver — snapshots page + images to disk, idempotently.

    python3 -m pytest censorwatch/tests/test_archiver.py
    python3 censorwatch/tests/test_archiver.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from censorwatch.archiver import archive_post, extract_image_urls
from censorwatch.config import CensorwatchSettings


def _settings(archive_dir: str) -> CensorwatchSettings:
    return CensorwatchSettings(
        enabled=True, proxy_url=None, min_delay_s=0.0, max_delay_s=0.0,
        request_timeout_s=5.0, collect_concurrency=4, recheck_concurrency=12,
        confirmations=3, archive_dir=archive_dir,
        velocity_window_min=60, velocity_baseline_windows=24, spike_z_threshold=3.0,
        cloud_sync_enabled=False, cloud_bucket=None, cloud_region="auto",
        cloud_endpoint_url=None, cloud_prefix="palimpsest/censorwatch",
        cloud_lookback_hours=24, cloud_include_archive=False,
        consolidate_lookback_hours=24, consolidate_max_rows=50000,
        promotion_gate_enabled=True, fusion_lookback_hours=48, fusion_alert_z=2.0,
    )


class _FakeFetcher:
    def __init__(self, html="<html></html>"):
        self.html = html
        self.page_fetches = 0
        self.byte_fetches = 0

    async def fetch(self, url, **kw):
        self.page_fetches += 1
        from censorwatch.interfaces import FetchResult
        return FetchResult(url=url, status=200, text=self.html, final_url=url)

    async def fetch_bytes(self, url, **kw):
        self.byte_fetches += 1
        return 200, b"\x89PNG\r\n\x1a\n fake image bytes", None


def test_extract_image_urls():
    html = ('<img src="/pic/a.png"><img data-src="https://cdn.x/b.jpg">'
            '<img src="data:image/png;base64,zzz"><img src="/pic/a.png">')
    urls = extract_image_urls(html, "https://guba.eastmoney.com/news,600519,1.html")
    assert urls == ["https://guba.eastmoney.com/pic/a.png", "https://cdn.x/b.jpg"], urls
    # relative resolved, data: skipped, duplicate deduped


def test_archive_writes_snapshot_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        f = _FakeFetcher(html='<html><body>帖子正文<img src="/p/1.jpg"></body></html>')
        # raw_html omitted → archiver fetches the page itself
        path = asyncio.run(archive_post("https://guba.eastmoney.com/news,600519,9.html",
                                        "eastmoney_guba", "9", fetcher=f, settings=s))
        base = Path(path)
        assert (base / "page.html").exists()
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        assert meta["source"] == "eastmoney_guba" and meta["post_id"] == "9"
        assert meta["n_images"] == 1 and len(meta["content_hash"]) == 64
        assert (base / meta["images"][0]["file"]).exists()
        assert f.page_fetches == 1 and f.byte_fetches == 1

        # Idempotent: second call returns same path, does NOT re-fetch/overwrite.
        path2 = asyncio.run(archive_post("https://guba.eastmoney.com/news,600519,9.html",
                                         "eastmoney_guba", "9", fetcher=f, settings=s))
        assert path2 == path
        assert f.page_fetches == 1, "must not re-fetch an already-archived post"


def test_archive_returns_none_on_bad_fetch():
    class _Dead:
        async def fetch(self, url, **kw):
            from censorwatch.interfaces import FetchResult
            return FetchResult(url=url, status=403, text=None)
        async def fetch_bytes(self, url, **kw):
            return None, None, "blocked"

    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        path = asyncio.run(archive_post("https://x/y", "eastmoney_guba", "bad",
                                        fetcher=_Dead(), settings=s))
        assert path is None  # nothing written → retried next capture
        assert not (Path(tmp) / "eastmoney_guba" / "bad").exists()


def _run_all():
    test_extract_image_urls(); print("  PASS extract_image_urls")
    test_archive_writes_snapshot_and_is_idempotent(); print("  PASS snapshot_idempotent")
    test_archive_returns_none_on_bad_fetch(); print("  PASS none_on_bad_fetch")
    print("\n3/3 archiver checks passed")


if __name__ == "__main__":
    _run_all()
