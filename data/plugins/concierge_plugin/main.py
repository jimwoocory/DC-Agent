"""飞书接待机器人 Star 插件（P1：员工档案 + 长期记忆注入）。

职责：
1. 飞书任何消息（私聊 + 群里 @ 都触发）→ 识别员工身份
2. 首次接触 → 自动建空档案 + 在 LLM system_prompt 里注入"友好引导自我介绍"hint
3. 后续每次对话 → 注入员工档案 + 长期记忆到 LLM system_prompt
4. 跟踪交互（last_seen + count）
5. **不抢答**：本插件只做"前置增强"，让 LLM 用增强后的上下文回答；现有 task/case
   /group_summary 等插件链路完全不受影响

跨平台说明：
- 飞书：``event.get_sender_id()`` 返回 open_id（app-scoped）
- QQ：sender_id 是 QQ 号；本插件不限制平台，但飞书是主战场

长期记忆来源：
- v0：员工的 ``preferences`` + 已写入 ``employee_memories`` 表的内容
- v1+：LLM 响应后自动摘要重要信息回写记忆表（留下一版）
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dc_engines.employee_directory import (
    Employee,
    EmployeeMemory,
    EmployeeMemoryBridge,
    EmployeeStore,
    RelationType,
    sync_from_feishu,
)
from dc_engines.feishu_reader import FeishuClient
from dc_engines.feishu_reader.whitelist import load_whitelist

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

# ── 自我介绍解析 ──────────────────────────────────────────────
# 简单关键词；命中即认为是在自我介绍并尝试抽取
_INTRO_TRIGGERS: tuple[str, ...] = (
    "我是",
    "我叫",
    "我的名字",
    "我来自",
    "我在",
    "i'm",
    "i am",
)

# 抽取 "我是 XXX" 中的名字
_NAME_RE = re.compile(r"(?:我是|我叫|我的名字(?:是|叫)?)\s*([一-龥A-Za-z·\.]{1,12})")
_DEPT_RE = re.compile(r"(?:我(?:来自|在|属于))\s*([一-龥]{2,15}(?:部|组|中心|团队))")
_ROLE_RE = re.compile(
    r"(?:我是|我做|我负责|岗位是|职位是).{0,10}?"
    r"(业务|执行|设计|文案|产品|运营|技术|开发|测试|HR|财务|行政|品宣|中台)"
)
_ADDRESS_CORRECTION_RE = re.compile(
    r"(?:以后|今后|之后|下次|请|麻烦|可以)?"
    r"(?:叫我|称呼我|喊我)\s*[「“\"]?"
    r"([一-龥A-Za-z0-9·]{1,16}(?:总|老师|哥|姐|同学|同事|先生|女士)?)"
    r"[」”\"]?"
)
_BAD_ADDRESS_WORDS = {"你", "您", "他", "她", "它", "我们", "他们", "她们"}
_FORMAL_STYLE_HINTS = ("用敬语", "多用敬语", "叫您", "称您", "用您", "尊敬")
_WARM_STYLE_HINTS = (
    "不用敬语",
    "别用敬语",
    "不要用敬语",
    "不用您",
    "别叫您",
    "别称您",
    "不用太正式",
    "别太正式",
    "自然一点",
)
_STYLE_APPEND_LIMIT = 220

# ── 长期事实抽取（每次 LLM 响应后跑） ──────────────────────────────
# 匹配的每一组都会写一条 EmployeeMemory（content=整句，kind 按下表）
# 注意：用反向 lookahead 避免短文本误命中
_FACT_PATTERNS: tuple[tuple[str, str, float], ...] = (
    # (regex, kind, relevance)
    (
        r"我(?:喜欢|爱|偏好)(?:用|做|看|听|吃|喝|玩)?\s*([一-龥A-Za-z·\s]{1,30})",
        "preference",
        0.7,
    ),
    (r"我不(?:喜欢|爱|想)\s*([一-龥A-Za-z·\s]{1,30})", "preference", 0.7),
    (r"我经常\s*([一-龥A-Za-z·\s]{1,30})", "preference", 0.5),
    (r"我(?:会|擅长|能|懂|熟悉)\s*([一-龥A-Za-z·\s]{1,30})", "skill", 0.7),
    (r"我(?:做|负责|管|在做)(?:的是)?\s*([一-龥A-Za-z·\s]{2,30})", "role", 0.6),
    (r"(?:记住|以后|备注|注意)[:：,，]?\s*(.{4,80})", "fact", 0.9),
)
_FACT_RE: list[tuple[re.Pattern, str, float]] = [
    (re.compile(p), k, r) for p, k, r in _FACT_PATTERNS
]

# 命令前缀——LLM 不会响应这些，事实抽取也要跳过
_COMMAND_PREFIXES: tuple[str, ...] = (
    "/me",
    "/employees",
    "/chat",
    "/task",
    "/case",
    "/help",
    "/reset",
    "me ",
    "employees ",
    "chat ",
)

_BOSS_ALIAS_NAMES = {"老板", "老总", "老大"}
_DEFAULT_BOSS_ADDRESS = "杨总"
_BOSS_NAME_HINTS = {*_BOSS_ALIAS_NAMES, "杨国民", "杨总"}
_BOSS_ROLE_HINTS = ("老板", "老总", "总经理", "董事长", "创始人", "CEO", "总裁")
_MANAGER_ROLE_HINTS = ("负责人", "主管", "经理", "总监")
FEATURE_KEY = "employee_memory_identity"
FEATURE_NAME = "员工身份称呼与记忆画像护栏"
FEATURE_STAGE = "P0-P2"
FEATURE_ENTRYPOINT = "feishu_daily_llm_pipeline"
FEATURE_HARNESS_ROLE = "governance_audit"
FEATURE_HARNESS_WORKFLOW_KIND = "employee_memory_identity_audit"
_EMPLOYEE_MEMORY_PLATFORM_IDS = {
    "巅池-Agent小助手",
    "巅池-技术（DevOps）",
    "巅池-技术",
}
_FEISHU_PLATFORM_NAMES = {"lark"}
_IDENTITY_OVERRIDE_FIELDS = {
    "display_name",
    "department",
    "role",
    "relation_type",
    "preferred_address",
    "honorific_policy",
    "personality_summary",
    "communication_style",
}
_PERSONA_MIN_EVIDENCE = 3
_PERSONA_EVIDENCE_PREFIX = "persona_evidence:"
_PERSONA_PROFILE_PREFIX = "persona_profile:"
_PERSONA_SIGNAL_RULES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "结论导向",
        ("结论", "重点", "直接说", "直说", "别绕", "一句话", "简单说", "先说"),
        "偏好结论先行、少铺垫、直接给重点",
        "表达出对直接、重点、结论先行的偏好",
    ),
    (
        "执行闭环意识强",
        ("尽快", "马上", "推进", "落地", "闭环", "验收", "排期", "结果"),
        "关注下一步动作、负责人、时间点和验收结果",
        "反复关注推进、落地、闭环或结果",
    ),
    (
        "风险敏感",
        ("风险", "稳妥", "不要出错", "安全", "权限", "合规", "备份", "回滚"),
        "需要提前说明风险、边界和兜底方案",
        "关注风险、安全、权限、合规或回滚",
    ),
    (
        "细节审慎",
        ("详细", "细节", "展开", "一步一步", "依据", "为什么", "完整"),
        "需要给出必要依据、步骤和关键细节",
        "要求更多细节、依据或完整步骤",
    ),
    (
        "重视正式尊重",
        ("尊敬", "敬语", "称呼", "您", "杨总", "老板", "老总"),
        "需要保持正式、尊重的称呼和语气",
        "明确强调敬语、称呼或尊重语气",
    ),
)


@register(
    "concierge_plugin",
    "dc_agent",
    f"公司接待机器人：{FEATURE_NAME}（{FEATURE_STAGE}）",
    "1.0.0",
)
class ConciergePlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.store: EmployeeStore | None = None
        self.memory_bridge = EmployeeMemoryBridge()
        self.feishu_client: FeishuClient | None = None
        self.identity_overrides: dict[str, dict] = {"by_open_id": {}, "by_name": {}}

    async def initialize(self) -> None:
        data_dir = Path(__file__).resolve().parents[3] / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = EmployeeStore(str(data_dir / "employees.db"))
        await self.store.initialize()
        self.identity_overrides = self._load_identity_overrides(
            data_dir / "config" / "employee_identity_overrides.json"
        )
        self.memory_bridge = self._load_memory_bridge(
            data_dir / "config" / "employee_memory_bridge.json"
        )

        # 暴露到 context（其他 plugin 也可读）
        self.context.employee_store = self.store

        logger.info("[concierge] EmployeeStore 启动：%s", data_dir / "employees.db")

        # 尝试加载 Feishu 凭证给 /employees sync 用
        wl_path = data_dir / "feishu_whitelist.yaml"
        try:
            _, creds = load_whitelist(wl_path)
            if creds and creds.enable:
                self.feishu_client = FeishuClient(creds)
                logger.info("[concierge] FeishuClient 启动 → /employees sync 可用")
            else:
                logger.info(
                    "[concierge] feishu 凭证缺失 → /employees sync 不可用"
                    "（编辑 %s 启用）",
                    wl_path,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] 加载 feishu 凭证异常：%s", exc)

        # 注册 Dashboard Web API
        try:
            self.context.register_web_api(
                "/employees",
                self._api_list_employees,
                ["GET"],
                "列出员工目录",
            )
            self.context.register_web_api(
                "/employees/eval",
                self._api_eval_employees,
                ["GET"],
                "评估员工目录记忆健康度",
            )
            self.context.register_web_api(
                "/employees/trace/<open_id>",
                self._api_trace_employee,
                ["GET"],
                "查询员工上下文注入命中日志",
            )
            self.context.register_web_api(
                "/employees/preview/<open_id>",
                self._api_preview_employee,
                ["GET"],
                "预览员工上下文注入内容",
            )
            self.context.register_web_api(
                "/employees/<open_id>",
                self._api_get_employee,
                ["GET"],
                "查询单个员工档案 + 记忆",
            )
            logger.info(
                "[concierge] Dashboard API 已注册："
                "/api/plug/employees, /api/plug/employees/eval, "
                "/api/plug/employees/trace/<open_id>, "
                "/api/plug/employees/preview/<open_id>, "
                "/api/plug/employees/<open_id>"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] 注册 web API 失败：%s", exc)

    # 接待引导职责【只归小助手】。
    # 其他机器人（巅池-技术 / 巅池-推广 等）只用自己的人格回答，不做员工身份采集。
    # 这样员工只会被"小助手"问一次自我介绍，记住后所有机器人共享档案。
    _CONCIERGE_PLATFORMS = {"巅池-Agent小助手"}

    def _is_concierge_platform(self, event: AstrMessageEvent) -> bool:
        return (event.get_platform_id() or "") in self._CONCIERGE_PLATFORMS

    def _bridge(self) -> EmployeeMemoryBridge:
        return getattr(self, "memory_bridge", EmployeeMemoryBridge())

    def _load_memory_bridge(self, path: Path) -> EmployeeMemoryBridge:
        default_nas_inbox = self._default_nas_inbox_dir(Path(__file__).parents[3])
        if not path.exists():
            return EmployeeMemoryBridge(nas_inbox_dir=default_nas_inbox)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] 员工记忆桥接配置读取失败：%s", exc)
            return EmployeeMemoryBridge(nas_inbox_dir=default_nas_inbox)
        kb_names = raw.get("kb_names") or list(EmployeeMemoryBridge().kb_names)
        if isinstance(kb_names, str):
            kb_names = [kb_names]
        if not isinstance(kb_names, list):
            kb_names = list(EmployeeMemoryBridge().kb_names)
        nas_inbox_dir = raw.get("nas_inbox_dir") or ""
        return EmployeeMemoryBridge(
            kb_names=tuple(str(name) for name in kb_names if str(name).strip()),
            nas_inbox_dir=Path(nas_inbox_dir) if nas_inbox_dir else default_nas_inbox,
        )

    def _default_nas_inbox_dir(self, project_root: Path) -> Path | None:
        cfg_path = project_root / "nas_sync" / "config.yaml"
        if not cfg_path.exists():
            return None
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("[concierge] 读取 nas_sync/config.yaml 失败：%s", exc)
            return None
        nas_cfg = raw.get("nas", {}) or {}
        watch_cfg = raw.get("watch", {}) or {}
        mount_point = nas_cfg.get("mount_point")
        inbox_dir = watch_cfg.get("inbox_dir", "inbox")
        if not mount_point:
            return None
        return Path(str(mount_point)) / str(inbox_dir)

    def _uses_employee_memory(self, event: AstrMessageEvent) -> bool:
        try:
            platform_id = event.get_platform_id() or ""
        except Exception:  # noqa: BLE001
            platform_id = ""
        try:
            platform_name = (event.get_platform_name() or "").lower()
        except Exception:  # noqa: BLE001
            platform_name = ""
        umo = str(getattr(event, "unified_msg_origin", "") or "").lower()
        return (
            platform_id in _EMPLOYEE_MEMORY_PLATFORM_IDS
            or platform_name in _FEISHU_PLATFORM_NAMES
            or umo.startswith("lark:")
        )

    def _load_identity_overrides(self, path: Path) -> dict[str, dict]:
        if not path.exists():
            return {"by_open_id": {}, "by_name": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] 身份校准表读取失败：%s", exc)
            return {"by_open_id": {}, "by_name": {}}

        by_open_id: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        records = raw.get("employees", raw if isinstance(raw, list) else [])
        if not isinstance(records, list):
            logger.warning("[concierge] 身份校准表格式错误：employees 必须是数组")
            return {"by_open_id": {}, "by_name": {}}
        for record in records:
            if not isinstance(record, dict):
                continue
            cleaned = {
                key: value
                for key, value in record.items()
                if key in _IDENTITY_OVERRIDE_FIELDS and value not in (None, "")
            }
            open_id = str(record.get("open_id") or "").strip()
            display_name = str(
                record.get("display_name") or record.get("name") or ""
            ).strip()
            if display_name and "display_name" not in cleaned:
                cleaned["display_name"] = display_name
            if cleaned.get("relation_type") not in (
                None,
                "boss",
                "manager",
                "employee",
                "unknown",
            ):
                logger.warning(
                    "[concierge] 身份校准表跳过非法 relation_type：%s",
                    cleaned.get("relation_type"),
                )
                cleaned.pop("relation_type", None)
            if cleaned.get("honorific_policy") not in (
                None,
                "boss_formal",
                "formal",
                "warm",
            ):
                logger.warning(
                    "[concierge] 身份校准表跳过非法 honorific_policy：%s",
                    cleaned.get("honorific_policy"),
                )
                cleaned.pop("honorific_policy", None)
            if open_id:
                by_open_id[open_id] = cleaned
            if display_name:
                by_name[display_name] = cleaned
            aliases = record.get("aliases") or record.get("match_names") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                for alias in aliases:
                    alias_name = str(alias or "").strip()
                    if alias_name:
                        by_name[alias_name] = cleaned
        logger.info(
            "[concierge] 身份校准表加载：open_id=%d name=%d",
            len(by_open_id),
            len(by_name),
        )
        return {"by_open_id": by_open_id, "by_name": by_name}

    def _identity_override_for(self, emp: Employee) -> dict:
        overrides = getattr(self, "identity_overrides", None) or {}
        by_open_id = overrides.get("by_open_id", {})
        by_name = overrides.get("by_name", {})
        return by_open_id.get(emp.open_id) or by_name.get(emp.display_name) or {}

    def _derive_relation_type(self, emp: Employee) -> RelationType:
        override = self._identity_override_for(emp)
        if override.get("relation_type"):
            return override["relation_type"]
        if emp.relation_type and emp.relation_type != "employee":
            return emp.relation_type
        name = (emp.display_name or "").strip()
        role = (emp.role or "").strip()
        joined = f"{name} {role}".lower()
        if name in _BOSS_NAME_HINTS or any(
            h.lower() in joined for h in _BOSS_ROLE_HINTS
        ):
            return "boss"
        if any(h.lower() in joined for h in _MANAGER_ROLE_HINTS):
            return "manager"
        if emp.display_name:
            return "employee"
        return "unknown"

    def _preferred_address_for(self, emp: Employee, relation_type: RelationType) -> str:
        override = self._identity_override_for(emp)
        if override.get("preferred_address"):
            return override["preferred_address"]
        if emp.preferred_address:
            if relation_type == "boss" and emp.preferred_address in _BOSS_ALIAS_NAMES:
                return self._boss_address_from_name(emp.display_name)
            return emp.preferred_address
        if relation_type == "boss":
            return self._boss_address_from_name(emp.display_name)
        if relation_type == "manager":
            if emp.display_name and not emp.display_name.endswith("总"):
                return f"{emp.display_name}老师"
            return emp.display_name or "负责人"
        return emp.display_name or "同事"

    def _boss_address_from_name(self, display_name: str) -> str:
        name = (display_name or "").strip()
        if not name or name in _BOSS_ALIAS_NAMES:
            return _DEFAULT_BOSS_ADDRESS
        if name.endswith("总") and name not in _BOSS_ALIAS_NAMES:
            return name
        return f"{name[0]}总"

    def _honorific_policy_for(
        self,
        emp: Employee,
        relation_type: RelationType,
    ) -> str:
        override = self._identity_override_for(emp)
        if override.get("honorific_policy"):
            return override["honorific_policy"]
        if emp.honorific_policy and emp.honorific_policy != "formal":
            return emp.honorific_policy
        if relation_type == "boss":
            return "boss_formal"
        if relation_type == "manager":
            return "formal"
        return "warm"

    def _render_address_guard(
        self,
        emp: Employee,
        relation_type: RelationType,
        preferred_address: str,
        honorific_policy: str,
    ) -> list[str]:
        lines = [
            "## 身份与称呼铁律（优先级高于普通记忆）",
            f"- 当前对话对象 open_id={emp.open_id[:8]}...，身份类型={relation_type}。",
            f"- 本轮必须优先称呼对方为「{preferred_address}」。",
        ]
        if relation_type == "boss":
            lines.extend(
                [
                    "- 对方是公司老板/老总层级，语气必须简洁、稳重、结论先行。",
                    "- 称呼必须固定为上面的「某总」形式；禁止直呼全名、只叫名字或改叫老板/老大。",
                    f"- 面向老板时必须多使用敬语和「您」，可用「尊敬的{preferred_address}」开场。",
                    "- 不要把对方当普通员工引导自我介绍。",
                ]
            )
        elif relation_type == "manager":
            lines.extend(
                [
                    "- 对方是负责人/管理者，语气保持专业、清晰、尊重。",
                    "- 不要直呼全名；优先使用上面的称呼。",
                ]
            )
        else:
            lines.extend(
                [
                    "- 对方是员工/同事，语气温和耐心；不要在已知名字时反复询问身份。",
                    "- 如果档案不完整，可以自然补问缺失信息，但不要打断正常任务。",
                ]
            )
        if honorific_policy:
            lines.append(f"- honorific_policy={honorific_policy}。")
        return lines

    async def _ensure_identity_defaults(self, emp: Employee) -> Employee:
        if self.store is None:
            return emp
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        honorific_policy = self._honorific_policy_for(emp, relation_type)
        override = self._identity_override_for(emp)
        updates = {}
        for field in (
            "display_name",
            "department",
            "role",
            "personality_summary",
            "communication_style",
        ):
            value = override.get(field)
            if value and getattr(emp, field) != value:
                updates[field] = value
        if emp.relation_type != relation_type:
            updates["relation_type"] = relation_type
        if emp.preferred_address != preferred_address and preferred_address:
            updates["preferred_address"] = preferred_address
        if emp.honorific_policy != honorific_policy:
            updates["honorific_policy"] = honorific_policy
        if not updates:
            return emp
        updated = await self.store.update_profile(emp.open_id, **updates)
        return updated or emp

    # ─────────────────────── 交互追踪 + 自我介绍抽取 ───────────────────────

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        if self.store is None:
            return
        if not self._uses_employee_memory(event):
            return
        open_id = self._extract_open_id(event)
        if not open_id:
            return

        # 首次接触自动建档
        emp, created = await self.store.get_or_create(
            open_id,
            platform_id=event.get_platform_id() or "",
        )
        if created:
            logger.info(
                "[concierge] 新员工接入：open_id=%s platform=%s",
                open_id,
                event.get_platform_id(),
            )

        # touch
        try:
            await self.store.touch(open_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[concierge] touch 失败：%s", exc)

        # 自我介绍抽取（命中关键词才跑）
        text = (event.message_str or "").strip()
        if text and any(k in text.lower() for k in _INTRO_TRIGGERS):
            await self._maybe_capture_intro(emp, text)
            refreshed = await self.store.get_employee(open_id)
            if refreshed is not None:
                emp = refreshed
        emp = await self._ensure_identity_defaults(emp)
        if text:
            await self._maybe_apply_explicit_correction(emp, text)

    async def _maybe_capture_intro(self, emp: Employee, text: str) -> None:
        if self.store is None:
            return
        updates = {}
        if not emp.display_name:
            m = _NAME_RE.search(text)
            if m:
                updates["display_name"] = m.group(1).strip()
        if not emp.department:
            m = _DEPT_RE.search(text)
            if m:
                updates["department"] = m.group(1).strip()
        if not emp.role:
            m = _ROLE_RE.search(text)
            if m:
                updates["role"] = m.group(1).strip()
        if updates:
            await self.store.update_profile(emp.open_id, **updates)
            logger.info(
                "[concierge] 从自我介绍抽到：open_id=%s %s",
                emp.open_id,
                updates,
            )
            # 同步记一条 memory（context 类）
            await self.store.add_memory(
                emp.open_id,
                "context",
                f"自我介绍抽取：{text[:200]}",
                relevance=0.7,
            )

    async def _maybe_apply_explicit_correction(
        self,
        emp: Employee,
        text: str,
    ) -> Employee:
        if self.store is None:
            return emp
        updates: dict = {}
        notes: list[str] = []
        relation_type = self._derive_relation_type(emp)

        address = self._extract_address_correction(text)
        if address:
            if relation_type == "boss" and not address.endswith("总"):
                notes.append(f"老板场景未采纳非「某总」称呼「{address}」")
            else:
                updates["preferred_address"] = address
                notes.append(f"用户显式要求称呼为「{address}」")
                if relation_type == "unknown" and address.endswith("总"):
                    updates["relation_type"] = "manager"

        honorific_policy, style_note = self._extract_honorific_correction(
            text,
            relation_type,
        )
        if honorific_policy:
            updates["honorific_policy"] = honorific_policy
        if style_note:
            updates["communication_style"] = self._append_style_note(
                emp.communication_style,
                style_note,
            )
            notes.append(style_note)

        if not updates:
            return emp
        updated = await self.store.update_profile(emp.open_id, **updates)
        await self.store.add_memory(
            emp.open_id,
            "fact",
            f"explicit_correction: {'；'.join(notes)}（出自：{text[:100]}）",
            relevance=0.95,
        )
        logger.info(
            "[concierge] 显式纠偏已应用：open_id=%s %s",
            emp.open_id[:8],
            updates,
        )
        return updated or emp

    def _extract_address_correction(self, text: str) -> str:
        matches = [
            m.group(1).strip(" ，,。.；;！!？?")
            for m in _ADDRESS_CORRECTION_RE.finditer(text)
        ]
        candidates = [
            value
            for value in matches
            if value and value not in _BAD_ADDRESS_WORDS and len(value) <= 16
        ]
        return candidates[-1] if candidates else ""

    def _extract_honorific_correction(
        self,
        text: str,
        relation_type: RelationType,
    ) -> tuple[str | None, str | None]:
        if any(hint in text for hint in _WARM_STYLE_HINTS):
            if relation_type == "boss":
                return None, "即使偏好自然表达，也需要保留老板场景的基本敬语和「您」。"
            return "warm", "偏好自然表达，不需要过度正式或频繁使用敬语。"
        if any(hint in text for hint in _FORMAL_STYLE_HINTS):
            if relation_type == "boss":
                return "boss_formal", "偏好正式尊重表达，多使用敬语和「您」。"
            return "formal", "偏好正式尊重表达，可适当使用敬语和「您」。"
        return None, None

    def _append_style_note(self, existing: str, note: str) -> str:
        existing = (existing or "").strip()
        if not existing:
            return note
        if note in existing:
            return existing
        combined = f"{existing}；{note}"
        return combined[:_STYLE_APPEND_LIMIT]

    # ─────────────────────── LLM system_prompt 注入 ───────────────────────

    @filter.on_llm_request()
    async def inject_employee_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if self.store is None:
            return
        if not self._uses_employee_memory(event):
            return
        open_id = self._extract_open_id(event)
        if not open_id:
            return

        emp = await self.store.get_employee(open_id)
        if emp is None:
            # 还没建过（on_message 钩子可能晚于 on_llm_request 触发）
            emp, _ = await self.store.get_or_create(
                open_id,
                platform_id=event.get_platform_id() or "",
            )
        emp = await self._ensure_identity_defaults(emp)

        memories = await self.store.list_memories(open_id, limit=6, min_relevance=0.3)

        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        honorific_policy = self._honorific_policy_for(emp, relation_type)
        block = self._render_employee_block(
            emp,
            memories,
            include_intro_hint=self._is_concierge_platform(event),
        )
        if not block:
            return
        kb_context = await self._retrieve_employee_kb_context(
            event,
            req,
            emp,
            memories,
            preferred_address=preferred_address,
        )
        if kb_context:
            block = f"{block}\n\n{kb_context}"

        existing = (req.system_prompt or "").rstrip()
        req.system_prompt = f"{existing}\n\n{block}" if existing else block
        await self._record_context_injection(
            event,
            emp,
            memories,
            block,
            relation_type=relation_type,
            preferred_address=preferred_address,
            honorific_policy=honorific_policy,
        )

    async def _retrieve_employee_kb_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        emp: Employee,
        memories: list[EmployeeMemory],
        *,
        preferred_address: str,
    ) -> str:
        query = self._bridge().build_kb_query(
            emp,
            req.prompt or event.message_str or "",
            memories,
            preferred_address=preferred_address,
        )
        if not query:
            return ""
        try:
            from astrbot.core.tools.knowledge_base_tools import retrieve_knowledge_base

            context_text = await retrieve_knowledge_base(
                query=query,
                umo=event.unified_msg_origin,
                context=self.context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[concierge] 员工记忆桥接 KB 检索失败：%s", exc)
            return ""
        return self._bridge().render_kb_context_block(context_text or "")

    async def _record_context_injection(
        self,
        event: AstrMessageEvent,
        emp: Employee,
        memories: list[EmployeeMemory],
        block: str,
        *,
        relation_type: RelationType,
        preferred_address: str,
        honorific_policy: str,
    ) -> None:
        if self.store is None:
            return
        try:
            await self.store.add_context_injection(
                emp.open_id,
                platform_id=event.get_platform_id() or "",
                relation_type=relation_type,
                preferred_address=preferred_address,
                honorific_policy=honorific_policy,
                memory_ids=[memory.memory_id for memory in memories],
                memory_kinds=[memory.kind for memory in memories],
                included_persona=bool(
                    emp.personality_summary
                    or emp.communication_style
                    or any(memory.kind == "persona" for memory in memories)
                ),
                block_chars=len(block),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[concierge] context injection trace 写入失败：%s", exc)

    # ─────────────────────── LLM 响应后：自动事实回写 ───────────────────────

    @filter.on_llm_response()
    async def capture_facts_after_llm(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        """LLM 答完后扫一遍用户消息，把"我喜欢/我会/记住X"等事实写进员工记忆。

        故意不调二次 LLM 摘要——廉价的正则 + 去重就够 v1 用。后续如要 LLM 摘要再 hook 进来。
        """
        if self.store is None:
            return
        if not self._uses_employee_memory(event):
            return
        open_id = self._extract_open_id(event)
        if not open_id:
            return
        emp = await self.store.get_employee(open_id)
        if emp is None:
            emp, _ = await self.store.get_or_create(
                open_id,
                platform_id=event.get_platform_id() or "",
            )
        emp = await self._ensure_identity_defaults(emp)
        self._guard_llm_response_for_identity(emp, resp)

        user_text = (event.message_str or "").strip()
        if not user_text or len(user_text) < 4:
            return
        # 跳过命令消息
        if any(user_text.startswith(p) for p in _COMMAND_PREFIXES):
            return

        # 已有的最近记忆——做轻量去重（同 kind 同 content 前 60 字相同即跳过）
        try:
            recent = await self.store.list_memories(
                open_id, limit=30, min_relevance=0.0
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[concierge] list_memories 失败：%s", exc)
            recent = []
        existing_keys = {(m.kind, m.content[:60]) for m in recent}

        written = 0
        for pat, kind, relevance in _FACT_RE:
            m = pat.search(user_text)
            if not m:
                continue
            captured = (m.group(1) or "").strip(" ，,。.；;！!？?")
            if not captured or len(captured) < 2:
                continue
            # 记一句话上下文，方便事后回看
            content = f"{kind}: {captured}（出自：{user_text[:80]}）"
            key = (kind, content[:60])
            if key in existing_keys:
                continue
            try:
                await self.store.add_memory(open_id, kind, content, relevance=relevance)
                existing_keys.add(key)
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("[concierge] add_memory 失败：%s", exc)
        if written:
            logger.info(
                "[concierge] 自动抽取写入 %d 条记忆 open_id=%s",
                written,
                open_id[:8],
            )
        await self._capture_persona_evidence(emp, user_text, recent)

    def _guard_llm_response_for_identity(
        self,
        emp: Employee,
        resp: LLMResponse,
    ) -> bool:
        if resp.is_chunk:
            return False
        relation_type = self._derive_relation_type(emp)
        if relation_type != "boss":
            return False
        original = (resp.completion_text or "").strip()
        if not original:
            return False

        guarded = self._rewrite_boss_response_text(original, emp)
        if guarded == original:
            return False
        resp.completion_text = guarded
        logger.info(
            "[concierge] boss response guard adjusted wording open_id=%s",
            emp.open_id[:8],
        )
        return True

    def _rewrite_boss_response_text(self, text: str, emp: Employee) -> str:
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        rewritten = text

        names = {
            (emp.display_name or "").strip(),
            str(self._identity_override_for(emp).get("display_name") or "").strip(),
        }
        for name in names:
            if name and name != preferred_address and name not in _BOSS_ALIAS_NAMES:
                rewritten = rewritten.replace(name, preferred_address)

        alias_pattern = "|".join(
            re.escape(alias) for alias in sorted(_BOSS_ALIAS_NAMES)
        )
        rewritten = re.sub(
            rf"(^|[\n\r])\s*(?:{alias_pattern})(?=[，,：:\s]|您好|您|好)",
            rf"\1{preferred_address}",
            rewritten,
        )
        rewritten = re.sub(r"你(?!们)", "您", rewritten)
        return self._ensure_boss_salutation(rewritten, preferred_address)

    def _ensure_boss_salutation(self, text: str, preferred_address: str) -> str:
        body = text.lstrip()
        separator_re = r"[，,：:\s]*"
        if body.startswith(f"尊敬的{preferred_address}"):
            if "您" in body[:80]:
                return body
            return re.sub(
                rf"^(尊敬的{re.escape(preferred_address)}){separator_re}",
                r"\1，您好，",
                body,
                count=1,
            )
        if body.startswith(preferred_address):
            rest = body[len(preferred_address) :].lstrip(" ，,：:")
            if rest.startswith("您好") or "您" in rest[:80]:
                return f"尊敬的{preferred_address}，{rest}"
            return f"尊敬的{preferred_address}，您好，{rest}"
        if body.startswith("您好"):
            return f"尊敬的{preferred_address}，{body}"
        if "您" in body[:80]:
            return f"尊敬的{preferred_address}，{body}"
        return f"尊敬的{preferred_address}，您好，{body}"

    async def _capture_persona_evidence(
        self,
        emp: Employee,
        user_text: str,
        recent_memories: list[EmployeeMemory] | None = None,
    ) -> None:
        if self.store is None:
            return
        relation_type = self._derive_relation_type(emp)
        signals = self._persona_signals_from_text(user_text)
        if not signals:
            return
        if recent_memories is None:
            recent_memories = await self.store.list_memories(
                emp.open_id,
                limit=80,
                min_relevance=0.0,
            )
        existing_keys = {(m.kind, m.content[:90]) for m in recent_memories}
        written = 0
        for trait, style, reason in signals[:2]:
            content = (
                f"{_PERSONA_EVIDENCE_PREFIX} trait={trait}; style={style}; "
                f"relation={relation_type}; reason={reason}; sample={user_text[:80]}"
            )
            key = ("persona_evidence", content[:90])
            if key in existing_keys:
                continue
            await self.store.add_memory(
                emp.open_id,
                "persona_evidence",
                content,
                relevance=0.25,
            )
            existing_keys.add(key)
            written += 1
        if written:
            await self._refresh_persona_profile(emp)

    def _persona_signals_from_text(self, text: str) -> list[tuple[str, str, str]]:
        normalized = text.lower()
        signals: list[tuple[str, str, str]] = []
        seen_traits: set[str] = set()
        for trait, keywords, style, reason in _PERSONA_SIGNAL_RULES:
            if trait in seen_traits:
                continue
            if any(keyword.lower() in normalized for keyword in keywords):
                signals.append((trait, style, reason))
                seen_traits.add(trait)
        return signals

    async def _refresh_persona_profile(self, emp: Employee) -> Employee | None:
        if self.store is None:
            return None
        memories = await self.store.list_memories(
            emp.open_id,
            limit=200,
            min_relevance=0.0,
        )
        evidence = [m for m in memories if m.kind == "persona_evidence"]
        if len(evidence) < _PERSONA_MIN_EVIDENCE:
            await self.store.update_profile(
                emp.open_id,
                persona_evidence_count=len(evidence),
            )
            return await self.store.get_employee(emp.open_id)

        personality_summary, communication_style = self._distill_persona_profile(
            emp,
            evidence,
        )
        updated = await self.store.update_profile(
            emp.open_id,
            personality_summary=personality_summary,
            communication_style=communication_style,
            persona_evidence_count=len(evidence),
            persona_updated_at=datetime.now(timezone.utc).isoformat(),
        )
        if updated is None:
            return None
        await self._write_persona_memory(updated, memories)
        return updated

    def _distill_persona_profile(
        self,
        emp: Employee,
        evidence: list[EmployeeMemory],
    ) -> tuple[str, str]:
        trait_counts: Counter[str] = Counter()
        style_counts: Counter[str] = Counter()
        for memory in evidence:
            parsed = self._parse_persona_evidence(memory.content)
            if not parsed:
                continue
            trait, style = parsed
            trait_counts[trait] += 1
            style_counts[style] += 1

        top_traits = [trait for trait, _ in trait_counts.most_common(3)]
        top_styles = [style for style, _ in style_counts.most_common(3)]
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)

        if relation_type == "boss":
            top_traits = self._prepend_unique(top_traits, "老板/老总层级")
            top_styles = self._prepend_unique(
                top_styles,
                f"称呼固定为{preferred_address}，多使用敬语和「您」，结论先行",
            )
        elif relation_type == "manager":
            top_traits = self._prepend_unique(top_traits, "管理者/负责人")
            top_styles = self._prepend_unique(top_styles, "保持专业、清晰、尊重")

        personality_summary = "、".join(top_traits[:4]) or "暂无稳定画像"
        personality_summary = (
            f"{personality_summary}；依据 {len(evidence)} 条对话证据生成。"
        )
        communication_style = "；".join(top_styles[:4]) or "按上下文自然沟通"
        return personality_summary[:220], communication_style[:220]

    def _parse_persona_evidence(self, content: str) -> tuple[str, str] | None:
        if not content.startswith(_PERSONA_EVIDENCE_PREFIX):
            return None
        trait_match = re.search(r"trait=([^;]+)", content)
        style_match = re.search(r"style=([^;]+)", content)
        if not trait_match or not style_match:
            return None
        return trait_match.group(1).strip(), style_match.group(1).strip()

    def _prepend_unique(self, values: list[str], value: str) -> list[str]:
        return [value, *(item for item in values if item != value)]

    async def _write_persona_memory(
        self,
        emp: Employee,
        recent_memories: list[EmployeeMemory],
    ) -> None:
        if self.store is None:
            return
        content = (
            f"{_PERSONA_PROFILE_PREFIX} 性格/画像={emp.personality_summary}; "
            f"沟通偏好={emp.communication_style}"
        )
        if any(m.kind == "persona" and m.content == content for m in recent_memories):
            return
        await self.store.add_memory(emp.open_id, "persona", content, relevance=0.85)

    def _render_employee_block(
        self,
        emp: Employee,
        memories: list,
        *,
        include_intro_hint: bool = True,
    ) -> str:
        lines: list[str] = []
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        honorific_policy = self._honorific_policy_for(emp, relation_type)
        lines.extend(
            self._render_address_guard(
                emp,
                relation_type,
                preferred_address,
                honorific_policy,
            )
        )
        lines.append("")

        if emp.is_anonymous and relation_type != "boss" and include_intro_hint:
            lines.append(
                "[公司接待机器人] 当前对话来自一位**还未自我介绍的同事**（open_id "
                f"{emp.open_id[:8]}...）。请用友好语气先打招呼，"
                "并自然地引导对方说出自己的名字、部门和岗位，便于以后记住 ta。"
            )
        elif emp.is_anonymous and relation_type != "boss":
            lines.append(
                "[员工身份记忆] 当前飞书对话对象还没有完整员工档案（open_id "
                f"{emp.open_id[:8]}...）。不要臆测姓名/职位；先正常完成当前任务，"
                "如确实需要身份信息，可自然补问。"
            )
        else:
            who = emp.display_name or preferred_address
            tags: list[str] = []
            if emp.department:
                tags.append(emp.department)
            if emp.role:
                tags.append(emp.role)
            tag_str = f"（{' · '.join(tags)}）" if tags else ""
            seen_part = ""
            if emp.interaction_count > 1:
                seen_part = f"，已交互 {emp.interaction_count} 次"
            lines.append(
                f"[公司接待机器人] 当前对话对象：**{who}**{tag_str}{seen_part}。"
            )
            lines.append(f"- 稳定称呼：{preferred_address}")
            lines.append(f"- 关系类型：{relation_type}")
        if emp.personality_summary:
            lines.append(f"- 性格/画像摘要：{emp.personality_summary[:160]}")
        if emp.communication_style:
            lines.append(f"- 沟通偏好：{emp.communication_style[:160]}")
        if emp.persona_evidence_count:
            lines.append(f"- 画像证据数：{emp.persona_evidence_count}")
        if emp.skill_tags:
            lines.append(f"- 技能标签：{', '.join(emp.skill_tags[:8])}")
        if emp.preferences:
            pref_summary = ", ".join(
                f"{k}={v}" for k, v in list(emp.preferences.items())[:5]
            )
            lines.append(f"- 偏好：{pref_summary}")
        if memories:
            lines.append("- 关于 ta 的长期记忆：")
            for m in memories:
                lines.append(f"  • [{m.kind}] {m.content[:120]}")
        return "\n".join(lines)

    async def _build_context_preview(
        self,
        token: str,
        *,
        memory_limit: int = 6,
    ) -> dict:
        if self.store is None:
            return {"status": "error", "message": "EmployeeStore not ready"}
        target = await self._resolve_employee_token(token)
        if target is None:
            return {"status": "error", "message": "not found"}
        if isinstance(target, list):
            return {
                "status": "error",
                "message": "ambiguous employee token",
                "matches": [
                    {
                        "open_id": emp.open_id,
                        "display_name": emp.display_name,
                    }
                    for emp in target[:20]
                ],
            }
        emp = await self._ensure_identity_defaults(target)
        memories = await self.store.list_memories(
            emp.open_id,
            limit=max(1, min(memory_limit, 20)),
            min_relevance=0.3,
        )
        block = self._render_employee_block(emp, memories)
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        return {
            "status": "ok",
            "employee": {
                "open_id": emp.open_id,
                "display_name": emp.display_name,
                "relation_type": relation_type,
                "preferred_address": preferred_address,
                "honorific_policy": self._honorific_policy_for(emp, relation_type),
            },
            "memory_count": len(memories),
            "memory_kinds": [memory.kind for memory in memories],
            "block_chars": len(block),
            "block": block,
        }

    def _format_context_preview(self, preview: dict) -> str:
        if preview.get("status") != "ok":
            message = preview.get("message") or "unknown error"
            if preview.get("matches"):
                sample = "、".join(
                    f"{item['open_id'][:8]}.../{item.get('display_name') or '匿名'}"
                    for item in preview["matches"][:5]
                )
                return f"⚠️ 上下文预览失败：{message}（{sample}）"
            return f"⚠️ 上下文预览失败：{message}"

        emp = preview["employee"]
        kinds = ", ".join(preview["memory_kinds"]) or "无长期记忆"
        return (
            "🧪 员工上下文注入预览\n"
            f"  • 员工：{emp['display_name'] or emp['open_id'][:8] + '...'}\n"
            f"  • 识别：{emp['relation_type']} / {emp['preferred_address']} / "
            f"{emp['honorific_policy']}\n"
            f"  • 记忆：{preview['memory_count']} 条（{kinds}）\n"
            f"  • 长度：{preview['block_chars']} 字\n\n"
            f"{preview['block']}"
        )

    async def _build_memory_eval(self, limit: int = 500) -> dict:
        if self.store is None:
            return {"status": "error", "message": "EmployeeStore not ready"}

        employees = await self.store.list_employees(limit=limit)
        total = len(employees)
        relation_counts: Counter[str] = Counter()
        memory_kind_counts: Counter[str] = Counter()
        risk_items: list[dict] = []
        identified = 0
        stable_address = 0
        short_term_ready = 0
        long_term_ready = 0
        persona_ready = 0
        boss_total = 0
        boss_guard_ready = 0
        total_memories = 0
        injectable_memories = 0

        for emp in employees:
            override = self._identity_override_for(emp)
            display_name = override.get("display_name") or emp.display_name
            relation_type = self._derive_relation_type(emp)
            preferred_address = self._preferred_address_for(emp, relation_type)
            honorific_policy = self._honorific_policy_for(emp, relation_type)
            relation_counts[relation_type] += 1

            employee_risks: list[str] = []
            if display_name:
                identified += 1
            else:
                employee_risks.append("未识别姓名，仍是匿名档案")

            if preferred_address and preferred_address != "同事":
                stable_address += 1
            else:
                employee_risks.append("缺稳定称呼")

            if relation_type == "boss":
                boss_total += 1
                if (
                    preferred_address.endswith("总")
                    and honorific_policy == "boss_formal"
                    and "您" in self._render_employee_block(emp, [])
                ):
                    boss_guard_ready += 1
                else:
                    employee_risks.append("老板称呼/敬语护栏不完整")

            memories = await self.store.list_memories(
                emp.open_id,
                limit=200,
                min_relevance=0.0,
            )
            total_memories += len(memories)
            for memory in memories:
                memory_kind_counts[memory.kind] += 1
                if memory.relevance >= 0.3:
                    injectable_memories += 1

            usable_memories = [
                memory
                for memory in memories
                if memory.relevance >= 0.3 and memory.kind != "persona_evidence"
            ]
            has_profile = bool(emp.personality_summary and emp.communication_style)
            has_identity_context = bool(
                preferred_address and relation_type != "unknown"
            )
            if has_identity_context:
                short_term_ready += 1
            if usable_memories or has_profile:
                long_term_ready += 1
            else:
                employee_risks.append("暂无可注入长期记忆")
            if has_profile and emp.persona_evidence_count >= _PERSONA_MIN_EVIDENCE:
                persona_ready += 1
            elif emp.persona_evidence_count:
                employee_risks.append(
                    f"画像证据不足 {emp.persona_evidence_count}/{_PERSONA_MIN_EVIDENCE}"
                )

            if employee_risks:
                risk_items.append(
                    {
                        "open_id": f"{emp.open_id[:8]}...",
                        "display_name": display_name,
                        "relation_type": relation_type,
                        "preferred_address": preferred_address,
                        "risks": employee_risks,
                    }
                )

        metrics = {
            "total_employees": total,
            "identified_employees": identified,
            "anonymous_employees": total - identified,
            "stable_address_employees": stable_address,
            "short_term_context_ready": short_term_ready,
            "long_term_memory_ready": long_term_ready,
            "persona_ready": persona_ready,
            "boss_total": boss_total,
            "boss_guard_ready": boss_guard_ready,
            "total_memories": total_memories,
            "injectable_memories": injectable_memories,
            "relation_counts": dict(relation_counts),
            "memory_kind_counts": dict(memory_kind_counts),
        }
        coverage = {
            "identity": self._ratio(identified, total),
            "stable_address": self._ratio(stable_address, total),
            "short_term_context": self._ratio(short_term_ready, total),
            "long_term_memory": self._ratio(long_term_ready, total),
            "persona": self._ratio(persona_ready, total),
            "boss_guard": self._ratio(boss_guard_ready, boss_total),
        }
        verdict = self._memory_eval_verdict(
            total, coverage, boss_total, boss_guard_ready
        )
        return {
            "status": "ok",
            "feature": {
                "key": FEATURE_KEY,
                "name": FEATURE_NAME,
                "stage": FEATURE_STAGE,
                "entrypoint": FEATURE_ENTRYPOINT,
                "harness": {
                    "role": FEATURE_HARNESS_ROLE,
                    "workflow_kind": FEATURE_HARNESS_WORKFLOW_KIND,
                    "runtime_entry": False,
                },
            },
            "metrics": metrics,
            "coverage": coverage,
            "verdict": verdict,
            "risks": risk_items[:20],
        }

    def _ratio(self, numerator: int, denominator: int) -> float | None:
        if denominator <= 0:
            return None
        return round(numerator / denominator, 4)

    def _memory_eval_verdict(
        self,
        total: int,
        coverage: dict,
        boss_total: int,
        boss_guard_ready: int,
    ) -> str:
        if total == 0:
            return "no_data"
        if boss_total and boss_guard_ready < boss_total:
            return "action_required"
        if (coverage["identity"] or 0) < 0.8 or (coverage["stable_address"] or 0) < 0.8:
            return "action_required"
        if (coverage["long_term_memory"] or 0) < 0.5:
            return "needs_observation"
        if (coverage["persona"] or 0) < 0.3:
            return "needs_observation"
        return "healthy"

    def _format_memory_eval(self, report: dict) -> str:
        if report.get("status") != "ok":
            return f"⚠️ 记忆评估不可用：{report.get('message') or 'unknown error'}"

        metrics = report["metrics"]
        coverage = report["coverage"]
        verdict = report["verdict"]
        risks = report["risks"]

        def pct(key: str) -> str:
            value = coverage.get(key)
            if value is None:
                return "n/a"
            return f"{value * 100:.1f}%"

        feature = report.get("feature") or {}
        feature_name = feature.get("name") or "员工记忆健康评估"
        feature_stage = feature.get("stage")
        harness = feature.get("harness") or {}
        title = f"🧠 {feature_name}健康评估"
        if feature_stage:
            title += f"（{feature_stage}）"

        lines = [
            title,
            f"  • 结论：{verdict}",
            f"  • 日常入口：{feature.get('entrypoint') or FEATURE_ENTRYPOINT}",
            f"  • 员工档案：{metrics['total_employees']} 个；匿名 {metrics['anonymous_employees']} 个",
            f"  • 身份覆盖：{pct('identity')}；稳定称呼：{pct('stable_address')}",
            f"  • 短期注入就绪：{pct('short_term_context')}；长期记忆就绪：{pct('long_term_memory')}",
            f"  • 画像覆盖：{pct('persona')}；老板护栏：{pct('boss_guard')}",
            f"  • 可注入记忆：{metrics['injectable_memories']} / {metrics['total_memories']}",
        ]
        if harness:
            lines.append(
                "  • Harness："
                f"{harness.get('role')} / {harness.get('workflow_kind')}"
                "（治理审计，不是员工日常入口）"
            )
        if metrics["relation_counts"]:
            relation = ", ".join(
                f"{key}={value}" for key, value in metrics["relation_counts"].items()
            )
            lines.append(f"  • 身份分布：{relation}")
        if metrics["memory_kind_counts"]:
            kinds = ", ".join(
                f"{key}={value}" for key, value in metrics["memory_kind_counts"].items()
            )
            lines.append(f"  • 记忆类型：{kinds}")
        if risks:
            lines.append("")
            lines.append("⚠️ Top 风险样本")
            for item in risks[:5]:
                name = item["display_name"] or item["open_id"]
                address = item["preferred_address"] or "(无称呼)"
                lines.append(
                    f"  • {name} · {item['relation_type']} · {address}："
                    f"{'；'.join(item['risks'])}"
                )
        return "\n".join(lines)

    # ─────────────────────── /employees admin 命令 ───────────────────────

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        event.set_result(MessageEventResult().message(text).use_t2i(False).stop_event())

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return getattr(event, "role", "") == "admin"

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            return False

    @filter.command(
        "employees",
        desc="员工目录管理：/employees sync（同步飞书通讯录） / ls（列出已记录员工）",
    )
    async def employees_command(self, event: AstrMessageEvent):
        if self.store is None:
            self._reply(event, "⚠️ EmployeeStore 未初始化")
            return
        if not self._is_admin_event(event):
            self._reply(
                event, "⚠️ 这是内部管理员命令，普通对话不会展示员工记忆和调试信息。"
            )
            return
        if not self._is_private_event(event):
            self._reply(event, "⚠️ 内部员工记忆管理命令请在管理员私聊中使用。")
            return

        text = (event.message_str or "").strip()
        for prefix in ("/employees", "employees"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        parts = text.split()
        sub = parts[0].lower() if parts else "help"

        if sub in ("help", ""):
            self._reply(
                event,
                "📒 员工目录命令：\n"
                "  /employees sync — 从飞书通讯录拉全员（admin）\n"
                "  /employees ls [N] — 列出 N 条已记录员工（默认 20）\n"
                "  /employees eval — 评估身份识别、称呼、长短期记忆是否生效\n"
                "  /employees fix <open_id前缀> key=value ... — 手工校准档案\n"
                "  /employees trace <open_id前缀> [N] — 查看上下文注入命中日志\n"
                "  /employees preview <open_id前缀> [N] — 预览实际注入内容\n"
                "  /employees archive <open_id前缀> [preview|kb|nas|both] — 受控归档员工协作画像",
            )
            return

        if sub == "sync":
            if self.feishu_client is None:
                self._reply(
                    event,
                    "⚠️ feishu 凭证未就绪：编辑 `data/feishu_whitelist.yaml` "
                    "并在飞书后台勾 `contact:user.base:read` + "
                    "`contact:user.id:read` + `contact:department.base:read`。",
                )
                return
            report = await sync_from_feishu(self.store, self.feishu_client)
            if not report.success:
                self._reply(event, f"⚠️ 同步失败：{report.error}")
                return
            samples = "、".join(report.samples[:5]) if report.samples else "（无）"
            self._reply(
                event,
                "✅ 同步完成：\n"
                f"  • 扫描部门 {report.departments_scanned} 个\n"
                f"  • 新增 {report.users_added}\n"
                f"  • 更新 {report.users_updated}\n"
                f"  • 跳过 {report.users_skipped}\n"
                f"  • 示例：{samples}",
            )
            return

        if sub == "ls":
            try:
                limit = int(parts[1]) if len(parts) > 1 else 20
            except ValueError:
                limit = 20
            limit = max(1, min(limit, 200))
            emps = await self.store.list_employees(limit=limit)
            if not emps:
                self._reply(event, "📒 员工目录为空。")
                return
            lines = [f"📒 员工目录（前 {len(emps)} 条）："]
            for e in emps:
                bits: list[str] = []
                if e.display_name:
                    bits.append(e.display_name)
                else:
                    bits.append(f"(匿名 {e.open_id[:8]}...)")
                if e.department:
                    bits.append(e.department)
                if e.role:
                    bits.append(e.role)
                if e.preferred_address:
                    bits.append(f"称呼:{e.preferred_address}")
                lines.append(f"  • {' · '.join(bits)} · 交互 {e.interaction_count}")
            self._reply(event, "\n".join(lines))
            return

        if sub == "eval":
            report = await self._build_memory_eval()
            self._reply(event, self._format_memory_eval(report))
            return

        if sub == "preview":
            if len(parts) < 2:
                self._reply(event, "用法：/employees preview <open_id前缀> [记忆条数]")
                return
            try:
                memory_limit = int(parts[2]) if len(parts) > 2 else 6
            except ValueError:
                memory_limit = 6
            preview = await self._build_context_preview(
                parts[1],
                memory_limit=memory_limit,
            )
            self._reply(event, self._format_context_preview(preview))
            return

        if sub == "trace":
            if len(parts) < 2:
                self._reply(event, "用法：/employees trace <open_id前缀> [N]")
                return
            target = await self._resolve_employee_token(parts[1])
            if target is None:
                self._reply(event, f"⚠️ 没找到员工：{parts[1]}")
                return
            if isinstance(target, list):
                sample = "、".join(
                    f"{emp.open_id[:8]}.../{emp.display_name or '匿名'}"
                    for emp in target[:5]
                )
                self._reply(event, f"⚠️ 前缀匹配了 {len(target)} 人：{sample}")
                return
            try:
                limit = int(parts[2]) if len(parts) > 2 else 5
            except ValueError:
                limit = 5
            traces = await self.store.list_context_injections(
                target.open_id,
                limit=max(1, min(limit, 20)),
            )
            self._reply(event, self._format_context_trace(target, traces))
            return

        if sub == "archive":
            if len(parts) < 2:
                self._reply(
                    event,
                    "用法：/employees archive <open_id前缀> [preview|kb|nas|both]",
                )
                return
            mode = parts[2].lower() if len(parts) > 2 else "preview"
            archive = await self._archive_employee_memory(parts[1], mode)
            self._reply(event, self._format_employee_archive(archive))
            return

        if sub == "fix":
            if len(parts) < 3:
                self._reply(
                    event,
                    "用法：/employees fix <open_id前缀> "
                    "name=杨国民 address=杨总 relation=boss honorific=boss_formal",
                )
                return
            target = await self._resolve_employee_token(parts[1])
            if target is None:
                self._reply(event, f"⚠️ 没找到员工：{parts[1]}")
                return
            if isinstance(target, list):
                sample = "、".join(
                    f"{emp.open_id[:8]}.../{emp.display_name or '匿名'}"
                    for emp in target[:5]
                )
                self._reply(event, f"⚠️ 前缀匹配了 {len(target)} 人：{sample}")
                return
            updates, errors = self._parse_employee_fix_args(parts[2:])
            if errors:
                self._reply(
                    event, "⚠️ 参数错误：\n" + "\n".join(f"  • {e}" for e in errors)
                )
                return
            if not updates:
                self._reply(event, "⚠️ 没有可更新字段")
                return
            updated = await self.store.update_profile(target.open_id, **updates)
            if updated is None:
                self._reply(event, "⚠️ 更新失败")
                return
            updated = await self._ensure_identity_defaults(updated)
            changed = ", ".join(f"{key}={value}" for key, value in updates.items())
            self._reply(
                event,
                "✅ 已校准员工档案：\n"
                f"  • open_id：{updated.open_id[:12]}...\n"
                f"  • 姓名：{updated.display_name or '(未设置)'}\n"
                f"  • 身份：{updated.relation_type}\n"
                f"  • 称呼：{updated.preferred_address or '(未设置)'}\n"
                f"  • 更新：{changed}",
            )
            return

        self._reply(event, f"未知子命令：{sub}\n用 `/employees help` 查看用法。")

    async def _archive_employee_memory(self, token: str, mode: str) -> dict:
        if self.store is None:
            return {"status": "error", "message": "EmployeeStore not ready"}
        if mode not in {"preview", "kb", "nas", "both"}:
            return {"status": "error", "message": "mode must be preview/kb/nas/both"}
        target = await self._resolve_employee_token(token)
        if target is None:
            return {"status": "error", "message": "not found"}
        if isinstance(target, list):
            return {
                "status": "error",
                "message": "ambiguous employee token",
                "matches": [
                    {
                        "open_id": emp.open_id,
                        "display_name": emp.display_name,
                    }
                    for emp in target[:20]
                ],
            }

        emp = await self._ensure_identity_defaults(target)
        memories = await self.store.list_memories(
            emp.open_id,
            limit=80,
            min_relevance=0.0,
        )
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        document = self._bridge().build_archive_document(
            emp,
            memories,
            preferred_address=preferred_address,
            relation_type=relation_type,
        )
        file_name = self._bridge().archive_file_name(emp, preferred_address)
        sync_results: list[dict] = []
        if mode in {"kb", "both"}:
            result = await self._bridge().sync_to_astrbot_kb(
                getattr(self.context, "kb_manager", None),
                document,
                file_name=file_name,
            )
            sync_results.append(
                {
                    "target": "astrbot_kb",
                    "status": result.status,
                    "detail": result.target or result.message,
                }
            )
        if mode in {"nas", "both"}:
            result = self._bridge().export_to_nas(document, file_name=file_name)
            sync_results.append(
                {
                    "target": "nas",
                    "status": result.status,
                    "detail": result.target or result.message,
                }
            )
        return {
            "status": "ok",
            "mode": mode,
            "employee": {
                "open_id": emp.open_id,
                "display_name": emp.display_name,
                "relation_type": relation_type,
                "preferred_address": preferred_address,
            },
            "file_name": file_name,
            "document": document,
            "sync_results": sync_results,
        }

    def _format_employee_archive(self, archive: dict) -> str:
        if archive.get("status") != "ok":
            message = archive.get("message") or "unknown error"
            if archive.get("matches"):
                sample = "、".join(
                    f"{item['open_id'][:8]}.../{item.get('display_name') or '匿名'}"
                    for item in archive["matches"][:5]
                )
                return f"⚠️ 员工记忆归档失败：{message}（{sample}）"
            return f"⚠️ 员工记忆归档失败：{message}"

        emp = archive["employee"]
        lines = [
            "🗂️ 员工记忆受控归档",
            f"  • 员工：{emp['display_name'] or emp['open_id'][:8] + '...'}",
            f"  • 识别：{emp['relation_type']} / {emp['preferred_address']}",
            f"  • 文件：{archive['file_name']}",
        ]
        for result in archive.get("sync_results", []):
            lines.append(
                f"  • {result['target']}：{result['status']} {result.get('detail') or ''}".rstrip()
            )
        lines.append("")
        lines.append(archive["document"][:1800])
        return "\n".join(lines)

    async def _resolve_employee_token(
        self,
        token: str,
    ) -> Employee | list[Employee] | None:
        if self.store is None:
            return None
        token = token.strip()
        if not token:
            return None
        exact = await self.store.get_employee(token)
        if exact is not None:
            return exact
        employees = await self.store.list_employees(limit=1000)
        matches = [
            emp
            for emp in employees
            if emp.open_id.startswith(token) or emp.display_name == token
        ]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return matches

    def _parse_employee_fix_args(
        self,
        args: list[str],
    ) -> tuple[dict, list[str]]:
        updates: dict = {}
        errors: list[str] = []
        aliases = {
            "name": "display_name",
            "display_name": "display_name",
            "dept": "department",
            "department": "department",
            "role": "role",
            "relation": "relation_type",
            "relation_type": "relation_type",
            "address": "preferred_address",
            "preferred_address": "preferred_address",
            "honorific": "honorific_policy",
            "honorific_policy": "honorific_policy",
            "personality": "personality_summary",
            "personality_summary": "personality_summary",
            "style": "communication_style",
            "communication_style": "communication_style",
        }
        valid_relations = {"boss", "manager", "employee", "unknown"}
        valid_honorific = {"boss_formal", "formal", "warm"}

        for arg in args:
            if "=" not in arg:
                errors.append(f"{arg} 缺少 '='")
                continue
            key, value = arg.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            field = aliases.get(key)
            if not field:
                errors.append(f"未知字段 {key}")
                continue
            if not value:
                errors.append(f"{key} 的值不能为空")
                continue
            if field == "relation_type" and value not in valid_relations:
                errors.append("relation 只能是 boss/manager/employee/unknown")
                continue
            if field == "honorific_policy" and value not in valid_honorific:
                errors.append("honorific 只能是 boss_formal/formal/warm")
                continue
            updates[field] = value
        return updates, errors

    def _format_context_trace(self, emp: Employee, traces: list[dict]) -> str:
        relation_type = self._derive_relation_type(emp)
        preferred_address = self._preferred_address_for(emp, relation_type)
        name = self._identity_override_for(emp).get("display_name") or emp.display_name
        who = name or f"{emp.open_id[:8]}..."
        if not traces:
            return (
                f"🧭 {who} 的上下文注入日志为空。\n"
                "这通常表示还没有触发过小助手 LLM 请求，或注入被平台过滤。"
            )

        lines = [
            f"🧭 {who} 的上下文注入日志（{len(traces)} 条）",
            f"  • 当前识别：{relation_type} / {preferred_address}",
        ]
        for trace in traces:
            memory_kinds = trace["memory_kinds"] or []
            kind_summary = ", ".join(memory_kinds) if memory_kinds else "无长期记忆"
            persona = "是" if trace["included_persona"] else "否"
            lines.append(
                f"  • {trace['created_at']} · {trace['preferred_address'] or '(无称呼)'}"
                f" · {trace['relation_type'] or 'unknown'}"
                f" · 画像:{persona}"
                f" · 记忆:{kind_summary}"
                f" · block:{trace['block_chars']}字"
            )
        return "\n".join(lines)

    # ─────────────────────── /me 自助命令 ───────────────────────

    @filter.command(
        "me",
        desc="查自己档案 + 记忆：/me / /me forget <memory_id 前缀>",
    )
    async def me_command(self, event: AstrMessageEvent):
        if self.store is None:
            self._reply(event, "⚠️ EmployeeStore 未初始化")
            return
        open_id = self._extract_open_id(event)
        if not open_id:
            self._reply(event, "⚠️ 无法识别你的身份")
            return

        text = (event.message_str or "").strip()
        for prefix in ("/me", "me"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        parts = text.split()
        sub = parts[0].lower() if parts else ""

        # /me forget <prefix>
        if sub == "forget":
            if len(parts) < 2:
                self._reply(event, "用法：/me forget <memory_id 前缀（至少 4 位）>")
                return
            target_prefix = parts[1].strip()
            if len(target_prefix) < 4:
                self._reply(event, "⚠️ memory_id 前缀至少 4 位以避免误删")
                return
            all_mems = await self.store.list_memories(
                open_id, limit=500, min_relevance=0.0
            )
            matches = [m for m in all_mems if m.memory_id.startswith(target_prefix)]
            if not matches:
                self._reply(event, f"⚠️ 在你的记忆里没找到前缀 `{target_prefix}`")
                return
            if len(matches) > 1:
                hits = ", ".join(f"#{m.memory_id[:8]}" for m in matches[:5])
                self._reply(
                    event,
                    f"⚠️ 前缀 `{target_prefix}` 匹配了 {len(matches)} 条："
                    f"{hits}\n请输入更长的前缀。",
                )
                return
            ok = await self.store.delete_memory(matches[0].memory_id)
            if ok:
                self._reply(
                    event,
                    f"✅ 已删除记忆 #{matches[0].memory_id[:8]}"
                    f"（{matches[0].content[:40]}...）",
                )
            else:
                self._reply(event, "⚠️ 删除失败（DB 写入异常）")
            return

        # /me 或 /me show
        emp = await self.store.get_employee(open_id)
        if emp is None:
            self._reply(event, "📒 还没记你的档案，先打个招呼试试 :)")
            return
        memories = await self.store.list_memories(open_id, limit=20, min_relevance=0.0)

        lines = [f"📒 **你的档案** `{open_id[:12]}...`"]
        lines.append(f"  • 名字：{emp.display_name or '(还没记)'}")
        lines.append(f"  • 部门：{emp.department or '(还没记)'}")
        lines.append(f"  • 岗位：{emp.role or '(还没记)'}")
        lines.append(f"  • 身份类型：{emp.relation_type or '(未判定)'}")
        lines.append(f"  • 稳定称呼：{emp.preferred_address or '(未设置)'}")
        lines.append(f"  • 称呼策略：{emp.honorific_policy or '(未设置)'}")
        lines.append(f"  • 平台：{emp.platform_id or '(?)'}")
        lines.append(f"  • 交互次数：{emp.interaction_count}")
        if emp.personality_summary:
            lines.append(f"  • 性格画像：{emp.personality_summary[:120]}")
        if emp.communication_style:
            lines.append(f"  • 沟通偏好：{emp.communication_style[:120]}")
        if emp.persona_evidence_count:
            lines.append(f"  • 画像证据：{emp.persona_evidence_count} 条")
        if emp.persona_updated_at:
            lines.append(f"  • 画像更新时间：{emp.persona_updated_at}")
        if emp.skill_tags:
            lines.append(f"  • 技能：{', '.join(emp.skill_tags[:8])}")
        if emp.preferences:
            prefs = ", ".join(f"{k}={v}" for k, v in list(emp.preferences.items())[:8])
            lines.append(f"  • 偏好：{prefs}")

        lines.append("")
        if memories:
            lines.append(f"💭 **长期记忆**（{len(memories)} 条）")
            for m in memories:
                lines.append(f"  #{m.memory_id[:8]} [{m.kind}] {m.content[:120]}")
            lines.append("")
            lines.append("_删除某条：/me forget <前 4-8 位>_")
        else:
            lines.append("💭 暂无长期记忆")

        self._reply(event, "\n".join(lines))

    # ─────────────────────── Dashboard Web API ───────────────────────

    async def _api_list_employees(self, *args, **kwargs):
        """GET /api/plug/employees — 列出员工目录（前 200 条）。"""
        if self.store is None:
            return {
                "status": "error",
                "message": "EmployeeStore not ready",
                "data": None,
            }
        try:
            emps = await self.store.list_employees(limit=200)
            payload = [
                {
                    "open_id": e.open_id,
                    "display_name": e.display_name,
                    "department": e.department,
                    "role": e.role,
                    "relation_type": e.relation_type,
                    "preferred_address": e.preferred_address,
                    "honorific_policy": e.honorific_policy,
                    "personality_summary": e.personality_summary,
                    "communication_style": e.communication_style,
                    "persona_evidence_count": e.persona_evidence_count,
                    "persona_updated_at": e.persona_updated_at,
                    "platform_id": e.platform_id,
                    "interaction_count": e.interaction_count,
                    "first_seen_at": e.first_seen_at,
                    "last_seen_at": e.last_seen_at,
                    "is_anonymous": e.is_anonymous,
                }
                for e in emps
            ]
            return {
                "status": "ok",
                "message": None,
                "data": {"employees": payload, "total": len(payload)},
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] _api_list_employees 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_eval_employees(self, *args, **kwargs):
        """GET /api/plug/employees/eval — 评估记忆健康度。"""
        try:
            report = await self._build_memory_eval()
            if report.get("status") != "ok":
                return {
                    "status": "error",
                    "message": report.get("message"),
                    "data": None,
                }
            return {"status": "ok", "message": None, "data": report}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] _api_eval_employees 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_trace_employee(self, *args, open_id: str = "", **kwargs):
        """GET /api/plug/employees/trace/<open_id> — 查询上下文注入命中日志。"""
        if self.store is None:
            return {
                "status": "error",
                "message": "EmployeeStore not ready",
                "data": None,
            }
        if not open_id:
            return {"status": "error", "message": "missing open_id", "data": None}
        try:
            target = await self._resolve_employee_token(open_id)
            if target is None:
                return {"status": "error", "message": "not found", "data": None}
            if isinstance(target, list):
                return {
                    "status": "error",
                    "message": "ambiguous open_id prefix",
                    "data": {
                        "matches": [
                            {
                                "open_id": emp.open_id,
                                "display_name": emp.display_name,
                            }
                            for emp in target[:20]
                        ]
                    },
                }
            traces = await self.store.list_context_injections(target.open_id, limit=20)
            return {
                "status": "ok",
                "message": None,
                "data": {
                    "employee": {
                        "open_id": target.open_id,
                        "display_name": target.display_name,
                        "relation_type": self._derive_relation_type(target),
                        "preferred_address": self._preferred_address_for(
                            target,
                            self._derive_relation_type(target),
                        ),
                    },
                    "traces": traces,
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] _api_trace_employee 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_preview_employee(self, *args, open_id: str = "", **kwargs):
        """GET /api/plug/employees/preview/<open_id> — 预览上下文注入内容。"""
        if not open_id:
            return {"status": "error", "message": "missing open_id", "data": None}
        try:
            preview = await self._build_context_preview(open_id)
            if preview.get("status") != "ok":
                return {
                    "status": "error",
                    "message": preview.get("message"),
                    "data": preview,
                }
            return {"status": "ok", "message": None, "data": preview}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] _api_preview_employee 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    async def _api_get_employee(self, *args, open_id: str = "", **kwargs):
        """GET /api/plug/employees/<open_id> — 单个员工详情 + 记忆。"""
        if self.store is None:
            return {
                "status": "error",
                "message": "EmployeeStore not ready",
                "data": None,
            }
        if not open_id:
            return {"status": "error", "message": "missing open_id", "data": None}
        try:
            emp = await self.store.get_employee(open_id)
            if emp is None:
                return {"status": "error", "message": "not found", "data": None}
            mems = await self.store.list_memories(open_id, limit=100, min_relevance=0.0)
            return {
                "status": "ok",
                "message": None,
                "data": {
                    "employee": {
                        "open_id": emp.open_id,
                        "display_name": emp.display_name,
                        "department": emp.department,
                        "role": emp.role,
                        "relation_type": emp.relation_type,
                        "preferred_address": emp.preferred_address,
                        "honorific_policy": emp.honorific_policy,
                        "personality_summary": emp.personality_summary,
                        "communication_style": emp.communication_style,
                        "persona_evidence_count": emp.persona_evidence_count,
                        "persona_updated_at": emp.persona_updated_at,
                        "platform_id": emp.platform_id,
                        "skill_tags": emp.skill_tags,
                        "preferences": emp.preferences,
                        "interaction_count": emp.interaction_count,
                        "first_seen_at": emp.first_seen_at,
                        "last_seen_at": emp.last_seen_at,
                    },
                    "memories": [
                        {
                            "memory_id": m.memory_id,
                            "kind": m.kind,
                            "content": m.content,
                            "relevance": m.relevance,
                            "created_at": m.created_at,
                        }
                        for m in mems
                    ],
                },
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[concierge] _api_get_employee 异常：%s", exc)
            return {"status": "error", "message": str(exc), "data": None}

    # ─────────────────────── helpers ───────────────────────

    def _extract_open_id(self, event: AstrMessageEvent) -> str | None:
        """从 event 提取稳定的用户 ID。

        飞书：sender_id 即 open_id。
        QQ：sender_id 是 QQ 号，也能用（但不是 P1 主战场）。
        """
        try:
            sid = event.get_sender_id()
        except Exception:  # noqa: BLE001
            return None
        if not sid:
            return None
        return str(sid).strip()
