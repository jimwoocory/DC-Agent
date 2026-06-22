from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import (
    HARNESS_TERMINAL_STATUSES,
    HarnessTask,
    HarnessTaskCreateRequest,
    HarnessTaskReview,
    HarnessTaskStatus,
)
from .guardrails import assess_harness_guardrails
from .loop_runtime import LoopOrchestrator
from .memory_promotion import HarnessMemoryPromoter
from .source_provenance_guard import assert_completion_source_provenance
from .task_store import HarnessTaskStore

try:
    from dc_engines.pet_live.store import PetLiveStore
except Exception:  # noqa: BLE001
    PetLiveStore = Any  # type: ignore[assignment,misc]

ALLOWED_HARNESS_STATUS_TRANSITIONS: dict[HarnessTaskStatus, set[HarnessTaskStatus]] = {
    "pending": {
        "pending",
        "in_progress",
        "blocked",
        "review_required",
        "completed",
        "cancelled",
        "failed",
    },
    "in_progress": {
        "in_progress",
        "blocked",
        "review_required",
        "completed",
        "cancelled",
        "failed",
    },
    "blocked": {
        "blocked",
        "in_progress",
        "cancelled",
        "failed",
    },
    "review_required": {
        "review_required",
        "blocked",
        "completed",
        "cancelled",
        "failed",
    },
    "completed": set(),
    "cancelled": set(),
    "failed": set(),
}


class HarnessEngine:
    """Thin lifecycle wrapper over the Harness task sidecar store.

    The first phase keeps this engine intentionally small so future AstrBot
    plugins, built-in commands, or business workflows can all share the same
    task contract.
    """

    def __init__(
        self,
        store: HarnessTaskStore,
        *,
        session_snapshot_getter: Callable[[str], Awaitable[dict | None] | dict | None]
        | None = None,
        cognitive_snapshot_getter: Callable[
            [HarnessTaskCreateRequest], Awaitable[dict | None] | dict | None
        ]
        | None = None,
        memory_promoter: HarnessMemoryPromoter | None = None,
        pet_live_store: PetLiveStore | None = None,
    ) -> None:
        self.store = store
        self.session_snapshot_getter = session_snapshot_getter
        self.cognitive_snapshot_getter = cognitive_snapshot_getter
        self.memory_promoter = memory_promoter
        self.pet_live_store = pet_live_store

    async def create_task(self, request: HarnessTaskCreateRequest) -> HarnessTask:
        payload = dict(request.payload)
        guardrails = assess_harness_guardrails(request)
        guardrails_payload = guardrails.to_dict()
        payload["guardrails"] = guardrails_payload

        session_context = await self._get_session_context(request.conversation_id)
        if session_context is not None:
            payload["session_context"] = session_context
        cognitive_context = await self._get_cognitive_context(request)
        if cognitive_context is not None:
            payload["cognitive_context"] = cognitive_context

        task = await self.store.create_task(
            HarnessTaskCreateRequest(
                title=request.title,
                conversation_id=request.conversation_id,
                platform_id=request.platform_id,
                session_id=request.session_id,
                domain=request.domain,
                payload=payload,
            )
        )
        if session_context is not None:
            await self.store.append_event(
                task.task_id,
                "session_context_linked",
                session_context,
            )
        if cognitive_context is not None:
            await self.store.append_event(
                task.task_id,
                "cognitive_context_linked",
                cognitive_context,
            )
        await self.store.append_event(
            task.task_id,
            "guardrails_attached",
            guardrails_payload,
        )
        await LoopOrchestrator(self.store).ensure_plan_created(task)
        self._publish_pet_event(task, "harness_task_created")
        return task

    async def mark_in_progress(
        self,
        task_id: str,
        *,
        note: str | None = None,
    ) -> HarnessTask:
        return await self._transition_task_status(
            task_id,
            "in_progress",
            event_payload={"note": note} if note else None,
        )

    async def mark_review_required(
        self,
        task_id: str,
        *,
        reviewer_note: str | None = None,
        result: dict | None = None,
    ) -> HarnessTask:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        await LoopOrchestrator(self.store).settle_for_review_required(
            task,
            review_reason=reviewer_note or "manual review required",
            summary=reviewer_note or "Task requires review before completion.",
            result=result,
            metadata={"source": "HarnessEngine.mark_review_required"},
        )
        return await self._transition_task_status(
            task_id,
            "review_required",
            result=result,
            event_payload={"reviewer_note": reviewer_note} if reviewer_note else None,
        )

    async def complete_task(
        self,
        task_id: str,
        *,
        result: dict | None = None,
    ) -> HarnessTask:
        await self._assert_completion_allowed(task_id, result)
        task = await self._transition_task_status(
            task_id,
            "completed",
            result=result,
        )
        await self._maybe_promote_memory(task)
        return task

    async def fail_task(
        self,
        task_id: str,
        *,
        reason: str,
    ) -> HarnessTask:
        return await self._transition_task_status(
            task_id,
            "failed",
            event_payload={"reason": reason},
        )

    async def append_trace(
        self,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        if task.status in HARNESS_TERMINAL_STATUSES:
            raise RuntimeError(
                f"cannot append trace to terminal task {task_id!r} ({task.status})"
            )
        await self.store.append_event(task_id, event_type, payload)

    async def merge_payload(
        self,
        task_id: str,
        patch: dict,
        *,
        event_type: str = "payload_merged",
    ) -> HarnessTask:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        if task.status in HARNESS_TERMINAL_STATUSES:
            raise RuntimeError(
                f"cannot merge payload into terminal task {task_id!r} ({task.status})"
            )
        return await self.store.merge_task_payload(
            task_id,
            patch,
            event_type=event_type,
        )

    async def set_status(
        self,
        task_id: str,
        status: HarnessTaskStatus,
        *,
        result: dict | None = None,
        event_payload: dict | None = None,
    ) -> HarnessTask:
        if status == "completed":
            await self._assert_completion_allowed(task_id, result)
        elif status == "blocked":
            existing_task = await self.store.get_task(task_id)
            if existing_task is None:
                raise LookupError(f"task {task_id!r} not found")
            blocking_reason = ""
            if event_payload:
                blocking_reason = str(
                    event_payload.get("blocking_reason")
                    or event_payload.get("reason")
                    or ""
                )
            await LoopOrchestrator(self.store).settle_for_blocked(
                existing_task,
                blocking_reason=blocking_reason,
                summary=blocking_reason or "Task blocked.",
                metadata=event_payload or {},
            )
        elif status == "review_required":
            existing_task = await self.store.get_task(task_id)
            if existing_task is None:
                raise LookupError(f"task {task_id!r} not found")
            review_reason = ""
            if event_payload:
                review_reason = str(
                    event_payload.get("review_reason")
                    or event_payload.get("reviewer_note")
                    or event_payload.get("reason")
                    or ""
                )
            await LoopOrchestrator(self.store).settle_for_review_required(
                existing_task,
                review_reason=review_reason or "review required",
                summary=review_reason or "Task requires review before completion.",
                result=result,
                metadata=event_payload or {},
            )
        return await self._transition_task_status(
            task_id,
            status,
            result=result,
            event_payload=event_payload,
        )

    async def approve_task(
        self,
        task_id: str,
        *,
        reviewer_id: str,
        note: str = "",
    ) -> HarnessTaskReview:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        if task.status != "review_required":
            raise RuntimeError(
                "cannot approve task "
                f"{task_id!r} while status is {task.status!r}; "
                "task must be review_required"
            )

        review = await self.store.create_review(
            task_id,
            reviewer_id,
            "approved",
            note,
        )
        await self.store.append_event(
            task_id,
            "review_recorded",
            {
                "reviewer_id": reviewer_id,
                "decision": "approved",
                "note": note,
            },
        )
        return review

    async def reject_task(
        self,
        task_id: str,
        *,
        reviewer_id: str,
        note: str,
    ) -> HarnessTaskReview:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        if task.status != "review_required":
            raise RuntimeError(
                "cannot reject task "
                f"{task_id!r} while status is {task.status!r}; "
                "task must be review_required"
            )

        review = await self.store.create_review(
            task_id,
            reviewer_id,
            "rejected",
            note,
        )
        await self._transition_task_status(
            task_id,
            "blocked",
            event_payload={
                "reviewer_id": reviewer_id,
                "review_decision": "rejected",
                "review_note": note,
            },
        )
        await self.store.append_event(
            task_id,
            "review_recorded",
            {
                "reviewer_id": reviewer_id,
                "decision": "rejected",
                "note": note,
            },
        )
        return review

    async def _get_session_context(self, conversation_id: str) -> dict | None:
        if self.session_snapshot_getter is None:
            return None
        snapshot = self.session_snapshot_getter(conversation_id)
        if hasattr(snapshot, "__await__"):
            snapshot = await snapshot
        if snapshot is None:
            return None
        if hasattr(snapshot, "to_dict"):
            return snapshot.to_dict()
        if isinstance(snapshot, dict):
            return snapshot
        return {"value": snapshot}

    async def _get_cognitive_context(
        self,
        request: HarnessTaskCreateRequest,
    ) -> dict | None:
        if self.cognitive_snapshot_getter is None:
            return None
        snapshot = self.cognitive_snapshot_getter(request)
        if hasattr(snapshot, "__await__"):
            snapshot = await snapshot
        if snapshot is None:
            return None
        if hasattr(snapshot, "to_dict"):
            return snapshot.to_dict()
        if isinstance(snapshot, dict):
            return snapshot
        return {"value": snapshot}

    async def _assert_completion_allowed(
        self,
        task_id: str,
        result: dict | None,
    ) -> None:
        existing_task = await self.store.get_task(task_id)
        if existing_task is None:
            raise LookupError(f"task {task_id!r} not found")
        await self._assert_transition_allowed(existing_task, "completed")
        await self._assert_default_review_gate_satisfied(existing_task)
        if existing_task.status == "review_required":
            await self._assert_approved_review_exists(existing_task)
        try:
            assert_completion_source_provenance(existing_task, result)
        except RuntimeError as exc:
            await self.store.append_event(
                task_id,
                "source_provenance_completion_blocked",
                {"reason": str(exc)},
            )
            raise
        await LoopOrchestrator(self.store).settle_for_completion(
            existing_task,
            result,
        )

    async def _transition_task_status(
        self,
        task_id: str,
        status: HarnessTaskStatus,
        *,
        result: dict | None = None,
        event_payload: dict | None = None,
    ) -> HarnessTask:
        existing_task = await self.store.get_task(task_id)
        if existing_task is None:
            raise LookupError(f"task {task_id!r} not found")
        await self._assert_transition_allowed(existing_task, status)
        updated = await self.store.update_task_status(
            task_id,
            status,
            result=result,
            event_payload=event_payload,
            expected_status=existing_task.status,
        )
        self._publish_pet_event(
            updated,
            self._event_type_for_status(status),
            payload=event_payload or result or {},
        )
        return updated

    def _publish_pet_event(
        self,
        task: HarnessTask,
        event_type: str,
        *,
        payload: dict | None = None,
    ) -> None:
        if self.pet_live_store is None:
            return
        pet_id = str(task.payload.get("pet_id") or "")
        employee_id = str(task.payload.get("employee_id") or "")
        user_id = str(
            employee_id
            or task.payload.get("user_id")
            or task.payload.get("employee_id")
            or task.payload.get("feishu_open_id")
            or task.session_id
            or ""
        )
        if not pet_id or not user_id:
            return
        try:
            from dc_engines.pet_live.event_bus import publish_pet_event
            from dc_engines.pet_live.integrations import pet_live_enabled

            if not pet_live_enabled():
                return

            publish_pet_event(
                self.pet_live_store,
                pet_id=pet_id,
                user_id=user_id,
                source="harness",
                event_type=event_type,
                source_ref={
                    "employee_id": employee_id,
                    "platform": task.platform_id,
                    "conversation_id": task.conversation_id,
                    "harness_task_id": task.task_id,
                },
                payload={
                    "employee_id": employee_id,
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status,
                    **(payload or {}),
                },
            )
        except Exception:
            return

    @staticmethod
    def _event_type_for_status(status: HarnessTaskStatus) -> str:
        return {
            "in_progress": "harness_task_in_progress",
            "review_required": "harness_task_review_required",
            "completed": "harness_task_completed",
            "failed": "harness_task_failed",
            "blocked": "harness_task_blocked",
            "cancelled": "harness_task_cancelled",
            "pending": "harness_task_pending",
        }.get(status, "harness_task_status_changed")

    async def _assert_transition_allowed(
        self,
        task: HarnessTask,
        next_status: HarnessTaskStatus,
    ) -> None:
        allowed = ALLOWED_HARNESS_STATUS_TRANSITIONS.get(task.status, set())
        if next_status in allowed:
            return
        reason = (
            f"illegal harness task status transition: {task.status} -> {next_status}"
        )
        await self.store.append_event(
            task.task_id,
            "status_transition_blocked",
            {
                "from_status": task.status,
                "to_status": next_status,
                "reason": reason,
            },
        )
        raise RuntimeError(reason)

    async def _assert_default_review_gate_satisfied(self, task: HarnessTask) -> None:
        if not task.payload.get("review_required_by_default"):
            return
        if task.status == "review_required":
            return
        reason = (
            "review_required_by_default task must enter review_required before "
            "completion"
        )
        await self.store.append_event(
            task.task_id,
            "review_default_completion_blocked",
            {"reason": reason, "status": task.status},
        )
        raise RuntimeError(reason)

    async def _assert_approved_review_exists(self, task: HarnessTask) -> None:
        reviews = await self.store.list_reviews(task.task_id)
        if any(review.decision == "approved" for review in reviews):
            return
        reason = "review_required task cannot complete without an approved review"
        await self.store.append_event(
            task.task_id,
            "review_completion_blocked",
            {"reason": reason},
        )
        raise RuntimeError(reason)

    async def _maybe_promote_memory(self, task: HarnessTask) -> None:
        if self.memory_promoter is None:
            return
        record = await self.memory_promoter.promote_from_task(task)
        if record is None:
            return
        await self.store.append_event(
            task.task_id,
            "memory_promoted",
            {
                "memory_id": record.memory_id,
                "memory_kind": record.memory_kind,
                "summary": record.summary,
            },
        )
