from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .case_store import CaseStore
from .contracts import Case, CaseStatus

if TYPE_CHECKING:
    from astrbot.core.harness import HarnessTaskStore

logger = logging.getLogger("astrbot")

ArchiveHook = Callable[[Case], Awaitable[None] | None]


class CaseEngine:
    """Lifecycle wrapper over the Case sidecar store.

    The engine sits *above* Harness — it reuses ``HarnessTaskStore`` to
    resolve task summaries inside the aggregated context view but never
    mutates Harness tables.
    """

    def __init__(
        self,
        store: CaseStore,
        *,
        harness_store: HarnessTaskStore | None = None,
        archive_hook: ArchiveHook | None = None,
    ) -> None:
        self.store = store
        self.harness_store = harness_store
        self._archive_hook = archive_hook or self._default_archive_hook

    async def create_case(
        self,
        *,
        name: str,
        platform_id: str,
        session_id: str,
        client_name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Case:
        if not name.strip():
            raise ValueError("case name cannot be empty")
        return await self.store.create_case(
            name=name.strip(),
            platform_id=platform_id,
            session_id=session_id,
            client_name=(client_name or "").strip() or None,
            payload=payload or {},
        )

    async def attach_task(self, case_id: str, task_id: str) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        if task_id in case.task_ids:
            return case
        next_task_ids = [*case.task_ids, task_id]
        return await self.store.update_case_fields(
            case_id,
            task_ids=next_task_ids,
            event_type="task_attached",
            event_payload={"task_id": task_id},
        )

    async def add_deliverable(
        self,
        case_id: str,
        *,
        kind: str,
        path: str,
        version: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        deliverable: dict[str, Any] = {
            "kind": kind,
            "path": path,
            "created_at": self._utcnow(),
        }
        if version is not None:
            deliverable["version"] = version
        if extra:
            deliverable.update(extra)
        next_deliverables = [*case.deliverables, deliverable]
        return await self.store.update_case_fields(
            case_id,
            deliverables=next_deliverables,
            event_type="deliverable_added",
            event_payload=deliverable,
        )

    async def assign_role(self, case_id: str, role: str, user_id: str) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        next_roles = {**case.roles, role: user_id}
        return await self.store.update_case_fields(
            case_id,
            roles=next_roles,
            event_type="role_assigned",
            event_payload={"role": role, "user_id": user_id},
        )

    async def bump_version(self, case_id: str) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        next_version = case.version + 1
        return await self.store.update_case_fields(
            case_id,
            version=next_version,
            event_type="version_bumped",
            event_payload={
                "from_version": case.version,
                "to_version": next_version,
            },
        )

    async def set_status(self, case_id: str, status: CaseStatus) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        if case.status == status:
            return case
        return await self.store.update_case_fields(
            case_id,
            status=status,
            event_type="status_changed",
            event_payload={"from": case.status, "to": status},
        )

    async def archive_case(self, case_id: str) -> Case:
        case = await self.store.get_case(case_id)
        if case is None:
            raise LookupError(f"case {case_id!r} not found")
        if case.status == "archived":
            return case
        archived = await self.store.update_case_fields(
            case_id,
            status="archived",
            event_type="archived",
            event_payload={"from": case.status},
        )
        await self._run_archive_hook(archived)
        return archived

    async def get_current_case_for_session(self, session_id: str) -> Case | None:
        return await self.store.get_active_case_for_session(session_id)

    async def get_case_context(self, case_id: str) -> dict[str, Any] | None:
        """Return the full aggregated view for a case.

        The view is a plain dict so it can be rendered as markdown by the CLI
        layer or shipped over a future webhook. ``tasks`` is a lightweight
        summary list pulled from ``HarnessTaskStore`` (when available).
        """
        case = await self.store.get_case(case_id)
        if case is None:
            return None

        tasks_summary: list[dict[str, Any]] = []
        if self.harness_store is not None and case.task_ids:
            for task_id in case.task_ids:
                try:
                    task = await self.harness_store.get_task(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[CaseEngine] failed to load task %s for case %s: %s",
                        task_id,
                        case_id,
                        exc,
                    )
                    continue
                if task is None:
                    tasks_summary.append({"task_id": task_id, "missing": True})
                    continue
                tasks_summary.append(
                    {
                        "task_id": task.task_id,
                        "title": task.title,
                        "status": task.status,
                        "domain": task.domain,
                        "updated_at": task.updated_at,
                    }
                )

        events = await self.store.list_events(case_id)
        return {
            "case_id": case.case_id,
            "name": case.name,
            "status": case.status,
            "client_name": case.client_name,
            "platform_id": case.platform_id,
            "session_id": case.session_id,
            "version": case.version,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "task_ids": case.task_ids,
            "tasks": tasks_summary,
            "deliverables": case.deliverables,
            "roles": case.roles,
            "payload": case.payload,
            "event_count": len(events),
        }

    async def _run_archive_hook(self, case: Case) -> None:
        try:
            result = self._archive_hook(case)
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CaseEngine] archive hook failed for case %s: %s",
                case.case_id,
                exc,
            )

    @staticmethod
    def _default_archive_hook(case: Case) -> None:
        logger.info(
            "[CaseEngine] case archived (kb-sync stub) case_id=%s name=%s "
            "deliverables=%d tasks=%d",
            case.case_id,
            case.name,
            len(case.deliverables),
            len(case.task_ids),
        )

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
