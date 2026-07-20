from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dc_engines.harness import HarnessEngine, HarnessTaskStore

from data.plugins.dc_router.preprocessing import media_route


class _Event:
    def __init__(
        self,
        *,
        text_id: str,
        unified_msg_origin: str,
        chat_id: str = "oc_media_chat",
        sender_id: str = "ou_employee",
    ) -> None:
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = SimpleNamespace(
            message=[],
            raw_message=SimpleNamespace(chat_id=chat_id, message_id=text_id),
        )
        self._sender_id = sender_id
        self.result = None
        self.llm_enabled = True
        self.extras: dict[str, str] = {}

    def get_platform_id(self) -> str:
        return "lark"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return ""

    def set_extra(self, key: str, value: str) -> None:
        self.extras[key] = value

    def should_call_llm(self, enabled: bool) -> None:
        self.llm_enabled = enabled

    def set_result(self, result) -> None:
        self.result = result


async def _runtime(db_path: Path) -> SimpleNamespace:
    store = HarnessTaskStore(db_path)
    await store.initialize()
    return SimpleNamespace(
        harness_store=store,
        harness_engine=HarnessEngine(store),
        send_message=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _clear_media_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    media_route._ACTIVE_MEDIA_TASKS.clear()
    media_route._ACTIVE_MEDIA_CONTEXT.clear()
    media_route._LAST_IMAGE_BY_SESSION.clear()
    monkeypatch.setattr(media_route, "_MEDIA_TASKS_PATH", tmp_path / "pending.json")
    yield
    media_route._ACTIVE_MEDIA_TASKS.clear()
    media_route._ACTIVE_MEDIA_CONTEXT.clear()
    media_route._LAST_IMAGE_BY_SESSION.clear()


@pytest.mark.asyncio
async def test_media_revision_survives_restart_and_session_origin_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_image = tmp_path / "poster-v1.png"
    revised_image = tmp_path / "poster-v2.png"
    first_image.touch()
    revised_image.touch()
    outputs = iter((first_image, revised_image))

    async def run_job(_context, _umo, route, _card):
        output = next(outputs)
        return media_route.MediaJobResult(
            success=True,
            artifact_kind="image",
            uri=str(output),
            mime_type="image/png",
            engine="test-image-engine",
            detail="generated",
            metadata={"prompt": route.prompt},
        )

    monkeypatch.setattr(
        media_route, "_start_waiting_card", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(media_route, "_background_job", run_job)
    db_path = tmp_path / "harness.db"
    first_context = await _runtime(db_path)
    first_event = _Event(text_id="om_first", unified_msg_origin="umo-message-1")

    assert await media_route.try_handle_media_route(
        first_context,
        first_event,
        "帮我生成一张年轻女性人物海报",
    )
    first_workers = list(media_route._ACTIVE_MEDIA_TASKS.values())
    await asyncio.gather(*first_workers)

    scope_key = "media:lark:oc_media_chat:ou_employee"
    first_artifact = await first_context.harness_store.get_latest_artifact(
        scope_key=scope_key,
        artifact_kind="image",
    )
    assert first_artifact is not None
    assert first_artifact.version == 1
    first_task = await first_context.harness_store.get_task(first_artifact.task_id)
    assert first_task is not None
    assert first_task.status == "completed"

    media_route._LAST_IMAGE_BY_SESSION.clear()
    media_route._ACTIVE_MEDIA_TASKS.clear()
    media_route._ACTIVE_MEDIA_CONTEXT.clear()
    restored_context = await _runtime(db_path)
    followup_event = _Event(text_id="om_followup", unified_msg_origin="umo-message-2")

    assert await media_route.try_handle_media_route(
        restored_context,
        followup_event,
        "人物再年轻一点",
    )
    followup_workers = list(media_route._ACTIVE_MEDIA_TASKS.values())
    await asyncio.gather(*followup_workers)

    revised_artifact = await restored_context.harness_store.get_latest_artifact(
        scope_key=scope_key,
        artifact_kind="image",
    )
    assert revised_artifact is not None
    assert revised_artifact.version == 2
    assert revised_artifact.parent_artifact_id == first_artifact.artifact_id
    assert revised_artifact.root_artifact_id == first_artifact.artifact_id
    revised_task = await restored_context.harness_store.get_task(
        revised_artifact.task_id
    )
    assert revised_task is not None
    assert revised_task.status == "completed"
    revised_link = await restored_context.harness_store.get_task_link(
        revised_artifact.task_id
    )
    assert revised_link is not None
    assert revised_link.relation_type == "revision"
    assert revised_link.parent_task_id == first_task.task_id
    assert (
        await restored_context.harness_store.get_task(first_task.task_id)
    ).status == ("completed")


@pytest.mark.asyncio
async def test_revision_like_text_without_artifact_does_not_route(
    tmp_path: Path,
) -> None:
    context = await _runtime(tmp_path / "empty-harness.db")
    event = _Event(text_id="om_no_source", unified_msg_origin="umo-empty")

    route = await media_route._detect_route(event, "人物再年轻一点", context)

    assert route is None
