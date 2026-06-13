from __future__ import annotations

import sys
from pathlib import Path

import pytest
from quart import Quart

DC_ENGINES_PATH = Path(__file__).resolve().parents[1] / "dc_engines"
if str(DC_ENGINES_PATH) not in sys.path:
    sys.path.insert(0, str(DC_ENGINES_PATH))

from dc_engines.department_workflows.content_rule_proposals import (  # noqa: E402
    ContentSopRuleProposalStore,
)

from astrbot.dashboard.routes.content_sop_ops import ContentSopOpsRoute  # noqa: E402
from astrbot.dashboard.routes.route import RouteContext  # noqa: E402


def _proposal() -> dict:
    return {
        "proposal_id": "proposal_route",
        "department_id": "client_dept",
        "scenario_id": "customer_greeting",
        "rule_type": "process",
        "rule_text": "客户触达默认使用飞书和私域，不默认使用邮件。",
        "support_count": 3,
        "evidence_candidate_ids": ["cand_1", "cand_2", "cand_3"],
        "status": "pending",
    }


def test_content_sop_ops_frontend_does_not_default_runtime_to_ok() -> None:
    source = Path("dashboard/src/views/ContentSopOpsPage.vue").read_text(
        encoding="utf-8"
    )

    assert "config_ok: false" in source
    assert 'status: "unknown"' in source
    assert "config_ok: true" not in source
    assert 'v-else-if="doctorReady"' in source
    assert "生产配置状态未确认。" in source
    assert "scheduled_import_export_enabled: true" not in source


@pytest.mark.asyncio
async def test_content_sop_ops_dashboard_route_reads_runtime_store(
    tmp_path: Path,
) -> None:
    (tmp_path / "data" / "config").mkdir(parents=True)
    (tmp_path / "ObsidianVault").mkdir()
    store = ContentSopRuleProposalStore(
        tmp_path / "data" / "content_sop_rule_proposals.db"
    )
    store.upsert_proposal(_proposal())
    app = Quart(__name__)
    ContentSopOpsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.get("/api/content-sop-ops/dashboard")
        payload = await response.get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["proposals"]["by_status"] == {"pending": 1}
    assert payload["data"]["runtime_overrides"]["config_ok"] is False
    assert payload["data"]["runtime_overrides"]["status"] == "missing"


@pytest.mark.asyncio
async def test_content_sop_ops_scheduled_route_exports_report(tmp_path: Path) -> None:
    (tmp_path / "data" / "config").mkdir(parents=True)
    (tmp_path / "ObsidianVault").mkdir()
    store = ContentSopRuleProposalStore(
        tmp_path / "data" / "content_sop_rule_proposals.db"
    )
    store.upsert_proposal(_proposal())
    app = Quart(__name__)
    ContentSopOpsRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.post(
            "/api/content-sop-ops/scheduled-run",
            json={
                "content_sop_ops": {
                    "scheduled_import_export_enabled": True,
                    "audit_report_enabled": True,
                }
            },
        )
        payload = await response.get_json()

    assert payload["status"] == "ok"
    assert Path(payload["data"]["report_path"]).exists()
