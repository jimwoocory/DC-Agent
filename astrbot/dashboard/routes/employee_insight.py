"""Employee insight loop dashboard routes."""

from __future__ import annotations

import sys
from pathlib import Path

from quart import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]

try:
    from dc_engines.feishu_writer import FeishuPrivateMessageSender
except ImportError:  # pragma: no cover - resolved after _ensure_import_path in runtime
    FeishuPrivateMessageSender = None  # type: ignore[assignment]


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
            "/employee-insight/verification/status": (
                "GET",
                self.employee_insight_verification_status,
            ),
            "/employee-insight/verification/report": (
                "GET",
                self.employee_insight_verification_report,
            ),
            "/employee-insight/verification/run": (
                "POST",
                self.employee_insight_verification_run,
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
        sender = _DisabledTextSender()
        if not dry_run:
            insight_config = _employee_insight_config(self.config)
            if not _config_bool(insight_config.get("real_sender_enabled"), False):
                return Response().error("real sender is not configured").__dict__
            if not approved:
                return Response().error("approved=true is required").__dict__
            approval_token = str(
                insight_config.get("real_send_approval_token") or ""
            ).strip()
            if not approval_token:
                return Response().error("real_send_approval_token is required").__dict__
            request_token = str(data.get("approval_token") or "").strip()
            if request_token != approval_token:
                return Response().error("approval_token is invalid").__dict__
            sender_factory = FeishuPrivateMessageSender
            if sender_factory is None:
                from dc_engines.feishu_writer import (
                    FeishuPrivateMessageSender as sender_factory,
                )

            sender = sender_factory()
        store = EmployeeInsightStore(self._store_path())
        dispatcher = EmployeeInsightOutreachDispatcher(
            store,
            sender=sender,
        )
        payload = await dispatcher.dispatch_daily_outreach(
            now=str(data.get("now") or "") or None,
            limit=int(data.get("limit") or 20),
            approved=approved,
            dry_run=dry_run,
            actor="dashboard",
            message_text=str(data.get("message_text") or "") or None,
        )
        return Response().ok(payload).__dict__

    async def employee_insight_verification_status(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore

        store = EmployeeInsightStore(self._store_path())
        payload = await _build_verification_status(store, self.config)
        return Response().ok(payload).__dict__

    async def employee_insight_verification_report(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore

        store = EmployeeInsightStore(self._store_path())
        payload = await _build_verification_report(store)
        return Response().ok(payload).__dict__

    async def employee_insight_verification_run(self):
        self._ensure_import_path()
        from dc_engines.employee_insight_loop import EmployeeInsightStore

        data = await request.get_json(silent=True) or {}
        store = EmployeeInsightStore(self._store_path())
        payload = await _run_verification_pipeline(
            store=store,
            config=self.config,
            approval_token=str(data.get("approval_token") or "").strip(),
            send_one=bool(data.get("send_one", False)),
            now=str(data.get("now") or "") or None,
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


async def _build_verification_status(store, config) -> dict:
    insight_config = _employee_insight_config(config)
    profiles = await _verification_profiles(store)
    report = await _build_verification_report(store)
    real_sender_enabled = _config_bool(insight_config.get("real_sender_enabled"), False)
    approval_token_configured = bool(
        str(insight_config.get("real_send_approval_token") or "").strip()
    )
    ready_for_one_person_send = (
        real_sender_enabled and approval_token_configured and bool(profiles)
    )
    return {
        "config": {
            "real_sender_enabled": real_sender_enabled,
            "approval_token_configured": approval_token_configured,
        },
        "profiles": {
            "verification_count": len(profiles),
            "items": [profile.to_dict() for profile in profiles],
        },
        "go_no_go": {
            "ready_for_one_person_send": ready_for_one_person_send,
            "reasons": _verification_readiness_reasons(
                real_sender_enabled=real_sender_enabled,
                approval_token_configured=approval_token_configured,
                verification_profiles=profiles,
            ),
        },
        "report": report,
    }


async def _build_verification_report(store) -> dict:
    from dc_engines.employee_insight_loop import EmployeeInsightSessionStatus

    sessions = await store.list_sessions(limit=1000)
    verification_sessions = [
        session
        for session in sessions
        if _config_bool(session.metadata.get("verification_scope"), False)
        and _config_bool(session.metadata.get("metric_sample"), True)
    ]
    event_types_by_session: dict[str, set[str]] = {}
    for session in verification_sessions:
        event_types_by_session[session.session_id] = {
            event.event_type for event in await store.list_events(session.session_id)
        }

    total = len(verification_sessions)
    sent_statuses = {
        EmployeeInsightSessionStatus.SENT,
        EmployeeInsightSessionStatus.OPENED,
        EmployeeInsightSessionStatus.ENGAGED,
        EmployeeInsightSessionStatus.COMPLETED,
        EmployeeInsightSessionStatus.BLOCKED,
        EmployeeInsightSessionStatus.MUTED,
    }
    sent_count = sum(
        1
        for session in verification_sessions
        if session.status in sent_statuses
        or "outreach_sent" in event_types_by_session.get(session.session_id, set())
    )
    opened_count = sum(
        1
        for session in verification_sessions
        if session.status
        in {
            EmployeeInsightSessionStatus.OPENED,
            EmployeeInsightSessionStatus.ENGAGED,
            EmployeeInsightSessionStatus.COMPLETED,
            EmployeeInsightSessionStatus.BLOCKED,
        }
        or "card_opened" in event_types_by_session.get(session.session_id, set())
    )
    clicked_count = sum(
        1
        for session in verification_sessions
        if event_types_by_session.get(session.session_id, set())
        & {"employee_insight_action_clicked", "action_clicked"}
    )
    form_opened_count = sum(
        1
        for session in verification_sessions
        if "form_opened" in event_types_by_session.get(session.session_id, set())
    )
    submitted_count = sum(
        1
        for session in verification_sessions
        if session.status == EmployeeInsightSessionStatus.COMPLETED
        or event_types_by_session.get(session.session_id, set())
        & {"task_submitted", "employee_replied"}
    )
    failed_count = sum(
        1
        for session in verification_sessions
        if session.status == EmployeeInsightSessionStatus.FAILED
    )
    blocked_count = sum(
        1
        for session in verification_sessions
        if session.status == EmployeeInsightSessionStatus.BLOCKED
    )
    anomaly_count = failed_count + blocked_count

    thresholds = {
        "sent_rate": 0.95,
        "open_rate": 0.80,
        "form_open_rate": 0.95,
        "submit_rate": 0.90,
        "anomaly_rate": 0.05,
    }
    rates = {
        "sent_rate": _rate(sent_count, total),
        "open_rate": _rate(opened_count, sent_count),
        "click_rate": _rate(clicked_count, opened_count),
        "form_open_rate": _rate(form_opened_count, clicked_count),
        "submit_rate": _rate(submitted_count, form_opened_count),
        "anomaly_rate": _rate(anomaly_count, total),
    }
    checks = [
        _metric_check("sent_rate", rates["sent_rate"], thresholds["sent_rate"], ">="),
        _metric_check("open_rate", rates["open_rate"], thresholds["open_rate"], ">="),
        _metric_check(
            "form_open_rate",
            rates["form_open_rate"],
            thresholds["form_open_rate"],
            ">=",
        ),
        _metric_check(
            "submit_rate", rates["submit_rate"], thresholds["submit_rate"], ">="
        ),
        _metric_check(
            "anomaly_rate", rates["anomaly_rate"], thresholds["anomaly_rate"], "<="
        ),
    ]
    rollback_reasons = [
        item["message"] for item in checks if not item["passed"] and total > 0
    ]
    if failed_count:
        rollback_reasons.append(
            "存在发送失败样本，需要先修复权限、网络或飞书接口返回。"
        )
    if blocked_count:
        rollback_reasons.append("存在任务阻塞样本，需要补齐新手引导或表单兜底。")
    return {
        "sample_size": total,
        "counts": {
            "sent": sent_count,
            "opened": opened_count,
            "clicked": clicked_count,
            "form_opened": form_opened_count,
            "submitted": submitted_count,
            "failed": failed_count,
            "blocked": blocked_count,
            "anomaly": anomaly_count,
        },
        "rates": rates,
        "thresholds": thresholds,
        "checks": checks,
        "go_no_go": {
            "passed": total > 0 and all(item["passed"] for item in checks),
            "next_scope": "扩大到 3-5 人灰度"
            if total > 0 and all(item["passed"] for item in checks)
            else "继续小范围修复",
        },
        "rollback": {
            "required": bool(rollback_reasons),
            "reasons": rollback_reasons,
            "actions": [
                "停止扩大灰度范围",
                "保留当前样本和事件日志",
                "修复失败项后重新运行一键灰度验证",
            ],
        },
    }


async def _run_verification_pipeline(
    *,
    store,
    config,
    approval_token: str,
    send_one: bool,
    now: str | None,
    message_text: str | None,
) -> dict:
    from dc_engines.employee_insight_loop import (
        EmployeeInsightOutreachDispatcher,
        EmployeeInsightOutreachScheduler,
    )

    status = await _build_verification_status(store, config)
    insight_config = _employee_insight_config(config)
    expected_token = str(insight_config.get("real_send_approval_token") or "").strip()
    profiles = await _verification_profiles(store)
    scheduler = EmployeeInsightOutreachScheduler(store)
    dry_dispatcher = EmployeeInsightOutreachDispatcher(
        store,
        sender=_DisabledTextSender(),
        scheduler=_VerificationScopeScheduler(scheduler, profiles),
    )
    dry_result = await dry_dispatcher.dispatch_daily_outreach(
        now=now,
        limit=1,
        approved=False,
        dry_run=True,
        actor="dashboard_verification",
        message_text=message_text,
    )
    wrong_token_blocked = "wrong-token-for-verification" != expected_token
    checks = [
        _check(
            "config_real_sender_enabled",
            status["config"]["real_sender_enabled"],
            "真实发送开关已开启",
            "real_sender_enabled 未开启",
        ),
        _check(
            "approval_token_configured",
            status["config"]["approval_token_configured"],
            "审批 token 已配置",
            "real_send_approval_token 未配置",
        ),
        _check(
            "verification_profile_present",
            bool(profiles),
            "存在测试白名单画像",
            "没有 metadata.verification_scope=true 的 active 测试画像",
        ),
        _check(
            "dry_run_no_send",
            dry_result["mode"] == "dry_run" and dry_result["sent"] == [],
            "dry-run 未真实发送",
            "dry-run 出现真实发送结果",
        ),
        _check(
            "wrong_token_blocked",
            wrong_token_blocked,
            "错误 approval_token 会被拦截",
            "错误 approval_token 未被拦截",
        ),
    ]
    simulation = await _record_verification_simulation(store, profiles)
    report = await _build_verification_report(store)
    checks.extend(
        [
            _check(
                "simulated_reply_recorded",
                simulation["reply_recorded"],
                "模拟员工回复已写入 session/event/audit",
                "模拟员工回复未完整写入",
            ),
            _check(
                "simulated_pause_recorded",
                simulation["pause_recorded"],
                "模拟暂停退出已写入 session/event/audit",
                "模拟暂停退出未完整写入",
            ),
        ]
    )
    send_result = None
    if send_one and all(
        item["passed"]
        for item in checks
        if item["id"]
        in {
            "config_real_sender_enabled",
            "approval_token_configured",
            "verification_profile_present",
        }
    ):
        if approval_token != expected_token:
            checks.append(
                _check(
                    "approval_token_matched",
                    False,
                    "审批 token 匹配",
                    "approval_token 不匹配",
                )
            )
        else:
            sender_factory = FeishuPrivateMessageSender
            if sender_factory is None:
                from dc_engines.feishu_writer import (
                    FeishuPrivateMessageSender as sender_factory,
                )

            send_dispatcher = EmployeeInsightOutreachDispatcher(
                store,
                sender=sender_factory(),
                scheduler=_VerificationScopeScheduler(scheduler, profiles[:1]),
            )
            send_result = await send_dispatcher.dispatch_daily_outreach(
                now=now,
                limit=1,
                approved=True,
                dry_run=False,
                actor="dashboard_verification",
                message_text=message_text,
            )
            checks.append(
                _check(
                    "send_one_test_profile",
                    bool(send_result["sent"]) and not send_result["failed"],
                    "已向 1 个测试 open_id 发送",
                    "测试 open_id 发送失败",
                )
            )
    await store.record_audit(
        action="verification_pipeline_run",
        actor="dashboard",
        target_id="employee_insight_verification",
        detail={
            "send_one": send_one,
            "checks": checks,
            "sent_count": len(send_result["sent"]) if send_result else 0,
            "report": report,
        },
    )
    return {
        "status": status,
        "checks": checks,
        "dry_run": dry_result,
        "simulation": simulation,
        "report": report,
        "send_result": send_result,
        "go_no_go": {
            "passed": all(item["passed"] for item in checks)
            and report["go_no_go"]["passed"],
            "next_scope": "3人灰度"
            if all(item["passed"] for item in checks) and report["go_no_go"]["passed"]
            else "继续修复",
        },
    }


async def _verification_profiles(store) -> list:
    from dc_engines.employee_insight_loop import PilotStatus

    profiles = await store.list_profiles(pilot_status=PilotStatus.ACTIVE, limit=500)
    return [
        profile
        for profile in profiles
        if _config_bool(profile.metadata.get("verification_scope"), False)
    ]


async def _record_verification_simulation(store, profiles: list) -> dict:
    import uuid

    from dc_engines.employee_insight_loop import (
        EmployeeInsightSession,
        EmployeeInsightSessionStatus,
        InsightEvent,
    )

    if not profiles:
        return {"reply_recorded": False, "pause_recorded": False}
    profile = profiles[0]
    reply_session = EmployeeInsightSession(
        session_id=uuid.uuid4().hex,
        employee_id=profile.employee_id,
        channel="lark_dm",
        trigger_type="verification_simulation",
        status=EmployeeInsightSessionStatus.COMPLETED,
        department_id=profile.department_id,
        role=profile.role,
        scenario_id="unknown_how_to_start",
        original_request="我不知道怎么用",
        normalized_goal="验证小白入口能进入任务陪跑。",
        metadata={
            "verification_scope": True,
            "metric_sample": True,
            "employee_hash": profile.employee_hash,
        },
        summary="灰度验证模拟员工回复。",
    )
    await store.upsert_session(reply_session)
    for event_type, payload in [
        ("outreach_sent", {"channel": "lark_dm"}),
        ("card_opened", {"card": "employee_insight_welcome"}),
        (
            "employee_insight_action_clicked",
            {"action": "unknown_how_to_start"},
        ),
        ("form_opened", {"form": "beginner_task_intake"}),
        ("employee_replied", {"text": "我不知道怎么用"}),
        ("task_submitted", {"scenario_id": "unknown_how_to_start"}),
    ]:
        await store.append_event(
            InsightEvent(
                event_id=uuid.uuid4().hex,
                session_id=reply_session.session_id,
                event_type=event_type,
                actor="verification",
                payload=payload,
            )
        )
    await store.record_audit(
        action="verification_reply_simulated",
        actor="dashboard",
        target_id=reply_session.session_id,
        detail={"employee_id": profile.employee_id},
    )

    pause_session = EmployeeInsightSession(
        session_id=uuid.uuid4().hex,
        employee_id=profile.employee_id,
        channel="lark_dm",
        trigger_type="verification_simulation",
        status=EmployeeInsightSessionStatus.MUTED,
        department_id=profile.department_id,
        role=profile.role,
        scenario_id="opt_out",
        original_request="暂停",
        normalized_goal="验证暂停退出能被记录。",
        metadata={
            "verification_scope": True,
            "metric_sample": False,
            "employee_hash": profile.employee_hash,
        },
        summary="灰度验证模拟暂停退出。",
    )
    await store.upsert_session(pause_session)
    await store.append_event(
        InsightEvent(
            event_id=uuid.uuid4().hex,
            session_id=pause_session.session_id,
            event_type="opt_out",
            actor="verification",
            payload={"text": "暂停"},
        )
    )
    await store.record_audit(
        action="verification_pause_simulated",
        actor="dashboard",
        target_id=pause_session.session_id,
        detail={"employee_id": profile.employee_id},
    )
    return {
        "reply_recorded": bool(await store.list_events(reply_session.session_id)),
        "pause_recorded": bool(await store.list_events(pause_session.session_id)),
        "reply_session_id": reply_session.session_id,
        "pause_session_id": pause_session.session_id,
    }


def _verification_readiness_reasons(
    *,
    real_sender_enabled: bool,
    approval_token_configured: bool,
    verification_profiles: list,
) -> list[str]:
    reasons: list[str] = []
    if not real_sender_enabled:
        reasons.append("real_sender_enabled 未开启")
    if not approval_token_configured:
        reasons.append("real_send_approval_token 未配置")
    if not verification_profiles:
        reasons.append("没有 active 测试画像")
    return reasons


def _check(check_id: str, passed: bool, ok: str, failed: str) -> dict:
    return {
        "id": check_id,
        "passed": bool(passed),
        "message": ok if passed else failed,
    }


def _metric_check(
    metric_id: str, actual: float, threshold: float, operator: str
) -> dict:
    passed = actual <= threshold if operator == "<=" else actual >= threshold
    relation = "不高于" if operator == "<=" else "不低于"
    return {
        "id": metric_id,
        "passed": passed,
        "actual": actual,
        "threshold": threshold,
        "message": (
            f"{metric_id} 达标：{actual:.0%}"
            if passed
            else f"{metric_id} 未达标：{actual:.0%}，需要{relation} {threshold:.0%}"
        ),
    }


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


class _VerificationScopeScheduler:
    def __init__(self, scheduler, profiles: list) -> None:
        self.scheduler = scheduler
        self.profiles = profiles

    async def build_daily_plan(self, *, now: str | None = None, limit: int = 20):
        eligible: list[dict] = []
        skipped: list[dict] = []
        now_value = now or ""
        for profile in self.profiles[:limit]:
            reason = (
                self.scheduler._skip_reason(profile, now_value) if now_value else ""
            )
            item = {
                "employee_id": profile.employee_id,
                "employee_hash": profile.employee_hash,
                "display_name": profile.display_name,
                "department_id": profile.department_id,
                "role": profile.role,
                "pilot_status": profile.pilot_status.value,
                "last_outreach_at": profile.last_outreach_at,
                "unanswered_outreach_count": profile.unanswered_outreach_count,
            }
            if reason:
                skipped.append({**item, "reason": reason})
            else:
                eligible.append({**item, "reason": "eligible"})
        return {"eligible": eligible, "skipped": skipped}

    async def record_outreach(self, **kwargs):
        return await self.scheduler.record_outreach(**kwargs)

    async def record_failed_outreach(self, **kwargs) -> None:
        await self.scheduler.record_failed_outreach(**kwargs)


def _employee_insight_config(config) -> dict:
    if isinstance(config, dict):
        value = config.get("employee_insight") or {}
    else:
        get = getattr(config, "get", None)
        value = get("employee_insight", {}) if callable(get) else {}
    return value if isinstance(value, dict) else {}


def _config_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class _DisabledTextSender:
    async def send_text(self, employee_id: str, text: str):
        from dc_engines.employee_insight_loop import TextSendResult

        return TextSendResult(
            success=False,
            error="real sender is not configured",
            raw={"employee_id": employee_id, "text": text},
        )
