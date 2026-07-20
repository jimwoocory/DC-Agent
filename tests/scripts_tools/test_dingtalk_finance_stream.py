import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts-tools"
    / "dingtalk_finance_bot"
    / "finance_bot.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("dingtalk_finance_bot", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_for(tmp_path: Path, module):
    return module.ExportConfig(
        app_key="app-key",
        app_secret="app-secret",
        process_code="PROC-CODE",
        start_time=datetime.fromisoformat("2026-07-01T00:00:00+08:00"),
        end_time=datetime.fromisoformat("2026-07-31T23:59:59+08:00"),
        output_dir=tmp_path,
        finance_reviewer_user_id="qin",
    )


def state_for_stream():
    return {
        "last_event_at": "",
        "last_topic": "",
        "last_event_type": "",
        "last_error": "",
        "skipped_count": 0,
        "last_ok": None,
        "last_reason": "",
        "last_instance_id": "",
        "last_mode": "",
        "error_count": 0,
        "processed_count": 0,
        "last_report_path": "",
    }


def stream_event(process_instance_id: str = "PROC-123"):
    return SimpleNamespace(
        headers=SimpleNamespace(
            topic="/v1.0/im/workflow/process",
            event_type="bpms_task_change",
            event_id="evt-1",
            message_id="msg-1",
        ),
        data={"processInstanceId": process_instance_id},
        extensions={},
    )


def test_stream_event_handler_runs_initial_review_for_process_instance(
    monkeypatch, tmp_path
):
    module = load_module()
    calls = []

    def fake_build_response(config, payload, ledger_path):
        calls.append((config, payload, ledger_path))
        return {
            "mode": "initial_review",
            "report_path": str(tmp_path / "review_PROC-123.json"),
        }

    monkeypatch.setattr(
        module,
        "build_oa_callback_review_response",
        fake_build_response,
    )
    state = state_for_stream()
    handler = module.DingTalkFinanceStreamEventHandler(
        config_for(tmp_path, module),
        tmp_path / "ledger.json",
        state,
        module.threading.Lock(),
    )

    code, message = asyncio.run(handler.process(stream_event()))

    assert (code, message) == (200, "OK")
    assert len(calls) == 1
    assert calls[0][1]["data"]["processInstanceId"] == "PROC-123"
    assert state["processed_count"] == 1
    assert state["last_instance_id"] == "PROC-123"
    assert state["last_mode"] == "initial_review"


def test_stream_event_handler_skips_irrelevant_events(tmp_path):
    module = load_module()
    state = state_for_stream()
    handler = module.DingTalkFinanceStreamEventHandler(
        config_for(tmp_path, module),
        tmp_path / "ledger.json",
        state,
        module.threading.Lock(),
    )
    event = SimpleNamespace(
        headers=SimpleNamespace(
            topic="/v1.0/contact/user",
            event_type="user_update",
            event_id="evt-contact",
            message_id="msg-contact",
        ),
        data={"userId": "someone"},
        extensions={},
    )

    code, message = asyncio.run(handler.process(event))

    assert (code, message) == (200, "OK")
    assert state["skipped_count"] == 1
    assert state["last_reason"] == "no_process_instance_id"


def test_stream_event_handler_deduplicates_events(monkeypatch, tmp_path):
    module = load_module()
    calls = []

    def fake_build_response(config, payload, ledger_path):
        calls.append(payload)
        return {"mode": "initial_review", "report_path": "report.json"}

    monkeypatch.setattr(
        module,
        "build_oa_callback_review_response",
        fake_build_response,
    )
    state = state_for_stream()
    handler = module.DingTalkFinanceStreamEventHandler(
        config_for(tmp_path, module),
        tmp_path / "ledger.json",
        state,
        module.threading.Lock(),
    )

    assert asyncio.run(handler.process(stream_event())) == (200, "OK")
    assert asyncio.run(handler.process(stream_event())) == (200, "OK")

    assert len(calls) == 1
    assert state["processed_count"] == 1
    assert state["skipped_count"] == 1
    assert state["last_reason"] == "duplicate_event"


def test_callback_skips_other_approval_processes(monkeypatch, tmp_path):
    module = load_module()

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def get_access_token(self):
            return "token"

        def get_approval_detail(self, token, instance_id):
            return {"processCode": "PROC-OTHER"}

    monkeypatch.setattr(module, "DingTalkFinanceClient", FakeClient)
    result = module.build_oa_callback_review_response(
        config_for(tmp_path, module),
        {"processInstanceId": "PROC-123"},
        tmp_path / "ledger.json",
    )

    assert result["mode"] == "initial_review_skipped"
    assert result["reason"] == "process_code_mismatch"


def test_capability_probe_is_read_only(tmp_path):
    module = load_module()
    requested_paths = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "temporary-token"})
        if request.url.path == "/topapi/processinstance/listids":
            return httpx.Response(
                200,
                json={"errcode": 0, "result": {"list": ["PROC-123"]}},
            )
        if request.url.path == "/topapi/processinstance/get":
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "process_instance": {
                        "processInstanceId": "PROC-123",
                        "operation_records": [
                            {
                                "attachments": [
                                    {"file_id": "file-1", "file_name": "invoice.pdf"}
                                ]
                            }
                        ],
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    config = config_for(tmp_path, module)
    config.operator_user_id = "operator-user"
    config.operator_union_id = "operator-union"
    config.local_ocr_url = "http://127.0.0.1:6194/ocr"
    client = module.DingTalkFinanceClient(
        config,
        http_client=httpx.Client(transport=httpx.MockTransport(handle_request)),
    )
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"records": []}), encoding="utf-8")

    report = module.run_capability_probe(
        config,
        client=client,
        ledger_path=ledger_path,
    )

    assert report["ok"] is True
    assert report["mode"] == "read_only"
    assert report["mutating_actions_tested"] is False
    assert report["ready_for_initial_review"] is True
    assert report["checks"]["workflow_instance_read"]["instance_count"] == 1
    assert report["checks"]["approval_detail_read"]["attachment_candidate_count"] == 1
    assert report["checks"]["duplicate_ledger"]["status"] == "ready"
    assert report["checks"]["approval_mutation"]["status"] == "not_tested"
    assert requested_paths == [
        "/v1.0/oauth2/accessToken",
        "/topapi/processinstance/listids",
        "/topapi/processinstance/get",
    ]


def test_capability_probe_redacts_credentials_and_reports_failure(tmp_path):
    module = load_module()
    config = config_for(tmp_path, module)

    class FailingClient:
        def get_access_token(self):
            raise RuntimeError(
                f"invalid app {config.app_key} secret {config.app_secret}"
            )

    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"records": []}), encoding="utf-8")

    report = module.run_capability_probe(
        config,
        client=FailingClient(),
        ledger_path=ledger_path,
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["ok"] is False
    assert report["ready_for_initial_review"] is False
    assert report["checks"]["app_access_token"]["status"] == "failed"
    assert report["checks"]["workflow_instance_read"]["status"] == "skipped"
    assert (
        report["checks"]["attachment_download_identity"]["status"] == "missing_config"
    )
    assert report["checks"]["local_ocr"]["status"] == "missing_config"
    assert config.app_key not in serialized
    assert config.app_secret not in serialized
