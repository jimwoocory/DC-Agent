from __future__ import annotations

from dc_engines.card_style import (
    CARD_BODY_PADDING,
    CARD_BODY_TEXT_SIZE,
    CARD_HEADER_PADDING,
    CARD_VERTICAL_SPACING,
    apply_card_visual_system,
)
from dc_engines.card_system import CARD_REGISTRY, build_sample_card

from data.plugins.feishu_pet_assistant.cards import build_status_card


def _find_tags(value: object, tag: str) -> list[dict]:
    matches: list[dict] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            matches.append(value)
        for item in value.values():
            matches.extend(_find_tags(item, tag))
    elif isinstance(value, list):
        for item in value:
            matches.extend(_find_tags(item, tag))
    return matches


def test_visual_system_does_not_mutate_builder_payload() -> None:
    raw = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "完成"},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "正文"},
                {"tag": "markdown", "content": "说明", "text_size": "notation"},
            ]
        },
    }

    styled = apply_card_visual_system(raw)

    assert raw["body"].get("padding") is None
    assert styled["header"]["template"] == "green"
    assert styled["header"]["padding"] == CARD_HEADER_PADDING
    assert styled["body"]["padding"] == CARD_BODY_PADDING
    assert styled["body"]["vertical_spacing"] == CARD_VERTICAL_SPACING
    assert styled["body"]["elements"][0]["text_size"] == CARD_BODY_TEXT_SIZE
    assert styled["body"]["elements"][1]["text_size"] == "notation"


def test_all_registered_samples_share_json_v2_visual_system() -> None:
    for card_type in CARD_REGISTRY:
        card = build_sample_card(card_type)

        assert card["schema"] == "2.0", card_type
        assert card["header"]["padding"] == CARD_HEADER_PADDING, card_type
        assert card["body"]["padding"] == CARD_BODY_PADDING, card_type
        assert card["body"]["vertical_spacing"] == CARD_VERTICAL_SPACING, card_type
        assert CARD_BODY_TEXT_SIZE in card["config"]["style"]["text_size"], card_type
        assert "font_family" not in str(card), card_type


def test_legacy_actions_keep_callbacks_when_upgraded() -> None:
    card = build_sample_card("pet_status")
    buttons = _find_tags(card, "button")

    assert not _find_tags(card, "action")
    assert {button["value"]["action"] for button in buttons} == {
        "pet_view_tasks",
        "pet_done_first",
    }
    assert all(button["size"] == "medium" for button in buttons)
    assert all(button["width"] == "fill" for button in buttons)


def test_legacy_url_buttons_use_json_v2_open_url_behaviors() -> None:
    raw = build_status_card(
        {"pet_name": "小橘", "energy": 80},
        {"pending": 0, "done": 0},
        h5_url="https://example.com/pet",
    )

    card = apply_card_visual_system(raw)
    url_button = next(
        button
        for button in _find_tags(card, "button")
        if button["text"]["content"] == "打开小橘房间"
    )

    assert "url" not in url_button
    assert url_button["behaviors"] == [
        {"type": "open_url", "default_url": "https://example.com/pet"}
    ]


def test_semantic_header_colors_survive_normalization() -> None:
    assert build_sample_card("task_result")["header"]["template"] == "green"
    assert build_sample_card("task_error")["header"]["template"] == "red"
    assert build_sample_card("thinking_waiting")["header"]["template"] == "indigo"
