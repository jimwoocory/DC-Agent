"""Harness 状态约束注入器主体。

关键钩子：`@filter.on_llm_request()` —— 在 LLM 真正调用前注入
active task 状态 + 反幻觉硬约束到 system_prompt。
"""

from __future__ import annotations

from pathlib import Path

from dc_engines.harness import HARNESS_TRUTH_GUARD

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest

# 注入条数上限（避免一个 session 几十个旧 task 把 context 撑爆）
MAX_TASKS_INJECT = 5
TRUTH_GUARD_PLATFORMS = {"巅池-Agent小助手", "巅池-技术（DevOps）", "巅池-技术"}

# Active 状态（非终态）
ACTIVE_STATUSES = ("pending", "in_progress", "blocked", "review_required")

# 注入到 system_prompt 的硬约束文本
HARD_GUARD_PREFIX = "\n\n## Harness 任务状态约束（必须遵守）\n"
HARD_GUARD_RULES = """
铁律（违反会被用户当作欺骗）：
- 上面列出的 task 是 Harness 真实在跑的，**不要假装"已完成""已分析"\
"已起草"**。
- 用户问『xxx 做完了吗？』时，**必须如实**答状态（如"还在 in_progress, \
Hermes 处理中"），不要编。
- task 状态是 **failed** 时，必须告知用户失败 + 建议重试，**不要**编"已完成"。
- 如果没有任何 active task，本约束自动失效，你正常对话即可。
- 若用户的请求需要 Harness 处理但目前没有 task，告诉用户「我会安排 \
Harness 处理」而不是直接假装做了。
"""

TRUTH_INTAKE_SOURCE = "llm_router_truth_intake"


@register(
    "harness_state_injector",
    "dc_agent",
    "Harness 任务状态硬约束注入（防止 LLM 假装『已分析』『已完成』）",
    "1.0.0",
)
class HarnessStateInjectorPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    def _format_task_line(self, t) -> str:
        """把 HarnessTask 渲染成一行人话摘要给 LLM 看。"""
        # 拿 payload 里有用的字段（workflow_kind/brief 通常在 payload 里）
        payload = getattr(t, "payload", {}) or {}
        wf = payload.get("workflow_kind") or t.domain or "general"
        brief = (payload.get("brief") or t.title or "").strip()
        if len(brief) > 80:
            brief = brief[:80] + "…"
        line = (
            f"- task_id={t.task_id[:8]}  status={t.status}  "
            f"workflow={wf}  created={t.created_at}\n"
            f"  brief: {brief}"
        )
        if payload.get("source") != TRUTH_INTAKE_SOURCE:
            return line

        archive_dir = str(payload.get("archive_dir") or "").strip()
        attachments = payload.get("attachments") or []
        material_lines: list[str] = []
        if archive_dir:
            material_lines.append(f"  source_archive_dir: {archive_dir}")
        for item in attachments[:5]:
            stored_path = str(item.get("stored_path") or "").strip()
            original_name = str(item.get("original_name") or "").strip()
            if not stored_path:
                continue
            suffix = Path(stored_path).suffix or "file"
            label = original_name or Path(stored_path).name
            material_lines.append(
                f"  source_attachment: {label} ({suffix}) -> {stored_path}"
            )
        if material_lines:
            material_lines.append(
                "  source_rule: 优先读取上述 source_attachment / source_archive_dir；"
                "不要把 data/temp 里的历史导入、旧日志或无关索引当作本次素材。"
            )
            line += "\n" + "\n".join(material_lines)
        return line

    @filter.on_llm_request()
    async def inject_active_tasks(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """LLM 调用前注入：当前 session 的 active Harness task 状态。"""
        text = (event.message_str or "").strip()
        platform_id = ""
        try:
            platform_id = event.get_platform_id() or ""
        except Exception:  # noqa: BLE001
            platform_id = ""

        original = req.system_prompt or ""
        if platform_id in TRUTH_GUARD_PLATFORMS:
            original += HARNESS_TRUTH_GUARD.rstrip()

        store = getattr(self.context, "harness_store", None)
        if store is None:
            req.system_prompt = original
            return  # Harness 没装载（可能本机环境精简）—— 静默跳过

        umo = event.unified_msg_origin
        if not umo:
            req.system_prompt = original
            return

        # Explicit prefix commands start new work. Keep the universal truth guard,
        # but avoid injecting stale active tasks that could distract the router.
        if text.startswith("#"):
            req.system_prompt = original
            return

        try:
            active = await store.list_tasks_for_session(
                umo,
                limit=MAX_TASKS_INJECT,
                statuses=ACTIVE_STATUSES,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[harness_state_injector] list_tasks_for_session 失败：%s", exc
            )
            req.system_prompt = original
            return

        if not active:
            req.system_prompt = original
            return  # 没 task —— 不污染 system_prompt

        task_lines = "\n".join(self._format_task_line(t) for t in active)
        injection = HARD_GUARD_PREFIX + task_lines + "\n" + HARD_GUARD_RULES.rstrip()

        # 拼到 system_prompt 尾部（保留原 persona prompt）
        req.system_prompt = original + injection

        logger.info(
            "[harness_state_injector] 注入 %d 个 active task 约束 → umo=%s",
            len(active),
            umo[:80],
        )
