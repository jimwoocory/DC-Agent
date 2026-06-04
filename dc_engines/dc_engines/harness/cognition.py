from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from astrbot.api import sp

from .contracts import HarnessTaskCreateRequest

if TYPE_CHECKING:
    from astrbot.core.knowledge_base.kb_mgr import KnowledgeBaseManager
    from astrbot.core.persona_mgr import PersonaManager

    from .memory_store import HarnessMemoryStore
    from .task_store import HarnessTaskStore


@dataclass(slots=True)
class HarnessCognitiveSnapshot:
    session_id: str
    conversation_id: str
    persona_id: str | None
    persona_name: str | None
    knowledge_base_names: list[str]
    recent_task_summaries: list[dict[str, Any]]
    recent_memories: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "knowledge_base_names": list(self.knowledge_base_names),
            "recent_task_summaries": list(self.recent_task_summaries),
            "recent_memories": list(self.recent_memories),
        }


class HarnessCognitionProvider:
    """Resolve the first reusable cognitive snapshot for Harness.

    This provider intentionally starts from existing JW-Bot runtime state:

    - selected persona
    - configured knowledge bases
    - recent completed tasks for the same session

    Later cross-session memory can extend this contract instead of replacing it.
    """

    def __init__(
        self,
        *,
        persona_manager: PersonaManager,
        kb_manager: KnowledgeBaseManager,
        harness_store: HarnessTaskStore,
        memory_store: HarnessMemoryStore | None = None,
    ) -> None:
        self.persona_manager = persona_manager
        self.kb_manager = kb_manager
        self.harness_store = harness_store
        self.memory_store = memory_store

    async def build_snapshot(
        self,
        request: HarnessTaskCreateRequest,
    ) -> HarnessCognitiveSnapshot:
        session_service_config = (
            await sp.get_async(
                scope="umo",
                scope_id=request.session_id,
                key="session_service_config",
                default={},
            )
            or {}
        )
        kb_config = (
            await sp.session_get(request.session_id, "kb_config", default={}) or {}
        )

        persona_id = session_service_config.get("persona_id")
        persona = self.persona_manager.get_persona_v3_by_id(persona_id)

        knowledge_base_names: list[str] = []
        kb_ids = kb_config.get("kb_ids", []) or []
        for kb_id in kb_ids:
            kb_helper = await self.kb_manager.get_kb(kb_id)
            if kb_helper is not None:
                knowledge_base_names.append(kb_helper.kb.kb_name)

        recent_tasks = await self.harness_store.list_tasks_for_session(
            request.session_id,
            limit=5,
            statuses=("completed", "review_required", "blocked"),
        )
        recent_task_summaries = []
        for task in recent_tasks:
            if task.task_id == request.payload.get("task_id"):
                continue
            recent_task_summaries.append(
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "status": task.status,
                    "domain": task.domain,
                    "summary": str(task.result.get("summary", ""))[:160],
                }
            )

        recent_memories: list[dict[str, Any]] = []
        if self.memory_store is not None:
            memories = await self.memory_store.list_for_session(
                request.session_id, limit=5
            )
            for memory in memories:
                recent_memories.append(
                    {
                        "memory_id": memory.memory_id,
                        "task_id": memory.task_id,
                        "title": memory.title,
                        "summary": memory.summary,
                        "memory_kind": memory.memory_kind,
                    }
                )

        return HarnessCognitiveSnapshot(
            session_id=request.session_id,
            conversation_id=request.conversation_id,
            persona_id=persona_id,
            persona_name=persona["name"] if persona else None,
            knowledge_base_names=knowledge_base_names,
            recent_task_summaries=recent_task_summaries,
            recent_memories=recent_memories,
        )
