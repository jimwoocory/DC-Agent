"""Watchdog 状态 API plugin（A 步骤的后端 API；C 步骤会消费它做 dashboard 状态灯）。

暴露 ``/api/plug/watchdog/recent``：返最近 N 条告警 + 当前 service 状态 + 最新
诊断报告路径。前端 dashboard 可以 5s 轮询这个 endpoint 显示绿/红灯。

故意做最薄一层——它**不**负责探活（那是 scripts-watchdog/dc-watchdog.sh 的事，
由 cron 触发），只读 ``data/watchdog/`` 下 cron 写好的文件返 JSON。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


@register(
    "watchdog_status",
    "dc_agent",
    "Watchdog 状态查询 API（读 cron 写好的 alerts.jsonl）",
    "1.0.0",
)
class WatchdogStatusPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.watchdog_dir: Path = Path(
            cfg.get("watchdog_dir", "/Users/dianchi/DC-Agent/data/watchdog")
        )
        self.recent_limit: int = int(cfg.get("recent_limit", 50))

    async def initialize(self) -> None:
        try:
            self.context.register_web_api(
                "/watchdog/recent",
                self._api_recent,
                ["GET"],
                "最近 N 条 watchdog 告警 + 当前各 service 状态",
            )
            self.context.register_web_api(
                "/watchdog/incident/<incident_id>",
                self._api_incident,
                ["GET"],
                "单个 incident 的诊断报告（markdown）",
            )
            logger.info(
                "[watchdog_status] API 已注册：/api/plug/watchdog/recent + "
                "/api/plug/watchdog/incident/<id>"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[watchdog_status] 注册 API 失败：%s", exc)

    # ─────────────────────── API handlers ───────────────────────

    async def _api_recent(self, *args, **kwargs):
        alerts_path = self.watchdog_dir / "alerts.jsonl"
        state_path = self.watchdog_dir / "state.json"

        # 最近 N 条 alerts
        alerts: list[dict] = []
        if alerts_path.exists():
            try:
                lines = alerts_path.read_text(encoding="utf-8").strip().split("\n")
                for line in reversed(lines[-self.recent_limit :]):
                    if line.strip():
                        try:
                            alerts.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError as exc:
                logger.warning("[watchdog_status] 读 alerts 失败：%s", exc)

        # 当前 state
        state: dict = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("[watchdog_status] 读 state 失败：%s", exc)

        return {
            "status": "ok",
            "message": None,
            "data": {
                "alerts": alerts,
                "current_state": state,
                "total_alerts_returned": len(alerts),
            },
        }

    async def _api_incident(self, *args, incident_id: str = "", **kwargs):
        # 安全：incident_id 只允许 0-9 字符
        if not incident_id or not incident_id.replace("_", "").isalnum():
            return {"status": "error", "message": "invalid incident_id", "data": None}

        md = self.watchdog_dir / "incidents" / f"incident-{incident_id}.md"
        snapshot = self.watchdog_dir / "incidents" / f"incident-{incident_id}.json"
        if not md.exists():
            return {"status": "error", "message": "incident not found", "data": None}

        try:
            return {
                "status": "ok",
                "message": None,
                "data": {
                    "incident_id": incident_id,
                    "report_markdown": md.read_text(encoding="utf-8"),
                    "snapshot_json": (
                        json.loads(snapshot.read_text(encoding="utf-8"))
                        if snapshot.exists()
                        else None
                    ),
                },
            }
        except (OSError, json.JSONDecodeError) as exc:
            return {"status": "error", "message": str(exc), "data": None}
