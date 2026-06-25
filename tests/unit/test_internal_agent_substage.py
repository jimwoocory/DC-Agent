"""Tests for internal agent substage helpers."""

from unittest.mock import MagicMock

from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    _is_router_web_search_required,
)


def test_router_required_search_extra_is_truthy() -> None:
    event = MagicMock()
    event.get_extra.return_value = "true"

    assert _is_router_web_search_required(event) is True
    event.get_extra.assert_called_once_with("dc_router_meta_search_required")


def test_router_required_search_extra_defaults_false() -> None:
    event = MagicMock()
    event.get_extra.return_value = None

    assert _is_router_web_search_required(event) is False
