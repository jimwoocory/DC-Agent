"""部门培训小测试自动批改。

员工在群里按培训卡格式回复：

    部门：市场部；答案：1B 2C 3B 4A 5A；一句话反馈：我最想用它帮我 ...

插件识别完整 5 题答案后自动返回分数、是否通过、错题提示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dc_engines.card_runtime import send_card_via_runtime
from dc_engines.feishu_card_streamer import ensure_streamers_on_context

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CARD_DIR = PROJECT_ROOT / "docs" / "ai_assistant_invite"
TRAINING_CARD_FILE = "飞书卡片_部门培训.json"
QUIZ_CARD_FILE = "飞书卡片_部门测试任务.json"

ANSWER_KEY: dict[int, str] = {
    1: "B",
    2: "C",
    3: "B",
    4: "A",
    5: "A",
}

QUESTION_HINTS: dict[int, str] = {
    1: "群里不要发送客户名单、合同金额、员工隐私、财务明细。",
    2: "好问题要说清任务、对象、语气、限制和输出格式。",
    3: "涉及公司真实信息时，请提供原文、截图、文件或飞书文档链接。",
    4: "部门题要选本部门最适合先落地的小助手场景。",
    5: "对外内容必须先人工复核事实、语气和敏感信息。",
}

SAFETY_CRITICAL_QUESTIONS = {1, 3, 5}
_LETTER_RE = re.compile(r"\b([1-5])\s*[\.:：、-]?\s*([A-Da-d])\b")
_DEPT_RE = re.compile(r"部门\s*[:：]\s*([^;；\n，, ]+)")
_FEEDBACK_RE = re.compile(r"(?:一句话反馈|反馈)\s*[:：]\s*(.+)", re.S)
_ACTIVATION_ALIASES = {
    "培训卡片": "training",
    "发培训卡片": "training",
    "部门培训卡片": "training",
    "发部门培训卡片": "training",
    "全部培训卡片": "all",
    "发全部培训卡片": "all",
    "培训和小测试": "all",
    "发培训和小测试": "all",
    "培训小测试": "quiz",
    "发培训小测试": "quiz",
    "部门培训小测试": "quiz",
    "发部门培训小测试": "quiz",
}
_CARD_WORDS = ("卡", "卡片")
_TRAINING_WORDS = ("培训", "教程")
_QUIZ_WORDS = ("小测试", "测验", "测试题", "考试", "答题")
_SEND_INTENT_WORDS = (
    "发",
    "发送",
    "推",
    "推送",
    "给我",
    "给大家",
    "拉出来",
    "调出来",
    "展示",
    "打开",
    "生成",
    "已经",
    "不是",
    "有没有",
    "哪里",
    "在哪",
    "能不能",
    "可以吗",
)
_ALL_CARD_INTENT_WORDS = (
    "全部",
    "一起",
    "两张",
    "2张",
    "都发",
    "全发",
    "培训和小测试",
    "培训及小测试",
    "培训+小测试",
)
_DIRECT_TRAINING_INTENT_PATTERNS = (
    "我现在需要培训",
    "现在需要培训",
    "我要培训",
    "需要培训",
    "开始培训",
    "进行培训",
    "培训一下",
    "安排培训",
    "给我培训",
    "我要开始培训",
    "可以开始培训",
)


@dataclass(frozen=True)
class QuizSubmission:
    department: str
    answers: dict[int, str]
    feedback: str


def parse_quiz_submission(text: str) -> QuizSubmission | None:
    """Parse a complete department training quiz submission from free text."""
    normalized = (text or "").strip()
    if not normalized:
        return None

    # Avoid intercepting ordinary messages that happen to contain a single answer.
    if "答案" not in normalized and "小测试" not in normalized:
        return None

    answers: dict[int, str] = {}
    for question_no, choice in _LETTER_RE.findall(normalized):
        answers[int(question_no)] = choice.upper()

    # Also support compact forms like: 答案：BCBAA
    if len(answers) < 5:
        answer_section = normalized.split("答案", 1)[-1]
        compact = re.search(
            r"[:：]?\s*([A-Da-d][\s,，、;；]*[A-Da-d][\s,，、;；]*[A-Da-d][\s,，、;；]*[A-Da-d][\s,，、;；]*[A-Da-d])",
            answer_section,
        )
        if compact:
            choices = re.findall(r"[A-Da-d]", compact.group(1))
            if len(choices) >= 5:
                answers = {
                    idx: choice.upper() for idx, choice in enumerate(choices[:5], 1)
                }

    if sorted(answers) != [1, 2, 3, 4, 5]:
        return None

    dept_match = _DEPT_RE.search(normalized)
    feedback_match = _FEEDBACK_RE.search(normalized)
    return QuizSubmission(
        department=(dept_match.group(1).strip() if dept_match else "未填写"),
        answers=answers,
        feedback=(feedback_match.group(1).strip() if feedback_match else ""),
    )


def grade_quiz(submission: QuizSubmission, *, pass_score: int = 4) -> str:
    """Return a user-facing grading result."""
    wrong = [
        question_no
        for question_no, correct in ANSWER_KEY.items()
        if submission.answers.get(question_no) != correct
    ]
    score = len(ANSWER_KEY) - len(wrong)
    passed = score >= pass_score

    status = "已通过" if passed else "未通过"
    lines = [
        f"## 部门培训小测试结果：{status}",
        "",
        f"部门：{submission.department}",
        f"得分：{score}/5",
    ]
    if wrong:
        wrong_text = "、".join(
            f"第 {question_no} 题（正确答案 {ANSWER_KEY[question_no]}）"
            for question_no in wrong
        )
        lines.extend(["", f"错题：{wrong_text}"])
        lines.append("")
        lines.append("复习提示：")
        for question_no in wrong:
            lines.append(f"- 第 {question_no} 题：{QUESTION_HINTS[question_no]}")
    else:
        lines.extend(["", "5 题全对，可以进入破冰任务。"])

    if wrong and SAFETY_CRITICAL_QUESTIONS.intersection(wrong):
        lines.extend(
            [
                "",
                "安全提醒：涉及客户、合同、财务、员工隐私的信息不要发群里；对外内容一定要人工复核。",
            ]
        )

    if passed:
        lines.extend(
            [
                "",
                "下一步：可以开始破冰任务，用你今天真实工作里的一件事 @ 小助手试一次。",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "建议再看一遍培训卡里的“安全边界”和“怎么提问”，然后重新提交答案。",
            ]
        )

    if submission.feedback:
        lines.extend(["", f"已收到你的反馈：{submission.feedback[:120]}"])

    return "\n".join(lines)


def parse_training_card_request(text: str) -> str | None:
    """Return which training card set to send, or None if not an activation."""
    normalized = (text or "").strip()
    if not normalized:
        return None
    if normalized in _ACTIVATION_ALIASES:
        return _ACTIVATION_ALIASES[normalized]

    command = normalized.lstrip("/")
    parts = command.split()
    if not parts or parts[0].lower() not in ("training", "train"):
        return None
    if len(parts) == 1 or parts[1].lower() in ("help", "h", "帮助"):
        return "help"
    if parts[1].lower() in ("cards", "all", "卡片", "全部"):
        return "all"
    if parts[1].lower() in ("training", "lesson", "培训"):
        return "training"
    if parts[1].lower() in ("quiz", "test", "小测试", "测试"):
        return "quiz"
    return "help"


def parse_natural_training_card_request(text: str) -> str | None:
    """Recognize natural-language requests to send training cards.

    The matcher is intentionally conservative: it requires a card word, a
    training/quiz word, and an activation phrase. This lets normal discussion
    about training content continue to the LLM.
    """
    normalized = re.sub(r"\s+", "", (text or "").strip().lower())
    if not normalized:
        return None
    if any(pattern in normalized for pattern in _DIRECT_TRAINING_INTENT_PATTERNS):
        return "training"
    if not any(word in normalized for word in _CARD_WORDS):
        return None
    has_training = any(word in normalized for word in _TRAINING_WORDS)
    has_quiz = any(word in normalized for word in _QUIZ_WORDS)
    if not (has_training or has_quiz):
        return None
    if not any(word in normalized for word in _SEND_INTENT_WORDS):
        return None
    if any(word in normalized for word in _ALL_CARD_INTENT_WORDS):
        return "all"
    if has_quiz:
        return "quiz"
    return "training"


def parse_card_activation(text: str) -> str | None:
    """Parse command-style or natural-language card activation."""
    return parse_training_card_request(text) or parse_natural_training_card_request(
        text
    )


@register(
    "department_training_quiz",
    "dc_agent",
    "部门培训小测试自动批改",
    "1.0.0",
)
class DepartmentTrainingQuizPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.pass_score = max(1, min(5, int(cfg.get("pass_score", 4))))

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        result = MessageEventResult().message(text).use_t2i(False).stop_event()
        event.set_result(result)

    def _chat_id(self, event: AstrMessageEvent) -> tuple[str, str]:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        chat_id = getattr(raw, "chat_id", None) or ""
        if not chat_id:
            chat_id = event.get_group_id() or event.get_sender_id() or ""
        receive_id_type = "chat_id" if str(chat_id).startswith("oc_") else "open_id"
        return str(chat_id), receive_id_type

    def _load_card(self, filename: str) -> dict:
        return json.loads((CARD_DIR / filename).read_text(encoding="utf-8"))

    async def _send_card_file(self, event: AstrMessageEvent, filename: str) -> bool:
        streamer = ensure_streamers_on_context(self.context).get(
            event.get_platform_id() or ""
        )
        chat_id, receive_id_type = self._chat_id(event)
        if streamer is None or not chat_id:
            return False
        card_type = "training_quiz" if filename == QUIZ_CARD_FILE else "training_lesson"
        stream = await send_card_via_runtime(
            streamer,
            card_type=card_type,
            chat_id=chat_id,
            receive_id_type=receive_id_type,
            card=self._load_card(filename),
            platform_id=event.get_platform_id() or "",
            event="start",
            detail=f"department training card file {filename}",
        )
        return stream is not None

    async def _send_training_cards(self, event: AstrMessageEvent, mode: str) -> None:
        if mode == "help":
            self._reply(
                event,
                "可用命令：\n"
                "- `/training cards`：发送部门培训卡 + 小测试卡\n"
                "- `/training training`：只发送部门培训卡\n"
                "- `/training quiz`：只发送部门培训小测试卡\n"
                "也可以直接发送：`发培训卡片`、`发培训小测试`。",
            )
            return

        files = []
        if mode in ("all", "training"):
            files.append(TRAINING_CARD_FILE)
        if mode in ("all", "quiz"):
            files.append(QUIZ_CARD_FILE)

        sent_count = 0
        for filename in files:
            if await self._send_card_file(event, filename):
                sent_count += 1

        if sent_count == len(files):
            if mode == "training":
                self._reply(event, "已发送部门培训说明卡。")
            elif mode == "quiz":
                self._reply(event, "已发送部门培训小测试卡。")
            else:
                self._reply(event, f"已发送 {sent_count} 张部门培训卡片。")
        elif sent_count:
            self._reply(
                event, f"已发送 {sent_count}/{len(files)} 张卡片，其余发送失败。"
            )
        else:
            self._reply(
                event,
                "没有找到可用的飞书卡片通道，暂时无法主动发卡。请确认小助手飞书平台已加载并重启插件。",
            )

    @filter.command(
        "training",
        desc="/training cards | training | quiz：发送部门培训卡片和小测试卡",
    )
    async def training_command(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        mode = parse_card_activation(event.message_str or "") or "help"
        await self._send_training_cards(event, mode)

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        card_mode = parse_card_activation(event.message_str or "")
        if card_mode is not None:
            await self._send_training_cards(event, card_mode)
            return
        submission = parse_quiz_submission(event.message_str or "")
        if submission is None:
            return
        self._reply(event, grade_quiz(submission, pass_score=self.pass_score))
