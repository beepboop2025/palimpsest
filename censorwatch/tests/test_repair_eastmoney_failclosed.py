"""Regression tests for the dry-run-first Eastmoney incident repair."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from censorwatch.repair_eastmoney_failclosed import (
    ABSTENTION_SCOPE,
    apply_plan,
    build_raw_url_index,
    create_plan,
    is_old_fabricated_url,
    validate_plan,
)


def _raw_page(*anchors: str) -> list[dict]:
    rows = "".join(
        '<tr class="listitem"><td>1</td><td>0</td><td>' + anchor
        + '</td><td>author</td><td>2026-08-11 12:00</td></tr>'
        for anchor in anchors
    )
    return [{"stock": "600519", "html": "<table>" + rows + "</table>"}]


def test_raw_url_index_uses_only_unambiguous_allowed_immutable_hrefs(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "one.json").write_text(json.dumps(_raw_page(
        '<a data-postid="1" href="/news,600519,1.html">one</a>',
        '<a data-postid="2" href="//caifuhao.eastmoney.com/news/real-two">two</a>',
        '<a data-postid="3" href="https://evil.invalid/news/three">three</a>',
        '<a data-postid="4" href="/news,600519,4.html">four-a</a>',
    )), encoding="utf-8")
    (raw / "two.json").write_text(json.dumps(_raw_page(
        '<a data-postid="4" href="//caifuhao.eastmoney.com/news/four-b">four-b</a>',
    )), encoding="utf-8")

    before = {p.name: p.read_bytes() for p in raw.iterdir()}
    index, report = build_raw_url_index(raw)

    assert index["1"]["url"] == "https://guba.eastmoney.com/news,600519,1.html"
    assert index["2"]["url"] == "https://caifuhao.eastmoney.com/news/real-two"
    assert "3" not in index, "off-allowlist href must not become repair evidence"
    assert "4" not in index and len(report["conflicts"]["4"]) == 2
    assert len(index["2"]["evidence"][0]["raw_sha256"]) == 64
    assert before == {p.name: p.read_bytes() for p in raw.iterdir()}, "raw is read-only"


def test_old_fabricated_url_predicate_is_exact():
    assert is_old_fabricated_url("https://guba.eastmoney.com/news,49.html", "49")
    assert not is_old_fabricated_url(
        "https://guba.eastmoney.com/news,600519,49.html", "49"
    )
    assert not is_old_fabricated_url(
        "https://caifuhao.eastmoney.com/news/49", "49"
    )
    assert not is_old_fabricated_url("https://guba.eastmoney.com/news,50.html", "49")


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
    def filter(self, *args): return self
    def order_by(self, *args): return self
    def all(self): return list(self.rows)
    def count(self): return len(self.rows)
    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]


class _FakeSession:
    def __init__(self, *, posts, snapshots=(), deletions=()):
        self.posts = list(posts)
        self.snapshots = list(snapshots)
        self.deletions = list(deletions)
        self.committed = False
        self.rolled_back = False
    def query(self, model):
        if model.__name__ == "CensoredPost":
            return _FakeQuery(self.posts)
        if model.__name__ == "DeletionVelocitySnapshot":
            return _FakeQuery(self.snapshots)
        if model.__name__ == "PostDeletion":
            return _FakeQuery(self.deletions)
        raise AssertionError(model)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True


def _snapshot():
    return SimpleNamespace(
        id=77,
        generated_at=datetime(2026, 8, 11, 14, 40, tzinfo=timezone.utc),
        n_deletions=0,
        n_terms=1,
        top_term="false-term",
        top_velocity=1.0,
        ranked=[{"term": "false-term"}],
        scope="all_sources",
    )


def test_plan_repairs_raw_url_quarantines_only_triple_shell_and_abstains(tmp_path):
    raw = tmp_path / "raw"
    archive = tmp_path / "archive"
    quarantine = tmp_path / "quarantine"
    raw.mkdir()
    (raw / "capture.json").write_text(json.dumps(_raw_page(
        '<a data-postid="49" href="//caifuhao.eastmoney.com/news/immutable-real">x</a>'
    )), encoding="utf-8")

    shell_dir = archive / "eastmoney_guba" / "49"
    shell_dir.mkdir(parents=True)
    shell = (
        b'<html><link href="validate.css"><script src="validate.js"></script>'
        b'<body>ok</body></html>'
    )
    (shell_dir / "page.html").write_bytes(shell)
    (shell_dir / "meta.json").write_text("{}", encoding="utf-8")

    healthy_dir = archive / "eastmoney_guba" / "50"
    healthy_dir.mkdir(parents=True)
    (healthy_dir / "page.html").write_text(
        '<html><script src="validate.js"></script><body>substantive archive</body></html>',
        encoding="utf-8",
    )

    posts = [
        SimpleNamespace(id=1, post_id="49", url="https://guba.eastmoney.com/news,49.html",
                        archive_path=str(shell_dir)),
        SimpleNamespace(id=2, post_id="50",
                        url="https://guba.eastmoney.com/news,600519,50.html",
                        archive_path=str(healthy_dir)),
    ]
    session = _FakeSession(posts=posts, snapshots=[_snapshot()])
    plan = create_plan(
        session,
        raw_dir=raw,
        archive_dir=archive,
        quarantine_dir=quarantine,
        false_snapshot_ids=[77],
        now=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
    )

    validate_plan(plan)
    assert plan["counts"] == {
        "post_rows": 2,
        "url_repairs": 1,
        "archive_quarantines": 1,
        "substantive_archives_kept": 1,
        "rows_without_archive": 0,
        "velocity_abstentions": 1,
        "unresolved": 0,
    }
    repair = plan["actions"]["url_repairs"][0]
    assert repair["to_url"] == "https://caifuhao.eastmoney.com/news/immutable-real"
    action = plan["actions"]["archive_quarantines"][0]
    assert all(action["predicate"].values())
    assert shell_dir.exists() and not quarantine.exists(), "dry-run changes no archive state"
    assert plan["actions"]["velocity_abstentions"][0]["after"]["n_deletions"] is None


def test_apply_revalidates_then_quarantines_without_deleting_and_clears_cache(
    tmp_path, monkeypatch
):
    raw = tmp_path / "raw"
    archive = tmp_path / "archive"
    quarantine = tmp_path / "quarantine"
    raw.mkdir()
    (raw / "capture.json").write_text(json.dumps(_raw_page(
        '<a data-postid="49" href="//caifuhao.eastmoney.com/news/immutable-real">x</a>'
    )), encoding="utf-8")
    shell_dir = archive / "eastmoney_guba" / "49"
    shell_dir.mkdir(parents=True)
    (shell_dir / "page.html").write_text(
        '<html><link href="validate.css"><script src="validate.js"></script>'
        '<body>ok</body></html>', encoding="utf-8",
    )
    (shell_dir / "meta.json").write_text("{}", encoding="utf-8")

    post = SimpleNamespace(
        id=1, post_id="49", url="https://guba.eastmoney.com/news,49.html",
        archive_path=str(shell_dir),
    )
    snapshot = _snapshot()
    session = _FakeSession(posts=[post], snapshots=[snapshot])
    plan = create_plan(
        session, raw_dir=raw, archive_dir=archive, quarantine_dir=quarantine,
        false_snapshot_ids=[77],
        now=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
    )

    deleted_keys = []

    class _Redis:
        def delete(self, *keys):
            deleted_keys.extend(keys)
            return len(keys)
        def close(self): pass

    fake_redis_module = SimpleNamespace(from_url=lambda *a, **k: _Redis())
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)

    result = apply_plan(session, plan, redis_url="redis://unused/0")
    destination = Path(plan["actions"]["archive_quarantines"][0]["to_quarantine_path"])
    assert session.committed and not session.rolled_back
    assert not shell_dir.exists() and (destination / "page.html").exists()
    assert post.archive_path is None
    assert post.url == "https://caifuhao.eastmoney.com/news/immutable-real"
    assert snapshot.n_deletions is None and snapshot.top_velocity is None
    assert snapshot.scope == ABSTENTION_SCOPE
    assert deleted_keys == ["censorwatch:velocity:latest", "health:eastmoney_guba"]
    assert result["mode"] == "applied" and result["redis_error"] is None


def test_manifest_tampering_is_rejected(tmp_path):
    # Minimal shape reaches digest validation without requiring a DB.
    plan = {
        "schema_version": "palimpsest.censorwatch-eastmoney-repair.v1",
        "mode": "dry-run",
        "source": "eastmoney_guba",
        "actions": {},
        "plan_sha256": "not-the-digest",
    }
    try:
        validate_plan(plan)
    except ValueError as exc:
        assert "digest" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("tampered plan accepted")
