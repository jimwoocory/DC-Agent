from __future__ import annotations

from pathlib import Path

MEDIA_PLUGIN_PATHS = (
    Path("data/plugins/dc_router/preprocessing/media_route.py"),
    Path("data/plugins/gpt_image_plugin/main.py"),
    Path("data/plugins/dreamina_plugin/main.py"),
)


def test_media_generation_runtime_paths_use_media_cards_and_records() -> None:
    for path in MEDIA_PLUGIN_PATHS:
        source = path.read_text(encoding="utf-8")

        assert "build_media_generation_card" in source
        assert "build_media_generation_record" in source
        assert 'card_type="media_generation"' in source
        assert "build_daily_response_card" not in source
        assert "build_error_card" not in source


def test_media_generation_runtime_paths_retract_success_task_cards() -> None:
    for path in MEDIA_PLUGIN_PATHS:
        source = path.read_text(encoding="utf-8")

        assert "retract_after_sec=8.0 if success else None" in source
