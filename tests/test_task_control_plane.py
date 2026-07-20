from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from harness.evaluator.kb_import_contract import load_contract, validate_contract

CONTRACT = Path("harness/contracts/task_control_plane.json")
CONTROL_PLANE = Path("scripts-watchdog/task_control_plane.py")
WATCHDOGCTL = Path("scripts-watchdog/watchdogctl.py")
KNOWLEDGE_CYCLE = Path("scripts-watchdog/knowledge_cycle.py")
CLOUD_WORKFLOW = Path("scripts-watchdog/feishu_cloud_workflow.py")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _create_runtime_databases(root: Path) -> None:
    data = root / "data"
    data.mkdir(parents=True)
    with sqlite3.connect(data / "data_v4.db") as db:
        db.execute(
            """
            CREATE TABLE cron_jobs (
                job_id TEXT,
                name TEXT,
                job_type TEXT,
                cron_expression TEXT,
                enabled INTEGER,
                status TEXT,
                last_run_at TEXT,
                next_run_time TEXT,
                last_error TEXT,
                updated_at TEXT
            )
            """
        )
        db.execute(
            """
            INSERT INTO cron_jobs VALUES (
                'cron-1', 'Morning brief', 'active_agent', '0 9 * * *', 1,
                'completed', '2026-07-11T01:00:00Z', '2026-07-12T01:00:00Z',
                NULL, '2026-07-11T01:00:00Z'
            )
            """
        )
    with sqlite3.connect(data / "harness_tasks.db") as db:
        db.execute(
            """
            CREATE TABLE harness_tasks (
                task_id TEXT,
                title TEXT,
                domain TEXT,
                status TEXT,
                updated_at TEXT
            )
            """
        )
        db.execute(
            """
            INSERT INTO harness_tasks VALUES (
                'task-1', 'Review campaign', 'content', 'running',
                '2026-07-11T02:00:00Z'
            )
            """
        )


def test_task_control_plane_contract_is_valid() -> None:
    contract = load_contract(CONTRACT)

    assert validate_contract(contract) == []


def test_control_plane_normalizes_sources_and_codex_role(tmp_path) -> None:
    module = _load_module(CONTROL_PLANE, "task_control_plane")
    _create_runtime_databases(tmp_path)
    watchdog = tmp_path / "data" / "watchdog"
    watchdog.mkdir(parents=True)
    (watchdog / "knowledge_cycle_state.json").write_text(
        json.dumps(
            {
                "steps": {
                    "mount": {
                        "status": "ok",
                        "last_started_at": "2026-07-11T03:00:00Z",
                    },
                    "feishu_nas_workflow": {
                        "status": "ok",
                        "last_started_at": "2026-07-11T03:10:00Z",
                        "exit_code": 0,
                    },
                    "feishu_repair": {
                        "status": "disabled",
                        "reason": "legacy_feishu_repair_disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (watchdog / "feishu_cloud_workflow.pause").touch()
    legacy = {
        "group": "all",
        "launchd": [
            {
                "key": "astrbot",
                "description": "AstrBot runtime",
                "groups": ["watchdog"],
                "enabled_state": "enabled",
                "loaded_state": "loaded",
                "schedule": "RunAtLoad",
            },
            {
                "key": "feishu-sync",
                "description": "Legacy Feishu folder polling",
                "groups": ["nas", "sync"],
                "enabled_state": "disabled",
                "loaded_state": "not-loaded",
                "schedule": "interval=3600s",
                "replacement_task_ids": ["knowledge_cycle:feishu_nas_workflow"],
            },
        ],
        "cron": [
            {
                "key": "dc-watchdog",
                "description": "Health loop",
                "groups": ["watchdog"],
                "state": "installed",
                "group_pause_protected": True,
                "impact_level": "critical",
                "impact_summary": "Stops health and scheduling",
            },
            {
                "key": "astrbot-http-watchdog",
                "description": "Legacy restart loop",
                "groups": ["watchdog", "astrbot"],
                "state": "installed",
                "replacement_task_ids": [
                    "launchd:astrbot-runtime",
                    "watchdog_probe:astrbot_api",
                ],
            },
        ],
        "codex": [
            {
                "key": "nas",
                "description": "Legacy analysis heartbeat",
                "groups": ["nas"],
                "status": "PAUSED",
            }
        ],
        "probes": [{"key": "astrbot_api", "groups": ["watchdog"], "state": "enabled"}],
        "repair": {
            "services": [
                {
                    "service": "astrbot_api",
                    "state": "circuit_open",
                    "groups": ["watchdog", "repair", "astrbot"],
                    "attempt_count_window": 2,
                    "attempts_remaining": 0,
                    "consecutive_failures": 2,
                    "circuit_remaining_seconds": 1800,
                    "last_incident_id": "incident-9",
                    "last_attempt_at": "2026-07-12T05:00:00+00:00",
                }
            ],
            "reviews": [
                {
                    "incident_id": "incident-9",
                    "service": "astrbot_api",
                    "status": "pending",
                    "groups": ["watchdog", "repair", "astrbot"],
                    "summary": "Configuration review required.",
                    "reason": "No automatic action is authorized.",
                    "risk": "high",
                    "created_at_unix": 1_000,
                    "updated_at_unix": 1_000,
                }
            ],
        },
    }

    snapshot = module.collect_control_plane(legacy, dc_root=tmp_path, group="all")
    tasks = {task["task_id"]: task for task in snapshot["tasks"]}

    assert snapshot["schema_version"] == 1
    assert {
        "launchd:astrbot",
        "launchd:feishu-sync",
        "crontab:dc-watchdog",
        "crontab:astrbot-http-watchdog",
        "codex_automation:nas",
        "watchdog_probe:astrbot_api",
        "watchdog_repair:astrbot_api",
        "watchdog_repair_review:incident-9",
        "knowledge_cycle:mount",
        "knowledge_cycle:feishu_nas_workflow",
        "astrbot_cron:cron-1",
        "harness:task-1",
    }.issubset(tasks)
    assert tasks["codex_automation:nas"]["executor_role"] == "advanced_executor"
    assert tasks["codex_automation:nas"]["control_mode"] == "pause_only"
    assert tasks["codex_automation:nas"]["authority_state"] == "non_authoritative"
    assert tasks["codex_automation:nas"]["status"] == "paused"
    assert tasks["knowledge_cycle:feishu_nas_workflow"]["status"] == "paused"
    assert tasks["knowledge_cycle:feishu_nas_workflow"]["native_status"] == "ok"
    assert tasks["knowledge_cycle:feishu_repair"]["status"] == "retired"
    assert tasks["knowledge_cycle:feishu_repair"]["authority_state"] == "superseded"
    assert tasks["knowledge_cycle:feishu_repair"]["superseded_by"] == [
        "knowledge_cycle:feishu_nas_workflow"
    ]
    assert tasks["launchd:feishu-sync"]["status"] == "retired"
    assert tasks["launchd:feishu-sync"]["authority_state"] == "superseded"
    assert tasks["launchd:feishu-sync"]["superseded_by"] == [
        "knowledge_cycle:feishu_nas_workflow"
    ]
    assert tasks["astrbot_cron:cron-1"]["schedule"] == "0 9 * * *"
    assert tasks["harness:task-1"]["status"] == "running"
    assert tasks["harness:task-1"]["control_mode"] == "read_only"
    assert tasks["crontab:astrbot-http-watchdog"]["status"] == "migration_required"
    assert tasks["crontab:astrbot-http-watchdog"]["authority_state"] == "superseded"
    assert tasks["crontab:astrbot-http-watchdog"]["superseded_by"] == [
        "launchd:astrbot-runtime",
        "watchdog_probe:astrbot_api",
    ]
    assert tasks["crontab:dc-watchdog"]["group_pause_protected"] is True
    assert tasks["crontab:dc-watchdog"]["impact_level"] == "critical"
    assert tasks["crontab:dc-watchdog"]["impact_summary"] == (
        "Stops health and scheduling"
    )
    assert tasks["watchdog_repair:astrbot_api"]["status"] == "circuit_open"
    assert tasks["watchdog_repair:astrbot_api"]["executor_role"] == (
        "deterministic_controller"
    )
    assert tasks["watchdog_repair:astrbot_api"]["control_mode"] == "read_only"
    assert (
        "circuit_remaining_seconds=1800"
        in tasks["watchdog_repair:astrbot_api"]["detail"]
    )
    assert tasks["watchdog_repair_review:incident-9"]["status"] == "pending"
    assert tasks["watchdog_repair_review:incident-9"]["control_mode"] == ("review_plan")
    assert tasks["watchdog_repair_review:incident-9"]["executor_role"] == (
        "human_operator"
    )


def test_control_plane_is_read_only_and_tolerates_missing_sources(tmp_path) -> None:
    module = _load_module(CONTROL_PLANE, "task_control_plane_missing")
    legacy = {
        "group": "all",
        "launchd": [],
        "cron": [],
        "codex": [],
        "probes": [],
    }

    snapshot = module.collect_control_plane(legacy, dc_root=tmp_path, group="all")

    assert snapshot["tasks"] == []
    assert set(snapshot["source_errors"]) == {
        "astrbot_cron",
        "harness",
        "knowledge_cycle",
    }
    assert list(tmp_path.rglob("*")) == []


def test_watchdogctl_adds_control_plane_without_removing_legacy_sections(
    monkeypatch, tmp_path
) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_task_control_plane")
    monkeypatch.setattr(module, "selected_launchd", lambda _group: [])
    monkeypatch.setattr(module, "selected_cron", lambda _group: [])
    monkeypatch.setattr(module, "selected_codex", lambda _group: [])
    monkeypatch.setattr(module, "watchdog_probe_groups", lambda: {})
    monkeypatch.setattr(
        module,
        "_load_repair_engine",
        lambda: SimpleNamespace(
            collect_repair_status=lambda **_kwargs: {
                "schema_version": 1,
                "mode": "read_only",
                "state_status": "empty",
                "services": [],
                "recent_results": [],
                "source_errors": {},
            }
        ),
    )
    monkeypatch.setattr(
        module,
        "_load_task_control_plane",
        lambda: SimpleNamespace(
            collect_control_plane=lambda legacy, **_kwargs: {
                "schema_version": 1,
                "tasks": [],
                "source_errors": {},
                "legacy_group": legacy["group"],
            }
        ),
    )
    monkeypatch.setattr(module, "DC_ROOT", tmp_path)

    status = module.collect_status("all")

    assert set(status) == {
        "group",
        "launchd",
        "cron",
        "codex",
        "probes",
        "repair",
        "control_plane",
    }
    assert status["control_plane"]["legacy_group"] == "all"
    assert status["repair"]["mode"] == "read_only"


def test_runtime_inventory_is_visible_but_not_controllable() -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_read_only_inventory")

    assert module.find_launchd("astrbot-runtime").controllable is False
    assert module.find_launchd("hermes-gateway").controllable is False
    assert module.find_cron("astrbot-http-watchdog").controllable is False
    assert module.find_cron("dc-watchdog").group_pause_protected is True
    assert module.find_cron("dc-watchdog").impact_level == "critical"
    assert "Knowledge Cycle" in module.find_cron("dc-watchdog").impact_summary
    assert module.find_cron("astrbot-http-watchdog").replacement_task_ids == (
        "launchd:astrbot-runtime",
        "watchdog_probe:astrbot_api",
    )
    assert module.find_launchd("nas-watchdog").replacement_task_ids == (
        "knowledge_cycle:mount",
    )
    assert module.find_launchd("feishu-sync").replacement_task_ids == (
        "knowledge_cycle:feishu_nas_workflow",
    )
    assert module.find_cron("dianchi-tech-cron").replacement_task_ids == (
        "launchd:dianchi-tech-night",
        "launchd:dianchi-tech-report",
    )


def test_watchdogctl_review_commands_delegate_to_repair_engine(
    monkeypatch, tmp_path, capsys
) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_review_commands")
    calls: list[tuple[str, str | None]] = []
    engine = SimpleNamespace(
        build_review_control_plan=lambda **kwargs: {
            "plan_id": "1000.review-plan",
            "incident_id": kwargs["incident_id"],
            "operation": kwargs["operation"],
        },
        apply_review_control_plan=lambda **kwargs: (
            calls.append((kwargs["operation"], kwargs["confirm_plan"]))
            or {
                "incident_id": kwargs["incident_id"],
                "to_status": "acknowledged",
            }
        ),
    )
    monkeypatch.setattr(module, "_load_repair_engine", lambda: engine)
    monkeypatch.setattr(module, "DC_ROOT", tmp_path)

    assert module.main(["plan-review", "incident-1", "acknowledge", "--json"]) == 0
    plan_output = json.loads(capsys.readouterr().out)
    assert plan_output["plan_id"] == "1000.review-plan"

    assert (
        module.main(
            [
                "review",
                "incident-1",
                "acknowledge",
                "--confirm-plan",
                "1000.review-plan",
                "--json",
            ]
        )
        == 0
    )
    assert calls == [("acknowledge", "1000.review-plan")]


def test_retire_one_removes_only_superseded_cron(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_retire_superseded")
    original = "\n".join(
        [
            "* * * * * ~/.local/bin/astrbot_watchdog.sh",
            "# DC-Agent watchdog (managed by scripts-watchdog/install-cron.sh)",
            "* * * * * '/Users/dianchi/DC-Agent/scripts-watchdog/dc-watchdog.sh'",
            "30 9 * * * employee_usage_audit.py",
            "",
        ]
    )
    installed: list[str] = []
    monkeypatch.setattr(module, "crontab_text", lambda: original)
    monkeypatch.setattr(module, "install_crontab", installed.append)

    assert module.retire_one("cron", "astrbot-http-watchdog") is True

    assert installed == [
        "# DC-Agent watchdog (managed by scripts-watchdog/install-cron.sh)\n"
        "* * * * * '/Users/dianchi/DC-Agent/scripts-watchdog/dc-watchdog.sh'\n"
        "30 9 * * * employee_usage_audit.py\n"
    ]


def test_retire_one_rejects_authoritative_cron() -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_reject_authoritative_retire")

    try:
        module.retire_one("cron", "dc-watchdog")
    except KeyError as exc:
        assert "not superseded" in str(exc)
    else:
        raise AssertionError("authoritative cron must not be retired")


def test_resume_skips_superseded_and_codex_schedulers(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_resume_authority")
    resumed_launchd: list[str] = []
    installer_calls: list[list[str]] = []
    codex_updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "selected_launchd",
        lambda _group: [
            module.find_launchd("nas-watchdog"),
            module.find_launchd("feishu-sync"),
        ],
    )
    monkeypatch.setattr(
        module,
        "selected_cron",
        lambda _group: [module.find_cron("dianchi-tech-cron")],
    )
    monkeypatch.setattr(
        module,
        "selected_codex",
        lambda _group: [module.find_codex("nas")],
    )
    monkeypatch.setattr(
        module,
        "resume_launchd",
        lambda job: resumed_launchd.append(job.key),
    )
    monkeypatch.setattr(
        module,
        "run",
        lambda args, **_kwargs: installer_calls.append(args),
    )
    monkeypatch.setattr(
        module,
        "set_codex_status",
        lambda job, status: codex_updates.append((job.key, status)),
    )
    monkeypatch.setattr(module, "print_status", lambda _group: None)

    plan = module.build_control_plan("nas", "resume")
    module.apply_control_plan(
        "nas",
        "resume",
        confirm_plan=plan["plan_id"],
    )

    assert resumed_launchd == []
    assert installer_calls == []
    assert codex_updates == []


def test_resume_plan_contains_only_inactive_authoritative_tasks(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_resume_plan")
    monkeypatch.setattr(module, "launchd_disabled", lambda _label: True)
    monkeypatch.setattr(module, "launchd_loaded", lambda _label: False)
    monkeypatch.setattr(module, "cron_installed", lambda _job: False)

    plan = module.build_control_plan("nas", "resume")
    task_ids = {action["task_id"] for action in plan["actions"]}
    skipped_ids = {item["task_id"] for item in plan["skipped"]}

    assert task_ids == {
        "launchd:baidu-nas-sync",
        "launchd:dianchi-tech-night",
        "launchd:dianchi-tech-report",
        "crontab:dc-watchdog",
    }
    assert {
        "launchd:feishu-sync",
        "launchd:nas-watchdog",
        "crontab:dianchi-tech-cron",
        "codex_automation:nas",
        "codex_automation:nas-workflow",
    }.issubset(skipped_ids)
    assert plan["requires_confirmation"] is True
    assert plan["operation"] == "resume"
    issued_at, digest = plan["plan_id"].split(".", 1)
    assert int(issued_at) == plan["issued_at_unix"]
    assert len(digest) == 32
    assert plan["expires_at_unix"] - plan["issued_at_unix"] == 120


def test_group_resume_rejects_missing_or_stale_plan_without_mutation(
    monkeypatch,
) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_resume_confirmation")
    resumed: list[str] = []
    plan = {
        "schema_version": 1,
        "group": "nas",
        "plan_id": "1000.current-plan-id",
        "requires_confirmation": True,
        "actions": [
            {
                "task_id": "launchd:baidu-nas-sync",
                "kind": "launchd",
                "key": "baidu-nas-sync",
                "description": "02:00 百度网盘到 NAS 同步",
            }
        ],
        "skipped": [],
    }
    monkeypatch.setattr(
        module,
        "build_control_plan",
        lambda _group, _operation, issued_at=None: plan,
    )
    monkeypatch.setattr(module.time, "time", lambda: 1050)
    monkeypatch.setattr(
        module,
        "resume_launchd",
        lambda job: resumed.append(job.key),
    )
    monkeypatch.setattr(module, "print_status", lambda _group: None)

    for confirmation in (None, "800.expired-plan-id", "1000.stale-plan-id"):
        try:
            module.apply_control_plan(
                "nas",
                "resume",
                confirm_plan=confirmation,
            )
        except ValueError as exc:
            assert "Control Plan" in str(exc)
        else:
            raise AssertionError("group resume must reject invalid confirmation")

    assert resumed == []

    module.apply_control_plan(
        "nas",
        "resume",
        confirm_plan="1000.current-plan-id",
    )
    assert resumed == ["baidu-nas-sync"]


def test_resume_plan_changes_when_execution_identity_changes(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_resume_identity")
    current = [module.find_launchd("baidu-nas-sync")]
    monkeypatch.setattr(module, "selected_launchd", lambda _group: current)
    monkeypatch.setattr(module, "selected_cron", lambda _group: [])
    monkeypatch.setattr(module, "selected_codex", lambda _group: [])
    monkeypatch.setattr(module, "launchd_disabled", lambda _label: True)
    monkeypatch.setattr(module, "launchd_loaded", lambda _label: False)

    first = module.build_control_plan("nas", "resume", issued_at=1000)
    current[0] = module.dataclasses.replace(
        current[0],
        label="com.dcagent.baidu-nas-sync.changed",
    )
    second = module.build_control_plan("nas", "resume", issued_at=1000)

    assert first["plan_id"] != second["plan_id"]


def test_cli_group_operations_require_confirm_plan() -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_resume_cli_guard")

    for operation in ("pause", "resume"):
        try:
            module.main([operation, "nas"])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(
                f"CLI group {operation} must fail without plan confirmation"
            )


def test_pause_plan_protects_watchdog_and_lists_only_active_tasks(
    monkeypatch,
) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_pause_plan")
    monkeypatch.setattr(module, "launchd_disabled", lambda _label: False)
    monkeypatch.setattr(module, "launchd_loaded", lambda _label: True)
    monkeypatch.setattr(module, "cron_installed", lambda _job: True)
    monkeypatch.setattr(module, "codex_status", lambda _job: "ACTIVE")

    plan = module.build_control_plan("nas", "pause", issued_at=1000)
    task_ids = {action["task_id"] for action in plan["actions"]}
    skipped = {item["task_id"]: item["reason"] for item in plan["skipped"]}

    assert task_ids == {
        "launchd:baidu-nas-sync",
        "launchd:dianchi-tech-night",
        "launchd:dianchi-tech-report",
        "codex_automation:nas",
        "codex_automation:nas-workflow",
    }
    assert skipped["crontab:dc-watchdog"] == "protected_controller"
    assert skipped["launchd:feishu-sync"] == "superseded"
    assert skipped["launchd:nas-watchdog"] == "superseded"
    assert plan["operation"] == "pause"


def test_critical_item_pause_plan_is_exact_and_not_group_protected(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_critical_item_plan")
    monkeypatch.setattr(module, "cron_installed", lambda _job: True)

    plan = module.build_control_plan(
        "all",
        "pause",
        issued_at=1000,
        target_kind="cron",
        target_key="dc-watchdog",
    )

    assert plan["scope"] == "item"
    assert plan["target_kind"] == "cron"
    assert plan["target_key"] == "dc-watchdog"
    assert [item["task_id"] for item in plan["actions"]] == ["crontab:dc-watchdog"]
    assert plan["actions"][0]["impact_level"] == "critical"
    assert "Knowledge Cycle" in plan["actions"][0]["impact_summary"]


def test_critical_pause_one_requires_exact_item_plan(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_critical_item_apply")
    removed: list[str] = []
    installed = {"value": True}
    monkeypatch.setattr(module, "cron_installed", lambda _job: installed["value"])
    monkeypatch.setattr(module, "remove_cron_job", lambda job: removed.append(job.key))
    monkeypatch.setattr(module.time, "time", lambda: 1050)
    item_plan = module.build_control_plan(
        "all",
        "pause",
        issued_at=1000,
        target_kind="cron",
        target_key="dc-watchdog",
    )

    for confirmation in (None, "1000.stale-plan"):
        try:
            module.pause_one("cron", "dc-watchdog", confirm_plan=confirmation)
        except ValueError as exc:
            assert "Control Plan" in str(exc)
        else:
            raise AssertionError("critical pause must reject an invalid item plan")

    monkeypatch.setattr(module, "selected_launchd", lambda _group: [])
    monkeypatch.setattr(
        module,
        "selected_cron",
        lambda _group: [module.find_cron("dc-watchdog")],
    )
    monkeypatch.setattr(module, "selected_codex", lambda _group: [])
    group_plan = module.build_control_plan("nas", "pause", issued_at=1000)
    try:
        module.pause_one(
            "cron",
            "dc-watchdog",
            confirm_plan=group_plan["plan_id"],
        )
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("group plan must not authorize an item pause")

    wrong_target_plan = module.build_control_plan(
        "all",
        "pause",
        issued_at=1000,
        target_kind="cron",
        target_key="onboarding-watch",
    )
    wrong_operation_plan = module.build_control_plan(
        "all",
        "resume",
        issued_at=1000,
        target_kind="cron",
        target_key="dc-watchdog",
    )
    for confirmation in (
        wrong_target_plan["plan_id"],
        wrong_operation_plan["plan_id"],
    ):
        try:
            module.pause_one(
                "cron",
                "dc-watchdog",
                confirm_plan=confirmation,
            )
        except ValueError as exc:
            assert "stale" in str(exc)
        else:
            raise AssertionError("wrong target or operation must not authorize pause")

    installed["value"] = False
    try:
        module.pause_one(
            "cron",
            "dc-watchdog",
            confirm_plan=item_plan["plan_id"],
        )
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("runtime-state changes must invalidate the plan")

    assert removed == []
    installed["value"] = True
    module.pause_one(
        "cron",
        "dc-watchdog",
        confirm_plan=item_plan["plan_id"],
    )
    assert removed == ["dc-watchdog"]


def test_noncritical_pause_one_remains_direct(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_standard_item_pause")
    removed: list[str] = []
    monkeypatch.setattr(module, "remove_cron_job", lambda job: removed.append(job.key))

    module.pause_one("cron", "onboarding-watch")

    assert removed == ["onboarding-watch"]


def test_cli_critical_pause_one_requires_confirm_plan(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_critical_item_cli")
    removed: list[str] = []
    monkeypatch.setattr(module, "remove_cron_job", lambda job: removed.append(job.key))

    try:
        module.main(["pause-one", "cron", "dc-watchdog"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("critical item pause must require a plan")

    assert removed == []


def test_confirmed_pause_executes_only_planned_actions(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_pause_confirmation")
    paused: list[str] = []
    plan = {
        "schema_version": 1,
        "group": "nas",
        "operation": "pause",
        "plan_id": "1000.pause-plan-id",
        "requires_confirmation": True,
        "actions": [
            {
                "task_id": "codex_automation:nas",
                "kind": "codex",
                "key": "nas",
                "description": "Legacy Codex heartbeat",
            }
        ],
        "skipped": [],
    }
    monkeypatch.setattr(
        module,
        "build_control_plan",
        lambda _group, _operation, issued_at=None: plan,
    )
    monkeypatch.setattr(module.time, "time", lambda: 1050)
    monkeypatch.setattr(
        module,
        "set_codex_status",
        lambda job, status: paused.append(f"{job.key}:{status}"),
    )
    monkeypatch.setattr(module, "print_status", lambda _group: None)

    module.apply_control_plan(
        "nas",
        "pause",
        confirm_plan="1000.pause-plan-id",
    )

    assert paused == ["nas:PAUSED"]


def test_resume_one_rejects_codex_scheduler(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_reject_codex_resume")
    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        module,
        "set_codex_status",
        lambda job, status: updates.append((job.key, status)),
    )

    try:
        module.resume_one("codex", "nas")
    except KeyError as exc:
        assert "cannot own scheduling" in str(exc)
    else:
        raise AssertionError("Codex automation must not be resumed")

    assert updates == []


def test_launchd_loaded_uses_service_specific_print(monkeypatch) -> None:
    module = _load_module(WATCHDOGCTL, "watchdogctl_launchd_print")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "state = running", "")

    monkeypatch.setattr(module, "run", fake_run)

    assert module.launchd_loaded("io.astrbot.bot") is True
    assert calls == [["launchctl", "print", f"gui/{module.UID}/io.astrbot.bot"]]


def test_paused_feishu_workflow_is_not_spawned(monkeypatch, tmp_path) -> None:
    module = _load_module(KNOWLEDGE_CYCLE, "knowledge_cycle_paused")
    pause_path = tmp_path / "feishu_cloud_workflow.pause"
    pause_path.touch()
    module.FEISHU_CLOUD_PAUSE_PATH = pause_path
    for step, config in module.STEP_CONFIG.items():
        if "enabled" in config:
            config["enabled"] = step == "feishu_nas_workflow"

    started: list[str] = []
    updates: list[tuple[str, dict]] = []
    state: dict = {}
    monkeypatch.setattr(module, "ensure_dirs", lambda: None)
    monkeypatch.setattr(module, "append_log", lambda _message: None)
    monkeypatch.setattr(module, "feishu_cloud_workflow_running", lambda: False)
    monkeypatch.setattr(
        module,
        "is_due",
        lambda step: step == "feishu_nas_workflow",
    )
    monkeypatch.setattr(
        module,
        "start_step",
        lambda step: started.append(step) or True,
    )
    monkeypatch.setattr(
        module,
        "update_step",
        lambda step, **fields: updates.append((step, fields)),
    )
    monkeypatch.setattr(module, "locked_state", lambda: nullcontext(state))

    assert module.tick() == 0

    assert "feishu_nas_workflow" not in started
    assert any(
        step == "feishu_nas_workflow" and fields["status"] == "paused"
        for step, fields in updates
    )


def test_legacy_feishu_repair_is_never_automatically_scheduled(
    monkeypatch,
    tmp_path,
) -> None:
    module = _load_module(KNOWLEDGE_CYCLE, "knowledge_cycle_retired_repair")
    module.FEISHU_CLOUD_PAUSE_PATH = tmp_path / "missing.pause"
    for step, config in module.STEP_CONFIG.items():
        if "enabled" in config:
            config["enabled"] = step == "feishu_repair"

    started: list[str] = []
    updates: list[tuple[str, dict]] = []
    state: dict = {}
    monkeypatch.setattr(module, "ensure_dirs", lambda: None)
    monkeypatch.setattr(module, "append_log", lambda _message: None)
    monkeypatch.setattr(module, "feishu_cloud_workflow_running", lambda: False)
    monkeypatch.setattr(module, "is_due", lambda step: step == "feishu_repair")
    monkeypatch.setattr(module, "start_step", lambda step: started.append(step) or True)
    monkeypatch.setattr(
        module,
        "update_step",
        lambda step, **fields: updates.append((step, fields)),
    )
    monkeypatch.setattr(module, "locked_state", lambda: nullcontext(state))

    assert module.tick() == 0

    assert "feishu_repair" not in started
    assert any(
        step == "feishu_repair"
        and fields["status"] == "retired"
        and fields["reason"] == "superseded_by_feishu_nas_workflow"
        for step, fields in updates
    )


def test_paused_cloud_worker_returns_before_lock_or_sync(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    module = _load_module(CLOUD_WORKFLOW, "feishu_cloud_workflow_paused")
    pause_path = tmp_path / "feishu_cloud_workflow.pause"
    pause_path.touch()
    module.RUN_PAUSE_PATH = pause_path
    monkeypatch.setattr(
        module,
        "acquire_run_lock",
        lambda: (_ for _ in ()).throw(AssertionError("lock must not be acquired")),
    )

    result = module.run_next(
        SimpleNamespace(attachment_limit=20, dry_run=False, learn=True)
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "workflow_paused"


def test_authoritative_feishu_workflow_uses_targeted_sync_adapter() -> None:
    knowledge = _load_module(KNOWLEDGE_CYCLE, "knowledge_cycle_targeted_feishu")
    workflow = _load_module(CLOUD_WORKFLOW, "feishu_cloud_workflow_targeted")

    command = knowledge.command_for_step("feishu_nas_workflow")
    assert command is not None
    assert "--run-next" in command
    assert "--learn" in command

    sync_command = workflow.build_sync_cmd(
        {"url": "https://example.invalid/docx/test"},
        Path("/python"),
        20,
    )
    assert sync_command is not None
    assert sync_command[1].endswith("nas_sync/feishu_sync.py")
    assert sync_command[-2:] == ["--url", "https://example.invalid/docx/test"]
