"""Concierge identity and address policy tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from dc_engines.employee_directory import Employee, EmployeeMemoryBridge
from dc_engines.employee_directory.store import EmployeeStore

from astrbot.api.provider import LLMResponse, ProviderRequest
from data.plugins.concierge_plugin.main import ConciergePlugin


def _plugin() -> ConciergePlugin:
    return object.__new__(ConciergePlugin)


class _DummyEvent:
    def __init__(self, *, admin: bool, private: bool) -> None:
        self.role = "admin" if admin else "member"
        self._private = private

    def is_admin(self) -> bool:
        return self.role == "admin"

    def is_private_chat(self) -> bool:
        return self._private


class _DummyFeishuEvent:
    def __init__(
        self,
        *,
        platform_id: str = "巅池-技术",
        platform_name: str = "lark",
        sender_id: str = "ou_user",
        message: str = "帮我看一下",
    ) -> None:
        self._platform_id = platform_id
        self._platform_name = platform_name
        self._sender_id = sender_id
        self.message_str = message
        self.unified_msg_origin = f"lark:{platform_id}:test-session"

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_platform_name(self) -> str:
        return self._platform_name

    def get_sender_id(self) -> str:
        return self._sender_id


def test_boss_address_uses_stable_surname_zong() -> None:
    plugin = _plugin()
    emp = Employee(open_id="ou_boss", display_name="杨国民")

    relation_type = plugin._derive_relation_type(emp)
    preferred_address = plugin._preferred_address_for(emp, relation_type)

    assert relation_type == "boss"
    assert preferred_address == "杨总"


def test_boss_alias_address_migrates_to_stable_zong() -> None:
    plugin = _plugin()
    emp = Employee(
        open_id="ou_boss",
        display_name="老大",
        relation_type="boss",
        preferred_address="老大",
    )

    preferred_address = plugin._preferred_address_for(emp, "boss")

    assert preferred_address == "杨总"


def test_boss_guard_requires_honorific_language() -> None:
    plugin = _plugin()
    emp = Employee(open_id="ou_boss", display_name="杨国民")

    guard = "\n".join(plugin._render_address_guard(emp, "boss", "杨总", "boss_formal"))

    assert "尊敬的杨总" in guard
    assert "您" in guard


def test_daily_feishu_memory_entry_is_not_limited_to_concierge_bot() -> None:
    plugin = _plugin()
    devops_event = _DummyFeishuEvent(platform_id="巅池-技术")

    assert plugin._uses_employee_memory(devops_event) is True
    assert plugin._is_concierge_platform(devops_event) is False


async def test_inject_employee_context_runs_on_daily_feishu_platform(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    plugin.identity_overrides = {"by_open_id": {}, "by_name": {}}
    await employee_store.get_or_create(
        "ou_boss_daily",
        platform_id="巅池-技术",
        display_name="杨国民",
    )
    await employee_store.add_memory(
        "ou_boss_daily",
        "fact",
        "fact: 偏好结论先行",
        relevance=0.8,
    )
    event = _DummyFeishuEvent(
        platform_id="巅池-技术",
        sender_id="ou_boss_daily",
    )
    req = ProviderRequest(system_prompt="base")

    await plugin.inject_employee_context(event, req)

    assert "base" in req.system_prompt
    assert "尊敬的杨总" in req.system_prompt
    assert "fact: 偏好结论先行" in req.system_prompt
    traces = await employee_store.list_context_injections("ou_boss_daily", limit=5)
    assert len(traces) == 1
    assert traces[0]["platform_id"] == "巅池-技术"


async def test_inject_employee_context_appends_kb_bridge_context(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    plugin.context = SimpleNamespace()
    plugin.identity_overrides = {"by_open_id": {}, "by_name": {}}
    plugin.memory_bridge = EmployeeMemoryBridge()
    await employee_store.get_or_create(
        "ou_staff_daily",
        platform_id="巅池-技术",
        display_name="张三",
    )
    event = _DummyFeishuEvent(
        platform_id="巅池-技术",
        sender_id="ou_staff_daily",
        message="帮我查一下项目流程",
    )
    req = ProviderRequest(prompt="帮我查一下项目流程", system_prompt="")

    async def fake_kb_context(*args, **kwargs) -> str:
        return "## 公司知识库补充（员工记忆桥接，只读检索）\n项目流程资料"

    plugin._retrieve_employee_kb_context = fake_kb_context

    await plugin.inject_employee_context(event, req)

    assert "当前对话对象" in req.system_prompt
    assert "公司知识库补充" in req.system_prompt
    assert "项目流程资料" in req.system_prompt


def test_boss_response_guard_replaces_full_name_and_adds_honorific() -> None:
    plugin = _plugin()
    emp = Employee(open_id="ou_boss", display_name="杨国民")
    resp = LLMResponse(role="assistant", completion_text="杨国民，你看这个方案可以。")

    changed = plugin._guard_llm_response_for_identity(emp, resp)

    assert changed is True
    assert "杨国民" not in resp.completion_text
    assert resp.completion_text.startswith("尊敬的杨总")
    assert "您看" in resp.completion_text


def test_boss_response_guard_keeps_respectful_text() -> None:
    plugin = _plugin()
    emp = Employee(open_id="ou_boss", display_name="杨国民")
    text = "尊敬的杨总，您看这个方案可以。"
    resp = LLMResponse(role="assistant", completion_text=text)

    changed = plugin._guard_llm_response_for_identity(emp, resp)

    assert changed is False
    assert resp.completion_text == text


def test_response_guard_skips_regular_employee() -> None:
    plugin = _plugin()
    emp = Employee(open_id="ou_staff", display_name="张三")
    resp = LLMResponse(role="assistant", completion_text="张三，你看这个方案可以。")

    changed = plugin._guard_llm_response_for_identity(emp, resp)

    assert changed is False
    assert resp.completion_text == "张三，你看这个方案可以。"


def test_employee_debug_commands_require_admin_private_chat() -> None:
    plugin = _plugin()

    assert plugin._is_admin_event(_DummyEvent(admin=True, private=True)) is True
    assert plugin._is_private_event(_DummyEvent(admin=True, private=True)) is True
    assert plugin._is_admin_event(_DummyEvent(admin=False, private=True)) is False
    assert plugin._is_private_event(_DummyEvent(admin=True, private=False)) is False


def test_identity_override_takes_priority(tmp_path) -> None:
    plugin = _plugin()
    path = tmp_path / "employee_identity_overrides.json"
    path.write_text(
        json.dumps(
            {
                "employees": [
                    {
                        "open_id": "ou_core",
                        "display_name": "王强",
                        "relation_type": "manager",
                        "preferred_address": "王总",
                        "honorific_policy": "formal",
                        "aliases": ["王经理"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plugin.identity_overrides = plugin._load_identity_overrides(path)
    emp = Employee(open_id="ou_core", display_name="王强", relation_type="employee")

    relation_type = plugin._derive_relation_type(emp)
    preferred_address = plugin._preferred_address_for(emp, relation_type)

    assert relation_type == "manager"
    assert preferred_address == "王总"
    assert plugin._honorific_policy_for(emp, relation_type) == "formal"
    alias_emp = Employee(open_id="ou_alias", display_name="王经理")
    assert plugin._derive_relation_type(alias_emp) == "manager"


async def test_ensure_identity_defaults_applies_override_fields(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    plugin.identity_overrides = {
        "by_open_id": {
            "ou_core": {
                "display_name": "杨国民",
                "relation_type": "boss",
                "preferred_address": "杨总",
                "honorific_policy": "boss_formal",
            }
        },
        "by_name": {},
    }
    emp, _ = await employee_store.get_or_create("ou_core")

    updated = await plugin._ensure_identity_defaults(emp)

    assert updated.display_name == "杨国民"
    assert updated.relation_type == "boss"
    assert updated.preferred_address == "杨总"
    assert updated.honorific_policy == "boss_formal"


async def test_employee_fix_args_update_profile(employee_store: EmployeeStore) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_fix_target",
        platform_id="巅池-Agent小助手",
    )

    target = await plugin._resolve_employee_token("ou_fix")
    updates, errors = plugin._parse_employee_fix_args(
        [
            "name=杨国民",
            "relation=boss",
            "address=杨总",
            "honorific=boss_formal",
            "role=总经理",
        ]
    )
    updated = await employee_store.update_profile(emp.open_id, **updates)
    assert updated is not None
    updated = await plugin._ensure_identity_defaults(updated)

    assert target is not None
    assert not isinstance(target, list)
    assert errors == []
    assert updated.display_name == "杨国民"
    assert updated.relation_type == "boss"
    assert updated.preferred_address == "杨总"
    assert updated.honorific_policy == "boss_formal"
    assert updated.role == "总经理"


def test_employee_fix_args_reject_invalid_values() -> None:
    plugin = _plugin()

    updates, errors = plugin._parse_employee_fix_args(
        ["relation=owner", "honorific=casual", "unknown=x"]
    )

    assert updates == {}
    assert any("relation 只能是" in error for error in errors)
    assert any("honorific 只能是" in error for error in errors)
    assert any("未知字段" in error for error in errors)


async def test_persona_evidence_distills_profile(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_staff",
        platform_id="巅池-Agent小助手",
        display_name="张三",
    )

    await plugin._capture_persona_evidence(emp, "简单说重点，先说结论。")
    emp = await employee_store.get_employee("ou_staff")
    assert emp is not None
    await plugin._capture_persona_evidence(emp, "这个任务要尽快推进并闭环。")
    emp = await employee_store.get_employee("ou_staff")
    assert emp is not None
    await plugin._capture_persona_evidence(emp, "注意权限和回滚风险，稳妥一点。")

    emp = await employee_store.get_employee("ou_staff")
    assert emp is not None
    assert emp.persona_evidence_count >= 3
    assert "结论导向" in emp.personality_summary
    assert "偏好结论先行" in emp.communication_style
    assert emp.persona_updated_at

    memories = await employee_store.list_memories(
        "ou_staff",
        limit=20,
        min_relevance=0.0,
    )
    assert any(memory.kind == "persona" for memory in memories)


async def test_boss_persona_keeps_honorific_style(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_boss",
        platform_id="巅池-Agent小助手",
        display_name="杨国民",
    )
    emp = await plugin._ensure_identity_defaults(emp)

    await plugin._capture_persona_evidence(emp, "杨总这边称呼要尊敬，多用您。")
    emp = await employee_store.get_employee("ou_boss")
    assert emp is not None
    await plugin._capture_persona_evidence(emp, "给杨总回复要先说结论和重点。")
    emp = await employee_store.get_employee("ou_boss")
    assert emp is not None
    await plugin._capture_persona_evidence(emp, "结果要尽快落地，形成闭环。")

    emp = await employee_store.get_employee("ou_boss")
    assert emp is not None
    assert emp.relation_type == "boss"
    assert emp.preferred_address == "杨总"
    assert "老板/老总层级" in emp.personality_summary
    assert "多使用敬语和「您」" in emp.communication_style


async def test_memory_eval_reports_ready_context_and_memory(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_staff",
        platform_id="巅池-Agent小助手",
        display_name="张三",
    )
    emp = await plugin._ensure_identity_defaults(emp)
    await employee_store.add_memory(
        emp.open_id,
        "fact",
        "fact: 喜欢先看结论和风险",
        relevance=0.7,
    )
    await employee_store.update_profile(
        emp.open_id,
        personality_summary="结论导向；依据 3 条对话证据生成。",
        communication_style="偏好结论先行、少铺垫、直接给重点",
        persona_evidence_count=3,
        persona_updated_at="2026-05-20T00:00:00+00:00",
    )

    report = await plugin._build_memory_eval()

    assert report["status"] == "ok"
    assert report["metrics"]["total_employees"] == 1
    assert report["metrics"]["short_term_context_ready"] == 1
    assert report["metrics"]["long_term_memory_ready"] == 1
    assert report["metrics"]["persona_ready"] == 1
    assert report["coverage"]["identity"] == 1.0
    assert report["feature"]["key"] == "employee_memory_identity"
    assert report["feature"]["entrypoint"] == "feishu_daily_llm_pipeline"
    assert report["feature"]["harness"]["role"] == "governance_audit"
    assert (
        report["feature"]["harness"]["workflow_kind"]
        == "employee_memory_identity_audit"
    )
    rendered = plugin._format_memory_eval(report)
    assert "员工身份称呼与记忆画像护栏健康评估" in rendered
    assert "治理审计，不是员工日常入口" in rendered


async def test_memory_eval_flags_incomplete_boss_guard(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    await employee_store.get_or_create(
        "ou_boss",
        platform_id="巅池-Agent小助手",
        display_name="杨国民",
    )
    await employee_store.update_profile(
        "ou_boss",
        relation_type="boss",
        preferred_address="老板",
        honorific_policy="warm",
    )

    report = await plugin._build_memory_eval()

    assert report["verdict"] == "action_required"
    assert report["metrics"]["boss_total"] == 1
    assert report["metrics"]["boss_guard_ready"] == 0
    assert any(
        "老板称呼/敬语护栏不完整" in "；".join(item["risks"])
        for item in report["risks"]
    )


async def test_context_trace_format_includes_memory_hits(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_trace",
        platform_id="巅池-Agent小助手",
        display_name="张三",
    )
    emp = await plugin._ensure_identity_defaults(emp)
    await employee_store.add_context_injection(
        emp.open_id,
        platform_id="巅池-Agent小助手",
        relation_type="employee",
        preferred_address="张三",
        honorific_policy="warm",
        memory_ids=["m1", "m2"],
        memory_kinds=["fact", "persona"],
        included_persona=True,
        block_chars=321,
    )

    traces = await employee_store.list_context_injections(emp.open_id, limit=5)
    rendered = plugin._format_context_trace(emp, traces)

    assert "上下文注入日志" in rendered
    assert "fact, persona" in rendered
    assert "画像:是" in rendered
    assert "block:321字" in rendered


def test_address_correction_uses_last_explicit_name() -> None:
    plugin = _plugin()

    address = plugin._extract_address_correction("不要叫我老王，以后叫我王老师")

    assert address == "王老师"


async def test_explicit_address_correction_updates_profile(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_correction",
        platform_id="巅池-Agent小助手",
        display_name="王强",
    )

    updated = await plugin._maybe_apply_explicit_correction(
        emp,
        "以后请叫我王老师，不用太正式。",
    )
    memories = await employee_store.list_memories(
        emp.open_id,
        limit=10,
        min_relevance=0.0,
    )

    assert updated.preferred_address == "王老师"
    assert updated.honorific_policy == "warm"
    assert "自然表达" in updated.communication_style
    assert any(
        memory.kind == "fact" and "explicit_correction" in memory.content
        for memory in memories
    )


async def test_boss_correction_keeps_formal_guard(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    emp, _ = await employee_store.get_or_create(
        "ou_boss_correction",
        platform_id="巅池-Agent小助手",
        display_name="杨国民",
    )
    emp = await plugin._ensure_identity_defaults(emp)

    updated = await plugin._maybe_apply_explicit_correction(
        emp,
        "以后叫我老杨，不用敬语。",
    )

    assert updated.preferred_address == "杨总"
    assert updated.honorific_policy == "boss_formal"
    assert "保留老板场景" in updated.communication_style


async def test_context_preview_renders_real_injection_block(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    plugin.identity_overrides = {
        "by_open_id": {},
        "by_name": {
            "老大": {
                "display_name": "杨国民",
                "relation_type": "boss",
                "preferred_address": "杨总",
                "honorific_policy": "boss_formal",
            }
        },
    }
    emp, _ = await employee_store.get_or_create(
        "ou_preview",
        platform_id="巅池-Agent小助手",
        display_name="老大",
    )
    await employee_store.add_memory(
        emp.open_id,
        "fact",
        "fact: 喜欢结论先行",
        relevance=0.8,
    )

    preview = await plugin._build_context_preview("ou_preview")
    rendered = plugin._format_context_preview(preview)

    assert preview["status"] == "ok"
    assert preview["employee"]["display_name"] == "杨国民"
    assert preview["employee"]["relation_type"] == "boss"
    assert preview["employee"]["preferred_address"] == "杨总"
    assert preview["memory_count"] == 1
    assert "尊敬的杨总" in preview["block"]
    assert "fact: 喜欢结论先行" in preview["block"]
    assert "员工上下文注入预览" in rendered


async def test_context_preview_reports_ambiguous_prefix(
    employee_store: EmployeeStore,
) -> None:
    plugin = _plugin()
    plugin.store = employee_store
    await employee_store.get_or_create("ou_same_1", display_name="张三")
    await employee_store.get_or_create("ou_same_2", display_name="李四")

    preview = await plugin._build_context_preview("ou_same")

    assert preview["status"] == "error"
    assert preview["message"] == "ambiguous employee token"
    assert len(preview["matches"]) == 2
