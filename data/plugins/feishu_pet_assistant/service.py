"""业务层：宠物状态 + 任务流转。

handler 不直接调 store。所有"能量怎么变 / 任务源是否可展示"
之类的规则都在这里，便于以后接真实任务源（飞书多维表 / Harness）时只换一层。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .store import PetStore

logger = logging.getLogger("feishu_pet_assistant.service")

ENERGY_PER_TASK = 10
ENERGY_MAX = 100
_BLOCKED_NON_PRODUCTION_SOURCES = {
    "demo",
    "fake",
    "mock",
    "sample",
    "placeholder",
    "stub",
}
_NON_PRODUCTION_SOURCE_RE = re.compile(
    r"(^|[^a-z0-9])(demo|fake|mock|sample|placeholder|stub)([^a-z0-9]|$)"
)


class PetService:
    def __init__(self, store: PetStore) -> None:
        self._store = store

    # ── 宠物 ──────────────────────────────────────────────────────────────

    def get_or_create_pet(self, user_id: str) -> dict[str, Any]:
        """返回宠物记录。新用户不会自动获得任务；任务必须来自真实来源。"""
        pet = self._store.get_pet(user_id)
        if pet is None:
            pet = self._store.create_pet(user_id)
            self._store.log_event(user_id, "pet_created", {"user_id": user_id})
        return pet

    # ── 任务 ──────────────────────────────────────────────────────────────

    def list_today_tasks(self, user_id: str) -> list[dict[str, Any]]:
        """第一版"今日"=该 user 的全部 pending；保留 due_date 过滤口子。"""
        return self._filter_real_tasks(
            self._store.list_tasks(user_id, status="pending", limit=50)
        )[:20]

    def list_all_tasks(self, user_id: str) -> list[dict[str, Any]]:
        return self._filter_real_tasks(self._store.list_tasks(user_id, limit=50))

    def complete_task(
        self, user_id: str, task_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """完成指定任务，返回 (pet, task)；任务不属于该 user 或已完成返回 None。"""
        task = self._store.get_task(task_id)
        if task is None or task["user_id"] != user_id:
            return None
        if not self._has_real_task_source(task):
            logger.warning(
                "[PetService] blocked non-production task source for user=%s task=%s",
                user_id[:8],
                task_id[:8],
            )
            return None
        if task["status"] == "completed":
            # 已经完成的不重复加能量，但仍返回当前宠物状态便于反馈卡
            pet = self._store.get_pet(user_id) or self.get_or_create_pet(user_id)
            return pet, task

        updated_task = self._store.mark_task_done(task_id)
        pet = self._apply_task_completion_reward(user_id)
        self._store.log_event(
            user_id,
            "task_completed",
            {"task_id": task_id, "title": task["title"]},
        )
        return pet, updated_task or task

    def complete_first_pending(
        self, user_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """完成该 user 的第一条 pending；没有 pending 返回 None。"""
        pending = self.list_today_tasks(user_id)
        if not pending:
            return None
        return self.complete_task(user_id, pending[0]["id"])

    # ── 能量 / 心情 ───────────────────────────────────────────────────────

    def _apply_task_completion_reward(self, user_id: str) -> dict[str, Any]:
        pet = self._store.get_pet(user_id)
        if pet is None:
            pet = self.get_or_create_pet(user_id)
        new_energy = min(ENERGY_MAX, int(pet["energy"]) + ENERGY_PER_TASK)
        new_mood = self._mood_for_energy(new_energy)
        updated = self._store.update_pet(user_id, energy=new_energy, mood=new_mood)
        return updated or pet

    def feed_pet(self, user_id: str, amount: int = ENERGY_PER_TASK) -> dict[str, Any]:
        """预留接口：纯加能量（未来"喂食"动作用）。"""
        pet = self.get_or_create_pet(user_id)
        new_energy = min(ENERGY_MAX, int(pet["energy"]) + amount)
        new_mood = self._mood_for_energy(new_energy)
        updated = self._store.update_pet(user_id, energy=new_energy, mood=new_mood)
        self._store.log_event(user_id, "pet_fed", {"amount": amount})
        return updated or pet

    @staticmethod
    def _mood_for_energy(energy: int) -> str:
        if energy >= 90:
            return "心满意足"
        if energy >= 70:
            return "精神不错"
        if energy >= 40:
            return "还行"
        return "有点蔫"

    # ── 聚合给卡片 ────────────────────────────────────────────────────────

    def build_stats(self, user_id: str) -> dict[str, int]:
        """卡片要的数字：pending / done。"""
        counts: dict[str, int] = {}
        for task in self.list_all_tasks(user_id):
            status = str(task.get("status", ""))
            counts[status] = counts.get(status, 0) + 1
        return {
            "pending": counts.get("pending", 0),
            "done": counts.get("completed", 0),
        }

    @staticmethod
    def _has_real_task_source(task: dict[str, Any]) -> bool:
        source = str(task.get("source") or "").strip().lower()
        return (
            source not in _BLOCKED_NON_PRODUCTION_SOURCES
            and _NON_PRODUCTION_SOURCE_RE.search(source) is None
        )

    @classmethod
    def _filter_real_tasks(cls, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [task for task in tasks if cls._has_real_task_source(task)]
