from __future__ import annotations

from dc_engines.hermes_bridge_engine.callback_payload import (
    build_harness_result,
    callback_failure_reason,
    callback_provenance_fields,
    callback_response_text,
)


def test_callback_payload_extracts_nested_response_and_provenance() -> None:
    data = {
        "result": {
            "message": "final answer",
            "source_citations": [{"source_path": "projects/customer.md"}],
        },
        "hits": [{"id": "doc-1"}],
    }

    assert callback_response_text(data) == "final answer"
    assert callback_provenance_fields(data) == {
        "source_citations": [{"source_path": "projects/customer.md"}],
        "hits": [{"id": "doc-1"}],
    }
    assert build_harness_result("final answer", callback_provenance_fields(data)) == {
        "summary": "final answer",
        "response_preview": "final answer",
        "source": "hermes",
        "source_citations": [{"source_path": "projects/customer.md"}],
        "hits": [{"id": "doc-1"}],
    }


def test_callback_payload_classifies_failure_status_and_final_error_text() -> None:
    assert (
        callback_failure_reason(
            {"status": "failed", "error": {"message": "quota exhausted"}},
            "",
        )
        == "quota exhausted"
    )
    assert (
        callback_failure_reason({}, "API failed after 3 retries: timeout")
        == "API failed after 3 retries: timeout"
    )
