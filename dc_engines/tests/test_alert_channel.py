from __future__ import annotations

from dc_engines.alert_channel.client import AlertLevel, _build_lark_card


def test_lark_alert_card_adds_safe_dashboard_link_button() -> None:
    card = _build_lark_card(
        title="人工介入待审核",
        body="Incident incident-1 等待处理。",
        level=AlertLevel.WARNING,
        action_url="http://127.0.0.1:6185/",
        action_label="打开 Dashboard 审核",
    )

    actions = [element for element in card["elements"] if element["tag"] == "action"]
    assert actions == [
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "打开 Dashboard 审核",
                    },
                    "url": "http://127.0.0.1:6185/",
                    "type": "primary",
                }
            ],
        }
    ]


def test_lark_alert_card_rejects_non_http_action_url() -> None:
    card = _build_lark_card(
        title="Unsafe link",
        body="Ignored.",
        level=AlertLevel.CRITICAL,
        action_url="javascript:alert(1)",
        action_label="Open",
    )

    assert all(element["tag"] != "action" for element in card["elements"])
