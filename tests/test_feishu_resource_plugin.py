import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dc_engines"))

import pytest
from dc_engines.feishu_reader import (
    FeishuCredentials,
    QueryHit,
    Whitelist,
    WhitelistDocument,
)  # noqa: E402

import data.plugins.feishu_resource_plugin.main as feishu_plugin_module  # noqa: E402
from data.plugins.feishu_resource_plugin.main import (
    FeishuResourcePlugin,
    _extract_user_query_text,
    _should_handle_resource_query,
)  # noqa: E402


def test_attachment_summary_does_not_trigger_resource_query() -> None:
    text = (
        "那为什么我在培训手册里看到的 /dr 跟你说的又不一样呢？\n\n"
        "<attachment_summary>\n"
        "图片里包含资料、跨源调研、输入 /dr 触发等文字。\n"
        "</attachment_summary>"
    )

    assert _extract_user_query_text(text) == (
        "那为什么我在培训手册里看到的 /dr 跟你说的又不一样呢？"
    )
    assert _should_handle_resource_query(text) is False


def test_explicit_resource_query_still_triggers() -> None:
    assert _should_handle_resource_query("查 员工手册") is True
    assert _should_handle_resource_query("查 Alpha") is True
    assert _should_handle_resource_query("帮我查一下客户资料") is True


def test_feishu_analysis_request_reaches_llm_workflow() -> None:
    text = "请你解读这个飞书链接里面所有方案文档，总结案例内容 https://dianchi.feishu.cn/docx/abc"

    assert _should_handle_resource_query(text) is False


def test_resource_discussion_question_does_not_trigger_query() -> None:
    assert _should_handle_resource_query("为什么资料里看到的和你说的不一样？") is False


def test_external_topic_query_reaches_llm_workflow() -> None:
    text = (
        "查询今天国内主流平台的 top10 话题，关键词必须击中五菱、柳汽、新能源、"
        "当下年轻人、年龄25-35之间"
    )

    assert _should_handle_resource_query(text) is False


def test_keyword_extraction_uses_visible_query_text_only() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    text = _extract_user_query_text(
        "帮我查一下客户资料\n\n<attachment_summary>\n资料 /dr\n</attachment_summary>"
    )

    assert plugin._extract_keyword(text) == "客户资料"


def test_keyword_extraction_prefers_longer_query_verbs() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)

    assert plugin._extract_keyword("查询员工手册") == "员工手册"
    assert plugin._extract_keyword("搜索 客户资料") == "客户资料"


@pytest.mark.asyncio
async def test_initialize_uses_metadata_only_mode_when_client_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DisabledClient:
        enabled = False

    def fake_load_whitelist(_path):
        return (
            Whitelist(
                documents=[
                    WhitelistDocument(
                        doc_token="doxc_disabled",
                        name="Disabled client doc",
                    )
                ]
            ),
            FeishuCredentials(
                app_id="cli_fake",
                app_secret="secret_fake",
                enable=True,
            ),
        )

    monkeypatch.setattr(feishu_plugin_module, "load_whitelist", fake_load_whitelist)
    monkeypatch.setattr(
        feishu_plugin_module,
        "FeishuClient",
        lambda _credentials: DisabledClient(),
    )
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)

    await plugin.initialize()

    assert plugin.mode == "metadata_only"
    assert plugin.client is not None


def test_hits_reply_text_includes_retrieval_provenance() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.mode = "metadata_only"
    hit = QueryHit(
        source_type="document",
        source_id="doxc_alpha",
        title="Alpha runbook",
        domain="ops",
        matched_field="meta",
        matched_snippet="Alpha escalation path",
        score=0.8,
        url="https://feishu.cn/docx/doxc_alpha",
        metadata={
            "retrieval_mode": "metadata_only",
            "credential_status": "disabled",
            "content_status": "error",
            "error_type": "TimeoutError",
        },
    )

    text = plugin._build_hits_reply_text("Alpha", [hit])

    assert "metadata_only" in text
    assert "disabled" in text
    assert "document" in text
    assert "Alpha escalation path" in text
    assert "https://feishu.cn/docx/doxc_alpha" in text
    assert "error" in text
    assert "TimeoutError" in text


def test_metadata_query_credential_status_distinguishes_disabled_from_missing() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)

    plugin.mode = "metadata_only"
    assert plugin._metadata_query_credential_status() == "disabled"

    plugin.mode = "v0"
    assert plugin._metadata_query_credential_status() == "missing"


@pytest.mark.parametrize("task_domain", ["truth_intake", "content_sop:client_dept"])
@pytest.mark.asyncio
async def test_harness_resource_result_attaches_hit_provenance_without_completion(
    task_domain: str,
) -> None:
    class FakeStore:
        async def get_task(self, _task_id: str):
            return SimpleNamespace(
                task_id="task_1",
                status="in_progress",
                domain=task_domain,
                payload={
                    "source_citations": [{"title": "Existing", "source_path": "old"}]
                },
            )

    class FakeHarnessEngine:
        def __init__(self) -> None:
            self.store = FakeStore()
            self.merged_payload = None
            self.trace = None
            self.status_updates: list[dict] = []
            self.completed_calls: list[dict] = []

        async def merge_payload(
            self,
            _task_id: str,
            patch: dict,
            *,
            event_type: str = "payload_merged",
        ) -> None:
            self.merged_payload = {"patch": patch, "event_type": event_type}

        async def append_trace(
            self,
            _task_id: str,
            event_type: str,
            payload: dict,
        ) -> None:
            self.trace = {"event_type": event_type, "payload": payload}

        async def set_status(
            self,
            task_id: str,
            status: str,
            *,
            result: dict | None = None,
            event_payload: dict | None = None,
        ) -> None:
            self.status_updates.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "result": result,
                    "event_payload": event_payload,
                }
            )

        async def complete_task(self, _task_id: str, *, result: dict) -> None:
            self.completed_calls.append({"result": result})

    class FakeEvent:
        unified_msg_origin = "GroupMessage:1"

        def get_extra(self, key: str):
            if key == "dc_truth_intake_task_id":
                return "task_1"
            return None

    harness_engine = FakeHarnessEngine()
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.context = SimpleNamespace(harness_engine=harness_engine)
    plugin.mode = "metadata_only"
    hit = QueryHit(
        source_type="document",
        source_id="doxc_alpha",
        title="Alpha runbook",
        domain="ops",
        matched_field="meta",
        matched_snippet="Alpha escalation path",
        url="https://feishu.cn/docx/doxc_alpha",
        metadata={
            "retrieval_mode": "metadata_only",
            "credential_status": "disabled",
            "content_status": "error",
            "error_type": "TimeoutError",
        },
    )

    await plugin._record_harness_resource_result(
        FakeEvent(),
        status="evidence_attached",
        summary="Returned one hit.",
        keyword="Alpha",
        hits=[hit],
    )

    assert harness_engine.merged_payload is not None
    assert harness_engine.merged_payload["event_type"] == (
        "feishu_resource_evidence_attached"
    )
    stored_hit = harness_engine.merged_payload["patch"]["feishu_resource_hits"][0]
    assert stored_hit["source_type"] == "document"
    assert stored_hit["url"] == "https://feishu.cn/docx/doxc_alpha"
    assert stored_hit["retrieval_mode"] == "metadata_only"
    assert stored_hit["matched_snippet"] == "Alpha escalation path"
    assert stored_hit["credential_status"] == "disabled"
    assert stored_hit["content_status"] == "error"
    assert stored_hit["error_type"] == "TimeoutError"
    source_paths = {
        item["source_path"]
        for item in harness_engine.merged_payload["patch"]["source_citations"]
    }
    assert source_paths == {"old", "https://feishu.cn/docx/doxc_alpha"}
    assert harness_engine.trace["event_type"] == "feishu_resource_retrieval_result"
    assert harness_engine.status_updates == []
    assert harness_engine.completed_calls == []


@pytest.mark.parametrize("task_status", ["pending", "in_progress", "blocked"])
@pytest.mark.asyncio
async def test_harness_resource_no_hits_blocks_only_blockable_statuses(
    task_status: str,
) -> None:
    class FakeStore:
        async def get_task(self, _task_id: str):
            return SimpleNamespace(
                task_id="task_1",
                status=task_status,
                domain="truth_intake",
                payload={},
            )

    class FakeHarnessEngine:
        def __init__(self) -> None:
            self.store = FakeStore()
            self.status_updates: list[dict] = []

        async def set_status(
            self,
            task_id: str,
            status: str,
            *,
            result: dict | None = None,
            event_payload: dict | None = None,
        ) -> None:
            self.status_updates.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "result": result,
                    "event_payload": event_payload,
                }
            )

        async def complete_task(self, _task_id: str, *, result: dict) -> None:
            raise AssertionError("missing resources must not complete harness tasks")

    class FakeEvent:
        unified_msg_origin = "GroupMessage:1"

        def get_extra(self, key: str):
            if key == "dc_truth_intake_task_id":
                return "task_1"
            return None

    harness_engine = FakeHarnessEngine()
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.context = SimpleNamespace(harness_engine=harness_engine)
    plugin.mode = "metadata_only"

    await plugin._record_harness_resource_result(
        FakeEvent(),
        status="blocked",
        summary="No hits.",
        keyword="Alpha",
    )

    assert harness_engine.status_updates[0]["status"] == "blocked"
    assert harness_engine.status_updates[0]["result"]["quality"] == "blocked"


@pytest.mark.asyncio
async def test_harness_resource_hits_attach_evidence_without_unblocking_task() -> None:
    class FakeStore:
        async def get_task(self, _task_id: str):
            return SimpleNamespace(
                task_id="task_1",
                status="blocked",
                domain="truth_intake",
                payload={},
            )

    class FakeHarnessEngine:
        def __init__(self) -> None:
            self.store = FakeStore()
            self.merged_payload = None
            self.trace = None
            self.status_updates: list[dict] = []
            self.completed_calls: list[dict] = []

        async def merge_payload(
            self,
            _task_id: str,
            patch: dict,
            *,
            event_type: str = "payload_merged",
        ) -> None:
            self.merged_payload = {"patch": patch, "event_type": event_type}

        async def append_trace(
            self,
            _task_id: str,
            event_type: str,
            payload: dict,
        ) -> None:
            self.trace = {"event_type": event_type, "payload": payload}

        async def set_status(
            self,
            task_id: str,
            status: str,
            *,
            result: dict | None = None,
            event_payload: dict | None = None,
        ) -> None:
            self.status_updates.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "result": result,
                    "event_payload": event_payload,
                }
            )

        async def complete_task(self, _task_id: str, *, result: dict) -> None:
            self.completed_calls.append({"result": result})

    class FakeEvent:
        unified_msg_origin = "GroupMessage:1"

        def get_extra(self, key: str):
            if key == "dc_truth_intake_task_id":
                return "task_1"
            return None

    harness_engine = FakeHarnessEngine()
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.context = SimpleNamespace(harness_engine=harness_engine)
    plugin.mode = "metadata_only"
    hit = QueryHit(
        source_type="document",
        source_id="doxc_alpha",
        title="Alpha runbook",
        domain="ops",
        matched_field="meta",
        matched_snippet="Alpha escalation path",
    )

    await plugin._record_harness_resource_result(
        FakeEvent(),
        status="evidence_attached",
        summary="Returned one hit.",
        keyword="Alpha",
        hits=[hit],
    )

    assert harness_engine.merged_payload["event_type"] == (
        "feishu_resource_evidence_attached"
    )
    assert harness_engine.merged_payload["patch"]["resource_retrieval_status"] == (
        "evidence_attached"
    )
    assert harness_engine.trace["event_type"] == "feishu_resource_retrieval_result"
    assert harness_engine.status_updates == []
    assert harness_engine.completed_calls == []


@pytest.mark.asyncio
async def test_harness_resource_no_hits_does_not_block_review_required_task() -> None:
    class FakeStore:
        async def get_task(self, _task_id: str):
            return SimpleNamespace(
                task_id="task_1",
                status="review_required",
                domain="truth_intake",
                payload={},
            )

    class FakeHarnessEngine:
        def __init__(self) -> None:
            self.store = FakeStore()
            self.traces: list[dict] = []

        async def append_trace(
            self,
            task_id: str,
            event_type: str,
            payload: dict,
        ) -> None:
            self.traces.append(
                {"task_id": task_id, "event_type": event_type, "payload": payload}
            )

        async def set_status(self, *_args, **_kwargs) -> None:
            raise AssertionError("review_required task status must not be changed")

        async def complete_task(self, _task_id: str, *, result: dict) -> None:
            raise AssertionError("review_required task must not be completed")

    class FakeEvent:
        unified_msg_origin = "GroupMessage:1"

        def get_extra(self, key: str):
            if key == "dc_truth_intake_task_id":
                return "task_1"
            return None

    harness_engine = FakeHarnessEngine()
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.context = SimpleNamespace(harness_engine=harness_engine)
    plugin.mode = "v1"

    await plugin._record_harness_resource_result(
        FakeEvent(),
        status="blocked",
        summary="No hits.",
        keyword="Alpha",
    )

    assert harness_engine.traces[0]["event_type"] == (
        "feishu_resource_missing_skipped_status_change"
    )


@pytest.mark.asyncio
async def test_on_message_enabled_v1_uses_query_engine_and_replies_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {}

    async def fake_query_resources_v1(
        keyword: str,
        *,
        whitelist: Whitelist,
        client,
        domain_hint: str | None = None,
    ) -> list[QueryHit]:
        calls["keyword"] = keyword
        calls["whitelist"] = whitelist
        calls["client"] = client
        calls["domain_hint"] = domain_hint
        return [
            QueryHit(
                source_type="document",
                source_id="doxc_alpha",
                title="Alpha playbook",
                domain="ops",
                matched_field="content",
                matched_snippet="Alpha launch checklist",
                score=0.9,
                url="https://feishu.cn/docx/doxc_alpha",
                metadata={
                    "retrieval_mode": "content",
                    "credential_status": "enabled",
                },
            )
        ]

    class FakeEvent:
        message_str = "查 Alpha"
        unified_msg_origin = "PrivateMessage:1"
        is_at_or_wake_command = False

    monkeypatch.setattr(
        feishu_plugin_module,
        "query_resources_v1",
        fake_query_resources_v1,
    )
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.mode = "v1"
    plugin.client = SimpleNamespace(enabled=True)
    plugin.whitelist = Whitelist()
    plugin.context = SimpleNamespace(case_engine=None)
    replies: list[str] = []

    async def fake_reply_card_or_text(_event, *, card: dict, fallback_text: str):
        replies.append(fallback_text)

    async def fake_record_harness_resource_result(_event, **_kwargs) -> None:
        return None

    plugin._reply_card_or_text = fake_reply_card_or_text
    plugin._record_harness_resource_result = fake_record_harness_resource_result

    await plugin.on_message(FakeEvent())

    assert calls["keyword"] == "Alpha"
    assert calls["client"] is plugin.client
    assert replies
    assert "content" in replies[0]
    assert "enabled" in replies[0]
    assert "Alpha launch checklist" in replies[0]


@pytest.mark.asyncio
async def test_on_message_v1_no_hits_explains_bounded_content_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"query_count": 0}

    async def fake_query_resources_v1(
        keyword: str,
        *,
        whitelist: Whitelist,
        client,
        domain_hint: str | None = None,
    ) -> list[QueryHit]:
        calls["query_count"] += 1
        calls["client"] = client
        return []

    class FakeEvent:
        message_str = "查 Alpha"
        unified_msg_origin = "PrivateMessage:1"
        is_at_or_wake_command = False

    monkeypatch.setattr(
        feishu_plugin_module,
        "query_resources_v1",
        fake_query_resources_v1,
    )
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    plugin.mode = "v1"
    plugin.client = SimpleNamespace(enabled=True)
    plugin.whitelist = Whitelist(
        documents=[
            WhitelistDocument(
                doc_token="doxc_alpha",
                name="Alpha playbook",
            )
        ]
    )
    plugin.context = SimpleNamespace(case_engine=None)
    replies: list[dict] = []
    harness_results: list[dict] = []

    async def fake_reply_card_or_text(_event, *, card: dict, fallback_text: str):
        replies.append({"card": card, "fallback_text": fallback_text})

    async def fake_record_harness_resource_result(_event, **kwargs) -> None:
        harness_results.append(kwargs)

    plugin._reply_card_or_text = fake_reply_card_or_text
    plugin._record_harness_resource_result = fake_record_harness_resource_result

    await plugin.on_message(FakeEvent())

    assert calls["query_count"] == 1
    assert calls["client"] is plugin.client
    assert replies
    combined_text = replies[0]["fallback_text"] + "\n" + str(replies[0]["card"])
    assert "有限" in combined_text or "bounded" in combined_text.lower()
    assert "元信息" in combined_text
    assert harness_results
    assert "有限" in harness_results[0]["summary"]
    assert (
        harness_results[0]["retrieval_scope"]
        == "bounded_content_with_metadata_fallback"
    )
