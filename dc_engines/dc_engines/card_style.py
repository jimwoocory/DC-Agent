"""Shared visual normalization for Feishu interactive cards."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

CARD_BODY_PADDING = "16px 16px 14px 16px"
CARD_VERTICAL_SPACING = "12px"
CARD_HEADER_PADDING = "12px 16px 12px 16px"
CARD_BODY_TEXT_SIZE = "dc_body"
CARD_TEXT_SIZE_STYLE = {
    CARD_BODY_TEXT_SIZE: {
        "default": "normal",
        "pc": "normal",
        "mobile": "heading",
    }
}


def _legacy_action_to_v2(element: dict[str, Any]) -> dict[str, Any]:
    """Convert one legacy action row into an equal-width JSON 2.0 row.

    Args:
        element: Legacy action element containing button dictionaries.

    Returns:
        A JSON 2.0 button or column set with preserved callback values.
    """
    actions = []
    for raw_action in element.get("actions", []):
        if not isinstance(raw_action, dict):
            continue
        action = deepcopy(raw_action)
        url = str(action.pop("url", "") or "").strip()
        if url and not action.get("behaviors"):
            action["behaviors"] = [{"type": "open_url", "default_url": url}]
        action["size"] = "medium"
        action["width"] = "fill"
        actions.append(action)
    if not actions:
        return {"tag": "markdown", "content": "", "text_size": CARD_BODY_TEXT_SIZE}
    if len(actions) == 1:
        return actions[0]
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [action],
            }
            for action in actions
        ],
    }


def _legacy_element_to_v2(element: dict[str, Any]) -> dict[str, Any]:
    """Convert one supported legacy element into JSON 2.0.

    Args:
        element: Legacy card element.

    Returns:
        A JSON 2.0-compatible element.
    """
    tag = str(element.get("tag") or "")
    if tag == "action":
        return _legacy_action_to_v2(element)
    if tag == "div":
        text = element.get("text", {})
        content = str(text.get("content") or "") if isinstance(text, dict) else ""
        return {
            "tag": "markdown",
            "content": content,
            "text_size": CARD_BODY_TEXT_SIZE,
            "text_align": "left",
            "margin": "0px",
        }
    if tag == "note":
        content = "\n".join(
            str(item.get("content") or "")
            for item in element.get("elements", [])
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        )
        return {
            "tag": "markdown",
            "content": f"<font color='grey'>{content}</font>",
            "text_size": "notation",
            "text_align": "left",
            "margin": "0px",
        }
    return deepcopy(element)


def _style_v2_element(element: dict[str, Any]) -> None:
    """Apply typography and spacing defaults to one JSON 2.0 element tree.

    Args:
        element: Mutable JSON 2.0 element.
    """
    tag = str(element.get("tag") or "")
    if tag == "markdown":
        original_size = str(element.get("text_size") or "normal")
        if original_size == "normal":
            element["text_size"] = CARD_BODY_TEXT_SIZE
        element.setdefault("text_align", "left")
        element.setdefault("margin", "0px")
    elif tag == "div":
        element.setdefault("width", "fill")
        element.setdefault("margin", "0px")
        text = element.get("text")
        if isinstance(text, dict):
            if str(text.get("text_size") or "normal") == "normal":
                text["text_size"] = CARD_BODY_TEXT_SIZE
            text.setdefault("text_align", "left")
            text.setdefault("text_color", "default")
    elif tag == "button":
        element.setdefault("size", "medium")
        element.setdefault("width", "fill")

    for key in ("elements", "columns", "actions"):
        children = element.get(key)
        if not isinstance(children, list):
            continue
        for child in children:
            if isinstance(child, dict):
                _style_v2_element(child)


def apply_card_visual_system(card: dict[str, Any]) -> dict[str, Any]:
    """Return a Card JSON 2.0 payload with the shared DC visual system.

    Feishu controls the client font family, so the visual system intentionally
    uses supported typography levels instead of emitting an ignored
    ``font_family`` field. Legacy Card JSON 1.0 payloads are upgraded at the
    runtime boundary so old grey-test cards share the same spacing and type
    hierarchy without rewriting their business builders.

    Args:
        card: Raw card payload from any registered builder.

    Returns:
        A deep-copied, normalized Card JSON 2.0 payload.
    """
    styled = deepcopy(card)
    if str(styled.get("schema") or "") != "2.0":
        legacy_elements = styled.pop("elements", [])
        styled["schema"] = "2.0"
        styled["body"] = {
            "elements": [
                _legacy_element_to_v2(element)
                for element in legacy_elements
                if isinstance(element, dict)
            ]
        }

    config = styled.setdefault("config", {})
    style = config.setdefault("style", {})
    text_size = style.setdefault("text_size", {})
    text_size.setdefault(
        CARD_BODY_TEXT_SIZE, deepcopy(CARD_TEXT_SIZE_STYLE[CARD_BODY_TEXT_SIZE])
    )

    header = styled.setdefault("header", {})
    header["padding"] = CARD_HEADER_PADDING
    header.setdefault("template", "blue")

    body = styled.setdefault("body", {})
    body.setdefault("direction", "vertical")
    body["padding"] = CARD_BODY_PADDING
    body["vertical_spacing"] = CARD_VERTICAL_SPACING
    elements = body.setdefault("elements", [])
    for element in elements:
        if isinstance(element, dict):
            _style_v2_element(element)
    return styled
