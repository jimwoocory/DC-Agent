from __future__ import annotations

import sys
from pathlib import Path

import pytest
from quart import Quart

DC_ENGINES_PATH = Path(__file__).resolve().parents[1] / "dc_engines"
if str(DC_ENGINES_PATH) not in sys.path:
    sys.path.insert(0, str(DC_ENGINES_PATH))

from dc_engines.employee_insight_loop import (  # noqa: E402
    CandidateType,
    EmployeeInsightCandidate,
    EmployeeInsightProfile,
    EmployeeInsightSession,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
    InsightEvent,
    PilotStatus,
)

from astrbot.dashboard.routes.employee_insight import EmployeeInsightRoute  # noqa: E402
from astrbot.dashboard.routes.route import RouteContext  # noqa: E402


@pytest.mark.asyncio
async def test_employee_insight_dashboard_route_reads_runtime_store(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_session(
        EmployeeInsightSession(
            session_id="sess_001",
            employee_id="ou_001",
            channel="lark_dm",
            trigger_type="daily_outreach",
            department_id="planning",
            scenario_id="write_notice",
            status=EmployeeInsightSessionStatus.COMPLETED,
        )
    )
    await store.upsert_candidate(
        EmployeeInsightCandidate(
            candidate_id="cand_001",
            candidate_type=CandidateType.TEMPLATE,
            source_session_ids=["sess_001"],
            department_id="planning",
            scenario_id="write_notice",
            title="通知模板优化",
            summary="员工需要内部通知格式。",
            evidence=[{"session_id": "sess_001"}],
            confidence=0.82,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.get("/api/employee-insight/dashboard")
        payload = await response.get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["metrics"]["total_sessions"] == 1
    assert payload["data"]["metrics"]["completed_sessions"] == 1
    assert payload["data"]["metrics"]["pending_candidates"] == 1


@pytest.mark.asyncio
async def test_employee_insight_candidates_route_filters_review_status(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_candidate(
        EmployeeInsightCandidate(
            candidate_id="cand_001",
            candidate_type=CandidateType.ONBOARDING,
            source_session_ids=["sess_001"],
            department_id="client",
            scenario_id="unknown_how_to_start",
            title="新手引导入口",
            summary="员工不知道怎么开始。",
            evidence=[{"session_id": "sess_001"}],
            confidence=0.86,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.get(
            "/api/employee-insight/candidates?review_status=review_required"
        )
        payload = await response.get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["items"][0]["candidate_id"] == "cand_001"
    assert payload["data"]["items"][0]["review_status"] == "review_required"


@pytest.mark.asyncio
async def test_employee_insight_sessions_and_audit_routes_are_safe_when_empty(
    tmp_path: Path,
) -> None:
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        sessions = await (await client.get("/api/employee-insight/sessions")).get_json()
        audit = await (await client.get("/api/employee-insight/audit")).get_json()
        doctor = await (await client.get("/api/employee-insight/doctor")).get_json()

    assert sessions["status"] == "ok"
    assert sessions["data"]["items"] == []
    assert audit["status"] == "ok"
    assert audit["data"]["items"] == []
    assert doctor["status"] == "ok"
    assert doctor["data"]["store"]["path"].endswith("data/employee_insight.db")


@pytest.mark.asyncio
async def test_employee_insight_audit_route_returns_target_events(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.record_audit(
        action="governance_exported",
        actor="system",
        target_id="cand_001",
        detail={"path": "Inbox/cand_001.md"},
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.get("/api/employee-insight/audit?target_id=cand_001")
        payload = await response.get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["items"][0]["action"] == "governance_exported"


def test_employee_insight_frontend_entry_is_registered() -> None:
    route_source = Path("dashboard/src/router/MainRoutes.ts").read_text(
        encoding="utf-8"
    )
    sidebar_source = Path(
        "dashboard/src/layouts/full/vertical-sidebar/sidebarItem.ts"
    ).read_text(encoding="utf-8")
    zh_navigation = Path(
        "dashboard/src/i18n/locales/zh-CN/core/navigation.json"
    ).read_text(encoding="utf-8")
    static_source = Path("astrbot/dashboard/routes/static_file.py").read_text(
        encoding="utf-8"
    )

    assert "EmployeeInsightPage.vue" in route_source
    assert "path: '/employee-insight'" in route_source
    assert "core.navigation.employeeInsight" in sidebar_source
    assert sidebar_source.index("core.navigation.employeeInsight") < sidebar_source.index(
        "core.navigation.groups.more"
    )
    assert "chip: '灰度'" in sidebar_source
    assert '"employeeInsight": "员工需求洞察"' in zh_navigation
    assert '"/employee-insight"' in static_source
    page_source = Path("dashboard/src/views/EmployeeInsightPage.vue").read_text(
        encoding="utf-8"
    )
    assert "/api/employee-insight/verification/status" in page_source
    assert "/api/employee-insight/verification/report" in page_source
    assert "/api/employee-insight/verification/run" in page_source
    assert "一键灰度验证" in page_source
    assert "发送率" in page_source


@pytest.mark.asyncio
async def test_employee_insight_profiles_and_outreach_plan_routes(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_ready",
            employee_hash="hash_ready",
            display_name="张三",
            department_id="planning",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        profiles = await (await client.get("/api/employee-insight/profiles")).get_json()
        plan = await (
            await client.get(
                "/api/employee-insight/outreach-plan?now=2026-06-14T10:00:00Z"
            )
        ).get_json()

    assert profiles["status"] == "ok"
    assert profiles["data"]["items"][0]["employee_id"] == "ou_ready"
    assert plan["status"] == "ok"
    assert plan["data"]["eligible"][0]["employee_id"] == "ou_ready"


@pytest.mark.asyncio
async def test_employee_insight_upsert_profile_and_record_outreach_routes(
    tmp_path: Path,
) -> None:
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        created = await (
            await client.post(
                "/api/employee-insight/profiles",
                json={
                    "employee_id": "ou_ready",
                    "employee_hash": "hash_ready",
                    "display_name": "张三",
                    "department_id": "planning",
                    "pilot_status": "active",
                },
            )
        ).get_json()
        recorded = await (
            await client.post(
                "/api/employee-insight/outreach-record",
                json={
                    "employee_id": "ou_ready",
                    "message_text": "今天想试一个真实任务吗？",
                    "now": "2026-06-14T10:00:00Z",
                },
            )
        ).get_json()

    assert created["status"] == "ok"
    assert recorded["status"] == "ok"
    assert recorded["data"]["status"] == "sent"
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    profile = await store.get_profile("ou_ready")
    assert profile.unanswered_outreach_count == 1


@pytest.mark.asyncio
async def test_employee_insight_outreach_dispatch_dry_run_route(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_ready",
            employee_hash="hash_ready",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/employee-insight/outreach-dispatch",
                json={"dry_run": True, "now": "2026-06-14T10:00:00Z"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["mode"] == "dry_run"
    assert payload["data"]["planned"][0]["employee_id"] == "ou_ready"
    assert payload["data"]["sent"] == []


@pytest.mark.asyncio
async def test_employee_insight_outreach_dispatch_real_send_requires_configured_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_ready",
            employee_hash="hash_ready",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(
        RouteContext(
            config={
                "employee_insight": {
                    "real_sender_enabled": True,
                    "real_send_approval_token": "approve-token",
                }
            },
            app=app,
        ),
        dc_root=tmp_path,
    )  # type: ignore[arg-type]

    class _FakeRouteSender:
        async def send_text(self, employee_id: str, text: str):
            from dc_engines.employee_insight_loop import TextSendResult

            return TextSendResult(
                success=True,
                provider_message_id=f"msg_{employee_id}",
            )

    monkeypatch.setattr(
        "astrbot.dashboard.routes.employee_insight.FeishuPrivateMessageSender",
        lambda: _FakeRouteSender(),
    )

    async with app.test_client() as client:
        rejected = await (
            await client.post(
                "/api/employee-insight/outreach-dispatch",
                json={
                    "dry_run": False,
                    "approved": True,
                    "approval_token": "wrong-token",
                    "now": "2026-06-14T10:00:00Z",
                },
            )
        ).get_json()
        sent = await (
            await client.post(
                "/api/employee-insight/outreach-dispatch",
                json={
                    "dry_run": False,
                    "approved": True,
                    "approval_token": "approve-token",
                    "now": "2026-06-14T10:00:00Z",
                },
            )
        ).get_json()

    assert rejected["status"] == "error"
    assert "approval_token" in rejected["message"]
    assert sent["status"] == "ok"
    assert sent["data"]["mode"] == "send"
    assert sent["data"]["sent"][0]["provider_message_id"] == "msg_ou_ready"


@pytest.mark.asyncio
async def test_employee_insight_outreach_dispatch_real_send_requires_sender(
    tmp_path: Path,
) -> None:
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/employee-insight/outreach-dispatch",
                json={"dry_run": False, "approved": True},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert "sender is not configured" in payload["message"]


@pytest.mark.asyncio
async def test_employee_insight_verification_status_reports_readiness(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_test",
            employee_hash="hash_test",
            display_name="测试员工",
            pilot_status=PilotStatus.ACTIVE,
            metadata={"verification_scope": True},
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(
        RouteContext(
            config={
                "employee_insight": {
                    "real_sender_enabled": True,
                    "real_send_approval_token": "approve-token",
                }
            },
            app=app,
        ),
        dc_root=tmp_path,
    )  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get("/api/employee-insight/verification/status")
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["config"]["real_sender_enabled"] is True
    assert payload["data"]["config"]["approval_token_configured"] is True
    assert payload["data"]["profiles"]["verification_count"] == 1
    assert payload["data"]["go_no_go"]["ready_for_one_person_send"] is True


@pytest.mark.asyncio
async def test_employee_insight_verification_run_automates_safe_checks(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_test",
            employee_hash="hash_test",
            pilot_status=PilotStatus.ACTIVE,
            metadata={"verification_scope": True},
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(
        RouteContext(
            config={
                "employee_insight": {
                    "real_sender_enabled": True,
                    "real_send_approval_token": "approve-token",
                }
            },
            app=app,
        ),
        dc_root=tmp_path,
    )  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/employee-insight/verification/run",
                json={"approval_token": "wrong-token", "send_one": False},
            )
        ).get_json()

    assert payload["status"] == "ok"
    checks = {item["id"]: item for item in payload["data"]["checks"]}
    assert checks["dry_run_no_send"]["passed"] is True
    assert checks["wrong_token_blocked"]["passed"] is True
    assert checks["simulated_reply_recorded"]["passed"] is True
    assert checks["simulated_pause_recorded"]["passed"] is True
    assert payload["data"]["report"]["rates"]["sent_rate"] == 1
    assert payload["data"]["report"]["rates"]["open_rate"] == 1
    assert payload["data"]["report"]["rates"]["form_open_rate"] == 1
    assert payload["data"]["report"]["rates"]["submit_rate"] == 1
    assert payload["data"]["report"]["go_no_go"]["passed"] is True
    assert payload["data"]["send_result"] is None


@pytest.mark.asyncio
async def test_employee_insight_verification_report_calculates_metrics_and_rollback(
    tmp_path: Path,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_session(
        EmployeeInsightSession(
            session_id="sess_good",
            employee_id="ou_test",
            channel="lark_dm",
            trigger_type="verification_simulation",
            status=EmployeeInsightSessionStatus.COMPLETED,
            metadata={"verification_scope": True, "metric_sample": True},
        )
    )
    for event_type in [
        "outreach_sent",
        "card_opened",
        "employee_insight_action_clicked",
        "form_opened",
        "task_submitted",
    ]:
        await store.append_event(
            InsightEvent(
                event_id=f"evt_{event_type}",
                session_id="sess_good",
                event_type=event_type,
                actor="test",
            )
        )
    await store.upsert_session(
        EmployeeInsightSession(
            session_id="sess_bad",
            employee_id="ou_bad",
            channel="lark_dm",
            trigger_type="verification_simulation",
            status=EmployeeInsightSessionStatus.FAILED,
            metadata={"verification_scope": True, "metric_sample": True},
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get("/api/employee-insight/verification/report")
        ).get_json()

    assert payload["status"] == "ok"
    report = payload["data"]
    assert report["sample_size"] == 2
    assert report["counts"]["sent"] == 1
    assert report["rates"]["sent_rate"] == 0.5
    assert report["go_no_go"]["passed"] is False
    assert report["rollback"]["required"] is True
    assert any("sent_rate" in reason for reason in report["rollback"]["reasons"])


@pytest.mark.asyncio
async def test_employee_insight_verification_send_one_requires_test_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_real_active",
            employee_hash="hash_real",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(
        RouteContext(
            config={
                "employee_insight": {
                    "real_sender_enabled": True,
                    "real_send_approval_token": "approve-token",
                }
            },
            app=app,
        ),
        dc_root=tmp_path,
    )  # type: ignore[arg-type]

    class _UnexpectedSender:
        async def send_text(self, employee_id: str, text: str):
            raise AssertionError("verification send must not touch non-test profile")

    monkeypatch.setattr(
        "astrbot.dashboard.routes.employee_insight.FeishuPrivateMessageSender",
        lambda: _UnexpectedSender(),
    )

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/employee-insight/verification/run",
                json={
                    "approval_token": "approve-token",
                    "send_one": True,
                    "now": "2026-06-14T10:00:00Z",
                },
            )
        ).get_json()

    assert payload["status"] == "ok"
    checks = {item["id"]: item for item in payload["data"]["checks"]}
    assert checks["verification_profile_present"]["passed"] is False
    assert payload["data"]["send_result"] is None


@pytest.mark.asyncio
async def test_employee_insight_verification_send_one_sends_only_test_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = EmployeeInsightStore(tmp_path / "data" / "employee_insight.db")
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_real_active",
            employee_hash="hash_real",
            pilot_status=PilotStatus.ACTIVE,
        )
    )
    await store.upsert_profile(
        EmployeeInsightProfile(
            employee_id="ou_test",
            employee_hash="hash_test",
            pilot_status=PilotStatus.ACTIVE,
            metadata={"verification_scope": True},
        )
    )
    app = Quart(__name__)
    EmployeeInsightRoute(
        RouteContext(
            config={
                "employee_insight": {
                    "real_sender_enabled": True,
                    "real_send_approval_token": "approve-token",
                }
            },
            app=app,
        ),
        dc_root=tmp_path,
    )  # type: ignore[arg-type]

    sent_to: list[str] = []

    class _FakeRouteSender:
        async def send_text(self, employee_id: str, text: str):
            from dc_engines.employee_insight_loop import TextSendResult

            sent_to.append(employee_id)
            return TextSendResult(
                success=True,
                provider_message_id=f"msg_{employee_id}",
            )

    monkeypatch.setattr(
        "astrbot.dashboard.routes.employee_insight.FeishuPrivateMessageSender",
        lambda: _FakeRouteSender(),
    )

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/employee-insight/verification/run",
                json={
                    "approval_token": "approve-token",
                    "send_one": True,
                    "now": "2026-06-14T10:00:00Z",
                },
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert sent_to == ["ou_test"]
    assert payload["data"]["send_result"]["sent"][0]["employee_id"] == "ou_test"
    assert (
        payload["data"]["send_result"]["sent"][0]["provider_message_id"]
        == "msg_ou_test"
    )
