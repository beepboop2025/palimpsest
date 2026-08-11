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
        request_timeout_s=5.0, confirmations=3, archive_dir=archive_dir,
        velocity_window_min=60, velocity_baseline_windows=24, spike_z_threshold=3.0,
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
        f = _FakeFetcher(
            html='<html><body><article>' + ('帖子正文内容仍然存在。' * 8)
            + '<img src="/p/1.jpg"></article></body></html>'
        )
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


def test_validation_shell_is_rejected_before_directories_or_images():
    shell = (
        '<html><head><link href="/validate.css" rel="stylesheet">'
        '<script src="/validate.js"></script></head><body>验证</body></html>'
    )
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        f = _FakeFetcher(html=shell)
        path = asyncio.run(archive_post(
            "https://guba.eastmoney.com/news,600519,9.html",
            "eastmoney_guba", "9", fetcher=f, settings=s,
        ))
        assert path is None
        assert f.byte_fetches == 0, "images must not be touched before LIVE validation"
        assert not (Path(tmp) / "eastmoney_guba" / "9").exists()


def test_deleted_notice_is_not_archived_as_live_evidence():
    deleted = "<html><body>" + ("页面框架内容" * 20) + "该帖子可能已被删除</body></html>"
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        f = _FakeFetcher(html=deleted)
        path = asyncio.run(archive_post(
            "https://guba.eastmoney.com/news,600519,10.html",
            "eastmoney_guba", "10", fetcher=f, settings=s,
            deletion_markers=("该帖子可能已被删除",),
        ))
        assert path is None
        assert not (Path(tmp) / "eastmoney_guba" / "10").exists()


def test_existing_incomplete_or_shell_archive_is_not_blessed():
    shell = (
        '<html><head><link href="validate.css"><script src="validate.js"></script>'
        '</head><body>验证</body></html>'
    )
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        base = Path(tmp) / "eastmoney_guba" / "11"
        base.mkdir(parents=True)
        (base / "page.html").write_text(shell, encoding="utf-8")
        f = _FakeFetcher(html="healthy content " * 20)
        path = asyncio.run(archive_post(
            "https://guba.eastmoney.com/news,600519,11.html",
            "eastmoney_guba", "11", fetcher=f, settings=s,
        ))
        assert path is None
        assert f.page_fetches == 0, "existing evidence is never overwritten in place"


def test_redirected_or_transport_tainted_page_stays_retryable():
    from censorwatch.interfaces import FetchResult

    class _Redirected:
        def __init__(self, *, error=None):
            self.error = error
            self.byte_fetches = 0
        async def fetch(self, url, **kw):
            return FetchResult(
                url=url,
                status=200,
                text="真实正文内容" * 20,
                final_url="https://passport.eastmoney.com/login",
                error=self.error,
            )
        async def fetch_bytes(self, url, **kw):
            self.byte_fetches += 1
            return 200, b"image", None

    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        path = asyncio.run(archive_post(
            "https://guba.eastmoney.com/news,600519,12.html",
            "eastmoney_guba", "12", fetcher=_Redirected(), settings=s,
        ))
        assert path is None
        assert not (Path(tmp) / "eastmoney_guba" / "12").exists()

        tainted = _Redirected(error="partial transport failure")
        path = asyncio.run(archive_post(
            "https://guba.eastmoney.com/news,600519,13.html",
            "eastmoney_guba", "13", fetcher=tainted, settings=s,
        ))
        assert path is None
        assert not (Path(tmp) / "eastmoney_guba" / "13").exists()


def _run_all():
    test_extract_image_urls()
    print("  PASS extract_image_urls")
    test_archive_writes_snapshot_and_is_idempotent()
    print("  PASS snapshot_idempotent")
    test_archive_returns_none_on_bad_fetch()
    print("  PASS none_on_bad_fetch")
    print("\n3/3 archiver checks passed")


if __name__ == "__main__":
    _run_all()
