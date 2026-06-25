from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from astrbot.api.message_components import Image as ImageComp
from data.plugins.dc_router.preprocessing import media_route


class _Event:
    unified_msg_origin = "test-session"
    message_obj = SimpleNamespace(message=[])


class _HandledEvent(_Event):
    def __init__(
        self,
        message: list | None = None,
        *,
        unified_msg_origin: str = "test-session",
        chat_id: str = "oc_test_chat",
        sender_id: str = "ou_sender",
    ) -> None:
        self.result = None
        self.llm_enabled = True
        self.extras: dict[str, str] = {}
        self.message_obj = SimpleNamespace(
            message=message or [],
            raw_message=SimpleNamespace(chat_id=chat_id),
        )
        self.unified_msg_origin = unified_msg_origin
        self._sender_id = sender_id

    def should_call_llm(self, enabled: bool) -> None:
        self.llm_enabled = enabled

    def set_result(self, result) -> None:
        self.result = result

    def set_extra(self, key: str, value: str) -> None:
        self.extras[key] = value

    def get_platform_id(self) -> str:
        return "巅池-Agent小助手"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return ""


class _FakeCardStreamer:
    def __init__(self) -> None:
        self.started = None
        self.finalized = None

    async def start(self, *, chat_id: str, receive_id_type: str, card: dict):
        self.started = {
            "chat_id": chat_id,
            "receive_id_type": receive_id_type,
            "card": card,
        }
        return SimpleNamespace(message_id="om_source_edit")

    async def finalize(self, message_id: str, card: dict) -> bool:
        self.finalized = {"message_id": message_id, "card": card}
        return True


@pytest.mark.asyncio
async def test_poster_need_request_routes_to_image_policy() -> None:
    route = await media_route._detect_route(
        _Event(), "我的设计同事需要一张中秋的宣传海报"
    )

    assert route is not None
    assert route.kind == "image"
    assert "海报" in route.prompt
    assert route.aspect_ratio == "portrait"
    assert route.image_provider_strategy == "gpt_first"


@pytest.mark.asyncio
async def test_generic_image_request_keeps_gpt_image_first_policy() -> None:
    route = await media_route._detect_route(_Event(), "帮我生成一张未来城市图片")

    assert route is not None
    assert route.kind == "image"
    assert route.image_provider_strategy == "gpt_first"


@pytest.mark.asyncio
async def test_explicit_dreamina_request_uses_dreamina_first_policy() -> None:
    route = await media_route._detect_route(_Event(), "用即梦帮我生成一张中秋宣传海报")

    assert route is not None
    assert route.kind == "image"
    assert route.image_provider_strategy == "dreamina_first"


@pytest.mark.asyncio
async def test_operational_plan_with_visual_terms_does_not_route_to_image() -> None:
    route = await media_route._detect_route(
        _Event(),
        (
            "你好，这是我之前写的通品店铺的整体运转规划，现在需要做一个可以"
            "落地的执行方案，主要包括店铺包装、抖音账号包装、LOGO、主色调、"
            "SLOGAN、整体VI、商品主图设计、账号封面设计、直播间视觉呈现，"
            "所有内容都要细化到可以直接执行的程度。"
        ),
    )

    assert route is None


@pytest.mark.asyncio
async def test_explicit_poster_generation_still_routes_inside_plan_context() -> None:
    route = await media_route._detect_route(
        _Event(), "先做活动方案，同时帮我生成一张夏季主视觉海报"
    )

    assert route is not None
    assert route.kind == "image"
    assert route.aspect_ratio == "portrait"


@pytest.mark.asyncio
async def test_source_image_cutout_request_does_not_route_to_generation() -> None:
    route = await media_route._detect_route(
        _Event(), "[image] 帮我把这张图片去掉背景，人物抠出来"
    )

    assert route is None


@pytest.mark.asyncio
async def test_source_image_cutout_request_without_file_returns_clear_reply() -> None:
    event = _HandledEvent()

    handled = await media_route.try_handle_media_route(
        SimpleNamespace(), event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )

    assert handled is True
    assert event.llm_enabled is False
    assert event.extras["dc_media_route_handled"] == "source_image_edit"
    assert event.result is not None
    assert event.result.is_stopped()
    assert event.result.use_t2i_ is False
    assert "没有取得可处理的图片文件" in event.result.chain[0].text
    assert "不会把它当成生图任务" in event.result.chain[0].text


@pytest.mark.asyncio
async def test_source_image_cutout_request_returns_transparent_png(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        media_route,
        "_remove_background_with_rembg",
        lambda _path: (_ for _ in ()).throw(RuntimeError("rembg disabled in test")),
    )
    source_path = tmp_path / "portrait.png"
    image = PILImage.new("RGB", (32, 32), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 7, 23, 27), fill=(170, 40, 40))
    image.save(source_path)
    event = _HandledEvent([ImageComp.fromFileSystem(str(source_path))])

    handled = await media_route.try_handle_media_route(
        SimpleNamespace(), event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )

    assert handled is True
    assert event.result is not None
    output_path = Path(event.result.chain[0].path)
    assert output_path.suffix == ".png"
    assert event.result.chain[1].text == "已去掉背景并导出透明 PNG。"
    with PILImage.open(output_path) as output:
        assert output.mode == "RGBA"
        assert output.getpixel((0, 0))[3] < 20
        assert output.getpixel((16, 16))[3] > 200


@pytest.mark.asyncio
async def test_source_image_cutout_sends_and_finalizes_card(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        media_route,
        "_remove_background_with_rembg",
        lambda _path: (_ for _ in ()).throw(RuntimeError("rembg disabled in test")),
    )
    source_path = tmp_path / "portrait.png"
    image = PILImage.new("RGB", (32, 32), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 7, 23, 27), fill=(170, 40, 40))
    image.save(source_path)
    event = _HandledEvent([ImageComp.fromFileSystem(str(source_path))])
    streamer = _FakeCardStreamer()
    context = SimpleNamespace(feishu_streamers={"巅池-Agent小助手": streamer})

    handled = await media_route.try_handle_source_image_edit(
        context, event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )

    assert handled is True
    assert streamer.started is not None
    assert streamer.started["chat_id"] == "oc_test_chat"
    assert streamer.started["card"]["header"]["title"]["content"] == "源图编辑 · 处理中"
    assert streamer.finalized is not None
    assert streamer.finalized["message_id"] == "om_source_edit"
    assert (
        streamer.finalized["card"]["header"]["title"]["content"] == "源图编辑 · 已完成"
    )
    assert event.result is not None
    assert Path(event.result.chain[0].path).suffix == ".png"


@pytest.mark.asyncio
async def test_source_image_cutout_feedback_stops_before_queue_without_auto_accepting(
    monkeypatch, tmp_path
) -> None:
    media_route._LAST_SOURCE_IMAGE_EDIT_BY_SESSION.clear()
    monkeypatch.setattr(
        media_route,
        "_remove_background_with_rembg",
        lambda _path, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rembg disabled in test")
        ),
    )
    source_path = tmp_path / "portrait.png"
    image = PILImage.new("RGB", (32, 32), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 7, 23, 27), fill=(170, 40, 40))
    image.save(source_path)

    first_event = _HandledEvent([ImageComp.fromFileSystem(str(source_path))])
    first_handled = await media_route.try_handle_source_image_edit(
        SimpleNamespace(), first_event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )
    followup_event = _HandledEvent()

    followup_handled = await media_route.try_handle_source_image_edit(
        SimpleNamespace(), followup_event, "头发部分不是很理想，能不能再精细一些吗？"
    )

    assert first_handled is True
    assert followup_handled is True
    assert followup_event.llm_enabled is False
    assert (
        followup_event.extras["dc_media_route_handled"] == "source_image_edit_followup"
    )
    assert followup_event.result is not None
    assert followup_event.result.is_stopped()
    assert followup_event.result.chain[0].text.startswith(
        "我识别到你是在反馈上一张抠图结果"
    )
    assert "不会把这句话送去排队" in followup_event.result.chain[0].text
    assert "执行精修" in followup_event.result.chain[0].text


@pytest.mark.asyncio
async def test_source_image_cutout_feedback_uses_lark_chat_context_when_origin_changes(
    monkeypatch, tmp_path
) -> None:
    media_route._LAST_SOURCE_IMAGE_EDIT_BY_SESSION.clear()
    monkeypatch.setattr(
        media_route,
        "_remove_background_with_rembg",
        lambda _path, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rembg disabled in test")
        ),
    )
    source_path = tmp_path / "portrait.png"
    image = PILImage.new("RGB", (32, 32), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 7, 23, 27), fill=(170, 40, 40))
    image.save(source_path)

    first_event = _HandledEvent(
        [ImageComp.fromFileSystem(str(source_path))],
        unified_msg_origin="lark:FriendMessage:ou_sender",
        chat_id="oc_fixed_window",
    )
    await media_route.try_handle_source_image_edit(
        SimpleNamespace(), first_event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )
    followup_event = _HandledEvent(
        unified_msg_origin="lark:FriendMessage:message-scoped-origin",
        chat_id="oc_fixed_window",
    )

    followup_handled = await media_route.try_handle_source_image_edit(
        SimpleNamespace(), followup_event, "边缘还有点毛边，能不能再细一点"
    )

    assert followup_handled is True
    assert followup_event.llm_enabled is False
    assert (
        followup_event.extras["dc_media_route_handled"] == "source_image_edit_followup"
    )
    assert followup_event.result is not None
    assert followup_event.result.is_stopped()
    assert "不会把这句话送去排队" in followup_event.result.chain[0].text


@pytest.mark.asyncio
async def test_source_image_cutout_explicit_refine_reuses_last_source(
    monkeypatch, tmp_path
) -> None:
    media_route._LAST_SOURCE_IMAGE_EDIT_BY_SESSION.clear()
    monkeypatch.setattr(
        media_route,
        "_remove_background_with_rembg",
        lambda _path, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("rembg disabled in test")
        ),
    )
    source_path = tmp_path / "portrait.png"
    image = PILImage.new("RGB", (32, 32), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 7, 23, 27), fill=(170, 40, 40))
    image.save(source_path)

    first_event = _HandledEvent([ImageComp.fromFileSystem(str(source_path))])
    await media_route.try_handle_source_image_edit(
        SimpleNamespace(), first_event, "[image] 帮我把这张图片去掉背景，人物抠出来"
    )
    refine_event = _HandledEvent()

    refine_handled = await media_route.try_handle_source_image_edit(
        SimpleNamespace(), refine_event, "执行精修，重新处理一下发丝边缘"
    )

    assert refine_handled is True
    assert refine_event.llm_enabled is False
    assert refine_event.result is not None
    assert Path(refine_event.result.chain[0].path).suffix == ".png"
    assert "重新精修发丝/边缘" in refine_event.result.chain[1].text


def test_source_image_cutout_keeps_subject_pixels_opaque(tmp_path) -> None:
    source_path = tmp_path / "portrait_like.png"
    image = PILImage.new("RGB", (64, 64), (220, 220, 220))
    draw = ImageDraw.Draw(image)
    draw.ellipse((22, 12, 42, 34), fill=(232, 178, 145))
    draw.rectangle((18, 30, 46, 56), fill=(28, 34, 48))
    draw.rectangle((19, 8, 45, 17), fill=(18, 18, 18))
    image.save(source_path)

    output_path = Path(
        media_route._remove_connected_background_to_png(str(source_path))
    )

    with PILImage.open(source_path).convert("RGBA") as original:
        original_face = original.getpixel((32, 24))
    with PILImage.open(output_path) as output:
        assert output.mode == "RGBA"
        assert output.getpixel((0, 0))[3] < 20
        output_face = output.getpixel((32, 24))
        assert output_face[:3] == original_face[:3]
        assert output_face[3] > 240


def test_dreamina_image_tool_description_does_not_claim_festival_posters() -> None:
    source_text = Path("data/plugins/dreamina_plugin/main.py").read_text(
        encoding="utf-8"
    )

    assert "普通生图、海报、插画必须优先调用 generate_image" in source_text
    assert "中文文字海报、国潮节日" not in source_text


@pytest.mark.asyncio
async def test_media_route_with_waiting_card_suppresses_plain_ack(monkeypatch) -> None:
    event = _HandledEvent()
    stored_records: list[dict] = []

    async def fake_start_waiting_card(_context, _event, route):
        return SimpleNamespace(
            message_id="om_waiting",
            chat_id="ou_user",
            receive_id_type="open_id",
            title="生图任务",
            brief=route.prompt,
            reasoning_tier="high",
            current_stage="generating",
        )

    async def fake_background_job(*_args, **_kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)

    monkeypatch.setattr(media_route, "_start_waiting_card", fake_start_waiting_card)
    monkeypatch.setattr(media_route, "_background_job_with_record", fake_background_job)
    monkeypatch.setattr(
        media_route,
        "_upsert_pending_media_task",
        lambda record: stored_records.append(record),
    )
    monkeypatch.setattr(media_route.asyncio, "create_task", fake_create_task)

    handled = await media_route.try_handle_media_route(
        SimpleNamespace(), event, "帮我生成一张未来城市图片"
    )

    assert handled is True
    assert event.llm_enabled is False
    assert event.extras["dc_media_route_handled"] == "image"
    assert event.extras["dc_media_route_ack_suppressed"] == "1"
    assert event.result is not None
    assert event.result.is_stopped()
    assert event.result.use_t2i_ is False
    assert event.result.chain == []
    assert stored_records
    assert stored_records[0]["card"]["message_id"] == "om_waiting"


@pytest.mark.asyncio
async def test_pending_media_tasks_resume_after_restart(monkeypatch, tmp_path) -> None:
    pending_path = tmp_path / "media_route_pending.json"
    monkeypatch.setattr(media_route, "_MEDIA_TASKS_PATH", pending_path)
    task_id = "task_media_001"
    media_route._upsert_pending_media_task(
        {
            "task_id": task_id,
            "created_at": 1.0,
            "updated_at": 1.0,
            "umo": "test-session",
            "platform_id": "lark",
            "route": {
                "kind": "image",
                "prompt": "future city",
                "quality": "medium",
                "aspect_ratio": "landscape",
                "image_provider_strategy": "gpt_first",
            },
            "card": {},
        }
    )
    scheduled = []

    async def fake_background_job(_context, umo, route, card, restored_task_id):
        scheduled.append((umo, route.kind, route.prompt, card, restored_task_id))

    def fake_create_task(coro):
        scheduled.append(coro)
        return SimpleNamespace(done=lambda: False, cancel=lambda: None)

    monkeypatch.setattr(media_route, "_background_job_with_record", fake_background_job)
    monkeypatch.setattr(media_route, "_restore_waiting_card", lambda *_args: None)
    monkeypatch.setattr(media_route.asyncio, "create_task", fake_create_task)

    resumed = await media_route._resume_pending_media_tasks(SimpleNamespace())

    assert resumed == 1
    assert len(scheduled) == 1
    await scheduled[0]
    assert scheduled[1] == ("test-session", "image", "future city", None, task_id)
