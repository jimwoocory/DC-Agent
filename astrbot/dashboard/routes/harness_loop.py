"""Harness Loop Engineering timeline dashboard routes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from dc_engines.harness import HarnessTaskStatus, HarnessTaskStore

from astrbot.dashboard.asgi_runtime import request

from .route import Response, Route, RouteContext

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VALID_STATUSES = {
    "pending",
    "in_progress",
    "blocked",
    "review_required",
    "completed",
    "cancelled",
    "failed",
}


class HarnessLoopRoute(Route):
    def __init__(self, context: RouteContext, dc_root: Path | None = None) -> None:
        super().__init__(context)
        self.dc_root = dc_root or PROJECT_ROOT
        self.routes = {
            "/harness-loop/tasks": ("GET", self.tasks),
            "/harness-loop/tasks/<task_id>/timeline": ("GET", self.timeline),
        }
        self.register_routes()

    async def tasks(self):
        store = self._store()
        if not Path(store.db_path).exists():
            return Response().ok({"tasks": [], "store_exists": False}).__dict__
        status = str(request.args.get("status") or "").strip()
        statuses: tuple[HarnessTaskStatus, ...] | None = None
        if status:
            if status not in VALID_STATUSES:
                return Response().error(f"invalid status: {status}").__dict__
            statuses = (status,)  # type: ignore[assignment]
        limit = _bounded_int(
            request.args.get("limit"), default=50, minimum=1, maximum=200
        )
        tasks = await store.list_tasks(limit=limit, statuses=statuses)
        return (
            Response()
            .ok(
                {
                    "tasks": [_task_to_dict(task) for task in tasks],
                    "store_exists": True,
                }
            )
            .__dict__
        )

    async def timeline(self, task_id: str):
        store = self._store()
        if not Path(store.db_path).exists():
            return Response().error("harness database not found").__dict__
        task = await store.get_task(task_id)
        if task is None:
            return Response().error(f"task not found: {task_id}").__dict__
        events = await store.list_events(task_id)
        return (
            Response()
            .ok(
                {
                    "task": _task_to_dict(task),
                    "timeline": [_event_to_dict(event) for event in events],
                }
            )
            .__dict__
        )

    def _store(self) -> HarnessTaskStore:
        return HarnessTaskStore(self.dc_root / "data" / "harness.db")


def _task_to_dict(task) -> dict:
    return asdict(task)


def _event_to_dict(event) -> dict:
    return asdict(event)


def _bounded_int(
    raw: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))
