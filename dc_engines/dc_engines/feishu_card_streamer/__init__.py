"""飞书卡片流式引擎 · 让长任务有真实进度反馈，让日常对话有结构化卡片渲染。

核心能力：
1. **方案 C 进度卡片** —— 长任务（Hermes 深度分析）发卡片 → 后台定时
   update 进度 → 完成时整张卡片换成结果。解决"等待心里空荡荡"。
2. **级别 3 渲染卡片** —— 日常对话超过一定长度/含 markdown 结构时，
   把 LLM 输出渲染成飞书 interactive card（带 emoji 标题 + 分隔线 +
   多色 callout），比纯文本 markdown 更精致。
3. **emoji 反应** —— 给用户消息贴 👀 / 🔄 / ✅ / ❌ 三态反馈，
   极轻量但直观。

设计原则（为引擎化做好）：
- **不依赖宿主**：不 import astrbot.*、不 import hermes_*
- **lark client 外部注入**：调用方建好 lark.Client 传进来
- **接口简单**：start / update / finalize / react 四个方法搞定

调用方：
- AstrBot plugin `hermes_escalation_plugin` —— 派发 hermes 任务时开卡片
- AstrBot plugin `hermes_bridge` —— Hermes 中间状态/最终结果时 update
- AstrBot plugin `daily_card_renderer` —— 日常对话渲染成卡片
- 5/25 后 Hermes Agent 自身也可调（pip install dc_engines 后）

TODO：
- 进度条 emoji 用 ▰▱ 切换，未来改成 Unicode 实心条 + ASCII art
- 多卡片并发支持（一个 chat 多任务）
"""

from dataclasses import dataclass
from typing import Any

from .streamer import CardStream, FeishuCardStreamer
from .templates import (
    QUIZ_QUESTIONS,
    QUIZ_QUESTIONS_BY_DEPT,
    TUTORIALS,
    build_antigravity_queue_card,
    build_boss_quicklook_card,
    build_case_overview_card,
    build_casual_response_card,
    build_copy_draft_card,
    build_daily_response_card,
    build_deleted_skill_list_card,
    build_devops_status_card,
    build_email_draft_card,
    build_employee_pending_card,
    build_error_card,
    build_final_card,
    build_kb_archive_card,
    build_media_generation_card,
    build_multimodal_understanding_card,
    build_onboarding_dept_card,
    build_onboarding_name_prompt_card,
    build_onboarding_role_card,
    build_onboarding_tutorial_list_card,
    build_progress_card,
    build_quiz_feedback_card,
    build_quiz_question_card,
    build_quiz_result_card,
    build_skill_confirm_card,
    build_skill_detail_card,
    build_skill_list_card,
    build_skill_review_card,
    build_source_trace_card,
    build_task_reminder_card,
    build_thinking_card,
    build_truth_intake_received_card,
    build_truth_intake_request_card,
    build_tutorial_lesson_card,
    get_dept_lesson_body,
    get_quiz_for_dept,
    next_lesson_id,
    progress_bar,
    smart_thinking_hint,
    waiting_pulse,
    waiting_track,
)


@dataclass(slots=True)
class WaitingCardHandle:
    streamer: FeishuCardStreamer
    message_id: str
    chat_id: str
    receive_id_type: str
    title: str
    brief: str
    reasoning_tier: str | None = None
    current_stage: str | None = None
    queue_position: int | None = None
    eta_text: str | None = None

    def build_card(self, elapsed_sec: float = 0) -> dict[str, Any]:
        return build_progress_card(
            title=self.title,
            brief=self.brief,
            elapsed_sec=elapsed_sec,
            reasoning_tier=self.reasoning_tier,
            current_stage=self.current_stage,
            queue_position=self.queue_position,
            eta_text=self.eta_text,
        )

    async def update_stage(self, stage: str | None) -> bool:
        self.current_stage = stage
        stream = self.streamer.get_stream(self.message_id)
        elapsed_sec = stream.elapsed_sec if stream else 0
        return await self.streamer.update(self.message_id, self.build_card(elapsed_sec))


def ensure_streamers_on_context(context) -> dict:
    """从 AstrBot context 拿（或 lazy 创建）feishu_streamers。

    适用场景：plugin initialize 时 lark platform 还没加载完，所以
    `context.feishu_streamers` 可能为空。第一次实际使用时调本函数
    遍历 platform_insts 建好 streamers，挂回 context 共享。

    Returns:
        dict[platform_id, FeishuCardStreamer]，找不到 lark 平台时返空 dict
    """
    existing = getattr(context, "feishu_streamers", None)
    streamers: dict = existing if isinstance(existing, dict) else {}
    try:
        platform_insts = getattr(context.platform_manager, "platform_insts", None) or []
        for inst in platform_insts:
            if not hasattr(inst, "lark_api") or inst.lark_api is None:
                continue
            cfg = getattr(inst, "config", None) or {}
            pid = cfg.get("id", "") if isinstance(cfg, dict) else ""
            if pid and pid not in streamers:
                streamers[pid] = FeishuCardStreamer(inst.lark_api)
    except Exception:
        pass
    context.feishu_streamers = streamers  # type: ignore[attr-defined]
    return streamers


def extract_chat_info_from_event(event) -> tuple[str, str]:
    raw_msg = getattr(getattr(event, "message_obj", None), "raw_message", None)
    chat_id = getattr(raw_msg, "chat_id", None) or ""
    if not chat_id:
        get_group_id = getattr(event, "get_group_id", None)
        get_sender_id = getattr(event, "get_sender_id", None)
        group_id = get_group_id() if callable(get_group_id) else ""
        sender_id = get_sender_id() if callable(get_sender_id) else ""
        chat_id = group_id or sender_id or ""
    receive_id_type = "chat_id" if str(chat_id).startswith("oc_") else "open_id"
    return str(chat_id), receive_id_type


async def start_waiting_card_for_event(
    context,
    event,
    *,
    title: str,
    brief: str | None = None,
    reasoning_tier: str | None = None,
    current_stage: str | None = None,
    queue_position: int | None = None,
    eta_text: str | None = None,
    interval_sec: float = 10.0,
) -> WaitingCardHandle | None:
    platform_id = ""
    get_platform_id = getattr(event, "get_platform_id", None)
    if callable(get_platform_id):
        platform_id = get_platform_id() or ""

    streamer = ensure_streamers_on_context(context).get(platform_id)
    if streamer is None:
        return None

    chat_id, receive_id_type = extract_chat_info_from_event(event)
    if not chat_id:
        return None

    fallback_brief = title
    message_str = getattr(event, "message_str", "") or ""
    if message_str.strip():
        fallback_brief = message_str.strip()[:200]
    handle = WaitingCardHandle(
        streamer=streamer,
        message_id="",
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        title=title,
        brief=(brief or fallback_brief)[:200],
        reasoning_tier=reasoning_tier,
        current_stage=current_stage,
        queue_position=queue_position,
        eta_text=eta_text,
    )

    from dc_engines.card_runtime import send_card_via_runtime

    stream = await send_card_via_runtime(
        streamer,
        card_type="thinking_waiting",
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        card=handle.build_card(0),
        platform_id=getattr(event, "get_platform_id", lambda: "")() or "",
        event="start",
        detail="waiting card helper",
    )
    if stream is None:
        return None

    handle.message_id = stream.message_id

    def _builder(s):
        return handle.build_card(s.elapsed_sec)

    streamer.start_auto_update(stream.message_id, _builder, interval_sec=interval_sec)
    return handle


__all__ = [
    "FeishuCardStreamer",
    "CardStream",
    "WaitingCardHandle",
    "build_antigravity_queue_card",
    "build_progress_card",
    "build_final_card",
    "build_error_card",
    "build_daily_response_card",
    "build_thinking_card",
    "build_truth_intake_request_card",
    "build_truth_intake_received_card",
    "build_source_trace_card",
    "build_devops_status_card",
    "build_kb_archive_card",
    "build_employee_pending_card",
    "build_media_generation_card",
    "build_multimodal_understanding_card",
    "build_case_overview_card",
    "build_casual_response_card",
    "build_task_reminder_card",
    "build_deleted_skill_list_card",
    "build_email_draft_card",
    "build_copy_draft_card",
    "build_boss_quicklook_card",
    "build_onboarding_dept_card",
    "build_onboarding_role_card",
    "build_onboarding_name_prompt_card",
    "build_onboarding_tutorial_list_card",
    "build_tutorial_lesson_card",
    "build_quiz_question_card",
    "build_quiz_feedback_card",
    "build_quiz_result_card",
    "build_skill_confirm_card",
    "build_skill_detail_card",
    "build_skill_list_card",
    "build_skill_review_card",
    "progress_bar",
    "smart_thinking_hint",
    "waiting_pulse",
    "waiting_track",
    "ensure_streamers_on_context",
    "extract_chat_info_from_event",
    "start_waiting_card_for_event",
    "QUIZ_QUESTIONS",
    "QUIZ_QUESTIONS_BY_DEPT",
    "get_dept_lesson_body",
    "get_quiz_for_dept",
    "next_lesson_id",
    "TUTORIALS",
]
