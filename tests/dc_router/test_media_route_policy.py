from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from astrbot.api.message_components import Image as ImageComp
from data.plugins.dc_router.preprocessing import media_route


@pytest.fixture(autouse=True)
def _clear_active_media_task_state():
    media_route._ACTIVE_MEDIA_TASKS.clear()
    media_route._ACTIVE_MEDIA_CONTEXT.clear()
    yield
    media_route._ACTIVE_MEDIA_TASKS.clear()
    media_route._ACTIVE_MEDIA_CONTEXT.clear()


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

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

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
async def test_task_card_media_parameters_survive_route_detection() -> None:
    image_route = await media_route._detect_route(
        _Event(),
        "#生图 夏季新品的小红书封面\n画面比例：3:4\n输出数量：2 张\n生成质量：高质量",
    )
    video_route = await media_route._detect_route(
        _Event(),
        "#视频 新品从水面升起，镜头环绕\n视频时长：10 秒\n画面比例：9:16\n视频质量：1080p",
    )

    assert image_route is not None
    assert image_route.aspect_ratio == "portrait"
    assert image_route.quality == "high"
    assert image_route.image_count == 2
    assert video_route is not None
    assert video_route.kind == "text2video"
    assert video_route.duration == 10
    assert video_route.aspect_ratio == "portrait"
    assert video_route.video_quality == "1080p"


@pytest.mark.asyncio
async def test_structured_image_parameters_build_route_without_trigger_text() -> None:
    route = await media_route._route_from_structured_capability(
        _Event(),
        "第二张视觉变体",
        SimpleNamespace(),
        capability_id="execute.image",
        parameters={
            "visual_prompt": "北欧冰雪足球怪兽商业海报",
            "aspect_ratio": "3:4",
            "image_count": "2",
            "quality": "high",
            "model_choice": "dreamina",
        },
    )

    assert route is not None
    assert route.kind == "image"
    assert route.prompt == "北欧冰雪足球怪兽商业海报"
    assert route.aspect_ratio == "portrait"
    assert route.image_count == 2
    assert route.quality == "high"
    assert route.image_provider_strategy == "dreamina_only"


@pytest.mark.asyncio
async def test_structured_image_capability_does_not_reclassify_confirmed_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute the approved image capability even without legacy trigger wording."""
    goal = (
        "启动第二张变体：以哈兰德为主体，呈现挪威红蓝白视觉、北欧冰雪环境、"
        "足球怪兽化力量感与商业海报质感，采用不同于上一张的构图和动作，并避免"
        "使用未经授权的官方赛事标识或队徽。"
    )
    event = _HandledEvent()
    stored_records: list[dict] = []

    async def fake_background_job(*_args, **_kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)

    monkeypatch.setattr(
        media_route, "_start_waiting_card", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(media_route, "_background_job_with_record", fake_background_job)
    monkeypatch.setattr(
        media_route,
        "_upsert_pending_media_task",
        lambda record: stored_records.append(record),
    )
    monkeypatch.setattr(
        media_route,
        "search_company_creative_memory",
        lambda *_args, **_kwargs: [],
        raising=False,
    )
    monkeypatch.setattr(media_route.asyncio, "create_task", fake_create_task)

    legacy_route = await media_route._detect_route(event, goal)
    handled = await media_route.try_handle_media_route(
        SimpleNamespace(),
        event,
        goal,
        capability_id="execute.image",
        parameters={},
    )

    assert legacy_route is None
    assert handled is True
    assert event.extras["dc_media_route_handled"] == "image"
    assert stored_records[0]["route"]["prompt"] == goal


@pytest.mark.asyncio
async def test_structured_media_prompt_injects_governed_obsidian_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _HandledEvent()
    stored_records: list[dict] = []

    async def fake_background_job(*_args, **_kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return SimpleNamespace(done=lambda: True, cancel=lambda: None)

    monkeypatch.setattr(
        media_route,
        "search_company_creative_memory",
        lambda *_args, **_kwargs: [
            {
                "title": "五菱春节传播口径",
                "source_path": "30_Entities/五菱/春节传播.md",
                "excerpt": "围绕返乡场景表达可靠陪伴，不使用未经确认的销量数字。",
                "review_status": "confirmed",
                "source_status": "已复核",
                "usage_policy": "facts_and_style",
            },
            {
                "title": "旧版短片脚本",
                "source_path": "00_Inbox/旧版短片脚本.md",
                "excerpt": "采用清晨出发、夜间抵达的双时空结构。",
                "review_status": "need_review",
                "source_status": "待复核",
                "usage_policy": "style_reference_only",
            },
        ],
        raising=False,
    )
    monkeypatch.setattr(
        media_route, "_start_waiting_card", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(media_route, "_background_job_with_record", fake_background_job)
    monkeypatch.setattr(
        media_route,
        "_upsert_pending_media_task",
        lambda record: stored_records.append(record),
    )
    monkeypatch.setattr(media_route.asyncio, "create_task", fake_create_task)

    handled = await media_route.try_handle_media_route(
        SimpleNamespace(),
        event,
        "为五菱之光 EV 生成春节返乡主视觉",
        capability_id="execute.image",
        parameters={"visual_prompt": "五菱之光 EV 春节返乡主视觉"},
    )

    prompt = stored_records[0]["route"]["prompt"]
    assert handled is True
    assert "<dc_creative_memory_context>" in prompt
    assert "可用于相关事实、术语与风格" in prompt
    assert "仅可用于风格与结构，不可作为事实" in prompt
    assert "30_Entities/五菱/春节传播.md" in prompt
    assert "00_Inbox/旧版短片脚本.md" in prompt
    assert event.extras["dc_creative_memory_sources"] == [
        {
            "title": "五菱春节传播口径",
            "source_path": "30_Entities/五菱/春节传播.md",
            "source_status": "已复核",
            "usage_policy": "facts_and_style",
        },
        {
            "title": "旧版短片脚本",
            "source_path": "00_Inbox/旧版短片脚本.md",
            "source_status": "待复核",
            "usage_policy": "style_reference_only",
        },
    ]


def test_dreamina_concurrency_failure_is_parsed_and_sanitized() -> None:
    output = (
        '{"submit_id":"task-123","gen_status":"fail",'
        '"fail_reason":"api error: ret=1310, message=ExceedConcurrencyLimit,'
        ' logid=202607132130181921680021624064C42"}'
    )

    ok, reason, gen_status, submit_id = media_route._check_dreamina_status(output)

    assert ok is False
    assert gen_status == "fail"
    assert submit_id == "task-123"
    assert "并发已满" in reason
    assert "ExceedConcurrencyLimit" not in reason
    assert "logid" not in reason


@pytest.mark.asyncio
async def test_video_job_does_not_report_concurrency_failure_as_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        '{"submit_id":"task-123","gen_status":"fail",'
        '"fail_reason":"api error: ret=1310, message=ExceedConcurrencyLimit,'
        ' logid=internal-log"}'
    )
    monkeypatch.setattr(
        media_route,
        "_run_dreamina_command",
        AsyncMock(return_value=(True, output)),
    )
    finalize = AsyncMock(return_value=False)
    monkeypatch.setattr(media_route, "_finalize_waiting_card", finalize)
    context = SimpleNamespace(send_message=AsyncMock())
    route = media_route.MediaRoute(kind="text2video", prompt="新能源车城市短片")

    result = await media_route._run_video_job(context, "test-video", route, None)

    assert result.success is False
    assert "并发已满" in result.detail
    assert "完成" not in result.detail
    assert "ExceedConcurrencyLimit" not in result.detail
    assert "logid" not in result.detail
    sent_text = context.send_message.await_args.args[1].chain[0].text
    assert "并发已满" in sent_text
    assert "完成" not in sent_text


@pytest.mark.asyncio
async def test_video_job_uses_sidebar_card_without_duplicate_native_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "result.mp4"
    video_path.touch()
    monkeypatch.setattr(
        media_route,
        "_run_dreamina_command",
        AsyncMock(
            return_value=(
                True,
                '{"submit_id":"task-456","gen_status":"success",'
                '"video_url":"https://example.com/result.mp4"}',
            )
        ),
    )
    monkeypatch.setattr(
        media_route,
        "_download_url_to_cache",
        AsyncMock(return_value=str(video_path)),
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(media_route, "_finalize_waiting_card", finalize)
    context = SimpleNamespace(send_message=AsyncMock())
    route = media_route.MediaRoute(kind="text2video", prompt="新能源车城市短片")

    result = await media_route._run_video_job(context, "test-video", route, object())

    assert result.success is True
    assert finalize.await_args.kwargs["output_url"] == (
        "https://example.com/result.mp4"
    )
    assert "结果卡中点击右侧播放" in finalize.await_args.kwargs["detail"]
    context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_job_generates_the_confirmed_number_of_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.touch()
    second.touch()
    generate = AsyncMock(
        side_effect=[
            (True, str(first), "GPT Image 2 · high"),
            (True, str(second), "GPT Image 2 · high"),
        ]
    )
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(media_route, "_load_gpt_image_module", lambda: object())
    monkeypatch.setattr(media_route, "_run_image_gpt_first", generate)
    monkeypatch.setattr(media_route, "_finalize_waiting_card", finalize)
    context = SimpleNamespace(send_message=AsyncMock())
    route = media_route.MediaRoute(
        kind="image",
        prompt="夏季新品封面",
        quality="high",
        aspect_ratio="portrait",
        image_count=2,
    )

    await media_route._run_image_job(context, "test-image-count", route, None)

    assert generate.await_count == 2
    assert context.send_message.await_count == 2
    assert media_route._LAST_IMAGE_BY_SESSION["test-image-count"] == str(second)
    assert "已生成 2 张图片" in finalize.await_args.kwargs["detail"]


@pytest.mark.asyncio
async def test_image_job_prefers_card_conversation_and_retries_original_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "generated.png"
    image_path.touch()
    monkeypatch.setattr(media_route, "_load_gpt_image_module", lambda: object())
    monkeypatch.setattr(
        media_route,
        "_run_image_gpt_first",
        AsyncMock(return_value=(True, str(image_path), "GPT Image 2 · medium")),
    )
    monkeypatch.setattr(
        media_route,
        "_finalize_waiting_card",
        AsyncMock(return_value=True),
    )
    context = SimpleNamespace(send_message=AsyncMock(side_effect=[False, True]))
    card = SimpleNamespace(chat_id="oc_card_chat", receive_id_type="chat_id")
    original_session = "lark-test:FriendMessage:ou_user"

    result = await media_route._run_image_job(
        context,
        original_session,
        media_route.MediaRoute(kind="image", prompt="NAS preview regression"),
        card,
    )

    assert result.success is True
    assert context.send_message.await_count == 2
    assert str(context.send_message.await_args_list[0].args[0]) == (
        "lark-test:GroupMessage:oc_card_chat"
    )
    assert context.send_message.await_args_list[1].args[0] == original_session


@pytest.mark.asyncio
async def test_image_job_sends_interactive_preview_card_without_native_post(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "generated.png"
    image_path.touch()
    monkeypatch.setattr(media_route, "_load_gpt_image_module", lambda: object())
    monkeypatch.setattr(
        media_route,
        "_run_image_gpt_first",
        AsyncMock(return_value=(True, str(image_path), "GPT Image 2 · medium")),
    )
    monkeypatch.setattr(
        media_route,
        "_finalize_waiting_card",
        AsyncMock(return_value=True),
    )
    send_card = AsyncMock(return_value=SimpleNamespace(message_id="om_preview"))
    monkeypatch.setattr("dc_engines.card_runtime.send_card_via_runtime", send_card)
    streamer = SimpleNamespace(upload_image=AsyncMock(return_value="img_v2_preview"))
    card = SimpleNamespace(
        chat_id="oc_card_chat",
        receive_id_type="chat_id",
        streamer=streamer,
    )
    context = SimpleNamespace(send_message=AsyncMock())

    result = await media_route._run_image_job(
        context,
        "lark-test:FriendMessage:ou_user",
        media_route.MediaRoute(kind="image", prompt="独立图片预览卡"),
        card,
    )

    assert result.success is True
    streamer.upload_image.assert_awaited_once_with(str(image_path))
    send_card.assert_awaited_once()
    assert send_card.await_args.kwargs["card_type"] == "media_generation"
    assert send_card.await_args.kwargs["event"] == "preview"
    assert send_card.await_args.kwargs["card"]["body"]["elements"][0] == {
        "tag": "img",
        "img_key": "img_v2_preview",
        "alt": {
            "tag": "plain_text",
            "content": "第 1/1 张（GPT Image 2 · medium）。",
        },
    }
    context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_dreamina_request_uses_dreamina_first_policy() -> None:
    route = await media_route._detect_route(_Event(), "用即梦帮我生成一张中秋宣传海报")

    assert route is not None
    assert route.kind == "image"
    assert route.image_provider_strategy == "dreamina_first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "strategy"),
    [
        ("Image2（仅使用）", "gpt_only"),
        ("即梦（仅使用）", "dreamina_only"),
    ],
)
async def test_workspace_provider_choice_uses_strict_image_policy(
    choice: str,
    strategy: str,
) -> None:
    route = await media_route._detect_route(
        _Event(),
        f"#生图 新车发布主视觉\n生图模型：{choice}",
    )

    assert route is not None
    assert route.image_provider_strategy == strategy


@pytest.mark.asyncio
async def test_explicit_image2_policy_does_not_fallback_to_dreamina() -> None:
    dreamina = AsyncMock()
    module = SimpleNamespace(
        _call_codex_image_gen=lambda *_args: (False, "Image2 unavailable"),
        _dreamina_text2image_sync=dreamina,
    )

    success, detail, provider = await media_route._run_image_gpt_first(
        SimpleNamespace(send_message=AsyncMock()),
        "test-session",
        asyncio.get_running_loop(),
        module,
        image2_prompt="prompt",
        dreamina_prompt="prompt",
        route=media_route.MediaRoute(
            kind="image",
            prompt="prompt",
            image_provider_strategy="gpt_only",
        ),
        card=None,
        allow_fallback=False,
    )

    assert success is False
    assert "Image2 unavailable" in detail
    assert provider == ""
    dreamina.assert_not_awaited()


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
    archived = {}
    monkeypatch.setattr(
        "dc_engines.card_runtime.archive_card_result",
        lambda **kwargs: archived.update(kwargs) or kwargs,
    )

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
    output_path = Path(event.result.chain[0].path)
    assert output_path.suffix == ".png"
    assert archived["source"] == "dc_router.source_image_edit"
    assert archived["task_id"] == "om_source_edit"
    assert archived["delivery_files"] == [
        {
            "kind": "transparent_png",
            "name": output_path.name,
            "path": str(output_path),
        }
    ]


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
async def test_media_route_final_card_uses_runtime_media_card_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalize a real media result without mocking the builder import boundary."""
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "dc_engines.card_runtime.finalize_card_via_runtime",
        finalize,
    )
    stream = SimpleNamespace(elapsed_sec=12.0)
    card = SimpleNamespace(
        message_id="om_media_result",
        streamer=SimpleNamespace(get_stream=lambda _message_id: stream),
    )

    finalized = await media_route._finalize_waiting_card(
        SimpleNamespace(),
        card,
        route=media_route.MediaRoute(kind="image", prompt="冰雪足球怪兽海报"),
        success=True,
        detail="已生成 1 张图片",
        output_path="/tmp/result.png",
    )

    assert finalized is True
    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["card_type"] == "media_generation"
    assert finalize.await_args.kwargs["retract_after_sec"] is None


@pytest.mark.asyncio
async def test_media_route_sends_independent_result_card_before_retracting_waiting_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliver a visible terminal card instead of relying only on a long-lived patch."""
    send = AsyncMock(return_value=SimpleNamespace(message_id="om_result"))
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr("dc_engines.card_runtime.send_card_via_runtime", send)
    monkeypatch.setattr(
        "dc_engines.card_runtime.finalize_card_via_runtime",
        finalize,
    )
    streamer = SimpleNamespace(
        get_stream=lambda _message_id: SimpleNamespace(elapsed_sec=12.0),
        retract=AsyncMock(return_value=True),
    )
    card = SimpleNamespace(
        message_id="om_waiting",
        chat_id="oc_chat",
        receive_id_type="chat_id",
        streamer=streamer,
    )

    finalized = await media_route._finalize_waiting_card(
        SimpleNamespace(),
        card,
        route=media_route.MediaRoute(kind="image", prompt="冰雪足球怪兽海报"),
        success=True,
        detail="已生成 1 张图片",
        output_path="/tmp/result.png",
    )

    assert finalized is True
    send.assert_awaited_once()
    assert send.await_args.kwargs["card_type"] == "media_generation"
    assert send.await_args.kwargs["chat_id"] == "oc_chat"
    streamer.retract.assert_awaited_once_with("om_waiting")
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_route_with_waiting_card_suppresses_plain_ack(monkeypatch) -> None:
    event = _HandledEvent()
    event.set_extra("assistant_workbench_task_type", "image")
    event.set_extra(
        "assistant_workbench_workspace_url",
        "http://127.0.0.1:6185/api/v1/assistant-attachments/image-token",
    )
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
        SimpleNamespace(
            get_platform_inst=lambda _platform_id: SimpleNamespace(
                config={"app_id": "cli_test"}
            )
        ),
        event,
        "帮我生成一张未来城市图片",
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
    assert stored_records[0]["assistant_workbench"] is True
    assert "mode%3Drevise" in stored_records[0]["route"]["material_completion_url"]
    assert "reload=true" in stored_records[0]["route"]["material_completion_url"]


@pytest.mark.asyncio
async def test_completed_workbench_media_task_sends_session_choice(
    monkeypatch,
) -> None:
    from data.plugins.dc_router.preprocessing import session_choice

    record = {
        "task_id": "task-media",
        "umo": "lark:FriendMessage:ou_test",
        "platform_id": "巅池-Agent小助手",
        "assistant_workbench": True,
        "card": {"chat_id": "ou_test", "receive_id_type": "open_id"},
    }
    result = media_route.MediaJobResult(
        success=True,
        artifact_kind="image",
        uri="/tmp/result.png",
        mime_type="image/png",
        engine="test",
        detail="completed",
        metadata={},
    )
    monkeypatch.setattr(media_route, "_load_pending_media_tasks", lambda: [record])
    monkeypatch.setattr(media_route, "_background_job", AsyncMock(return_value=result))
    monkeypatch.setattr(
        media_route, "_remove_pending_media_task", lambda _task_id: None
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(session_choice, "send_session_choice", send)

    await media_route._background_job_with_record(
        SimpleNamespace(),
        record["umo"],
        media_route.MediaRoute(kind="image", prompt="future city"),
        None,
        record["task_id"],
    )

    send.assert_awaited_once_with(
        ANY,
        task_id="task-media",
        unified_msg_origin="lark:FriendMessage:ou_test",
        platform_id="巅池-Agent小助手",
        chat_id="ou_test",
        receive_id_type="open_id",
    )


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


@pytest.mark.asyncio
async def test_cancel_session_media_tasks_stops_worker_and_clears_record(
    monkeypatch, tmp_path
) -> None:
    pending_path = tmp_path / "media_route_pending.json"
    monkeypatch.setattr(media_route, "_MEDIA_TASKS_PATH", pending_path)
    task_id = "task_media_cancel"
    route = media_route.MediaRoute(kind="image", prompt="future city")
    card = SimpleNamespace(message_id="om_cancel")

    class FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    worker = FakeTask()
    media_route._upsert_pending_media_task(
        {
            "task_id": task_id,
            "created_at": 1.0,
            "updated_at": 1.0,
            "umo": "test-session",
            "platform_id": "lark",
            "route": media_route._route_to_dict(route),
            "card": {"message_id": "om_cancel"},
        }
    )
    media_route._ACTIVE_MEDIA_TASKS[task_id] = worker
    media_route._ACTIVE_MEDIA_CONTEXT[task_id] = ("test-session", route, card)
    finalized = AsyncMock(return_value=True)
    monkeypatch.setattr(media_route, "_finalize_waiting_card", finalized)

    cancelled = await media_route.cancel_session_media_tasks(
        SimpleNamespace(),
        "test-session",
        reason="user requested stop",
    )

    assert cancelled == 1
    assert worker.cancelled is True
    assert media_route._load_pending_media_tasks() == []
    assert task_id not in media_route._ACTIVE_MEDIA_TASKS
    assert task_id not in media_route._ACTIVE_MEDIA_CONTEXT
    finalized.assert_awaited_once_with(
        SimpleNamespace(),
        card,
        route=route,
        success=False,
        detail="user requested stop",
        cancelled=True,
    )
