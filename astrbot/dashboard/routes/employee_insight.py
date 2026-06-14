"""Employee insight loop dashboard routes."""

from __future__ import annotations

import sys
from pathlib import Path

from quart import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EmployeeInsightRoute(Route):
    """Read-only APIs for the employee insight closed-loop dashboard."""

    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/employee-insight/dashboard": ("GET", self.employee_insight_dashboard),
            "/employee-insight/sessions": ("GET", self.employee_insight_sessions),
            "/employee-insight/candidates": ("GET", self.employee_insight_candidates),
            "/employee-insight/profiles": [
                ("GET", self.employee_insight_profiles),
                ("POST", self.employee_insight_upsert_profile),
            ],
            "/employee-insight/outreach-plan": (
                "GET",
                self.employee_insight_outreach_plan,
            ),
            "/employee-insight/outreach-record": (
                "POST",
                self.employee_insight_record_outreach,
            ),
            "/employee-insight/outreach-dispatch": (
                "POST",
                self.employee_insight_dispatch_outreach,
            ),
            "/employee-insight/audit": ("GET", self.employee_insight_audit),
            "/employee-insight/doctor": ("GET", self.employee_insight_doctor),
        }
        self.register_routes()

    async def employee_insight_dashboard(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightDashboardSnapshot,
            EmployeeInsightStore,
        )

        store = EmployeeInsightStore(self._store_path())
        snapshot = await EmployeeInsightDashboardSnapshot.from_store(store)
        return Response().ok(_snapshot_to_dict(snapshot)).__dict__

    async def employee_insight_sessions(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightSessionStatus,
            EmployeeInsightStore,
        )

        status_filter = (request.args.get("status") or "").strip()
        limit = _positive_int(request.args.get("limit"), default=50, max_value=200)
        store = EmployeeInsightStore(self._store_path())
        sessions = await store.list_sessions(limit=limit)
        if status_filter:
            try:
                status = EmployeeInsightSessionStatus(status_filter)
            except ValueError:
                return Response().error(f"invalid status: {status_filter}").__dict__
            sessions = [session for session in sessions if session.status == status]
        return (
            Response()
            .ok({"items": [session.to_dict() for session in sessions]})
            .__dict__
        )

    async def employee_insight_candidates(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore, ReviewStatus

        review_status_arg = (request.args.get("review_status") or "").strip()
        review_status = None
        if review_status_arg:
            try:
                review_status = ReviewStatus(review_status_arg)
            except ValueError:
                return (
                    Response()
                    .error(f"invalid review_status: {review_status_arg}")
                    .__dict__
                )
        limit = _positive_int(request.args.get("limit"), default=50, max_value=200)
        store = EmployeeInsightStore(self._store_path())
        candidates = await store.list_candidates(
            review_status=review_status,
            limit=limit,
        )
        return (
            Response()
            .ok({"items": [candidate.to_dict() for candidate in candidates]})
            .__dict__
        )

    async def employee_insight_profiles(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore, PilotStatus

        pilot_status_arg = (request.args.get("pilot_status") or "").strip()
        pilot_status = None
        if pilot_status_arg:
            try:
                pilot_status = PilotStatus(pilot_status_arg)
            except ValueError:
                return (
                    Response()
                    .error(f"invalid pilot_status: {pilot_status_arg}")
                    .__dict__
                )
        limit = _positive_int(request.args.get("limit"), default=100, max_value=500)
        store = EmployeeInsightStore(self._store_path())
        profiles = await store.list_profiles(pilot_status=pilot_status, limit=limit)
        return (
            Response()
            .ok({"items": [profile.to_dict() for profile in profiles]})
            .__dict__
        )

    async def employee_insight_upsert_profile(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightProfile,
            EmployeeInsightStore,
            PilotStatus,
        )

        data = await request.get_json(silent=True) or {}
        employee_id = str(data.get("employee_id") or "").strip()
        if not employee_id:
            return Response().error("employee_id is required").__dict__
        try:
            pilot_status = PilotStatus(str(data.get("pilot_status") or "active"))
        except ValueError:
            return Response().error("invalid pilot_status").__dict__
        store = EmployeeInsightStore(self._store_path())
        existing = await store.get_profile(employee_id)
        profile = EmployeeInsightProfile(
            employee_id=employee_id,
            employee_hash=str(data.get("employee_hash") or employee_id),
            display_name=str(data.get("display_name") or ""),
            department_id=str(data.get("department_id") or ""),
            role=str(data.get("role") or ""),
            pilot_status=pilot_status,
            preferred_touch_time=str(data.get("preferred_touch_time") or ""),
            last_outreach_at=str(data.get("last_outreach_at") or "")
            or (existing.last_outreach_at if existing else ""),
            unanswered_outreach_count=int(
                data.get(
                    "unanswered_outreach_count",
                    existing.unanswered_outreach_count if existing else 0,
                )
                or 0
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        await store.upsert_profile(profile)
        await store.record_audit(
            action="profile_upserted",
            actor="dashboard",
            target_id=profile.employee_id,
            detail={"pilot_status": profile.pilot_status.value},
        )
        return Response().ok(profile.to_dict()).__dict__

    async def employee_insight_outreach_plan(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightOutreachScheduler,
            EmployeeInsightStore,
        )

        now = (request.args.get("now") or "").strip() or None
        limit = _positive_int(request.args.get("limit"), default=20, max_value=200)
        store = EmployeeInsightStore(self._store_path())
        scheduler = EmployeeInsightOutreachScheduler(store)
        plan = await scheduler.build_daily_plan(now=now, limit=limit)
        return Response().ok(plan).__dict__

    async def employee_insight_record_outreach(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightOutreachScheduler,
            EmployeeInsightStore,
        )

        data = await request.get_json(silent=True) or {}
        employee_id = str(data.get("employee_id") or "").strip()
        message_text = str(data.get("message_text") or "").strip()
        if not employee_id:
            return Response().error("employee_id is required").__dict__
        if not message_text:
            return Response().error("message_text is required").__dict__
        store = EmployeeInsightStore(self._store_path())
        scheduler = EmployeeInsightOutreachScheduler(store)
        try:
            session = await scheduler.record_outreach(
                employee_id=employee_id,
                message_text=message_text,
                now=str(data.get("now") or "") or None,
                actor="dashboard",
            )
        except ValueError as exc:
            return Response().error(str(exc)).__dict__
        return Response().ok(session.to_dict()).__dict__

    async def employee_insight_dispatch_outreach(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import (
            EmployeeInsightOutreachDispatcher,
            EmployeeInsightStore,
        )

        data = await request.get_json(silent=True) or {}
        dry_run = bool(data.get("dry_run", True))
        approved = bool(data.get("approved", False))
        if not dry_run:
            return Response().error("real sender is not configured").__dict__
        store = EmployeeInsightStore(self._store_path())
        dispatcher = EmployeeInsightOutreachDispatcher(
            store,
            sender=_DisabledTextSender(),
        )
        payload = await dispatcher.dispatch_daily_outreach(
            now=str(data.get("now") or "") or None,
            limit=int(data.get("limit") or 20),
            approved=approved,
            dry_run=True,
            actor="dashboard",
            message_text=str(data.get("message_text") or "") or None,
        )
        return Response().ok(payload).__dict__

    async def employee_insight_audit(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore

        target_id = (request.args.get("target_id") or "").strip()
        if not target_id:
            return Response().ok({"items": []}).__dict__
        store = EmployeeInsightStore(self._store_path())
        events = await store.list_audit_events(target_id)
        return (
            Response()
            .ok({"items": [_audit_to_dict(event) for event in events]})
            .__dict__
        )

    async def employee_insight_doctor(self):
        store_path = self._store_path()
        vault_path = (
            self.dc_root / "ObsidianVault" / "40_MemoryGovernance" / "EmployeeInsight"
        )
        contract_path = (
            self.dc_root / "harness" / "contracts" / "employee_insight_loop.json"
        )
        return (
            Response()
            .ok(
                {
                    "store": {
                        "path": str(store_path),
                        "exists": store_path.exists(),
                    },
                    "obsidian": {
                        "path": str(vault_path),
                        "exists": vault_path.exists(),
                    },
                    "contract": {
                        "path": str(contract_path),
                        "exists": contract_path.exists(),
                    },
                    "runtime_policy": {
                        "employee_channel": "lark_dm",
                        "mutation_policy": "approved-only",
                        "side_effects": "read-only dashboard route",
                    },
                }
            )
            .__dict__
        )

    def _store_path(self) -> Path:
        return self.dc_root / "data" / "employee_insight.db"

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


def _snapshot_to_dict(snapshot) -> dict:
    return {
        "metrics": snapshot.metrics,
        "top_scenarios": snapshot.top_scenarios,
        "friction_points": snapshot.friction_points,
        "pending_candidates": snapshot.pending_candidates,
        "hermes_candidates": snapshot.hermes_candidates,
    }


def _audit_to_dict(event) -> dict:
    return {
        "audit_id": event.audit_id,
        "action": event.action,
        "actor": event.actor,
        "target_id": event.target_id,
        "detail": event.detail,
        "created_at": event.created_at,
    }


def _positive_int(value: str | None, *, default: int, max_value: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return min(parsed, max_value)


class _DisabledTextSender:
    async def send_text(self, employee_id: str, text: str):
        from dc_engines.employee_insight_loop import TextSendResult

        return TextSendResult(
            success=False,
            error="real sender is not configured",
            raw={"employee_id": employee_id, "text": text},
        )
