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
    EmployeeInsightSession,
    EmployeeInsightSessionStatus,
    EmployeeInsightStore,
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
    assert '"employeeInsight": "员工需求洞察"' in zh_navigation
    assert '"/employee-insight"' in static_source
