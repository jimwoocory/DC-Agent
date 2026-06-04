"""巅池-技术 日报 plugin —— 只暴露 API 给 dashboard 看历史。

真正的工作（夜间 agy 跑完整链路、早上推送）是 cron 脚本干的，见：
    scripts-tools/dianchi-tech-night.sh    # 01:00 北京 = 美西 10:00
    scripts-tools/dianchi-tech-report.sh   # 09:30 北京 推飞书 + wiki

这里的 plugin 只做一件事：暴露 ``/api/plug/dianchi_tech/recent`` 让 dashboard
能列出最近 N 天的报告元信息 + 推送状态。同款范式见 watchdog_status plugin。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star, register


@register(
    "dianchi_tech",
    "dc_agent",
    "巅池-技术 日报（agy 爬硅谷 AI 资讯 + agy 学习/巡检 + 飞书私聊+wiki）",
    "0.1.0",
)
class DianchiTechPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.data_root: Path = Path(
            cfg.get("data_root", "/Users/dianchi/DC-Agent/data/dianchi_tech")
        )
        self.recent_limit: int = int(cfg.get("recent_limit", 14))
        self.cai_ting_open_id: str = str(cfg.get("cai_ting_open_id", "") or "").strip()
        self.wiki_space_name: str = str(cfg.get("wiki_space_name", "DC-Agent 运维"))

        # 不再回写 dianchi_tech_config.json —— 之前每次 plugin reload 都用
        # dashboard 里空的 open_id 覆盖文件里手工配的 union_id，导致 reporter 失败。
        # 文件由人工维护（含 cai_ting_union_id），plugin 只读。

    async def initialize(self) -> None:
        try:
            self.context.register_web_api(
                "/dianchi_tech/recent",
                self._api_recent,
                ["GET"],
                "最近 N 天的『巅池-技术』日报元信息 + 推送状态",
            )
            self.context.register_web_api(
                "/dianchi_tech/report/<date>",
                self._api_report,
                ["GET"],
                "单日 report.md 全文",
            )
            self.context.register_web_api(
                "/dianchi_tech/health",
                self._api_health,
                ["GET"],
                "plugin 自检（看门狗用）",
            )
            logger.info(
                "[dianchi_tech] API 注册完成：/api/plug/dianchi_tech/{recent,report/<date>,health}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dianchi_tech] 注册 API 失败：%s", exc)

    # ─────────────────────── API handlers ───────────────────────

    async def _api_recent(self, *args, **kwargs):
        """返最近 N 天的报告 metadata（不返 markdown 全文，前端按需拉 /report/<date>）。"""
        if not self.data_root.exists():
            return {"status": "ok", "message": None, "data": {"days": []}}

        # 列日期目录（YYYY-MM-DD），倒序取前 N
        day_dirs = sorted(
            [d for d in self.data_root.iterdir() if d.is_dir() and len(d.name) == 10],
            reverse=True,
        )[: self.recent_limit]

        days = []
        for d in day_dirs:
            entry = {
                "date": d.name,
                "has_raw_news": (d / "raw_news.md").exists(),
                "has_report": (d / "report.md").exists()
                and (d / "report.md").stat().st_size > 0,
                "report_bytes": (
                    (d / "report.md").stat().st_size
                    if (d / "report.md").exists()
                    else 0
                ),
            }
            for meta_name in ("run.json", "delivery.json"):
                p = d / meta_name
                if p.exists():
                    try:
                        entry[meta_name.replace(".json", "")] = json.loads(
                            p.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        pass
            days.append(entry)

        return {
            "status": "ok",
            "message": None,
            "data": {
                "days": days,
                "data_root": str(self.data_root),
                "wiki_space_name": self.wiki_space_name,
                "cai_ting_configured": bool(self.cai_ting_open_id),
            },
        }

    async def _api_report(self, *args, date: str = "", **kwargs):
        if not date or len(date) != 10 or date.count("-") != 2:
            return {
                "status": "error",
                "message": "invalid date (YYYY-MM-DD)",
                "data": None,
            }
        # 防路径穿越
        if any(c in date for c in ("/", "\\", "..")):
            return {"status": "error", "message": "invalid date chars", "data": None}

        report = self.data_root / date / "report.md"
        if not report.exists():
            return {"status": "error", "message": "report not found", "data": None}
        try:
            return {
                "status": "ok",
                "message": None,
                "data": {
                    "date": date,
                    "markdown": report.read_text(encoding="utf-8"),
                },
            }
        except OSError as exc:
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_health(self, *args, **kwargs):
        """看门狗探活用：检查最近一次跑得是否成功。"""
        if not self.data_root.exists():
            return {"status": "ok", "message": "no data yet", "data": {"healthy": True}}

        day_dirs = sorted(
            [d for d in self.data_root.iterdir() if d.is_dir() and len(d.name) == 10],
            reverse=True,
        )[:3]
        latest_report_date: str | None = None
        for d in day_dirs:
            if (d / "report.md").exists() and (d / "report.md").stat().st_size > 0:
                latest_report_date = d.name
                break

        return {
            "status": "ok",
            "message": None,
            "data": {
                "healthy": latest_report_date is not None,
                "latest_report_date": latest_report_date,
                "cai_ting_configured": bool(self.cai_ting_open_id),
            },
        }
