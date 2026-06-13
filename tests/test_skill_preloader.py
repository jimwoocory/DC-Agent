"""Unit tests for ``data/plugins/skill_preloader/main.py``.

验证 ``SkillPreloaderPlugin.inject_matched_skills`` 在 LLM 调用前:
    1. 读 ``event.message_str`` + ``event.get_extra('dc_router_intent')``
    2. 匹配 skill 后注入到 ``req.system_prompt`` 尾部
    3. 前缀指令 ``#xxx`` 时跳过 (不污染显式路由)
    4. 已被注入过的 system_prompt (含 marker) 不再重复注入
    5. 没匹配上 skill 时不动 system_prompt
    6. ``has_attachments=True`` 强制注入 ``document-intake``
    7. 异常隔离 — match 失败时不动 system_prompt
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = PROJECT_ROOT / "data" / "plugins" / "skill_preloader" / "main.py"


# ─────────────────────────── module loader ─────────────────────────────


@pytest.fixture
def plugin_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """独立 import skill_preloader.main 模块, 不污染全局.

    用 ``importlib.util.spec_from_file_location`` 加载, 避免 import 路径
    冲突; 同时切到 fake skills root 让测试结果可预测.
    """
    # 切 fake skills root
    fake_root = tmp_path / "skills"
    fake_root.mkdir()

    # 写 brand-marketing, 含 trigger phrases + 中文 body
    (fake_root / "brand-marketing").mkdir()
    (fake_root / "brand-marketing" / "SKILL.md").write_text(
        """---
name: Brand Marketing Planning
description: &gt;
  Use this skill when the user asks to create brand marketing plans. Trigger phrases:
  &quot;marketing plan&quot;, &quot;brand strategy&quot;.
version: 1.0.0
---

# Brand Marketing Planning Skill

专业的品牌推广策划技能, 涵盖品牌定位、渠道策略、内容营销、KOL 合作、
传播规划等场景.
""",
        encoding="utf-8",
    )
    (fake_root / "brand-marketing" / "references").mkdir()
    (fake_root / "brand-marketing" / "references" / "CHANNELS.md").write_text(
        "# Channels\n\nUse owned, earned, paid, and partner channels.",
        encoding="utf-8",
    )
    brand_skill_md = fake_root / "brand-marketing" / "SKILL.md"
    brand_skill_md.write_text(
        brand_skill_md.read_text(encoding="utf-8")
        + "\nSee [Channels](references/CHANNELS.md).\n",
        encoding="utf-8",
    )

    # 写 document-intake (无 trigger phrases, 走 has_attachments 入口)
    (fake_root / "document-intake").mkdir()
    (fake_root / "document-intake" / "SKILL.md").write_text(
        """---
name: document-intake
description: 文件/图片入库技能, 用于把附件 OCR 解析后灌入 Harness.
version: 1.0.0
---

# Document Intake

处理上传文件, 提取文本, 归档到 Harness store.
""",
        encoding="utf-8",
    )

    # 写 obsidian-markdown, 验证全局格式类 skill 不被业务 intent allowlist 拦掉.
    (fake_root / "obsidian-markdown").mkdir()
    (fake_root / "obsidian-markdown" / "references").mkdir()
    (fake_root / "obsidian-markdown" / "SKILL.md").write_text(
        """---
name: obsidian-markdown
description: Create Obsidian notes with wikilinks, callouts, frontmatter, tags, embeds, and Obsidian-specific Markdown.
---

# Obsidian Markdown

Use wikilinks and callouts. See [Callouts](references/CALLOUTS.md).
""",
        encoding="utf-8",
    )
    (fake_root / "obsidian-markdown" / "references" / "CALLOUTS.md").write_text(
        "# Callouts\n\nUse > [!note], > [!warning], and foldable callouts.",
        encoding="utf-8",
    )

    monkeypatch.setenv("DC_AGENT_SKILLS_ROOT", str(fake_root))

    # 重置 skill_loader 内部状态
    from dc_router_core import skill_loader

    skill_loader.reset_skills_root_for_testing()
    skill_loader.clear_skill_cache()

    spec = importlib.util.spec_from_file_location(
        "skill_preloader_main", str(PLUGIN_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_preloader_main"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("skill_preloader_main", None)
        skill_loader.reset_skills_root_for_testing()
        skill_loader.clear_skill_cache()


def _make_event(
    *,
    text: str = "",
    intent: str = "",
    has_attachments: bool = False,
) -> SimpleNamespace:
    """造一个最小可用 event mock."""
    components: list = []
    if has_attachments:
        # _detect_attachments 用 ``type(comp).__name__`` 判断, 所以构造一个
        # class 名为 "Image" 的对象.
        image_cls = type("Image", (), {})
        components.append(image_cls())

    def _get_extra(key: str):
        return intent if key == "dc_router_intent" else None

    return SimpleNamespace(
        message_str=text,
        message_obj=SimpleNamespace(message=components),
        get_extra=_get_extra,
    )


def _make_request(system_prompt: str = "") -> SimpleNamespace:
    return SimpleNamespace(system_prompt=system_prompt)


# ─────────────────────────── 测试 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_matched_skill_appends_to_system_prompt(
    plugin_module,
) -> None:
    """DEEP_CREATIVE + 营销相关文本 → 注入 brand-marketing."""
    event = _make_event(
        text="请基于 brand-marketing skill 帮我做一个内容营销方案",
        intent="deep_creative",
    )
    req = _make_request("base persona prompt")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)  # 跳过 __init__ (避免 AstrBot Context)
    await plugin.inject_matched_skills(event, req)

    # 应当把匹配上的 skill 内容追加到 system_prompt
    assert req.system_prompt.startswith("base persona prompt")
    assert "AstrBot skill hints" in req.system_prompt
    assert "brand-marketing" in req.system_prompt


@pytest.mark.asyncio
async def test_inject_matched_skill_includes_agent_skill_references(
    plugin_module,
) -> None:
    """Matched Agent Skills preload directly linked ``references/*.md`` files."""
    event = _make_event(
        text="请基于 brand-marketing skill 帮我做一个内容营销方案",
        intent="deep_creative",
    )
    req = _make_request("base persona prompt")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    assert "Referenced skill files" in req.system_prompt
    assert "references/CHANNELS.md" in req.system_prompt
    assert "owned, earned, paid" in req.system_prompt


@pytest.mark.asyncio
async def test_inject_skips_when_text_starts_with_prefix(
    plugin_module,
) -> None:
    """``#xxx`` 前缀路由时, 跳过注入 (用户显式指定了路径)."""
    event = _make_event(
        text="#超深 帮我做内容营销方案",
        intent="deep_creative",
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    # 不应注入
    assert req.system_prompt == "base"


@pytest.mark.asyncio
async def test_inject_is_idempotent(plugin_module) -> None:
    """已经被注入过 (含 marker) 时, multi-turn 二次调用直接 skip."""
    event = _make_event(
        text="做个内容营销方案",
        intent="deep_creative",
    )
    already = "base\n\n[AstrBot skill hints — 以下技能库内容已预读注入, 请直接基于这些内容回答, 不要假装调用 astrbot_file_read_tool]\n1. brand-marketing — ...: body"
    req = _make_request(already)

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    # 内容不变
    assert req.system_prompt == already
    # 不会重复出现 "1. brand-marketing" 两次
    assert req.system_prompt.count("1. brand-marketing") == 1


@pytest.mark.asyncio
async def test_inject_no_match_keeps_system_prompt_untouched(
    plugin_module,
) -> None:
    """没匹配上 skill 时, system_prompt 保持原样."""
    event = _make_event(
        text="今天天气真好",
        intent="casual",  # CASUAL → skip
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    assert req.system_prompt == "base"


@pytest.mark.asyncio
async def test_inject_force_document_intake_on_attachments(
    plugin_module,
) -> None:
    """has_attachments=True 强制把 document-intake 加进候选.

    注意: CASUAL / REALTIME / FALLBACK 在 _INTENT_SKIP_SKILLS 里, 会先被
    短路返回空 — 所以这里用 None intent (不传), 让 has_attachments 路径
    走通.
    """
    event = _make_event(
        text="看下这个文件",
        intent="",  # 空 intent, plugin 内部 intent_enum = None, 跳过 skip
        has_attachments=True,
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    # attachments 应当触发 document-intake 注入
    assert "document-intake" in req.system_prompt


@pytest.mark.asyncio
async def test_inject_obsidian_skill_under_business_intent(plugin_module) -> None:
    """Global Obsidian skills bypass business allowlists when text matches."""
    event = _make_event(
        text="帮我写一篇 Obsidian note，要有 wikilinks、callouts 和 frontmatter",
        intent="deep_insight",
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    await plugin.inject_matched_skills(event, req)

    assert "obsidian-markdown" in req.system_prompt
    assert "references/CALLOUTS.md" in req.system_prompt


@pytest.mark.asyncio
async def test_inject_swallows_match_exception(
    plugin_module,
) -> None:
    """match_skill_for_intent 抛异常时, system_prompt 保持原样 (异常隔离)."""
    event = _make_event(
        text="做个内容营销方案",
        intent="deep_creative",
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)

    # plugin 在 import 时把 match_skill_for_intent 复制到了自己的 module
    # namespace, 所以 patch ``dc_router_core.skill_loader.match_skill_for_intent``
    # 不影响 plugin 内部调用. 这里直接 patch plugin 自己的引用.
    original = plugin_module.match_skill_for_intent
    plugin_module.match_skill_for_intent = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("simulated failure")
    )
    try:
        # 不应向上抛异常
        await plugin.inject_matched_skills(event, req)
    finally:
        plugin_module.match_skill_for_intent = original

    # system_prompt 不变
    assert req.system_prompt == "base"


@pytest.mark.asyncio
async def test_inject_handles_unknown_intent_string(plugin_module) -> None:
    """event.get_extra 给的 intent 是不在 RouterIntent 里的 str, 也能跑通."""
    event = _make_event(
        text="做个内容营销方案",
        intent="some_weird_intent",  # 不在 enum 里
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    # 不应抛 ValueError
    await plugin.inject_matched_skills(event, req)

    # 应当走关键词兜底, 命中 brand-marketing
    assert "brand-marketing" in req.system_prompt


@pytest.mark.asyncio
async def test_inject_handles_event_without_get_extra(
    plugin_module,
) -> None:
    """event 没有 get_extra 方法时 (老旧 mock), 不应抛 AttributeError."""
    event = SimpleNamespace(
        message_str="做个内容营销方案",
        message_obj=SimpleNamespace(message=[]),
        # 故意没有 get_extra
    )
    req = _make_request("base")

    plugin_cls = plugin_module.SkillPreloaderPlugin
    plugin = plugin_cls.__new__(plugin_cls)
    # 不应抛 AttributeError — 应静默降级到关键词匹配
    await plugin.inject_matched_skills(event, req)

    # 没 intent 也能命中 (关键词兜底)
    assert "brand-marketing" in req.system_prompt
