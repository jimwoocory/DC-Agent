from __future__ import annotations

from collections.abc import Awaitable, Callable

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
from .task_store import HarnessTaskStore


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
    ) -> None:
        self.store = store
        self.session_snapshot_getter = session_snapshot_getter
        self.cognitive_snapshot_getter = cognitive_snapshot_getter
        self.memory_promoter = memory_promoter

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
        return task

    async def mark_in_progress(
        self,
        task_id: str,
        *,
        note: str | None = None,
    ) -> HarnessTask:
        return await self.store.update_task_status(
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
        task = await self._require_task(task_id)
        await LoopOrchestrator(self.store).settle_for_review_required(
            task,
            review_reason=reviewer_note or "manual review required",
            summary=reviewer_note or "Task requires review before completion.",
            result=result,
            metadata={"source": "HarnessEngine.mark_review_required"},
        )
        return await self.store.update_task_status(
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
        task = await self._require_task(task_id)
        await self._assert_completion_review_gate(task)
        await LoopOrchestrator(self.store).settle_for_completion(task, result)
        task = await self.store.update_task_status(
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
        return await self.store.update_task_status(
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
        task = await self._require_task(task_id)
        if status == "completed":
            await self._assert_completion_review_gate(task)
            await LoopOrchestrator(self.store).settle_for_completion(task, result)
        elif status == "blocked":
            blocking_reason = ""
            if event_payload:
                blocking_reason = str(
                    event_payload.get("blocking_reason")
                    or event_payload.get("reason")
                    or ""
                )
            await LoopOrchestrator(self.store).settle_for_blocked(
                task,
                blocking_reason=blocking_reason,
                summary=blocking_reason or "Task blocked.",
                metadata=event_payload or {},
            )
        elif status == "review_required":
            review_reason = ""
            if event_payload:
                review_reason = str(
                    event_payload.get("review_reason")
                    or event_payload.get("reviewer_note")
                    or event_payload.get("reason")
                    or ""
                )
            await LoopOrchestrator(self.store).settle_for_review_required(
                task,
                review_reason=review_reason or "review required",
                summary=review_reason or "Task requires review before completion.",
                result=result,
                metadata=event_payload or {},
            )
        return await self.store.update_task_status(
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

        review = await self.store.create_review(
            task_id,
            reviewer_id,
            "rejected",
            note,
        )
        await self.store.update_task_status(
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

    async def _require_task(self, task_id: str) -> HarnessTask:
        task = await self.store.get_task(task_id)
        if task is None:
            raise LookupError(f"task {task_id!r} not found")
        return task

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

    async def _assert_completion_review_gate(self, task: HarnessTask) -> None:
        if (
            task.payload.get("review_required_by_default")
            and task.status != "review_required"
        ):
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
        if task.status != "review_required":
            return
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
