"""Engineering registry and health checks for Feishu card rendering.

This module is intentionally host-agnostic: it does not import AstrBot. Runtime
plugins can use it to prove that card templates, trigger ownership and upgrade
metadata are present before handling messages.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
CARD_RUNTIME_BYPASS_PATTERNS = (
    "streamer.start(",
    "streamer.finalize(",
    ".streamer.finalize(",
    "CreateMessageRequest",
    "message.create(",
    "message.acreate(",
    "record_card_runtime_event",
)


@dataclass(frozen=True, slots=True)
class CardSpec:
    card_type: str
    version: str
    owner: str
    builder: str
    triggers: tuple[str, ...]
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
    "skill_list": CardSpec(
        card_type="skill_list",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_list_card",
        triggers=("skill list", "boss/cowork skill list"),
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
    "skill_confirm": CardSpec(
        card_type="skill_confirm",
        version="1.0",
        owner="hermes_bridge",
        builder="build_skill_confirm_card",
        triggers=("dangerous skill action", "delete/rollback/restore confirm"),
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


REQUIRED_CARD_TYPES = frozenset(
    {
        "casual_reply",
        "thinking_waiting",
        "daily_response",
        "media_generation",
        "truth_intake_request",
        "truth_intake_received",
        "source_trace",
        "devops_status",
        "onboarding_department",
        "onboarding_role",
        "training_lesson",
        "training_quiz",
        "skill_list",
        "skill_review",
        "skill_confirm",
    }
)
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

    recent_failures = [item for item in events if not item.get("ok")]
    if recent_failures:
        latest = recent_failures[-1]
        return (
            "NEXT: inspect recent card runtime failure before widening rollout: "
            f"{latest.get('card_type')} {latest.get('event')} {latest.get('detail')}"
        )

    return (
        "NEXT: health and runtime events are green; proceed to small Feishu grey "
        "validation with scripts-tools/card-grey-push.py or continue template upgrade."
    )


def _template_builders() -> dict[str, Callable[..., dict[str, Any]]]:
    from dc_engines.feishu_card_streamer import templates

    return {
        name: getattr(templates, name)
        for name in dir(templates)
        if name.startswith("build_") and callable(getattr(templates, name))
    }


def build_sample_card(card_type: str) -> dict[str, Any]:
    spec = CARD_REGISTRY.get(card_type)
    if spec is None:
        raise KeyError(f"unknown card_type: {card_type}")
    builders = _template_builders()
    builder = builders.get(spec.builder)
    if builder is None:
        raise KeyError(f"missing builder for {card_type}: {spec.builder}")
    return builder(**_sample_payload(spec.builder))


def _sample_payload(builder_name: str) -> dict[str, Any]:
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
        "build_tutorial_lesson_card": {"lesson_id": "lesson_reasoning"},
        "build_quiz_question_card": {"q_num": 1, "total": 5},
        "build_skill_list_card": {
            "kind": "boss",
            "skills": [{"slug": "demo", "title": "示例 skill", "status": "active"}],
        },
        "build_skill_review_card": {
            "kind": "boss",
            "review": {
                "slug": "demo",
                "title": "示例 skill",
                "summary": "结构完整，可进入灰度。",
            },
        },
        "build_skill_confirm_card": {
            "operation": "回滚",
            "kind": "boss",
            "slug": "demo",
            "version": "v1",
        },
    }
    return samples.get(builder_name, {})


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
            for lineno, line in enumerate(text.splitlines(), start=1):
                if any(pattern in line for pattern in CARD_RUNTIME_BYPASS_PATTERNS):
                    findings.append(f"{rel}:{lineno}")
    return findings


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
    recent_failures = [item for item in events if not item.get("ok")]
    report.add(
        "runtime_events_recent",
        not recent_failures,
        "no runtime events yet"
        if not events
        else f"{len(events)} recent events, {len(recent_failures)} failures",
    )

    try:
        builders = _template_builders()
        report.add("template_import", True, "dc_engines.feishu_card_streamer.templates")
    except Exception as exc:  # noqa: BLE001
        report.add("template_import", False, f"{type(exc).__name__}: {exc}")
        return report

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
        "no active plugin/script bypasses"
        if not bypasses
        else "; ".join(bypasses[:20]),
    )

    for spec in CARD_REGISTRY.values():
        builder = builders.get(spec.builder)
        if builder is None:
            report.add(f"builder:{spec.card_type}", False, spec.builder)
            continue
        try:
            payload = _sample_payload(spec.builder)
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
