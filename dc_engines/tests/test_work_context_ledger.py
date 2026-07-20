from __future__ import annotations

import hashlib
from pathlib import Path

import aiosqlite
import pytest
from dc_engines.harness import (
    HarnessEngine,
    HarnessTaskCreateRequest,
    HarnessTaskStore,
)


async def _runtime(tmp_path: Path) -> tuple[HarnessTaskStore, HarnessEngine]:
    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    return store, HarnessEngine(store)


async def _task(
    engine: HarnessEngine,
    *,
    title: str,
    session_id: str,
) -> object:
    return await engine.create_task(
        HarnessTaskCreateRequest(
            title=title,
            conversation_id="oc_media_chat",
            platform_id="lark",
            session_id=session_id,
            domain="media",
            payload={"source": "dc_router_media"},
        )
    )


async def test_work_context_survives_session_origin_changes(tmp_path: Path) -> None:
    store, _engine = await _runtime(tmp_path)

    first = await store.get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="lark:FriendMessage:ou_employee",
    )
    restored = await HarnessTaskStore(
        tmp_path / "harness.db"
    ).get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="lark:FriendMessage:message-scoped-origin",
    )

    assert restored.context_id == first.context_id
    assert restored.last_session_id == "lark:FriendMessage:message-scoped-origin"


async def test_message_reference_keeps_digest_not_message_body(tmp_path: Path) -> None:
    store, _engine = await _runtime(tmp_path)
    context = await store.get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="umo-1",
    )
    text = "人物再年轻一点"

    reference = await store.record_message_reference(
        context_id=context.context_id,
        task_id=None,
        platform_message_id="om_followup",
        session_id="umo-1",
        direction="inbound",
        content=text,
    )

    assert reference.content_digest == hashlib.sha256(text.encode()).hexdigest()
    assert not hasattr(reference, "content")
    async with aiosqlite.connect(store.db_path) as db:
        cursor = await db.execute("PRAGMA table_info(harness_message_refs)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert "content" not in columns
    assert "content_json" not in columns


async def test_execution_start_is_idempotent(tmp_path: Path) -> None:
    store, engine = await _runtime(tmp_path)
    task = await _task(engine, title="生成海报", session_id="umo-1")

    first = await store.start_execution(
        task_id=task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:om_first:attempt:1",
        request_digest="digest-1",
        metadata={"provider": "gpt_first"},
    )
    replay = await store.start_execution(
        task_id=task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:om_first:attempt:1",
        request_digest="digest-1",
        metadata={"provider": "gpt_first"},
    )

    assert replay.execution_id == first.execution_id
    assert replay.status == "running"

    with pytest.raises(RuntimeError, match="different request"):
        await store.start_execution(
            task_id=task.task_id,
            executor_kind="media",
            capability="image_generation",
            idempotency_key="media:om_first:attempt:1",
            request_digest="digest-conflict",
        )


async def test_artifact_settlement_is_idempotent(tmp_path: Path) -> None:
    store, engine = await _runtime(tmp_path)
    context = await store.get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="umo-1",
    )
    task = await _task(engine, title="生成海报", session_id="umo-1")
    await store.link_task_to_context(
        task_id=task.task_id,
        context_id=context.context_id,
        relation_type="root",
    )
    execution = await store.start_execution(
        task_id=task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:om_first:attempt:1",
        request_digest="digest-1",
    )

    first = await store.settle_execution_with_artifact(
        execution_id=execution.execution_id,
        context_id=context.context_id,
        task_id=task.task_id,
        artifact_kind="image",
        uri="/tmp/poster-v1.png",
        mime_type="image/png",
        idempotency_key="artifact:media:om_first:primary",
        metadata={"prompt": "年轻女性人物海报"},
    )
    replay = await store.settle_execution_with_artifact(
        execution_id=execution.execution_id,
        context_id=context.context_id,
        task_id=task.task_id,
        artifact_kind="image",
        uri="/tmp/poster-v1.png",
        mime_type="image/png",
        idempotency_key="artifact:media:om_first:primary",
        metadata={"prompt": "年轻女性人物海报"},
    )

    assert replay.artifact_id == first.artifact_id
    assert replay.root_artifact_id == first.artifact_id
    assert replay.version == 1
    assert (await store.get_execution(execution.execution_id)).status == "succeeded"

    with pytest.raises(RuntimeError, match="different artifact"):
        await store.settle_execution_with_artifact(
            execution_id=execution.execution_id,
            context_id=context.context_id,
            task_id=task.task_id,
            artifact_kind="image",
            uri="/tmp/conflicting-poster.png",
            mime_type="image/png",
            idempotency_key="artifact:media:om_first:primary",
        )


async def test_revision_uses_child_task_and_preserves_terminal_source(
    tmp_path: Path,
) -> None:
    store, engine = await _runtime(tmp_path)
    context = await store.get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="umo-1",
    )
    root_task = await _task(engine, title="生成海报", session_id="umo-1")
    await store.link_task_to_context(
        task_id=root_task.task_id,
        context_id=context.context_id,
        relation_type="root",
    )
    root_execution = await store.start_execution(
        task_id=root_task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:root:attempt:1",
        request_digest="digest-root",
    )
    root_artifact = await store.settle_execution_with_artifact(
        execution_id=root_execution.execution_id,
        context_id=context.context_id,
        task_id=root_task.task_id,
        artifact_kind="image",
        uri="/tmp/poster-v1.png",
        mime_type="image/png",
        idempotency_key="artifact:root:primary",
    )
    await engine.complete_task(
        root_task.task_id,
        result={"summary": "图片已生成", "output_path": root_artifact.uri},
    )

    revision_task = await _task(engine, title="修改海报", session_id="umo-2")
    await store.link_task_to_context(
        task_id=revision_task.task_id,
        context_id=context.context_id,
        parent_task_id=root_task.task_id,
        relation_type="revision",
    )
    revision_execution = await store.start_execution(
        task_id=revision_task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:revision:attempt:1",
        request_digest="digest-revision",
    )
    revision_artifact = await store.settle_execution_with_artifact(
        execution_id=revision_execution.execution_id,
        context_id=context.context_id,
        task_id=revision_task.task_id,
        artifact_kind="image",
        uri="/tmp/poster-v2.png",
        mime_type="image/png",
        idempotency_key="artifact:revision:primary",
        parent_artifact_id=root_artifact.artifact_id,
    )
    await engine.complete_task(
        revision_task.task_id,
        result={"summary": "图片修订完成", "output_path": revision_artifact.uri},
    )

    assert (await store.get_task(root_task.task_id)).status == "completed"
    assert (await store.get_task(revision_task.task_id)).status == "completed"
    task_link = await store.get_task_link(revision_task.task_id)
    assert task_link.parent_task_id == root_task.task_id
    assert task_link.relation_type == "revision"
    assert revision_artifact.parent_artifact_id == root_artifact.artifact_id
    assert revision_artifact.root_artifact_id == root_artifact.artifact_id
    assert revision_artifact.version == 2
    assert await store.get_artifact(root_artifact.artifact_id) == root_artifact


async def test_cancel_execution_preserves_completed_artifacts(tmp_path: Path) -> None:
    store, engine = await _runtime(tmp_path)
    context = await store.get_or_create_work_context(
        scope_key="media:lark:oc_media_chat:ou_employee",
        platform_id="lark",
        conversation_id="oc_media_chat",
        subject_ref="ou_employee",
        session_id="umo-1",
    )
    root_task = await _task(engine, title="生成海报", session_id="umo-1")
    await store.link_task_to_context(
        task_id=root_task.task_id,
        context_id=context.context_id,
        relation_type="root",
    )
    root_execution = await store.start_execution(
        task_id=root_task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:root:attempt:1",
        request_digest="digest-root",
    )
    root_artifact = await store.settle_execution_with_artifact(
        execution_id=root_execution.execution_id,
        context_id=context.context_id,
        task_id=root_task.task_id,
        artifact_kind="image",
        uri="/tmp/poster-v1.png",
        mime_type="image/png",
        idempotency_key="artifact:root:primary",
    )
    pending_task = await _task(engine, title="修改海报", session_id="umo-2")
    pending_execution = await store.start_execution(
        task_id=pending_task.task_id,
        executor_kind="media",
        capability="image_generation",
        idempotency_key="media:pending:attempt:1",
        request_digest="digest-pending",
    )

    cancelled = await store.cancel_execution(
        pending_execution.execution_id,
        reason="user requested stop",
    )

    assert cancelled.status == "cancelled"
    assert await store.get_artifact(root_artifact.artifact_id) == root_artifact
    assert (
        await store.get_latest_artifact(
            scope_key=context.scope_key,
            artifact_kind="image",
        )
    ).artifact_id == root_artifact.artifact_id
