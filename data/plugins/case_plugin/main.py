"""Case 聚合层 Star 插件（迁移自 W0 Plan A 2A-0）。

新架构原则：业务逻辑放 dc_engines.case，本插件只做：
1. 在 initialize() 启动 CaseEngine + CaseStore（不依赖 core_lifecycle 修改）
2. 把 engine/store 装到 context 上，方便别的插件读
3. 注册 `/case` CLI：new / context / list / attach / archive / status

数据库路径与旧版本一致（``data/cases.db``），所以迁移过来的 Case 数据可直接复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from dc_engines.case import Case, CaseEngine, CaseStatus, CaseStore, archive_to_nas

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

VALID_STATUSES: tuple[str, ...] = tuple(get_args(CaseStatus))


@register(
    "case_plugin",
    "dc_agent",
    "Case 业务聚合层（W0 Plan A 2A-0 重装移植版），提供 /case CLI",
    "1.0.0",
)
class CasePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.engine: CaseEngine | None = None
        self.store: CaseStore | None = None

    async def initialize(self) -> None:
        # 默认指向 ``<repo>/data/cases.db``；这也是 rsync 迁移过来的数据所在位置
        # __file__ = data/plugins/case_plugin/main.py → parents[3] = repo root
        data_dir = Path(__file__).resolve().parents[3] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        self.store = CaseStore(str(data_dir / "cases.db"))
        await self.store.initialize()

        # 真实 archive_hook：写 NAS + DLQ 兜底（W3 / G4 闭环）
        # NAS 根：默认 /tmp/dc-agent-nas（开发用），生产改 hermes_bridge 配置或环境变量
        cfg = self.context.get_config() or {}
        case_cfg = cfg.get("case", {}) if isinstance(cfg, dict) else {}
        nas_root_str = case_cfg.get("nas_root", "/Volumes/NAS")
        nas_root = Path(nas_root_str)
        dlq_path = data_dir / "case_archive_dlq.jsonl"

        store_for_hook = self.store

        async def _archive_hook(case: Case) -> None:
            result = await archive_to_nas(
                case,
                case_store=store_for_hook,
                nas_root=nas_root,
                dlq_path=dlq_path,
            )
            if result.success:
                logger.info(
                    "[case_plugin] case %s archived to %s (%d deliverables)",
                    case.case_id[:8],
                    result.nas_path,
                    result.deliverables_count,
                )
            else:
                logger.warning(
                    "[case_plugin] case %s archive failed: %s (DLQ: %s)",
                    case.case_id[:8],
                    result.error,
                    result.fallback_dlq_path,
                )

        self.engine = CaseEngine(self.store, archive_hook=_archive_hook)

        # 装到 context 供其他插件（hermes_bridge / group_summary 等）使用
        self.context.case_engine = self.engine
        self.context.case_store = self.store

        logger.info(
            "[case_plugin] CaseEngine 启动：%s · NAS root=%s",
            data_dir / "cases.db",
            nas_root,
        )

    # --------------------------- helpers ---------------------------

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    async def _resolve_active_case(self, event: AstrMessageEvent) -> Case | None:
        if self.engine is None:
            self._reply(event, "Case 引擎未初始化。")
            return None
        case = await self.engine.get_current_case_for_session(event.unified_msg_origin)
        if case is None:
            self._reply(
                event,
                "当前会话没有活跃 case。用 `/case new <名称>` 新建一个。",
            )
        return case

    @staticmethod
    def _parse_new_args(arg_text: str) -> tuple[str, str | None]:
        """Parse ``<name> [--client <name>]``."""
        text = (arg_text or "").strip()
        if not text:
            return "", None
        if "--client" in text:
            head, _, tail = text.partition("--client")
            name = head.strip()
            client = tail.strip() or None
            return name, client
        return text, None

    def _render_case_context(self, view: dict) -> str:
        # view 是 get_case_context 返回的 flat dict（顶层就有 case_id/name/payload 等）；
        # 旧代码用 view.get("case", {}) 是 bug 残留，这里同时兼容两种 shape。
        case = view.get("case") if isinstance(view.get("case"), dict) else view
        payload = case.get("payload") or {}
        lines = [
            f"Case {case.get('case_id', '?')[:8]}：{case.get('name', '?')}",
            f"- 状态: {case.get('status', '?')}",
            f"- 甲方: {case.get('client_name') or '—'}",
            f"- 版本: v{case.get('version', '?')}",
        ]
        # 发起人（payload 里的 requester_*；走 employee_directory 富化时才有）
        requester_name = payload.get("requester_display_name")
        requester_dept = payload.get("requester_department")
        requester_oid = payload.get("requester_open_id")
        if requester_name or requester_oid:
            label = requester_name or (
                str(requester_oid)[:12] + "..." if requester_oid else "?"
            )
            if requester_dept:
                label = f"{label} · {requester_dept}"
            lines.append(f"- 发起人: {label}")
        tasks = view.get("tasks") or []
        lines.append(f"- 任务 ({len(tasks)}):" if tasks else "- 任务: —")
        for t in tasks[:10]:
            lines.append(
                f"  • [{t.get('status', '?')}] {t.get('title', '?')} (#{t.get('task_id', '?')[:8]})"
            )
        if len(tasks) > 10:
            lines.append(f"  ... 共 {len(tasks)} 条")

        deliverables = view.get("deliverables") or []
        if deliverables:
            lines.append(f"- 交付物 ({len(deliverables)}):")
            for d in deliverables[:10]:
                version = d.get("version")
                version_str = f" v{version}" if version is not None else ""
                lines.append(f"  • [{d.get('kind')}{version_str}] {d.get('path')}")
            if len(deliverables) > 10:
                lines.append(f"  ... 共 {len(deliverables)} 条")
        else:
            lines.append("- 交付物: —")
        return "\n".join(lines)

    # --------------------------- /case dispatch ---------------------------

    @filter.command(
        "case",
        desc="Case 业务聚合：/case new|context|list|attach|archive|status",
    )
    async def case_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        # 兼容 "/case xxx" 与 "case xxx"
        if text.startswith("/case"):
            text = text[len("/case") :].strip()
        elif text.startswith("case"):
            text = text[len("case") :].strip()
        parts = text.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "new":
            await self._case_new(event, rest)
        elif sub == "context":
            await self._case_context(event)
        elif sub == "list":
            await self._case_list(event)
        elif sub == "attach":
            await self._case_attach(event, rest)
        elif sub == "archive":
            await self._case_archive(event)
        elif sub == "status":
            await self._case_status(event, rest)
        else:
            self._reply(
                event,
                "用法：\n"
                "  /case new <名称> [--client <甲方>]\n"
                "  /case context\n"
                "  /case list\n"
                "  /case attach <task_id>\n"
                "  /case archive\n"
                "  /case status <" + "|".join(VALID_STATUSES) + ">",
            )

    # --------------------------- subcommand impls ---------------------------

    async def _case_new(self, event: AstrMessageEvent, rest: str) -> None:
        if self.engine is None:
            self._reply(event, "Case 引擎未初始化。")
            return
        name, client_name = self._parse_new_args(rest)
        if not name:
            self._reply(event, "用法: /case new <名称> [--client <甲方>]")
            return

        # 查发起人画像（找不到不阻断建 case）
        payload: dict = {"source": "case_plugin"}
        emp_store = getattr(self.context, "employee_store", None)
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            sender_id = ""
        if emp_store is not None and sender_id:
            try:
                emp = await emp_store.get_employee(sender_id)
                if emp is not None:
                    payload.update(
                        {
                            "requester_open_id": emp.open_id,
                            "requester_display_name": emp.display_name or "",
                            "requester_department": emp.department or "",
                            "requester_role": emp.role or "",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[case_plugin] 查请求人失败：%s", exc)
        if "requester_open_id" not in payload and sender_id:
            payload["requester_open_id"] = sender_id

        case = await self.engine.create_case(
            name=name,
            platform_id=event.get_platform_id(),
            session_id=event.unified_msg_origin,
            client_name=client_name,
            payload=payload,
        )

        # 回执里多显示一行发起人（如果有名字）
        requester_label = payload.get("requester_display_name") or (
            sender_id[:8] + "..." if sender_id else "—"
        )
        dept = payload.get("requester_department")
        requester_line = f"{requester_label}" + (f" · {dept}" if dept else "")
        self._reply(
            event,
            "已创建 Case：\n"
            f"- case_id: {case.case_id}\n"
            f"- name: {case.name}\n"
            f"- status: {case.status}\n"
            f"- 甲方: {case.client_name or '—'}\n"
            f"- 发起人: {requester_line}",
        )

    async def _case_context(self, event: AstrMessageEvent) -> None:
        case = await self._resolve_active_case(event)
        if case is None or self.engine is None:
            return
        view = await self.engine.get_case_context(case.case_id)
        if view is None:
            self._reply(event, "未找到该 case。")
            return
        self._reply(event, self._render_case_context(view))

    async def _case_list(self, event: AstrMessageEvent) -> None:
        if self.store is None:
            self._reply(event, "Case 存储未初始化。")
            return
        cases = await self.store.list_cases_for_session(
            event.unified_msg_origin,
            limit=10,
        )
        if not cases:
            self._reply(event, "当前会话还没有 case。")
            return
        lines = ["当前会话最近的 Case："]
        for case in cases:
            client = case.client_name or "—"
            lines.append(
                f"- {case.case_id[:8]} | {case.status} | {case.name}（甲方 {client}）",
            )
        self._reply(event, "\n".join(lines))

    async def _case_attach(self, event: AstrMessageEvent, task_id: str) -> None:
        if not task_id.strip():
            self._reply(event, "用法: /case attach <task_id>")
            return
        case = await self._resolve_active_case(event)
        if case is None or self.engine is None:
            return
        try:
            updated = await self.engine.attach_task(case.case_id, task_id.strip())
        except LookupError:
            self._reply(event, "Case 已不存在。")
            return
        self._reply(
            event,
            f"已挂接 task {task_id.strip()[:8]} 到 case {updated.case_id[:8]}。"
            f"\n当前任务数: {len(updated.task_ids)}",
        )

    async def _case_archive(self, event: AstrMessageEvent) -> None:
        case = await self._resolve_active_case(event)
        if case is None or self.engine is None:
            return
        archived = await self.engine.archive_case(case.case_id)
        self._reply(
            event,
            f"Case 已归档：{archived.case_id[:8]} | {archived.name}",
        )

    async def _case_status(self, event: AstrMessageEvent, status: str) -> None:
        normalized = (status or "").strip().lower()
        if normalized not in VALID_STATUSES:
            self._reply(
                event,
                "用法: /case status <状态>。可选: " + ", ".join(VALID_STATUSES),
            )
            return
        case = await self._resolve_active_case(event)
        if case is None or self.engine is None:
            return
        updated = await self.engine.set_status(case.case_id, normalized)  # type: ignore[arg-type]
        self._reply(
            event,
            f"Case 状态已更新：{updated.case_id[:8]} -> {updated.status}",
        )
