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


def test_media_generation_runtime_paths_keep_success_image_result_cards() -> None:
    router_source = MEDIA_PLUGIN_PATHS[0].read_text(encoding="utf-8")
    image_source = MEDIA_PLUGIN_PATHS[1].read_text(encoding="utf-8")
    dreamina_source = MEDIA_PLUGIN_PATHS[2].read_text(encoding="utf-8")

    assert "retract_after_sec=None" in router_source
    assert "retract_after_sec=None" in image_source
    assert "retract_after_sec=None" in dreamina_source
    assert "retract_after_sec=8.0" not in router_source
    assert "retract_after_sec=8.0" not in image_source
    assert "retract_after_sec=8.0" not in dreamina_source
    assert "player_origin=player_origin" in router_source
    assert "player_app_id=player_app_id" in router_source


def test_codex_image_adapter_uses_advanced_executor_policy() -> None:
    source = Path("data/plugins/gpt_image_plugin/main.py").read_text(encoding="utf-8")

    assert "authorize_codex" in source
    assert '"image_generation"' in source
    assert 'authorized_by="user"' in source


def test_media_generation_paths_do_not_depend_on_mac_home() -> None:
    router_source = MEDIA_PLUGIN_PATHS[0].read_text(encoding="utf-8")
    image_source = MEDIA_PLUGIN_PATHS[1].read_text(encoding="utf-8")

    assert "/Users/dianchi" not in router_source
    assert "/Users/dianchi" not in image_source
    assert 'data_path("output", "images")' in router_source
    assert "get_astrbot_data_path" in image_source
