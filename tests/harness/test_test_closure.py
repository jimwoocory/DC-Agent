from scripts.check_test_closure import classify_path


def status_for(path: str) -> str:
    item = classify_path(path)
    assert item is not None
    return item.status


def test_main_pytest_scripts_are_covered() -> None:
    assert status_for("tests/test_main.py") == "covered"
    assert (
        status_for("tests/harness/truth_intake_runtime_contract_test.py") == "covered"
    )


def test_dc_engine_and_router_tests_are_covered() -> None:
    assert status_for("dc_engines/tests/test_feishu_sync.py") == "covered"
    assert status_for("data/plugins/llm_router/test_dc_router_path.py") == "covered"


def test_dashboard_and_docs_tests_are_covered() -> None:
    assert status_for("dashboard/tests/hashRouteTabs.test.mjs") == "covered"
    assert status_for("docs/tests/test_sync_docs_to_wiki.py") == "covered"


def test_external_and_non_script_candidates_do_not_pass_as_main_closure() -> None:
    assert (
        status_for("openclaw-control-center/test/chat-api.test.ts")
        == "referenced_external"
    )
    assert status_for("data/skills/stock-analysis/test.py") == "external"
    assert status_for("data/attachments/openapi_test.txt") == "non_script"


def test_unknown_main_test_script_fails_closure() -> None:
    assert status_for("scripts/test_probe.py") == "unclosed"
