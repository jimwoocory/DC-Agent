from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from data.plugins.dc_router.preprocessing import media_route


class _Event:
    unified_msg_origin = "test-session"
    message_obj = SimpleNamespace(message=[])


@pytest.mark.asyncio
async def test_poster_need_request_routes_to_image_policy() -> None:
    route = await media_route._detect_route(
        _Event(), "我的设计同事需要一张中秋的宣传海报"
    )

    assert route is not None
    assert route.kind == "image"
    assert "海报" in route.prompt
    assert route.aspect_ratio == "portrait"


def test_dreamina_image_tool_description_does_not_claim_festival_posters() -> None:
    source_text = Path("data/plugins/dreamina_plugin/main.py").read_text(
        encoding="utf-8"
    )

    assert "普通生图、海报、插画必须优先调用 generate_image" in source_text
    assert "中文文字海报、国潮节日" not in source_text
