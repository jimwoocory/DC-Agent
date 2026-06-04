from __future__ import annotations

from pathlib import Path

MEDIA_PLUGIN_PATHS = (
    Path("data/plugins/llm_router/main.py"),
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
