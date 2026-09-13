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
    assert "scripts.build_chinese_translations --retain-last-good" in script
    assert "scripts.build_chinese_translations --offline" not in script
    assert "scripts.build_chinese_translations --check" not in script
    assert 'translation_publication_state="$(jq -r' in script
    assert (
        "translation retention status does not satisfy its closed release contract"
        in script
    )
    assert 'if [[ "$translation_publication_state" == "retained-last-good" ]]' in script
    assert "bri_translation_args=(--omit-unbound-chinese-translations)" in script
    assert 'scripts.build_bri_observatory "${bri_translation_args[@]}"' in script
    assert (
        'scripts.build_bri_observatory --check "${bri_translation_args[@]}"' in script
    )
    assert '"$PYTHON_BIN" -m scripts.build_chinese_translations\n' not in script
    _assert_order(
        script,
        (
            '"$PYTHON_BIN" -m scripts.build_newsroom --check',
            "scripts.build_chinese_translations --retain-last-good",
            "scripts.build_chinese_translation_pages",
            "scripts.build_bri_observatory",
            "scripts.build_data_catalog",
            "git commit --quiet",
            '"$RAILWAY_BIN" up',
        ),
    )


def test_railway_publisher_keeps_translation_and_selects_a_monotonic_ledger() -> None:
    script = PUBLISHER_PATH.read_text(encoding="utf-8")
    start = script.index("copy_host_public_file() {")
    end = script.index("\n}\n\nwhile IFS= read", start)
    overlay_functions = script[start:end]

    assert "readings/chinese-translations-latest.json" in overlay_functions
    assert "readings/readings-ledger.jsonl" in overlay_functions
    assert overlay_functions.index('case "$relative" in') < overlay_functions.index(
        'source="$snapshot_readings/'
    )
    assert 'cmp -n "$destination_bytes" "$destination" "$source"' in overlay_functions
    assert 'cmp -n "$source_bytes" "$source" "$destination"' in overlay_functions
    assert "readings ledger overlay diverges" in overlay_functions
    assert script.index("overlay_monotonic_readings_ledger\n") < script.index(
        "scripts.build_chinese_translations --retain-last-good"
    )
    _assert_order(script, (
        "overlay_monotonic_readings_ledger\n",
        "scripts.translation_refresh admit",
        "scripts.build_chinese_translations --retain-last-good",
    ))


def test_scheduled_translation_separates_capture_models_and_promotion() -> None:
    script = (ROOT / "ops/translation/palimpsest-translation-refresh").read_text()
    _assert_order(script, (
        "scripts.translation_refresh capture",
        "scripts.build_chinese_translations",
        "scripts.translation_refresh promote",
    ))
    assert '--batch-size 8 --workers "$WORKERS" --max-batches' in script
    assert 'PALIMPSEST_TRANSLATION_WORKERS:-1' in script
    assert '[[ "$WORKERS" =~ ^[1-4]$ ]]' in script
    assert '--work-cache "$STATE/work-cache.json" --keep-work-cache' in script
    assert script.count('git -c safe.directory="$SOURCE"') == 3
    assert 'archive --format=tar "$base_sha"' in script
    assert "git clone" not in script
    assert "45m" in script
    unit = (ROOT / "ops/systemd/palimpsest-translation-refresh.service").read_text()
    timer = (ROOT / "ops/systemd/palimpsest-translation-refresh.timer").read_text()
    assert "EnvironmentFile=/etc/palimpsest/openrouter.env" in unit
    assert "OnSuccess=palimpsest-railway-publish.service" in unit
    assert "Persistent=true" in timer and "OnCalendar=hourly" in timer
