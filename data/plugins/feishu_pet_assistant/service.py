"""业务层：宠物状态 + 任务流转。

handler 不直接调 store。所有"能量怎么变 / 第一次访问要不要种 demo 任务"
之类的规则都在这里，便于以后接真实任务源（飞书多维表 / Harness）时只换一层。
"""

from __future__ import annotations

import logging
from typing import Any

from .store import PetStore

logger = logging.getLogger("feishu_pet_assistant.service")

# 第一次访问的 demo 任务种子。换成真实业务时这里替换成接入逻辑即可。
DEMO_TASKS: list[str] = [
    "跟进 A 客户报价",
    "更新 B 项目进度",
    "确认 C 审批",
]

ENERGY_PER_TASK = 10
ENERGY_MAX = 100


class PetService:
    def __init__(self, store: PetStore) -> None:
        self._store = store

    # ── 宠物 ──────────────────────────────────────────────────────────────

    def get_or_create_pet(self, user_id: str) -> dict[str, Any]:
        """返回宠物记录。第一次访问的 user 顺手种 demo 任务。"""
        pet = self._store.get_pet(user_id)
        if pet is None:
            pet = self._store.create_pet(user_id)
            self._seed_demo_tasks(user_id)
            self._store.log_event(user_id, "pet_created", {"user_id": user_id})
        return pet

    def _seed_demo_tasks(self, user_id: str) -> None:
        for title in DEMO_TASKS:
            self._store.insert_task(user_id, title, source="demo")
        logger.info(
            "[PetService] seeded %d demo tasks for %s", len(DEMO_TASKS), user_id[:8]
        )

    # ── 任务 ──────────────────────────────────────────────────────────────

    def list_today_tasks(self, user_id: str) -> list[dict[str, Any]]:
        """第一版"今日"=该 user 的全部 pending；保留 due_date 过滤口子。"""
        return self._store.list_tasks(user_id, status="pending", limit=20)

    def list_all_tasks(self, user_id: str) -> list[dict[str, Any]]:
        return self._store.list_tasks(user_id, limit=50)

    def complete_task(
        self, user_id: str, task_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """完成指定任务，返回 (pet, task)；任务不属于该 user 或已完成返回 None。"""
        task = self._store.get_task(task_id)
        if task is None or task["user_id"] != user_id:
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
        pending = self._store.list_tasks(user_id, status="pending", limit=1)
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
        counts = self._store.count_by_status(user_id)
        return {
            "pending": counts.get("pending", 0),
            "done": counts.get("completed", 0),
        }
