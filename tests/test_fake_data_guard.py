from pathlib import Path

from scripts.check_fake_data_guard import scan_paths, scan_text, should_scan_path


def test_flags_fake_completion_near_runtime_anchor(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/llm_router/main.py"
    source = """
async def complete_employee_request(engine, task):
    result = {"reply": "demo placeholder completion"}
    await engine.complete_task(task.task_id, result=result)
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (3, "fake-data-completion-anchor")
    ]


def test_flags_fabricated_completion_in_any_production_plugin(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/gpt_image_plugin/main.py"
    source = """
async def finalize_image_reply(event):
    reply = "fabricated data returned while image generation is unavailable"
    await event.send_card(reply)
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert should_scan_path(runtime_file, tmp_path)
    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (3, "fake-data-completion-anchor")
    ]


def test_flags_fake_seed_path_without_nearby_completion_anchor(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/feishu_pet_assistant/service.py"
    source = """
DEMO_TASKS = ["Follow up customer quote"]

def get_or_create_pet(user_id):
    self._seed_demo_tasks(user_id)
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (2, "fake-data-seed-path"),
        (5, "fake-data-seed-path"),
    ]


def test_flags_fake_completion_near_card_wrapper_anchor(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/employee_onboarding/main.py"
    source = """
async def show_onboarding_card(self, event):
    card = build_onboarding_card("placeholder employee plan")
    await self._send_card(event, card)
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (3, "fake-data-completion-anchor")
    ]


def test_flags_pass_only_inside_employee_facing_runtime_handler(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/hermes_bridge/hermes_bridge.py"
    source = """
async def on_message(event):
    pass

def cleanup_temp_file(path):
    pass
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (3, "pass-runtime-handler")
    ]


def test_ignores_pass_inside_handler_exception_cleanup(tmp_path: Path) -> None:
    runtime_file = tmp_path / "data/plugins/feishu_resource_plugin/main.py"
    source = """
async def on_message(event):
    try:
        await event.load_context()
    except Exception:
        pass
    return await event.send("real unavailable message")
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert findings == []


def test_ignores_test_docs_and_dashboard_placeholder_noise(tmp_path: Path) -> None:
    paths = [
        tmp_path / "data/plugins/llm_router/tests/test_fixture.py",
        tmp_path / "data/plugins/llm_router/test/test_fixture.py",
        tmp_path / "data/plugins/employee_onboarding/docs/example_runtime.py",
        tmp_path / "data/plugins/system_entries/pages/dashboard/placeholder.py",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "async def on_message(event):\n"
            '    await event.send("demo placeholder reply")\n',
            encoding="utf-8",
        )

    findings = scan_paths(paths, tmp_path)

    assert all(not should_scan_path(path, tmp_path) for path in paths)
    assert findings == []


def test_ignores_local_backup_runtime_files(tmp_path: Path) -> None:
    backup_file = tmp_path / "data/plugins/hermes_bridge/_backup_main.py"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_text(
        "async def complete_task(event):\n"
        '    await event.send("demo placeholder reply")\n',
        encoding="utf-8",
    )

    findings = scan_paths([backup_file], tmp_path)

    assert not should_scan_path(backup_file, tmp_path)
    assert findings == []


def test_scans_production_plugin_main_and_finds_fake_completion(
    tmp_path: Path,
) -> None:
    production_file = tmp_path / "data/plugins/employee_onboarding/main.py"
    production_file.parent.mkdir(parents=True, exist_ok=True)
    production_file.write_text(
        "async def complete_onboarding(event):\n"
        '    reply = "fake sample onboarding completion"\n'
        "    await event.send_card(reply)\n",
        encoding="utf-8",
    )

    findings = scan_paths([production_file], tmp_path)

    assert should_scan_path(production_file, tmp_path)
    assert [(finding.line, finding.rule_id) for finding in findings] == [
        (2, "fake-data-completion-anchor")
    ]


def test_ignores_sql_placeholders_in_runtime_store(tmp_path: Path) -> None:
    runtime_file = tmp_path / "dc_engines/dc_engines/case/case_store.py"
    source = """
def build_query(statuses):
    placeholders = ", ".join("?" for _ in statuses)
    return f"status IN ({placeholders})"
"""

    findings = scan_text(runtime_file, source, tmp_path)

    assert findings == []


def test_should_scan_only_runtime_python_paths(tmp_path: Path) -> None:
    assert should_scan_path(
        tmp_path / "data/plugins/case_plugin/main.py",
        tmp_path,
    )
    assert not should_scan_path(
        tmp_path / "data/plugins/case_plugin/README.md",
        tmp_path,
    )
    assert not should_scan_path(
        tmp_path / "data/plugins/case_plugin/tests/test_main.py",
        tmp_path,
    )
