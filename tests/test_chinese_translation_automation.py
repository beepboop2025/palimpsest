from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "newswire-refresh.yml"
PUBLISHER_PATH = ROOT / "ops" / "railway" / "palimpsest-railway-publish"


def _workflow_step(name: str) -> str:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    marker = f"      - name: {name}\n"
    start = text.index(marker)
    end = text.find("\n      - ", start + len(marker))
    return text[start:] if end == -1 else text[start:end]


def _assert_order(script: str, fragments: tuple[str, ...]) -> None:
    positions = [script.index(fragment) for fragment in fragments]
    assert positions == sorted(positions)


def test_news_refresh_translates_once_and_replays_offline() -> None:
    initial_script = _workflow_step("Correlate, render and seal the evidence wire")
    assert 'OPENROUTER_API_KEY="${{ secrets.OPENROUTER_API_KEY }}"' in initial_script
    assert "\n        env:" not in initial_script
    assert "GOOGLE_AI_STUDIO_API_KEY" not in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "python -m scripts.build_chinese_translations\n" in initial_script
    assert "scripts.build_chinese_translations --offline" not in initial_script
    _assert_order(
        initial_script,
        (
            "python -m scripts.build_newsroom --check",
            "python -m scripts.build_chinese_translations\n",
            "python -m scripts.build_chinese_translations --check",
            "python -m scripts.build_chinese_translation_pages\n",
            "python -m scripts.build_chinese_translation_pages --check",
            "python -m scripts.build_bri_observatory\n",
            "python -m scripts.build_bri_observatory --check",
            "python -m scripts.build_data_catalog\n",
            "python -m scripts.sync_nav\n",
            "python scripts/seal_readings.py",
        ),
    )

    for name in (
        "Rebuild and reseal after a pre-publication ledger change",
        "Rebuild and reseal after a push race",
    ):
        script = _workflow_step(name)
        assert "\n        env:" not in script
        assert "chinese-translations-replay-cache.json" in script
        assert "scripts.build_chinese_translations --offline" in script
        assert "python -m scripts.build_chinese_translations\n" not in script
        _assert_order(
            script,
            (
                "python -m scripts.build_newsroom --check",
                "scripts.build_chinese_translations --offline",
                "python -m scripts.build_chinese_translations --check",
                "python -m scripts.build_chinese_translation_pages\n",
                "python -m scripts.build_bri_observatory\n",
                "python -m scripts.build_data_catalog\n",
                "python -m scripts.sync_nav\n",
                "python scripts/seal_readings.py",
            ),
        )


def test_every_refresh_candidate_stages_translation_and_regional_outputs() -> None:
    for name in (
        "Create the candidate refresh commit",
        "Replace the candidate with revalidated bytes",
        "Commit the race-safe rebuilt bytes",
    ):
        script = _workflow_step(name)
        for public_path in (
            "readings/chinese-translations-latest.json",
            "news/",
            "belt-and-road/",
            ".well-known/ai-catalog.json",
            "config/public_data_catalog.json",
            "datapackage.json",
            "sitemap.xml",
            "':(glob)**/*.html'",
        ):
            assert public_path in script


def test_railway_publisher_is_model_free_and_fails_before_deploy() -> None:
    script = PUBLISHER_PATH.read_text(encoding="utf-8")
    assert "GOOGLE_AI_STUDIO_API_KEY" not in script
    assert "OPENROUTER_API_KEY" not in script
    assert 'scripts.build_chinese_translations --offline' in script
    assert 'scripts.build_chinese_translations --check' in script
    assert '"$PYTHON_BIN" -m scripts.build_chinese_translations\n' not in script
    _assert_order(
        script,
        (
            '"$PYTHON_BIN" -m scripts.build_newsroom --check',
            'scripts.build_chinese_translations --offline',
            'scripts.build_chinese_translation_pages',
            'scripts.build_bri_observatory',
            'scripts.build_data_catalog',
            'git commit --quiet',
            '"$RAILWAY_BIN" up',
        ),
    )
