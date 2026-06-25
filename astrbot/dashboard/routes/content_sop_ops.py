"""Content SOP operations dashboard routes."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.dashboard.asgi_runtime import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ContentSopOpsRoute(Route):
    """Control-plane APIs for Content SOP operational readiness."""

    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/content-sop-ops/dashboard": ("GET", self.content_sop_ops_dashboard),
            "/content-sop-ops/reminders": ("GET", self.content_sop_ops_reminders),
            "/content-sop-ops/doctor": ("GET", self.content_sop_ops_doctor),
            "/content-sop-ops/scheduled-run": (
                "POST",
                self.content_sop_ops_scheduled_run,
            ),
        }
        self.register_routes()

    async def content_sop_ops_dashboard(self):
        self._ensure_import_path()
        from dc_engines.department_workflows.content_rule_proposals import (
            ContentSopRuleProposalStore,
        )
        from dc_engines.department_workflows.content_sop_ops import (
            build_content_sop_ops_dashboard,
        )

        paths = self._paths()
        payload = build_content_sop_ops_dashboard(
            proposal_store=ContentSopRuleProposalStore(paths["proposals"]),
            overrides_path=paths["overrides"],
            governed_memory_db_path=paths["governed_db"],
            now=_now_iso(),
        )
        return Response().ok(payload).__dict__

    async def content_sop_ops_reminders(self):
        self._ensure_import_path()
        from dc_engines.department_workflows.content_rule_proposals import (
            ContentSopRuleProposalStore,
        )
        from dc_engines.department_workflows.content_sop_ops import (
            build_content_sop_ops_dashboard,
            build_content_sop_ops_reminder_card,
            build_content_sop_ops_reminders,
        )

        paths = self._paths()
        dashboard = build_content_sop_ops_dashboard(
            proposal_store=ContentSopRuleProposalStore(paths["proposals"]),
            overrides_path=paths["overrides"],
            governed_memory_db_path=paths["governed_db"],
            now=_now_iso(),
        )
        reminders = build_content_sop_ops_reminders(dashboard)
        return (
            Response()
            .ok(
                {
                    "items": [item.to_dict() for item in reminders],
                    "card": build_content_sop_ops_reminder_card(reminders),
                }
            )
            .__dict__
        )

    async def content_sop_ops_doctor(self):
        self._ensure_import_path()
        from dc_engines.department_workflows.content_sop_ops import (
            confirm_content_sop_production_config,
        )

        paths = self._paths()
        cfg = dict(self.config) if isinstance(self.config, dict) else {}
        payload = confirm_content_sop_production_config(
            cfg,
            proposal_store_path=paths["proposals"],
            overrides_path=paths["overrides"],
            governed_memory_db_path=paths["governed_db"],
            obsidian_vault_path=paths["vault"],
        )
        return Response().ok(payload).__dict__

    async def content_sop_ops_scheduled_run(self):
        self._ensure_import_path()
        from dc_engines.department_workflows.content_rule_proposals import (
            ContentSopRuleProposalStore,
        )
        from dc_engines.department_workflows.content_sop_ops import (
            run_scheduled_content_sop_ops,
        )

        data = await request.get_json(silent=True) or {}
        paths = self._paths()
        cfg: dict[str, Any] = dict(self.config) if isinstance(self.config, dict) else {}
        if isinstance(data.get("content_sop_ops"), dict):
            cfg["content_sop_ops"] = data["content_sop_ops"]
        payload = run_scheduled_content_sop_ops(
            cfg,
            proposal_store=ContentSopRuleProposalStore(paths["proposals"]),
            overrides_path=paths["overrides"],
            governed_memory_db_path=paths["governed_db"],
            output_dir=paths["reports"],
            now=_now_iso(),
        )
        return Response().ok(payload).__dict__

    def _paths(self) -> dict[str, Path]:
        return {
            "proposals": self.dc_root / "data" / "content_sop_rule_proposals.db",
            "overrides": self.dc_root
            / "data"
            / "config"
            / "content_sop_rule_overrides.json",
            "governed_db": self.dc_root / "data" / "governed_memory.db",
            "vault": self.dc_root / "ObsidianVault",
            "reports": self.dc_root / "data" / "output" / "content_sop_ops",
        }

    def _ensure_import_path(self) -> None:
        for path in (
            self.dc_root,
            self.dc_root / "dc_engines",
            PROJECT_ROOT,
            PROJECT_ROOT / "dc_engines",
        ):
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
