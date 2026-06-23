"""Engineering registry and health checks for Feishu card rendering.

This module is intentionally host-agnostic: it does not import AstrBot. Runtime
plugins can use it to prove that card templates, trigger ownership and upgrade
metadata are present before handling messages.
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

DC_ROOT = Path(__file__).resolve().parents[2]
CARD_STATE_PATH = DC_ROOT / "data" / "config" / "card_system_state.json"
CARD_RUNTIME_EVENTS_PATH = DC_ROOT / "data" / "card_runtime" / "events.jsonl"
CARD_CONTRACT_PATH = DC_ROOT / "harness" / "contracts" / "feishu_card_system.json"
CARD_SOP_PATH = (
    DC_ROOT
    / "DOC"
    / "05_Dashboard与卡片"
    / "卡片系统工程级升级与验收方案_2026-06-04.md"
)
CARD_RUNTIME_MODULE_PATH = DC_ROOT / "dc_engines" / "dc_engines" / "card_runtime.py"
CARD_RUNTIME_BYPASS_ROOTS = (
    DC_ROOT / "data" / "plugins",
    DC_ROOT / "scripts-tools",
)
CARD_RUNTIME_CARD_TYPE_ROOTS = (
    DC_ROOT / "data" / "plugins",
    DC_ROOT / "scripts-tools",
    DC_ROOT / "dc_engines" / "dc_engines",
)
CARD_RUNTIME_BYPASS_PATTERNS = (
    "streamer.start(",
    "streamer.finalize(",
    ".streamer.finalize(",
    "record_card_runtime_event",
)
CARD_RUNTIME_DIRECT_FEISHU_SEND_PATTERNS = (
    "CreateMessageRequest",
    "message.create(",
    "message.acreate(",
)
CARD_RUNTIME_INTERACTIVE_CARD_MARKERS = (
    '.msg_type("interactive")',
    ".msg_type('interactive')",
    '"msg_type": "interactive"',
    "'msg_type': 'interactive'",
)
NON_PRODUCTION_CARD_EVENT_PLATFORM_IDS = {"webchat", "open_api", "openapi"}
DEFAULT_CARD_BUILDER_MODULE = "dc_engines.feishu_card_streamer.templates"
CARD_TYPE_LITERAL_RE = re.compile(r"card_type\s*=\s*[\"']([^\"']+)[\"']")
CARD_RUNTIME_GATEWAY_CALLS = frozenset(
    {"send_card_via_runtime", "finalize_card_via_runtime"}
)
CARD_RUNTIME_DYNAMIC_CARD_TYPE_FORWARDERS = {
    ("scripts-tools/card-grey-push.py", "_send_cards"): "registry-driven grey push",
    (
        "data/plugins/hermes_bridge/hermes_bridge.py",
        "_send_skill_card",
    ): "typed skill-card wrapper; call sites are literal-gated",
    (
        "data/plugins/content_sop_rule_review_plugin/main.py",
        "_send_card",
    ): "typed content SOP wrapper; call sites are literal-gated",
    (
        "dc_engines/dc_engines/feishu_writer/private_message_sender.py",
        "send_interactive_card",
    ): "library API accepts a registered caller-provided card_type",
    (
        "data/plugins/department_training_quiz/main.py",
        "_send_card_file",
    ): "filename-to-card_type mapper is local and literal-gated",
    (
        "data/plugins/employee_onboarding/main.py",
        "_send_card",
    ): "typed onboarding wrapper; call sites are literal-gated",
    (
        "data/plugins/employee_onboarding/main.py",
        "_send_entry_card_to_employee",
    ): "stage mapper returns registered onboarding card types",
    (
        "data/plugins/feishu_pet_assistant/main.py",
        "_send_card",
    ): "typed pet-card wrapper; call sites are literal-gated",
}


@dataclass(frozen=True, slots=True)
class CardSpec:
    card_type: str
    version: str
    owner: str
    builder: str
    triggers: tuple[str, ...]
    builder_module: str = DEFAULT_CARD_BUILDER_MODULE
    fallback: str = "plain_text"
    health_required: bool = True
    upgrade_mode: str = "versioned"
    rollback_to: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CardHealthReport:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = ok
        if detail:
            self.details[name] = detail
        self.ok = self.ok and ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class CardVersionState:
    card_type: str
    active_version: str
    previous_version: str | None = None
    rollout: str = "stable"
    updated_at: str = ""
    updated_by: str = "system"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardRuntimeCallSite:
    path: str
    line: int
    function: str
    gateway: str
    expression: str
    literal_card_types: tuple[str, ...] = ()
    dynamic: bool = False
    dynamic_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CARD_REGISTRY: dict[str, CardSpec] = {
    "casual_reply": CardSpec(
        card_type="casual_reply",
        version="1.0",
        owner="daily_card_renderer",
        builder="build_casual_response_card",
        triggers=("intent=casual", "short fallback non-task"),
        fallback="plain_text",
        rollback_to=None,
        notes="Blue header + white body daily chat card.",
    ),
    "thinking_waiting": CardSpec(
        card_type="thinking_waiting",
        version="1.1",
        owner="daily_card_renderer",
        builder="build_thinking_card",
        triggers=("task-like LLM request", "reasoning_tier present"),
        fallback="plain_text",
        notes="Patched waiting card with elapsed time and progress affordance.",
    ),
    "task_progress": CardSpec(
        card_type="task_progress",
        version="1.0",
        owner="feishu_card_streamer",
        builder="build_progress_card",
        triggers=("long task progress update", "Hermes intermediate status"),
        fallback="plain_text",
        notes="Shared progress card used by waiting-card helpers and Hermes updates.",
    ),
    "antigravity_queue": CardSpec(
        card_type="antigravity_queue",
        version="1.0",
        owner="dc_router",
        builder="build_antigravity_queue_card",
        triggers=("Antigravity over capacity", "fallback channel selected"),
        fallback="plain_text",
    ),
    "task_result": CardSpec(
        card_type="task_result",
        version="1.0",
        owner="hermes_bridge",
        builder="build_final_card",
        triggers=("deep task completed", "waiting card final success"),
        fallback="plain_text",
    ),
    "task_error": CardSpec(
        card_type="task_error",
        version="1.0",
        owner="hermes_bridge/feishu_resource_plugin",
        builder="build_error_card",
        triggers=("deep task failed", "card-rendered error response"),
        fallback="plain_text",
    ),
    "daily_response": CardSpec(
        card_type="daily_response",
        version="1.0",
        owner="daily_card_renderer",
        builder="build_daily_response_card",
        triggers=("long structured response", "waiting finalize"),
        fallback="plain_text",
    ),
    "media_generation": CardSpec(
        card_type="media_generation",
        version="1.0",
        owner="gpt_image_plugin/dreamina_plugin",
        builder="build_media_generation_card",
        triggers=("image generation", "video generation"),
        fallback="plain_text",
    ),
    "truth_intake_request": CardSpec(
        card_type="truth_intake_request",
        version="1.0",
        owner="harness_sensor_plugin",
        builder="build_truth_intake_request_card",
        triggers=("missing verifiable source", "truth blocked"),
        fallback="plain_text",
    ),
    "truth_intake_received": CardSpec(
        card_type="truth_intake_received",
        version="1.0",
        owner="harness_sensor_plugin",
        builder="build_truth_intake_received_card",
        triggers=("truth material archived",),
        fallback="plain_text",
    ),
    "source_trace": CardSpec(
        card_type="source_trace",
        version="1.0",
        owner="harness_sensor_plugin",
        builder="build_source_trace_card",
        triggers=("source trace requested", "partial truth confirmation"),
        fallback="plain_text",
    ),
    "devops_status": CardSpec(
        card_type="devops_status",
        version="1.0",
        owner="devops_tools/dianchi_tech",
        builder="build_devops_status_card",
        triggers=("service status", "card runtime health"),
        fallback="plain_text",
    ),
    "onboarding_department": CardSpec(
        card_type="onboarding_department",
        version="1.0",
        owner="employee_onboarding",
        builder="build_onboarding_dept_card",
        triggers=("new friend", "manual onboarding push"),
        fallback="plain_text",
    ),
    "onboarding_role": CardSpec(
        card_type="onboarding_role",
        version="1.0",
        owner="employee_onboarding",
        builder="build_onboarding_role_card",
        triggers=("department selected",),
        fallback="plain_text",
    ),
    "onboarding_name_prompt": CardSpec(
        card_type="onboarding_name_prompt",
        version="1.0",
        owner="employee_onboarding",
        builder="build_onboarding_name_prompt_card",
        triggers=("role selected", "missing employee display name"),
        fallback="plain_text",
    ),
    "onboarding_tutorial_list": CardSpec(
        card_type="onboarding_tutorial_list",
        version="1.0",
        owner="employee_onboarding",
        builder="build_onboarding_tutorial_list_card",
        triggers=("identity registration completed", "quiz failed review list"),
        fallback="plain_text",
    ),
    "employee_insight_welcome": CardSpec(
        card_type="employee_insight_welcome",
        version="1.0",
        owner="employee_insight_plugin",
        builder="build_employee_insight_welcome_card",
        triggers=("daily employee insight outreach", "unknown how to start"),
        fallback="plain_text",
    ),
    "training_lesson": CardSpec(
        card_type="training_lesson",
        version="1.0",
        owner="department_training_quiz",
        builder="build_tutorial_lesson_card",
        triggers=("training lesson selected", "next lesson"),
        fallback="plain_text",
    ),
    "training_quiz": CardSpec(
        card_type="training_quiz",
        version="1.0",
        owner="department_training_quiz",
        builder="build_quiz_question_card",
        triggers=("quiz start", "quiz next question"),
        fallback="plain_text",
    ),
    "training_quiz_feedback": CardSpec(
        card_type="training_quiz_feedback",
        version="1.0",
        owner="employee_onboarding",
        builder="build_quiz_feedback_card",
        triggers=("quiz answer submitted",),
        fallback="plain_text",
    ),
    "training_quiz_result": CardSpec(
        card_type="training_quiz_result",
        version="1.0",
        owner="employee_onboarding",
        builder="build_quiz_result_card",
        triggers=("quiz completed",),
        fallback="plain_text",
    ),
    "kb_archive": CardSpec(
        card_type="kb_archive",
        version="1.0",
        owner="harness_sensor_plugin/knowledge_base",
        builder="build_kb_archive_card",
        triggers=("materials archived", "knowledge base sync completed"),
        fallback="plain_text",
    ),
    "employee_pending": CardSpec(
        card_type="employee_pending",
        version="1.0",
        owner="workflow_intent_plugin",
        builder="build_employee_pending_card",
        triggers=("draft needs employee confirmation",),
        fallback="plain_text",
    ),
    "multimodal_understanding": CardSpec(
        card_type="multimodal_understanding",
        version="1.0",
        owner="media_generation_sop",
        builder="build_multimodal_understanding_card",
        triggers=("image understanding", "video understanding", "audio understanding"),
        fallback="plain_text",
    ),
    "case_overview": CardSpec(
        card_type="case_overview",
        version="1.0",
        owner="case_archive_knowledge_sync",
        builder="build_case_overview_card",
        triggers=("case snapshot requested", "project progress summary"),
        fallback="plain_text",
    ),
    "task_reminder": CardSpec(
        card_type="task_reminder",
        version="1.0",
        owner="task_cli_plugin",
        builder="build_task_reminder_card",
        triggers=("task due reminder", "overdue reminder"),
        fallback="plain_text",
    ),
    "skill_list": CardSpec(
        card_type="skill_list",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_list_card",
        triggers=("skill list", "boss/cowork skill list"),
        fallback="plain_text",
    ),
    "skill_detail": CardSpec(
        card_type="skill_detail",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_detail_card",
        triggers=("skill inspect",),
        fallback="plain_text",
    ),
    "skill_review": CardSpec(
        card_type="skill_review",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_review_card",
        triggers=("skill review", "quality review"),
        fallback="plain_text",
    ),
    "skill_deleted_list": CardSpec(
        card_type="skill_deleted_list",
        version="1.0",
        owner="hermes_bridge",
        builder="build_deleted_skill_list_card",
        triggers=("deleted skill list", "skill recycle bin"),
        fallback="plain_text",
    ),
    "skill_confirm": CardSpec(
        card_type="skill_confirm",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_confirm_card",
        triggers=("dangerous skill action", "delete/rollback/restore confirm"),
        fallback="plain_text",
    ),
    "email_draft": CardSpec(
        card_type="email_draft",
        version="1.0",
        owner="content_sop/workflow_intent_plugin",
        builder="build_email_draft_card",
        triggers=("email draft generated", "customer reply draft"),
        fallback="plain_text",
    ),
    "copy_draft": CardSpec(
        card_type="copy_draft",
        version="1.0",
        owner="content_sop/workflow_intent_plugin",
        builder="build_copy_draft_card",
        triggers=("copy draft generated", "announcement draft"),
        fallback="plain_text",
    ),
    "boss_quicklook": CardSpec(
        card_type="boss_quicklook",
        version="1.0",
        owner="content_sop/workflow_intent_plugin",
        builder="build_boss_quicklook_card",
        triggers=("boss summary requested", "executive quicklook"),
        fallback="plain_text",
    ),
    "god_mode_approval": CardSpec(
        card_type="god_mode_approval",
        version="1.0",
        owner="god_mode_plugin",
        builder_module="dc_engines.god_mode",
        builder="build_god_mode_approval_card",
        triggers=("/god side-effect approval",),
        fallback="plain_text",
    ),
    "god_mode_execution": CardSpec(
        card_type="god_mode_execution",
        version="1.0",
        owner="god_mode_plugin",
        builder_module="dc_engines.god_mode",
        builder="build_god_mode_execution_card",
        triggers=("approved /god Feishu card action",),
        fallback="plain_text",
    ),
    "assistant_distillation_review": CardSpec(
        card_type="assistant_distillation_review",
        version="1.0",
        owner="assistant_distillation_plugin",
        builder_module="dc_engines.assistant_distillation",
        builder="build_candidate_review_card",
        triggers=("assistant language candidate pending review",),
        fallback="plain_text",
    ),
    "content_sop_rule_review": CardSpec(
        card_type="content_sop_rule_review",
        version="1.0",
        owner="content_sop_rule_review_plugin",
        builder_module="dc_engines.department_workflows.content_rule_proposals",
        builder="build_rule_proposal_review_card",
        triggers=("content SOP rule proposal pending review",),
        fallback="plain_text",
    ),
    "content_sop_ops_reminder": CardSpec(
        card_type="content_sop_ops_reminder",
        version="1.0",
        owner="content_sop_rule_review_plugin",
        builder_module="dc_engines.department_workflows.content_sop_ops",
        builder="build_content_sop_ops_reminder_card",
        triggers=("content SOP ops reminder requested",),
        fallback="plain_text",
    ),
    "pet_status": CardSpec(
        card_type="pet_status",
        version="1.0",
        owner="feishu_pet_assistant",
        builder_module="data.plugins.feishu_pet_assistant.cards",
        builder="build_status_card",
        triggers=("/pet", "pet card action status refresh"),
        fallback="plain_text",
    ),
    "pet_tasks": CardSpec(
        card_type="pet_tasks",
        version="1.0",
        owner="feishu_pet_assistant",
        builder_module="data.plugins.feishu_pet_assistant.cards",
        builder="build_tasks_card",
        triggers=("pet task list requested",),
        fallback="plain_text",
    ),
    "pet_done": CardSpec(
        card_type="pet_done",
        version="1.0",
        owner="feishu_pet_assistant",
        builder_module="data.plugins.feishu_pet_assistant.cards",
        builder="build_done_card",
        triggers=("pet task completed",),
        fallback="plain_text",
    ),
    "pet_error": CardSpec(
        card_type="pet_error",
        version="1.0",
        owner="feishu_pet_assistant",
        builder_module="data.plugins.feishu_pet_assistant.cards",
        builder="build_error_card",
        triggers=("pet card action failed",),
        fallback="plain_text",
    ),
    "department_memory_prompt": CardSpec(
        card_type="department_memory_prompt",
        version="1.0",
        owner="dc_router",
        builder_module="dc_engines.router_card_templates",
        builder="build_department_memory_prompt_card",
        triggers=("department memory prompt suggested",),
        fallback="plain_text",
    ),
    "sop_signal_confirmation": CardSpec(
        card_type="sop_signal_confirmation",
        version="1.0",
        owner="dc_router",
        builder_module="dc_engines.router_card_templates",
        builder="build_sop_signal_confirmation_card",
        triggers=("employee SOP memory signal captured",),
        fallback="plain_text",
    ),
}


CASUAL_INTENTS = frozenset({"casual"})
TASK_HINT_RE = re.compile(
    r"(帮我|请|需要|生成|写一|写个|起草|整理|分析|总结|设计|做一|查一下|搜索|解读|看一下|处理|修复|排查|测试|发卡|生图|视频|方案|报告|文案|邮件|通知|PRD|prd)"
)


def is_task_like_message(text: str) -> bool:
    return bool(TASK_HINT_RE.search(text or ""))


def should_start_waiting_card(
    *,
    intent: str,
    message: str,
    reasoning_tier: str | None = None,
) -> bool:
    """Return whether a request should show a waiting/progress card."""
    normalized_intent = (intent or "").strip()
    if normalized_intent in CASUAL_INTENTS:
        return False
    if reasoning_tier:
        return True

    text = (message or "").strip()
    if not text:
        return False
    if len(text) <= 80 and not is_task_like_message(text):
        return False
    return True


def should_render_casual_reply_card(*, intent: str, message: str) -> bool:
    """Return whether the final LLM response should become a casual card."""
    normalized_intent = (intent or "").strip()
    if normalized_intent in CASUAL_INTENTS:
        return True
    text = (message or "").strip()
    if normalized_intent in {"", "fallback"} and len(text) <= 80:
        return not is_task_like_message(text)
    return False


PRIVATE_TEMPLATE_BUILDERS = frozenset[str]()
REQUIRED_CARD_TYPES = frozenset(CARD_REGISTRY)
REQUIRED_CONTRACT_IDS = frozenset(
    {
        "card-system-001",
        "card-system-002",
        "card-system-003",
        "card-system-004",
        "card-system-005",
        "card-system-006",
        "card-system-007",
        "card-system-008",
        "card-system-009",
    }
)


def list_card_specs() -> list[dict[str, Any]]:
    return [spec.to_dict() for spec in CARD_REGISTRY.values()]


def load_card_contract() -> dict[str, Any]:
    return json.loads(CARD_CONTRACT_PATH.read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_version_state() -> dict[str, dict[str, Any]]:
    now = _utc_now()
    return {
        card_type: CardVersionState(
            card_type=card_type,
            active_version=spec.version,
            previous_version=spec.rollback_to,
            rollout="stable",
            updated_at=now,
            updated_by="registry",
            note="initialized from card registry",
        ).to_dict()
        for card_type, spec in CARD_REGISTRY.items()
    }


def load_card_version_state() -> dict[str, dict[str, Any]]:
    if not CARD_STATE_PATH.exists():
        state = _default_version_state()
        save_card_version_state(state)
        return state

    raw = json.loads(CARD_STATE_PATH.read_text(encoding="utf-8"))
    cards = raw.get("cards", raw)
    if not isinstance(cards, dict):
        raise ValueError("card state file must contain a cards object")

    state: dict[str, dict[str, Any]] = {}
    for card_type, spec in CARD_REGISTRY.items():
        item = cards.get(card_type)
        if not isinstance(item, dict):
            item = CardVersionState(
                card_type=card_type,
                active_version=spec.version,
                previous_version=spec.rollback_to,
                rollout="stable",
                updated_at=_utc_now(),
                updated_by="registry",
                note="backfilled from card registry",
            ).to_dict()
        state[card_type] = item
    if state != cards:
        save_card_version_state(state)
    return state


def save_card_version_state(state: dict[str, dict[str, Any]]) -> None:
    CARD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "updated_at": _utc_now(),
        "cards": state,
    }
    CARD_STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_card_versions() -> list[dict[str, Any]]:
    state = load_card_version_state()
    versions: list[dict[str, Any]] = []
    for card_type, spec in CARD_REGISTRY.items():
        item = dict(state[card_type])
        item["registry_version"] = spec.version
        item["owner"] = spec.owner
        item["builder"] = spec.builder
        versions.append(item)
    return versions


def validate_card_contract() -> tuple[bool, str]:
    if not CARD_CONTRACT_PATH.exists():
        return False, f"missing contract: {CARD_CONTRACT_PATH}"
    if not CARD_SOP_PATH.exists():
        return False, f"missing SOP: {CARD_SOP_PATH}"
    if not CARD_RUNTIME_MODULE_PATH.exists():
        return False, f"missing runtime gateway: {CARD_RUNTIME_MODULE_PATH}"

    try:
        contract = load_card_contract()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    if contract.get("feature") != "feishu_card_system":
        return False, "contract feature must be feishu_card_system"
    criteria = contract.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return False, "acceptance_criteria must be a list"

    found_ids = {
        str(item.get("id"))
        for item in criteria
        if isinstance(item, dict) and item.get("id")
    }
    missing_ids = sorted(REQUIRED_CONTRACT_IDS.difference(found_ids))
    if missing_ids:
        return False, f"missing criteria={missing_ids}"

    missing_verification = sorted(
        str(item.get("id"))
        for item in criteria
        if isinstance(item, dict) and not item.get("verification")
    )
    if missing_verification:
        return False, f"missing verification={missing_verification}"

    sop = CARD_SOP_PATH.read_text(encoding="utf-8")
    required_sop_terms = (
        "运行时网关",
        "升级流程",
        "回滚流程",
        "测试后的衔接规则",
        "真机灰度验收",
        "未来升级方式",
    )
    missing_terms = [term for term in required_sop_terms if term not in sop]
    if missing_terms:
        return False, f"SOP missing sections={missing_terms}"

    return True, f"{CARD_CONTRACT_PATH} + {CARD_SOP_PATH}"


def set_card_version(
    card_type: str,
    version: str,
    *,
    rollout: str = "grey",
    updated_by: str = "operator",
    note: str = "",
) -> dict[str, Any]:
    if card_type not in CARD_REGISTRY:
        raise KeyError(f"unknown card_type: {card_type}")
    state = load_card_version_state()
    current = state[card_type]
    state[card_type] = CardVersionState(
        card_type=card_type,
        active_version=version,
        previous_version=current.get("active_version"),
        rollout=rollout,
        updated_at=_utc_now(),
        updated_by=updated_by,
        note=note,
    ).to_dict()
    save_card_version_state(state)
    return state[card_type]


def rollback_card_version(
    card_type: str,
    *,
    updated_by: str = "operator",
    note: str = "",
) -> dict[str, Any]:
    if card_type not in CARD_REGISTRY:
        raise KeyError(f"unknown card_type: {card_type}")
    state = load_card_version_state()
    current = state[card_type]
    target = current.get("previous_version") or CARD_REGISTRY[card_type].rollback_to
    if not target:
        target = CARD_REGISTRY[card_type].version
    state[card_type] = CardVersionState(
        card_type=card_type,
        active_version=str(target),
        previous_version=current.get("active_version"),
        rollout="rollback",
        updated_at=_utc_now(),
        updated_by=updated_by,
        note=note or "rollback requested",
    ).to_dict()
    save_card_version_state(state)
    return state[card_type]


def record_card_runtime_event(
    *,
    event: str,
    card_type: str,
    ok: bool,
    platform_id: str = "",
    chat_id: str = "",
    message_id: str = "",
    receive_id_type: str = "",
    detail: str = "",
    fallback: str = "",
) -> None:
    if os.environ.get("TESTING", "").lower() == "true":
        return
    CARD_RUNTIME_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _utc_now(),
        "event": event,
        "card_type": card_type,
        "ok": ok,
        "platform_id": platform_id,
        "chat_id": chat_id[:40],
        "receive_id_type": receive_id_type,
        "message_id": message_id,
        "detail": detail[:500],
        "fallback": fallback,
    }
    with CARD_RUNTIME_EVENTS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _card_failure_dedupe_key(item: dict[str, Any]) -> tuple[str, ...]:
    message_id = str(item.get("message_id") or "")
    event = str(item.get("event") or "")
    if message_id:
        return ("message", message_id, event)
    return (
        "empty-message",
        str(item.get("ts") or ""),
        event,
        str(item.get("card_type") or ""),
        str(item.get("platform_id") or ""),
        str(item.get("chat_id") or ""),
        str(item.get("receive_id_type") or ""),
    )


def _dedupe_consecutive_failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold consecutive duplicate-failure events on the same message_id.

    Streamer / harness races can produce 2-3 ``finalize`` attempts on the
    same ``message_id`` (e.g. when the LLM streams complete mid-card and
    the runtime also fires its own finalize). The first attempt
    succeeds; the retries fall through to ``plain_text`` because the
    card is already finalized. Counting each retry as a separate
    failure inflates the recent-failures count past the ``<= 2``
    tolerance and turns known-bad signal into false-positive
    ``runtime_events_recent`` FAILED.

    The dedup keeps **only the first** failure for a given ``message_id`` +
    ``event`` pair within the recent window. Empty ``message_id`` failures
    include timestamp, card type and target fields in the key so independent
    create failures do not collapse into one bucket.
    """
    seen: set[tuple[str, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in events:
        if not item.get("ok"):
            key = _card_failure_dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
        result.append(item)
    return result


def _grey_push_recovery_key(item: dict[str, Any]) -> tuple[str, ...]:
    if str(item.get("event") or "") != "grey_push":
        return ()
    return (
        "grey_push",
        str(item.get("card_type") or ""),
        str(item.get("platform_id") or ""),
        str(item.get("chat_id") or ""),
        str(item.get("receive_id_type") or ""),
        str(item.get("detail") or ""),
    )


def _drop_recovered_grey_push_failures(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ignore grey-push failures that were retried successfully later.

    ``card-grey-push.py`` is an operator smoke tool. A sandbox/network failure
    can write an ``ok=false`` event, then the same validation run can be retried
    outside the sandbox and succeed seconds later. For handoff health, the
    latest result of that grey validation batch is what matters. This is
    intentionally limited to ``grey_push`` so real user-facing card failures
    still require inspection.
    """
    successful_keys: set[tuple[str, ...]] = set()
    kept_reversed: list[dict[str, Any]] = []
    for item in reversed(events):
        key = _grey_push_recovery_key(item)
        if item.get("ok") and key:
            successful_keys.add(key)
            kept_reversed.append(item)
            continue
        if not item.get("ok") and key in successful_keys:
            continue
        kept_reversed.append(item)
    return list(reversed(kept_reversed))


def _is_production_card_runtime_event(item: dict[str, Any]) -> bool:
    platform_id = str(item.get("platform_id") or "").strip().lower()
    return platform_id not in NON_PRODUCTION_CARD_EVENT_PLATFORM_IDS


def _production_card_runtime_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [item for item in events if _is_production_card_runtime_event(item)]


def _deduped_production_card_failures(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    production_events = _production_card_runtime_events(events)
    recovered = _drop_recovered_grey_push_failures(production_events)
    deduped = _dedupe_consecutive_failures(recovered)
    return [item for item in deduped if not item.get("ok")]


def recent_card_runtime_events(limit: int = 20) -> list[dict[str, Any]]:
    if not CARD_RUNTIME_EVENTS_PATH.exists():
        return []
    lines = CARD_RUNTIME_EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for line in lines[-max(limit, 0) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def card_system_next_step(report: CardHealthReport | None = None) -> str:
    if report is None:
        report = run_card_system_health()
    if not report.ok:
        failed = [name for name, ok in report.checks.items() if not ok]
        return (
            "STOP: fix failed card checks before restart, grey rollout or real-card push: "
            + ", ".join(failed)
        )

    events = recent_card_runtime_events(30)
    if not events:
        return (
            "NEXT: health is green; run scripts-tools/card-grey-push.py for "
            "casual_reply and thinking_waiting, then check runtime events."
        )

    production_events = _production_card_runtime_events(events)
    recent_failures = _deduped_production_card_failures(events)
    if recent_failures:
        latest = recent_failures[-1]
        return (
            "NEXT: inspect recent card runtime failure before widening rollout: "
            f"{latest.get('card_type')} {latest.get('event')} {latest.get('detail')}"
        )
    if events and not production_events:
        return (
            "NEXT: health is green; recent failures are non-production webchat smoke "
            "events. Run scripts-tools/card-grey-push.py for a fresh Feishu grey "
            "validation, then check runtime events."
        )

    return (
        "NEXT: health and runtime events are green; proceed to small Feishu grey "
        "validation with scripts-tools/card-grey-push.py or continue template upgrade."
    )


def _template_builders() -> dict[str, Callable[..., dict[str, Any]]]:
    return _builders_for_module(DEFAULT_CARD_BUILDER_MODULE)


def _builders_for_module(module_name: str) -> dict[str, Callable[..., dict[str, Any]]]:
    module = import_module(module_name)
    return {
        name: getattr(module, name)
        for name in dir(module)
        if callable(getattr(module, name))
    }


def _builder_for_spec(spec: CardSpec) -> Callable[..., dict[str, Any]] | None:
    return _builders_for_module(spec.builder_module).get(spec.builder)


def _builder_key(builder_name: str, builder_module: str) -> str:
    return f"{builder_module}:{builder_name}"


def build_sample_card(card_type: str) -> dict[str, Any]:
    spec = CARD_REGISTRY.get(card_type)
    if spec is None:
        raise KeyError(f"unknown card_type: {card_type}")
    builder = _builder_for_spec(spec)
    if builder is None:
        raise KeyError(f"missing builder for {card_type}: {spec.builder}")
    return builder(**_sample_payload(spec.builder, spec.builder_module))


def _sample_payload(
    builder_name: str,
    builder_module: str = DEFAULT_CARD_BUILDER_MODULE,
) -> dict[str, Any]:
    samples: dict[str, dict[str, Any]] = {
        "build_casual_response_card": {
            "content_md": (
                "蔡挺，我这边在。\n\n"
                "这张是闲聊卡真机验证：正文保持白底、段落留足呼吸感，"
                "**重点文字会加粗**，不会再退回灰色原生气泡。\n\n"
                "如果只是日常聊天，它应该轻一点；如果你开始安排任务，"
                "系统才切到等待卡和结果卡。"
            ),
            "user_msg": "你好呀",
        },
        "build_thinking_card": {
            "user_msg": "帮我写一段端午客户微信问候话术，语气专业但不要太销售。",
            "elapsed_sec": 63,
            "reasoning_tier": "high",
        },
        "build_progress_card": {
            "title": "Hermes 深度分析",
            "brief": "整理端午客户触达方案",
            "elapsed_sec": 42,
            "current_stage": "任务推理中（这个问题稍复杂）",
            "reasoning_tier": "high",
        },
        "build_antigravity_queue_card": {
            "job_id": "agy-card-health-001",
            "queue_position": 2,
            "eta_text": "约 1 分钟",
            "elapsed_sec": 12,
            "original_prompt": "帮我整理客户触达方案",
        },
        "build_final_card": {
            "title": "Hermes 深度分析完成",
            "result_md": "## 结论\n\n方案可以推进。\n\n- 先确认客户名单\n- 再发送节日问候",
            "elapsed_sec": 88,
            "reasoning_tier": "high",
        },
        "build_error_card": {
            "title": "Hermes 深度分析失败",
            "error_msg": "上游模型暂时不可用。",
            "elapsed_sec": 21,
            "retry_hint": "可以稍后重试，或切换备用模型继续。",
        },
        "build_daily_response_card": {
            "content_md": "## 结论\n\n这件事可以继续推进。",
            "title": "巅池-Agent小助手",
        },
        "build_media_generation_card": {
            "task_title": "图片生成中",
            "media_type": "image",
            "prompt": "端午节品牌海报",
            "status": "running",
        },
        "build_truth_intake_request_card": {
            "task_title": "端午客户问候话术",
            "task_id": "card-health-001",
            "missing_fields": ["客户对象", "原文/附件", "发送时间"],
            "task_brief": "请补充客户对象、附件和发送时间。",
        },
        "build_truth_intake_received_card": {
            "task_id": "card-health-002",
            "sources_summary": {"attachments": 2, "links": 1, "texts": 1},
            "archive_path": "data/harness_intake/raw/card-health-002/",
        },
        "build_source_trace_card": {
            "truth_status": "部分待确认",
            "sources": ["飞书 wiki", "附件", "知识库"],
            "kb_names": ["品牌规范"],
            "unconfirmed": ["最终预算"],
        },
        "build_devops_status_card": {
            "services": [
                {"name": "Cards", "status": "正常", "pid": "-", "detail": "patch 正常"}
            ],
            "queue_summary": {"Hermes": 0, "Claude CLI": 0, "Codex": 1},
        },
        "build_onboarding_dept_card": {"welcome_name": "蔡挺"},
        "build_onboarding_role_card": {"dept_name": "策划"},
        "build_onboarding_name_prompt_card": {"role_name": "策划经理"},
        "build_onboarding_tutorial_list_card": {
            "display_name": "蔡挺",
            "dept_code": "planning",
        },
        "build_employee_insight_welcome_card": {"employee_name": "测试员工"},
        "build_tutorial_lesson_card": {"lesson_id": "lesson_reasoning"},
        "build_quiz_question_card": {"q_num": 1, "total": 5},
        "build_quiz_feedback_card": {
            "q_num": 1,
            "correct": False,
            "explain": "这类任务需要先确认真实资料。",
            "next_q": 2,
            "total": 5,
            "lesson_id": "lesson_truth",
        },
        "build_quiz_result_card": {
            "display_name": "蔡挺",
            "correct_count": 5,
            "total": 5,
            "invite_link": "https://example.com/invite",
            "invite_note": "通过后进入内测群。",
        },
        "build_kb_archive_card": {
            "archived_items": {"附件": 2, "文字材料": 1},
            "kb_name": "品牌规范",
            "status": "已入库",
            "archive_path": "data/harness_intake/archived/card-health-003/",
            "doc_id": "doc_card_health_003",
        },
        "build_employee_pending_card": {
            "task_title": "端午客户问候话术草稿",
            "pending_items": [
                {"field": "客户名单", "hint": "请补充收件范围"},
                {"field": "发送时间", "hint": "请确认发送窗口"},
            ],
            "current_draft_summary": "已整理主文案和署名。",
            "task_id": "card-health-004",
        },
        "build_skill_list_card": {
            "kind": "boss",
            "skills": [{"slug": "demo", "title": "示例 skill", "status": "active"}],
        },
        "build_multimodal_understanding_card": {
            "task_title": "看一下客户发来的素材",
            "modality": "mixed",
            "status": "已理解",
            "files_summary": {"图片": 2, "视频": 1},
            "truth_status": "部分待确认",
            "findings": ["包含门店场景", "出现产品外观"],
            "limits": ["缺少拍摄时间"],
            "task_id": "mm-card-health-001",
        },
        "build_case_overview_card": {
            "case_id": "case-card-health-001",
            "case_title": "端午客户运营项目",
            "status": "部分待确认",
            "owner": "客户部",
            "progress": "60%",
            "task_summary": {"todo": 2, "in_progress": 1, "done": 4, "blocked": 1},
            "risks": ["预算待确认"],
            "next_actions": ["确认客户名单"],
        },
        "build_task_reminder_card": {
            "task_id": "task-card-health-001",
            "task_title": "确认端午触达名单",
            "assignee": "蔡挺",
            "due_text": "今天 18:00",
            "priority": "紧急",
            "overdue": True,
        },
        "build_skill_detail_card": {
            "kind": "colleague",
            "skill": {
                "name": "市场同事",
                "slug": "marketing",
                "version": "v2",
                "conversation_type": "group",
                "message_count": 8,
                "speaker_count": 3,
                "corrections_count": 1,
                "updated_at": "2026-05-21T13:00:00Z",
                "knowledge_sources": ["knowledge/corrections.md"],
                "files": ["SKILL.md", "work.md"],
            },
        },
        "build_skill_review_card": {
            "kind": "boss",
            "review": {
                "slug": "demo",
                "title": "示例 skill",
                "summary": "结构完整，可进入灰度。",
            },
        },
        "build_deleted_skill_list_card": {
            "kind": "boss",
            "skills": [
                {
                    "name": "杨总",
                    "slug": "yang-zong",
                    "deleted_at": "2026-05-21T14:00:00Z",
                    "path": "/tmp/.deleted/boss_yang-zong",
                }
            ],
        },
        "build_skill_confirm_card": {
            "operation": "回滚",
            "kind": "boss",
            "slug": "demo",
            "version": "v1",
        },
        "build_email_draft_card": {
            "email_subject": "端午客户问候",
            "sendable_body": "您好，端午将至，祝您和家人安康顺遂。",
            "truth_status": "已核验",
            "modify_suggestions": ["补充客户姓名后发送"],
            "pending_items": ["客户名单待确认"],
            "sources": ["品牌节日问候规范"],
            "task_id": "email-card-health-001",
        },
        "build_copy_draft_card": {
            "copy_title": "端午节朋友圈文案",
            "main_version": "端午安康，愿每一次相聚都带着温度。",
            "alternatives": ["粽香渐近，祝一路顺遂。"],
            "usage_hint": "发送前确认品牌语气。",
            "truth_status": "已核验",
            "sources": ["品牌规范"],
            "target_channel": "朋友圈",
            "task_id": "copy-card-health-001",
        },
        "build_boss_quicklook_card": {
            "task_title": "端午客户运营项目",
            "one_line_conclusion": "方案可推进，但客户名单和预算仍需确认。",
            "usability": "需要确认",
            "risks": ["预算未最终确认"],
            "next_actions": ["确认名单", "锁定发送时间"],
            "detail_summary": "已完成主文案和执行节奏梳理。",
            "task_id": "boss-card-health-001",
        },
        _builder_key(
            "build_god_mode_approval_card",
            "dc_engines.god_mode",
        ): {
            "run": SimpleNamespace(
                run_id="god-card-health-001",
                status="waiting_approval",
                summary="发送一张审批卡样例",
                approval_required=True,
                audit_id="audit-card-health-001",
                actions=(
                    SimpleNamespace(
                        action_id="act-card-health-001",
                        tool_name="dc_agent_send_feishu_card",
                        capability="feishu_card",
                        description="发送 Feishu 灰度样卡",
                        side_effect=True,
                    ),
                ),
            ),
        },
        _builder_key(
            "build_god_mode_execution_card",
            "dc_engines.god_mode",
        ): {
            "action": SimpleNamespace(
                action_id="act-card-health-001",
                tool_name="dc_agent_send_feishu_card",
                capability="feishu_card",
                description="God Mode 已批准执行样例",
            ),
        },
        _builder_key(
            "build_candidate_review_card",
            "dc_engines.assistant_distillation",
        ): {
            "candidate": SimpleNamespace(
                candidate_id="distill-card-health-001",
                kind="chitchat_keyword",
                intent="greeting",
                normalized_text="滴滴",
                pattern="滴滴",
                template_body="我在。",
                count=3,
                rationale="多次稳定触发轻量问候。",
            ),
        },
        _builder_key(
            "build_rule_proposal_review_card",
            "dc_engines.department_workflows.content_rule_proposals",
        ): {
            "proposal": SimpleNamespace(
                proposal_id="sop-rule-card-health-001",
                department_id="planning",
                scenario_id="proposal",
                support_count=4,
                status="pending",
                rule_text="交付方案前必须列出资料来源和待确认项。",
            ),
        },
        _builder_key(
            "build_content_sop_ops_reminder_card",
            "dc_engines.department_workflows.content_sop_ops",
        ): {
            "reminders": [
                SimpleNamespace(
                    severity="critical",
                    title="内容 SOP 质量 gate 阻塞",
                    message="2 个交付结果需要运营复盘。",
                )
            ],
        },
        _builder_key(
            "build_status_card",
            "data.plugins.feishu_pet_assistant.cards",
        ): {
            "pet": {"pet_name": "小橘", "mood": "精神不错", "energy": 80},
            "stats": {"pending": 2, "done": 1},
            "desktop_bound": True,
        },
        _builder_key(
            "build_tasks_card",
            "data.plugins.feishu_pet_assistant.cards",
        ): {
            "pet": {"pet_name": "小橘"},
            "tasks": [
                {"id": "task-card-health-001", "title": "确认客户名单"},
                {"id": "task-card-health-002", "title": "整理项目进度"},
            ],
        },
        _builder_key(
            "build_done_card",
            "data.plugins.feishu_pet_assistant.cards",
        ): {
            "pet": {"pet_name": "小橘", "energy": 90},
            "task": {"id": "task-card-health-001", "title": "确认客户名单"},
            "stats": {"pending": 1, "done": 2},
        },
        _builder_key(
            "build_error_card",
            "data.plugins.feishu_pet_assistant.cards",
        ): {"message": "这条任务找不到或不属于你。"},
        _builder_key(
            "build_department_memory_prompt_card",
            "dc_engines.router_card_templates",
        ): {
            "state": SimpleNamespace(
                department_names=["策划", "客户部"],
                suggestion_id="dept-memory-card-health-001",
            ),
        },
        _builder_key(
            "build_sop_signal_confirmation_card",
            "dc_engines.router_card_templates",
        ): {
            "state": SimpleNamespace(
                signal_id="sop-signal-card-health-001",
                signal={
                    "title": "记住这个 SOP",
                    "message": "以后客户节日问候先确认客户名单和发送窗口。",
                },
            ),
        },
    }
    return samples.get(
        _builder_key(builder_name, builder_module), samples.get(builder_name, {})
    )


def _active_runtime_bypasses() -> list[str]:
    findings: list[str] = []
    for root in CARD_RUNTIME_BYPASS_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or any(
                part.startswith("_backup") for part in parts
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(DC_ROOT)
            has_direct_interactive_send = any(
                marker in text for marker in CARD_RUNTIME_INTERACTIVE_CARD_MARKERS
            )
            for lineno, line in enumerate(text.splitlines(), start=1):
                if any(pattern in line for pattern in CARD_RUNTIME_BYPASS_PATTERNS):
                    findings.append(f"{rel}:{lineno}")
                    continue
                if has_direct_interactive_send and any(
                    pattern in line
                    for pattern in CARD_RUNTIME_DIRECT_FEISHU_SEND_PATTERNS
                ):
                    findings.append(f"{rel}:{lineno}")
    return findings


def _active_runtime_card_type_literals() -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for root in CARD_RUNTIME_CARD_TYPE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or any(
                part.startswith("_backup") for part in parts
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(DC_ROOT)
            for lineno, line in enumerate(text.splitlines(), start=1):
                for match in CARD_TYPE_LITERAL_RE.finditer(line):
                    card_type = match.group(1)
                    findings.setdefault(card_type, []).append(f"{rel}:{lineno}")
    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _literal_strings(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.IfExp):
        values = set(_literal_strings(node.body))
        values.update(_literal_strings(node.orelse))
        return tuple(sorted(values))
    values: set[str] = set()
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            values.update(_literal_strings(value))
    elif isinstance(node, ast.Tuple | ast.List | ast.Set):
        for value in node.elts:
            values.update(_literal_strings(value))
    return tuple(sorted(values))


def _card_type_default_for_function(node: ast.AST | None) -> str:
    if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
        return ""
    kw_defaults = zip(node.args.kwonlyargs, node.args.kw_defaults, strict=False)
    for arg, default in kw_defaults:
        if arg.arg == "card_type" and isinstance(default, ast.Constant):
            return str(default.value) if isinstance(default.value, str) else ""
    positional_args = node.args.posonlyargs + node.args.args
    positional_defaults = list(node.args.defaults)
    default_offset = len(positional_args) - len(positional_defaults)
    for index, default in enumerate(positional_defaults, start=default_offset):
        if positional_args[index].arg == "card_type" and isinstance(
            default, ast.Constant
        ):
            return str(default.value) if isinstance(default.value, str) else ""
    return ""


def _runtime_gateway_call_sites() -> list[CardRuntimeCallSite]:
    sites: list[CardRuntimeCallSite] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, *, rel_path: str, source: str) -> None:
            self.rel_path = rel_path
            self.source = source
            self.function_stack: list[ast.AsyncFunctionDef | ast.FunctionDef] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self.function_stack.append(node)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self.function_stack.append(node)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Call(self, node: ast.Call) -> Any:
            gateway = _call_name(node.func)
            if gateway not in CARD_RUNTIME_GATEWAY_CALLS:
                self.generic_visit(node)
                return

            card_type_kw = next(
                (keyword for keyword in node.keywords if keyword.arg == "card_type"),
                None,
            )
            if card_type_kw is None:
                sites.append(
                    CardRuntimeCallSite(
                        path=self.rel_path,
                        line=node.lineno,
                        function=self._function_name(),
                        gateway=gateway,
                        expression="<missing>",
                        dynamic=True,
                    )
                )
                self.generic_visit(node)
                return

            value = card_type_kw.value
            literals = _literal_strings(value)
            dynamic = not (
                isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
            sites.append(
                CardRuntimeCallSite(
                    path=self.rel_path,
                    line=node.lineno,
                    function=self._function_name(),
                    gateway=gateway,
                    expression=ast.unparse(value),
                    literal_card_types=literals,
                    dynamic=dynamic,
                    dynamic_reason=self._dynamic_reason(value, literals, dynamic),
                )
            )
            self.generic_visit(node)

        def _function_name(self) -> str:
            if not self.function_stack:
                return "<module>"
            return self.function_stack[-1].name

        def _dynamic_reason(
            self,
            value: ast.AST,
            literals: tuple[str, ...],
            dynamic: bool,
        ) -> str:
            if not dynamic:
                return ""
            if literals and all(card_type in CARD_REGISTRY for card_type in literals):
                return "registered literal expression"
            function_name = self._function_name()
            forwarder = CARD_RUNTIME_DYNAMIC_CARD_TYPE_FORWARDERS.get(
                (self.rel_path, function_name)
            )
            if forwarder:
                return forwarder
            default_card_type = _card_type_default_for_function(
                self.function_stack[-1] if self.function_stack else None
            )
            if isinstance(value, ast.Name) and value.id == "card_type":
                if default_card_type in CARD_REGISTRY:
                    return "registered card_type parameter default"
            return ""

    for root in CARD_RUNTIME_CARD_TYPE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or any(
                part.startswith("_backup") for part in parts
            ):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                source = path.read_text(encoding="utf-8", errors="ignore")
            rel_path = str(path.relative_to(DC_ROOT))
            try:
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                sites.append(
                    CardRuntimeCallSite(
                        path=rel_path,
                        line=1,
                        function="<module>",
                        gateway="<parse>",
                        expression="<syntax-error>",
                        dynamic=True,
                    )
                )
                continue
            Visitor(rel_path=rel_path, source=source).visit(tree)
    return sorted(sites, key=lambda item: (item.path, item.line, item.gateway))


def _runtime_card_type_reference_sites() -> list[CardRuntimeCallSite]:
    sites: list[CardRuntimeCallSite] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, *, rel_path: str) -> None:
            self.rel_path = rel_path
            self.function_stack: list[ast.AsyncFunctionDef | ast.FunctionDef] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self.function_stack.append(node)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self.function_stack.append(node)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Call(self, node: ast.Call) -> Any:
            card_type_kw = next(
                (keyword for keyword in node.keywords if keyword.arg == "card_type"),
                None,
            )
            if card_type_kw is None:
                self.generic_visit(node)
                return

            literals = _literal_strings(card_type_kw.value)
            if literals:
                sites.append(
                    CardRuntimeCallSite(
                        path=self.rel_path,
                        line=node.lineno,
                        function=self._function_name(),
                        gateway=_call_name(node.func) or "<call>",
                        expression=ast.unparse(card_type_kw.value),
                        literal_card_types=literals,
                        dynamic=not (
                            isinstance(card_type_kw.value, ast.Constant)
                            and isinstance(card_type_kw.value.value, str)
                        ),
                    )
                )
            self.generic_visit(node)

        def _function_name(self) -> str:
            if not self.function_stack:
                return "<module>"
            return self.function_stack[-1].name

    for root in CARD_RUNTIME_CARD_TYPE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or any(
                part.startswith("_backup") for part in parts
            ):
                continue
            rel_path = str(path.relative_to(DC_ROOT))
            if rel_path == "dc_engines/dc_engines/card_system.py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                source = path.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                continue
            Visitor(rel_path=rel_path).visit(tree)
    return sorted(sites, key=lambda item: (item.path, item.line, item.gateway))


def _latest_grey_push_by_card_type(limit: int = 500) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in recent_card_runtime_events(limit):
        if item.get("event") != "grey_push":
            continue
        card_type = str(item.get("card_type") or "")
        if card_type in CARD_REGISTRY:
            latest[card_type] = item
    return latest


def build_card_asset_manifest() -> dict[str, Any]:
    runtime_call_sites = _runtime_gateway_call_sites()
    runtime_reference_sites = _runtime_card_type_reference_sites()
    latest_grey = _latest_grey_push_by_card_type()
    cards = []
    for card_type, spec in CARD_REGISTRY.items():
        sample_payload = _sample_payload(spec.builder, spec.builder_module)
        card_gateway_sites = [
            item.to_dict()
            for item in runtime_call_sites
            if card_type in item.literal_card_types
        ]
        card_reference_sites = [
            item.to_dict()
            for item in runtime_reference_sites
            if card_type in item.literal_card_types
        ]
        runtime_status = (
            "gateway"
            if card_gateway_sites
            else "wrapper"
            if card_reference_sites
            else "sample_only"
        )
        cards.append(
            {
                **spec.to_dict(),
                "builder_key": _builder_key(spec.builder, spec.builder_module),
                "sample_payload_keys": sorted(sample_payload),
                "has_sample_payload": bool(sample_payload),
                "runtime_call_sites": card_gateway_sites,
                "runtime_card_type_references": card_reference_sites,
                "runtime_status": runtime_status,
                "latest_grey_push": latest_grey.get(card_type),
            }
        )
    return {
        "schema": 1,
        "generated_at": _utc_now(),
        "card_count": len(cards),
        "runtime_gateway_call_site_count": len(runtime_call_sites),
        "runtime_card_type_reference_count": len(runtime_reference_sites),
        "cards": cards,
        "runtime_gateway_call_sites": [item.to_dict() for item in runtime_call_sites],
        "runtime_card_type_references": [
            item.to_dict() for item in runtime_reference_sites
        ],
    }


def format_card_asset_matrix(manifest: dict[str, Any] | None = None) -> str:
    if manifest is None:
        manifest = build_card_asset_manifest()

    lines = [
        f"Card Asset Matrix: {manifest['card_count']} cards, "
        f"{manifest['runtime_gateway_call_site_count']} runtime gateway call sites, "
        f"{manifest['runtime_card_type_reference_count']} card_type references",
        "card_type | owner | builder | triggers | runtime_status | gateway_sites/runtime_refs | grey_push",
        "--- | --- | --- | --- | --- | --- | ---",
    ]
    for item in manifest["cards"]:
        runtime_sites = item.get("runtime_call_sites") or []
        runtime_refs = item.get("runtime_card_type_references") or []
        latest_grey = item.get("latest_grey_push") or {}
        grey_status = "OK" if latest_grey.get("ok") else "-"
        trigger_text = ", ".join(item.get("triggers") or ())
        builder_key = item.get("builder_key") or (
            f"{item.get('builder_module')}:{item.get('builder')}"
        )
        lines.append(
            f"{item['card_type']} | {item['owner']} | {builder_key} | "
            f"{trigger_text} | {item['runtime_status']} | "
            f"{len(runtime_sites)}/{len(runtime_refs)} | {grey_status}"
        )
    return "\n".join(lines)


def run_card_system_engineering_gate(
    *,
    require_recent_grey: bool = False,
) -> CardHealthReport:
    report = run_card_system_health()
    manifest = build_card_asset_manifest()

    missing_metadata = sorted(
        card["card_type"]
        for card in manifest["cards"]
        if not card.get("owner")
        or not card.get("version")
        or not card.get("builder")
        or not card.get("builder_module")
        or not card.get("triggers")
        or not card.get("fallback")
        or not card.get("upgrade_mode")
        or card.get("runtime_status") not in {"gateway", "wrapper", "sample_only"}
    )
    report.add(
        "engineering_manifest:registry_metadata",
        not missing_metadata,
        "all registered cards have owner/version/builder/module/trigger/fallback/upgrade metadata"
        if not missing_metadata
        else f"missing metadata for {missing_metadata}",
    )

    missing_samples = sorted(
        card["card_type"]
        for card in manifest["cards"]
        if not card["has_sample_payload"]
    )
    report.add(
        "engineering_manifest:sample_payloads",
        not missing_samples,
        "all registered cards have sample payloads"
        if not missing_samples
        else f"missing samples for {missing_samples}",
    )

    unknown_gateway_literals: list[str] = []
    unknown_reference_literals: list[str] = []
    unsupported_dynamic: list[str] = []
    for site in manifest["runtime_gateway_call_sites"]:
        unknown_literals = sorted(
            card_type
            for card_type in site["literal_card_types"]
            if card_type not in CARD_REGISTRY
        )
        if unknown_literals:
            unknown_gateway_literals.append(
                f"{site['path']}:{site['line']} {unknown_literals}"
            )
        if site["dynamic"] and not site["dynamic_reason"]:
            unsupported_dynamic.append(
                f"{site['path']}:{site['line']} {site['function']} {site['expression']}"
            )
    for site in manifest["runtime_card_type_references"]:
        unknown_literals = sorted(
            card_type
            for card_type in site["literal_card_types"]
            if card_type not in CARD_REGISTRY
        )
        if unknown_literals:
            unknown_reference_literals.append(
                f"{site['path']}:{site['line']} {unknown_literals}"
            )

    report.add(
        "engineering_manifest:runtime_gateway_card_types",
        not unknown_gateway_literals
        and not unknown_reference_literals
        and not unsupported_dynamic,
        "runtime gateway and wrapper card_type references are registered or approved typed forwarders"
        if not unknown_gateway_literals
        and not unknown_reference_literals
        and not unsupported_dynamic
        else "; ".join(
            (
                unknown_gateway_literals
                + unknown_reference_literals
                + unsupported_dynamic
            )[:20]
        ),
    )

    latest_grey = _latest_grey_push_by_card_type()
    grey_missing = sorted(set(CARD_REGISTRY).difference(latest_grey))
    grey_failed = sorted(
        card_type for card_type, item in latest_grey.items() if not item.get("ok")
    )
    grey_ok = not grey_failed and (not require_recent_grey or not grey_missing)
    detail = f"{len(latest_grey)}/{len(CARD_REGISTRY)} registered card types have grey_push evidence"
    if grey_failed:
        detail += f"; failed={grey_failed}"
    if require_recent_grey and grey_missing:
        detail += f"; missing={grey_missing}"
    report.add("engineering_manifest:grey_evidence", grey_ok, detail)

    return report


def run_card_system_health() -> CardHealthReport:
    report = CardHealthReport(ok=True)
    contract_ok, contract_detail = validate_card_contract()
    report.add("contract:feishu_card_system", contract_ok, contract_detail)

    missing_specs = sorted(REQUIRED_CARD_TYPES.difference(CARD_REGISTRY))
    report.add(
        "registry_required_types",
        not missing_specs,
        ", ".join(missing_specs)
        if missing_specs
        else "all required card types registered",
    )

    try:
        state = load_card_version_state()
        missing_state = sorted(REQUIRED_CARD_TYPES.difference(state))
        bad_rollout = sorted(
            card_type
            for card_type, item in state.items()
            if str(item.get("rollout", "")) not in {"stable", "grey", "rollback"}
        )
        blank_version = sorted(
            card_type
            for card_type, item in state.items()
            if not item.get("active_version")
        )
        state_ok = not missing_state and not bad_rollout and not blank_version
        detail_parts = []
        if missing_state:
            detail_parts.append(f"missing={missing_state}")
        if bad_rollout:
            detail_parts.append(f"bad_rollout={bad_rollout}")
        if blank_version:
            detail_parts.append(f"blank_version={blank_version}")
        report.add(
            "version_state",
            state_ok,
            "; ".join(detail_parts) if detail_parts else str(CARD_STATE_PATH),
        )
    except Exception as exc:  # noqa: BLE001
        report.add("version_state", False, f"{type(exc).__name__}: {exc}")

    try:
        CARD_RUNTIME_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CARD_RUNTIME_EVENTS_PATH.touch(exist_ok=True)
        report.add("runtime_events_writable", True, str(CARD_RUNTIME_EVENTS_PATH))
    except Exception as exc:  # noqa: BLE001
        report.add("runtime_events_writable", False, f"{type(exc).__name__}: {exc}")

    events = recent_card_runtime_events(30)
    production_events = _production_card_runtime_events(events)
    recovered_events = _drop_recovered_grey_push_failures(production_events)
    # Dedupe retries: see ``_dedupe_consecutive_failures`` for the
    # streamer race that produced 2-3 finalizes on the same message_id.
    # We count distinct production failures only.
    deduped = _dedupe_consecutive_failures(recovered_events)
    recent_failures = [item for item in deduped if not item.get("ok")]
    # Tolerate a small number of distinct failures so one known streamer race
    # does not flood health logs, while independent failures are still counted.
    # Do not raise this threshold casually; dedup should absorb retry noise.
    events_ok = len(recent_failures) <= 3
    raw_failures = sum(1 for item in production_events if not item.get("ok"))
    recovered_failures = raw_failures - sum(
        1 for item in recovered_events if not item.get("ok")
    )
    retry_failures_folded = sum(
        1 for item in recovered_events if not item.get("ok")
    ) - len(recent_failures)
    ignored_events = len(events) - len(production_events)
    report.add(
        "runtime_events_recent",
        events_ok,
        "no runtime events yet"
        if not events
        else (
            f"{len(events)} recent events, no production card events "
            f"({ignored_events} non-production events ignored)"
            if not production_events
            else (
                f"{len(events)} recent events, {len(production_events)} production, "
                f"{len(recent_failures)} distinct production failures "
                f"({retry_failures_folded} dup retries folded, "
                f"{recovered_failures} recovered grey failures cleared, "
                f"{ignored_events} non-production events ignored, tolerates <=3)"
                if events_ok
                else f"{len(events)} recent events, {len(production_events)} production, "
                f"{len(recent_failures)} distinct production failures "
                f"({retry_failures_folded} dup retries folded, "
                f"{recovered_failures} recovered grey failures cleared, "
                f"{ignored_events} non-production events ignored)"
            )
        ),
    )

    try:
        builders = _template_builders()
        report.add("template_import", True, "dc_engines.feishu_card_streamer.templates")
    except Exception as exc:  # noqa: BLE001
        report.add("template_import", False, f"{type(exc).__name__}: {exc}")
        return report

    public_template_builders = {
        name
        for name in builders
        if name.startswith("build_") and name.endswith("_card")
    }
    template_registered_builders = {
        spec.builder
        for spec in CARD_REGISTRY.values()
        if spec.builder_module == DEFAULT_CARD_BUILDER_MODULE
    }
    registered_builder_keys = {
        _builder_key(spec.builder, spec.builder_module)
        for spec in CARD_REGISTRY.values()
    }
    missing_registered_builders = sorted(
        spec.card_type
        for spec in CARD_REGISTRY.values()
        if _builder_for_spec(spec) is None
    )
    unregistered_public_builders = sorted(
        public_template_builders.difference(template_registered_builders).difference(
            PRIVATE_TEMPLATE_BUILDERS
        )
    )
    duplicate_builders = sorted(
        key
        for key in registered_builder_keys
        if sum(
            1
            for spec in CARD_REGISTRY.values()
            if _builder_key(spec.builder, spec.builder_module) == key
        )
        > 1
    )
    complete_ok = (
        not missing_registered_builders
        and not unregistered_public_builders
        and not duplicate_builders
    )
    detail_parts = []
    if missing_registered_builders:
        detail_parts.append(f"missing_builders={missing_registered_builders}")
    if unregistered_public_builders:
        detail_parts.append(f"unregistered_builders={unregistered_public_builders}")
    if duplicate_builders:
        detail_parts.append(f"duplicate_builders={duplicate_builders}")
    report.add(
        "registry_template_completeness",
        complete_ok,
        "; ".join(detail_parts)
        if detail_parts
        else f"{len(public_template_builders)} public template builders registered",
    )

    try:
        from dc_engines import card_runtime  # noqa: PLC0415

        runtime_callables = (
            "assert_registered_card",
            "send_card_via_runtime",
            "finalize_card_via_runtime",
        )
        runtime_ok = all(
            callable(getattr(card_runtime, name, None)) for name in runtime_callables
        )
        report.add(
            "runtime_gateway",
            runtime_ok,
            "dc_engines.card_runtime"
            if runtime_ok
            else "missing runtime gateway callables",
        )
    except Exception as exc:  # noqa: BLE001
        report.add("runtime_gateway", False, f"{type(exc).__name__}: {exc}")

    bypasses = _active_runtime_bypasses()
    report.add(
        "runtime_gateway_bypass_scan",
        not bypasses,
        "no active plugin/tool card runtime bypasses"
        if not bypasses
        else "; ".join(bypasses[:20]),
    )

    runtime_card_types = _active_runtime_card_type_literals()
    unknown_runtime_card_types = sorted(
        card_type
        for card_type in runtime_card_types
        if card_type not in CARD_REGISTRY and card_type != "unknown_card"
    )
    report.add(
        "runtime_card_type_literals",
        not unknown_runtime_card_types,
        "all runtime literal card_type values are registered"
        if not unknown_runtime_card_types
        else "; ".join(
            f"{card_type}: {', '.join(runtime_card_types[card_type][:3])}"
            for card_type in unknown_runtime_card_types
        ),
    )

    for spec in CARD_REGISTRY.values():
        try:
            builder = _builder_for_spec(spec)
        except Exception as exc:  # noqa: BLE001
            report.add(
                f"builder:{spec.card_type}",
                False,
                f"{spec.builder_module}:{spec.builder} import failed: {type(exc).__name__}: {exc}",
            )
            continue
        if builder is None:
            report.add(
                f"builder:{spec.card_type}",
                False,
                f"{spec.builder_module}:{spec.builder}",
            )
            continue
        try:
            payload = _sample_payload(spec.builder, spec.builder_module)
            card = builder(**payload)
            ok = isinstance(card, dict) and bool(
                card.get("body") or card.get("elements")
            )
            report.add(
                f"sample:{spec.card_type}",
                ok,
                "sample card generated" if ok else "builder returned invalid card",
            )
        except TypeError as exc:
            report.add(
                f"sample:{spec.card_type}", False, f"sample payload mismatch: {exc}"
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                f"sample:{spec.card_type}", False, f"{type(exc).__name__}: {exc}"
            )

    report.add(
        "router:casual_fallback",
        should_render_casual_reply_card(intent="", message="你好呀"),
        "short non-task message routes to casual card",
    )
    report.add(
        "router:task_waiting",
        should_start_waiting_card(intent="", message="帮我写一段端午客户问候话术"),
        "task-like message routes to waiting card",
    )

    return report
