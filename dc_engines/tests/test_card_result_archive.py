"""Formal Feishu result-card archive and lifecycle trace tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dc_engines.card_runtime import (
    finalize_card_via_runtime,
    record_card_action_via_runtime,
    send_card_via_runtime,
)
from dc_engines.card_system import (
    CARD_REGISTRY,
    archive_card_result,
    attach_card_delivery_files,
    load_card_result_archive,
    mark_card_result_retracted,
    record_card_runtime_event,
)
from dc_engines.feishu_card_streamer import CardStream, FeishuCardStreamer


def _configure_runtime_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Route all card runtime persistence into the current test directory.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Isolated pytest directory.

    Returns:
        None.
    """
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setenv("CARD_RUNTIME_PERSIST_IN_TESTS", "true")
    monkeypatch.setattr(
        "dc_engines.card_system.CARD_RESULT_ARCHIVE_PATH",
        tmp_path / "results",
    )
    monkeypatch.setattr(
        "dc_engines.card_system.CARD_RUNTIME_EVENTS_PATH",
        tmp_path / "events.jsonl",
    )
    monkeypatch.setattr(
        "dc_engines.card_system.CARD_STATE_PATH",
        tmp_path / "card_system_state.json",
    )


def _result_card(content: str, *, url: str = "") -> dict:
    """Build a compact Card JSON fixture.

    Args:
        content: Markdown content retained as the formal result.
        url: Optional delivered file URL.

    Returns:
        Card JSON fixture.
    """
    elements: list[dict] = [{"tag": "markdown", "content": content}]
    if url:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Open result"},
                "multi_url": {"url": url, "pc_url": url},
            }
        )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "Result"},
            "app_secret": "must-not-persist",
        },
        "body": {"elements": elements},
    }


def test_formal_result_archive_redacts_card_and_links_delivery_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)
    card = _result_card(
        "**编号**\n`#task-2026-001`\napi_key=visible-secret\n"
        "输出：`/srv/implicit/result.pdf`",
        url="https://cdn.example.com/report.pdf?token=signed-secret&download=1",
    )

    stored = archive_card_result(
        archive_event="finalize",
        card_type="task_result",
        message_id="om_result_001",
        conversation_id="oc_conversation_001",
        card=card,
        platform_id="lark-main",
        source="hermes_bridge",
        source_message_id="om_user_001",
        delivery_files=[
            "/srv/deliveries/report.docx",
            {"artifact_id": "artifact-001", "name": "report.docx"},
        ],
    )

    assert stored is not None
    serialized = json.dumps(stored, ensure_ascii=False)
    assert "must-not-persist" not in serialized
    assert "visible-secret" not in serialized
    assert "signed-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert stored["card_type"] == "task_result"
    assert stored["card_version"] == CARD_REGISTRY["task_result"].version
    assert stored["message_id"] == "om_result_001"
    assert stored["conversation_id"] == "oc_conversation_001"
    assert stored["source_task"] == {
        "source": "hermes_bridge",
        "task_id": "task-2026-001",
        "source_message_id": "om_user_001",
    }
    assert {item.get("path") for item in stored["delivery_files"]} >= {
        "/srv/deliveries/report.docx",
        "/srv/implicit/result.pdf",
    }
    assert {item.get("artifact_id") for item in stored["delivery_files"]} >= {
        "artifact-001"
    }
    assert stored["versions"][0]["card_json"] == stored["final_card"]
    assert load_card_result_archive("om_result_001") == stored


def test_late_office_artifacts_are_attached_without_fake_card_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)
    archive_card_result(
        archive_event="finalize",
        card_type="daily_response",
        message_id="om_office_result",
        conversation_id="oc_office",
        card=_result_card("LLM analysis result"),
    )

    updated = attach_card_delivery_files(
        "om_office_result",
        [
            "/srv/deliveries/analysis.docx",
            "https://feishu.cn/docx/doccn_office",
        ],
    )

    assert updated is True
    stored = load_card_result_archive("om_office_result")
    assert stored is not None
    assert len(stored["versions"]) == 1
    assert stored["lifecycle"][-1]["event"] == "artifact_delivery"
    assert {item.get("path") for item in stored["delivery_files"]} >= {
        "/srv/deliveries/analysis.docx"
    }
    assert {item.get("url") for item in stored["delivery_files"]} >= {
        "https://feishu.cn/docx/doccn_office"
    }


def test_result_version_regeneration_action_and_retraction_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)
    archive_card_result(
        archive_event="send",
        card_type="daily_response",
        message_id="om_parent",
        conversation_id="oc_trace",
        card=_result_card("Version one"),
    )
    archive_card_result(
        archive_event="finalize",
        card_type="daily_response",
        message_id="om_parent",
        conversation_id="oc_trace",
        card=_result_card("Version two"),
    )
    record_card_action_via_runtime(
        message_id="om_parent",
        conversation_id="oc_trace",
        action="regenerate",
        source="daily_response",
        task_id="task-trace-001",
        operator_id="ou_private_operator",
    )
    child = archive_card_result(
        archive_event="regenerated",
        card_type="daily_response",
        message_id="om_child",
        conversation_id="oc_trace",
        card=_result_card("Regenerated output"),
        regeneration_of_message_id="om_parent",
    )

    parent = load_card_result_archive("om_parent")
    assert parent is not None
    assert len(parent["versions"]) == 2
    assert parent["actions"][-1]["user_regeneration"] is True
    assert parent["actions"][-1]["operator_digest"]
    assert "ou_private_operator" not in json.dumps(parent, ensure_ascii=False)
    assert parent["regenerated_message_ids"] == ["om_child"]
    assert child is not None
    assert child["regeneration_of_message_id"] == "om_parent"

    assert mark_card_result_retracted("om_child") is True
    retracted = load_card_result_archive("om_child")
    assert retracted is not None
    assert retracted["status"] == "retracted"
    assert retracted["lifecycle"][-1]["event"] == "retract"


@pytest.mark.asyncio
async def test_runtime_archives_result_send_but_waiting_updates_are_event_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)

    class _Response:
        code = 0
        msg = "ok"

        def __init__(self, message_id: str = "") -> None:
            self.data = SimpleNamespace(message_id=message_id) if message_id else None

        def success(self) -> bool:
            return True

    class _MessageApi:
        def __init__(self) -> None:
            self.counter = 0

        async def acreate(self, _request):
            self.counter += 1
            return _Response(f"om_runtime_{self.counter}")

        async def apatch(self, _request):
            return _Response()

        async def adelete(self, _request):
            return _Response()

    class _Client:
        class _V1:
            message = _MessageApi()

        im = SimpleNamespace(v1=_V1())

    streamer = FeishuCardStreamer(_Client())  # type: ignore[arg-type]
    result_stream = await send_card_via_runtime(
        streamer,
        card_type="casual_reply",
        chat_id="oc_runtime_trace",
        receive_id_type="chat_id",
        card=_result_card("Formal answer"),
        platform_id="lark-main",
    )
    waiting_stream = await send_card_via_runtime(
        streamer,
        card_type="thinking_waiting",
        chat_id="oc_runtime_trace",
        receive_id_type="chat_id",
        card=_result_card("Working"),
        platform_id="lark-main",
    )

    assert result_stream is not None
    assert waiting_stream is not None
    assert load_card_result_archive(result_stream.message_id) is not None
    assert load_card_result_archive(waiting_stream.message_id) is None

    assert await streamer.update(
        result_stream.message_id,
        _result_card("Formal answer version two"),
    )
    updated_result = load_card_result_archive(result_stream.message_id)
    assert updated_result is not None
    assert len(updated_result["versions"]) == 2
    assert updated_result["final_card"]["body"]["elements"][0]["content"] == (
        "Formal answer version two"
    )

    assert await streamer.update(
        waiting_stream.message_id,
        _result_card("Still working"),
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        item["event"] == "update" and item["message_id"] == waiting_stream.message_id
        for item in events
    )
    assert load_card_result_archive(waiting_stream.message_id) is None
    assert await streamer.retract(result_stream.message_id)
    retracted_result = load_card_result_archive(result_stream.message_id)
    assert retracted_result is not None
    assert retracted_result["status"] == "retracted"

    def fail_archive(**_kwargs):
        raise OSError("archive disk unavailable")

    monkeypatch.setattr("dc_engines.card_runtime.archive_card_result", fail_archive)
    delivered_without_archive = await send_card_via_runtime(
        streamer,
        card_type="casual_reply",
        chat_id="oc_runtime_trace",
        receive_id_type="chat_id",
        card=_result_card("Delivery must still succeed"),
        platform_id="lark-main",
    )
    assert delivered_without_archive is not None
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        item["event"] == "result_archive"
        and item["message_id"] == delivered_without_archive.message_id
        and item["ok"] is False
        for item in events
    )


@pytest.mark.asyncio
async def test_runtime_finalize_retains_result_before_recorded_retraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)

    class _Streamer:
        def __init__(self) -> None:
            self.stream = CardStream(
                message_id="om_terminal",
                chat_id="oc_terminal_trace",
                receive_id_type="chat_id",
            )

        def get_stream(self, message_id: str):
            return self.stream if message_id == self.stream.message_id else None

        async def finalize(self, _message_id: str, _card: dict) -> bool:
            return True

        def schedule_retract(self, _message_id, _delay, *, on_result=None):
            if on_result is not None:
                on_result(True)
            return object()

    ok = await finalize_card_via_runtime(
        _Streamer(),  # type: ignore[arg-type]
        card_type="media_generation",
        message_id="om_terminal",
        card=_result_card(
            "**编号**\n`#media-task-001`",
            url="https://cdn.example.com/generated.png",
        ),
        platform_id="lark-main",
        delivery_files=["/srv/deliveries/generated.png"],
        retract_after_sec=8.0,
    )

    assert ok is True
    archive = load_card_result_archive("om_terminal")
    assert archive is not None
    assert archive["conversation_id"] == "oc_terminal_trace"
    assert archive["source_task"]["task_id"] == "media-task-001"
    assert archive["status"] == "retracted"
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["event"] for item in events} == {
        "finalize",
        "result_archive",
        "retract",
        "result_archive_retract",
        "retract_scheduled",
    }


@pytest.mark.asyncio
async def test_finalize_recovers_conversation_context_from_send_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_runtime_paths(monkeypatch, tmp_path)
    record_card_runtime_event(
        event="start",
        card_type="thinking_waiting",
        ok=True,
        platform_id="lark-recovered",
        chat_id="oc_recovered_trace",
        receive_id_type="chat_id",
        message_id="om_recovered",
    )

    class _Finalizer:
        async def finalize(self, _message_id: str, _card: dict) -> bool:
            return True

    ok = await finalize_card_via_runtime(
        _Finalizer(),  # type: ignore[arg-type]
        card_type="daily_response",
        message_id="om_recovered",
        card=_result_card("Recovered final result"),
    )

    assert ok is True
    archive = load_card_result_archive("om_recovered")
    assert archive is not None
    assert archive["conversation_id"] == "oc_recovered_trace"
    assert archive["platform_id"] == "lark-recovered"


def test_waiting_and_progress_registry_policies_are_event_only() -> None:
    assert CARD_REGISTRY["thinking_waiting"].archive_events == ()
    assert CARD_REGISTRY["task_progress"].archive_events == ()
