"""Unit tests for ``dc_router_core.skill_loader``.

验证:
    1. ``list_available_skills()`` 能扫到 ``data/skills/`` 里的真实 skill
    2. ``parse_skill_frontmatter()`` 能正确抽 frontmatter + trigger phrases
    3. ``match_skill_for_intent()`` 中英文都能命中, intent 门控正确
    4. ``build_skill_prompt_block()`` 输出格式 + 字节预算
    5. ``read_skill_card()`` mtime 缓存 + 30s TTL 复用
    6. ``SkillInfo.matches()`` 大小写不敏感, 含空格的短语也能命中
    7. 中文关键词滑动窗口 (e.g. "内容营销" 拆出 "内容" / "营销")
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dc_router_core.skill_loader import (  # noqa: I001
    DEFAULT_MAX_PROMPT_BYTES,
    DEFAULT_SKILL_CARD_CHARS,
    SkillInfo,
    build_skill_prompt_block,
    clear_skill_cache,
    list_available_skills,
    match_skill_for_intent,
    parse_skill_frontmatter,
    read_skill_card,
    reset_skills_root_for_testing,
)
from dc_router_core.taxonomy import RouterIntent
from scripts.sync_bundled_skills import sync_bundled_skills

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_SKILLS_ROOT = PROJECT_ROOT / "data" / "skills"


# ─────────────────────────── fixtures ──────────────────────────────────


@pytest.fixture
def fake_skills_root(tmp_path: Path) -> Path:
    """建一个临时 skills 目录, 写 3 个有代表性的 SKILL.md.

    用于完全可控的 matcher / keyword 抽取测试 (避开真实 12 个 skill 的
    frontmatter 噪音).
    """
    root = tmp_path / "skills"
    root.mkdir()

    # 1) brand-marketing (有 trigger phrases + 中文 body)
    (root / "brand-marketing").mkdir()
    (root / "brand-marketing" / "SKILL.md").write_text(
        """---
name: Brand Marketing Planning
description: &gt;
  This skill should be used when the user asks to create brand marketing plans,
  promotion strategies, content calendars. Trigger phrases: &quot;marketing plan&quot;,
  &quot;brand strategy&quot;, &quot;promotion strategy&quot;, &quot;content calendar&quot;.
version: 1.0.0
---

# Brand Marketing Planning Skill

专业的品牌推广策划技能, 涵盖品牌定位、渠道策略、内容营销、KOL 合作、
传播规划等场景. 适用于快消、汽车、地产等行业.
""",
        encoding="utf-8",
    )

    # 2) creative-copywriting (中英混合 body)
    (root / "creative-copywriting").mkdir()
    (root / "creative-copywriting" / "SKILL.md").write_text(
        """---
name: Creative Copywriting
description: &gt;
  Use this skill when the user needs slogans, ad copy, social media posts.
  Trigger phrases: &quot;write copy&quot;, &quot;create slogan&quot;, &quot;social media post&quot;.
version: 1.0.0
---

# Creative Copywriting

创意文案撰写, 包括 slogan、推文、朋友圈文案、产品描述、活动稿件等.
""",
        encoding="utf-8",
    )

    # 3) document-intake (无 trigger phrases, 走 has_attachments 入口)
    (root / "document-intake").mkdir()
    (root / "document-intake" / "SKILL.md").write_text(
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

    # 4) execution-oriented Agent Skills. These should not bypass business
    # intent allowlists unless explicitly promoted into policy.
    (root / "obsidian-cli").mkdir()
    (root / "obsidian-cli" / "SKILL.md").write_text(
        """---
name: obsidian-cli
description: Interact with Obsidian vaults using the Obsidian CLI. Use when the user asks to search vault content or run Obsidian commands.
---

# Obsidian CLI

Run obsidian commands against a local vault.
""",
        encoding="utf-8",
    )

    (root / "defuddle").mkdir()
    (root / "defuddle" / "SKILL.md").write_text(
        """---
name: defuddle
description: Extract clean markdown content from web pages using Defuddle CLI. Use when the user provides a URL to read or analyze.
---

# Defuddle

Run defuddle parse with a URL.
""",
        encoding="utf-8",
    )

    # 5) 一个故意写坏的 frontmatter (会在 fallback 路径被吸收, 不抛错)
    (root / "broken").mkdir()
    (root / "broken" / "SKILL.md").write_text(
        "This file has no frontmatter at all\n",
        encoding="utf-8",
    )

    return root


@pytest.fixture
def real_skills_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    """强制 ``get_skills_root()`` 指向真实 ``data/skills/`` (供"端到端"测试用)."""
    sync_bundled_skills(
        source=PROJECT_ROOT / "bundled" / "skills",
        target=REAL_SKILLS_ROOT,
    )
    monkeypatch.setenv("DC_AGENT_SKILLS_ROOT", str(REAL_SKILLS_ROOT))
    reset_skills_root_for_testing()
    clear_skill_cache()
    try:
        return REAL_SKILLS_ROOT
    finally:
        reset_skills_root_for_testing()
        clear_skill_cache()


@pytest.fixture
def use_fake_root(fake_skills_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ``get_skills_root()`` 切到临时 fake 目录."""
    monkeypatch.setenv("DC_AGENT_SKILLS_ROOT", str(fake_skills_root))
    reset_skills_root_for_testing()
    clear_skill_cache()
    try:
        return fake_skills_root
    finally:
        reset_skills_root_for_testing()
        clear_skill_cache()


# ─────────────────────────── list_available_skills ─────────────────────


def test_list_skills_finds_bundled_obsidian_skills(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    """Runtime ``data/skills/`` includes synced bundled Obsidian authoring skills."""
    skills = list_available_skills()
    names = {s.name for s in skills}
    assert "obsidian-markdown" in names
    assert "obsidian-bases" in names
    assert "json-canvas" in names
    assert "mermaid-visualizer" in names
    assert "obsidian-cli" not in names
    assert "defuddle" not in names
    # 每个 skill 都有 title + description
    for s in skills:
        assert s.title
        assert s.path.is_file()


def test_list_skills_skips_dirs_without_skill_md(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """没 SKILL.md 的目录根本不出现; 没 frontmatter 的目录会 fallback 出现.

    我们另外加一个空 SKILL.md (没 frontmatter) — 它应当出现在列表中, 但
    description 是空 (确认 fallback 路径工作).
    """
    # 在 use_fake_root 里加一个没 SKILL.md 的目录
    (use_fake_root / "no_md_dir").mkdir()
    # 加一个 SKILL.md 但 frontmatter 完全缺失
    (use_fake_root / "no_frontmatter").mkdir()
    (use_fake_root / "no_frontmatter" / "SKILL.md").write_text(
        "Just some body text, no frontmatter at all.\n",
        encoding="utf-8",
    )

    skills = list_available_skills()
    names = {s.name for s in skills}

    # 没 SKILL.md 的目录被跳过
    assert "no_md_dir" not in names
    # 有 SKILL.md 但没 frontmatter 的目录 fallback 出现
    assert "no_frontmatter" in names
    # 正常 skill 仍然列出
    assert "brand-marketing" in names


def test_list_skills_returns_empty_for_nonexistent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skills 根目录不存在时返回 [], 不抛异常."""
    monkeypatch.setenv("DC_AGENT_SKILLS_ROOT", str(tmp_path / "no_such_dir"))
    reset_skills_root_for_testing()
    assert list_available_skills() == []


# ─────────────────────────── parse_skill_frontmatter ────────────────────


def test_parse_frontmatter_extracts_trigger_phrases(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """trigger phrases 应当从 description 里 quoted list 抽出来."""
    info = parse_skill_frontmatter(use_fake_root / "brand-marketing" / "SKILL.md")
    assert info is not None
    # 4 个 trigger phrases (大小写无关)
    assert "marketing plan" in info.trigger_phrases
    assert "brand strategy" in info.trigger_phrases
    assert "promotion strategy" in info.trigger_phrases
    assert "content calendar" in info.trigger_phrases
    # description 应当被反转义 (避免出现 ``&quot;`` 之类 HTML 实体)
    assert "&quot;" not in info.description
    # title
    assert info.title == "Brand Marketing Planning"


def test_parse_frontmatter_supports_standard_yaml_folded_description(
    tmp_path: Path,
) -> None:
    """Agent Skills commonly use normal YAML ``description: >`` blocks."""
    skill_dir = tmp_path / "obsidian-markdown"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: obsidian-markdown
description: >
  Create and edit Obsidian notes. Trigger phrases: "wikilinks",
  "obsidian notes", "callouts".
---

# Obsidian Markdown
""",
        encoding="utf-8",
    )

    info = parse_skill_frontmatter(skill_md)

    assert info is not None
    assert info.description.startswith("Create and edit Obsidian notes")
    assert "obsidian notes" in info.trigger_phrases


def test_parse_frontmatter_extracts_zh_keywords_via_sliding_window(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """中文 body 应当被抽出, 包括 sliding window 拆出的 2 字词.

    e.g. "内容营销" 拆出 "内容" / "营销" / "营销" 之外的还能拿到 "品牌" /
    "推广" / "策划" 等.
    """
    info = parse_skill_frontmatter(use_fake_root / "brand-marketing" / "SKILL.md")
    assert info is not None
    kws = set(info.keywords)
    # 至少包含这几个高频词
    for must in ("内容", "营销", "品牌", "推广", "策划"):
        assert must in kws, f"关键词 '{must}' 应该被抽出, 实际 kws: {kws}"


def test_parse_frontmatter_no_triggers_for_document_intake(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """document-intake 没有 trigger phrases (走 has_attachments 入口)."""
    info = parse_skill_frontmatter(use_fake_root / "document-intake" / "SKILL.md")
    assert info is not None
    assert info.trigger_phrases == ()
    # 仍然能抽到中文关键词
    assert any("文件" in k for k in info.keywords)


# ─────────────────────────── match_skill_for_intent ─────────────────────


def test_match_chinese_intent_hits_brand_marketing(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """中文 '内容营销方案' 应当命中 brand-marketing (关键词匹配, 不依赖 trigger)."""
    matched = match_skill_for_intent(
        RouterIntent.DEEP_CREATIVE, "请帮我做一个内容营销方案"
    )
    names = [s.name for s in matched]
    assert "brand-marketing" in names


def test_match_trigger_phrase_english(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """trigger phrase 命中: 'create slogan'."""
    matched = match_skill_for_intent(
        RouterIntent.CREATIVE, "Please create slogan for our new product"
    )
    names = [s.name for s in matched]
    assert "creative-copywriting" in names


def test_match_intent_skip_casual() -> None:
    """CASUAL intent 应当返回空 list (闲聊不注入 skill)."""
    matched = match_skill_for_intent(RouterIntent.CASUAL, "今天天气怎么样")
    assert matched == []


def test_match_intent_skip_realtime() -> None:
    """REALTIME intent 不注入 skill."""
    matched = match_skill_for_intent(RouterIntent.REALTIME, "现在股价多少")
    assert matched == []


def test_match_intent_allowlist_filters_irrelevant() -> None:
    """DEEP_INSIGHT intent 只允许 budget-management / data-analytics /
    pr-management / client-project-management 候选, 不会把 creative-copywriting
    拉进来 (即使关键词命中)."""
    matched = match_skill_for_intent(
        RouterIntent.DEEP_INSIGHT,
        "做个内容营销方案",  # 内容营销触发 creative-copywriting 关键词
    )
    names = [s.name for s in matched]
    # creative-copywriting 不在 DEEP_INSIGHT allowlist
    assert "creative-copywriting" not in names


def test_match_attachments_force_document_intake() -> None:
    """has_attachments=True 时强制把 document-intake 加进候选.

    注意: CASUAL / REALTIME / FALLBACK 在 _INTENT_SKIP_SKILLS 里, 会先被
    短路返回空 — 所以这里用 ``None`` intent (不传), 让 has_attachments 路径
    走通.
    """
    matched = match_skill_for_intent(
        None,  # 不传 intent, 跳过 skip / allowlist 限制
        "你看下这个",  # 普通文本
        has_attachments=True,
    )
    names = [s.name for s in matched]
    assert "document-intake" in names


def test_match_no_intent_string_falls_back_to_keyword() -> None:
    """intent 是未知 str 时, 不抛异常, 走关键词兜底匹配."""
    matched = match_skill_for_intent("weird_intent_value", "做个内容营销方案")
    # 至少 brand-marketing 应该命中 (关键词 "营销" 在 description 里)
    assert any(s.name == "brand-marketing" for s in matched)


def test_match_obsidian_markdown_bypasses_business_intent_allowlist(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    """Obsidian authoring skills should match even under business intents."""
    matched = match_skill_for_intent(
        RouterIntent.DEEP_INSIGHT,
        "帮我写一篇 Obsidian note，要有 wikilinks、callouts 和 frontmatter",
    )
    assert [s.name for s in matched][:1] == ["obsidian-markdown"]


def test_match_obsidian_bases_bypasses_business_intent_allowlist(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    matched = match_skill_for_intent(
        RouterIntent.WORK_PREFLIGHT,
        "帮我创建一个 Obsidian Bases .base table view，带 filters 和 formulas",
    )
    assert any(s.name == "obsidian-bases" for s in matched)


def test_match_json_canvas_bypasses_business_intent_allowlist(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    matched = match_skill_for_intent(
        RouterIntent.DEEP_INSIGHT,
        "创建一个 Obsidian Canvas mind map，输出 .canvas JSON nodes and edges",
    )
    assert any(s.name == "json-canvas" for s in matched)


def test_match_mermaid_visualizer_bypasses_business_intent_allowlist(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    matched = match_skill_for_intent(
        RouterIntent.DEEP_INSIGHT,
        "Create a Mermaid sequence diagram for the API authentication flow",
    )
    assert any(s.name == "mermaid-visualizer" for s in matched)


def test_global_obsidian_skills_ignore_generic_business_text(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    prompts = (
        "edit files",
        "write YAML frontmatter for a Hugo blog post",
        "summarize customer callouts from this report",
    )

    for prompt in prompts:
        matched = match_skill_for_intent(RouterIntent.DEEP_INSIGHT, prompt)
        assert {
            "obsidian-markdown",
            "obsidian-bases",
            "json-canvas",
            "mermaid-visualizer",
        }.isdisjoint({s.name for s in matched})


def test_global_obsidian_skills_ignore_execution_style_obsidian_text(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    prompts = (
        "search my Obsidian vault using Obsidian CLI and run obsidian commands",
        "list files in my Obsidian vault",
        "open Obsidian settings",
    )

    for prompt in prompts:
        matched = match_skill_for_intent(RouterIntent.DEEP_INSIGHT, prompt)
        assert {
            "obsidian-markdown",
            "obsidian-bases",
            "json-canvas",
            "mermaid-visualizer",
        }.isdisjoint({s.name for s in matched})


def test_execution_oriented_obsidian_skills_do_not_bypass_allowlist(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """CLI-like Agent Skills must not quietly bypass business intent policy."""
    cli_matched = match_skill_for_intent(
        RouterIntent.DEEP_INSIGHT,
        "search my vault using Obsidian CLI and run obsidian commands",
    )
    defuddle_matched = match_skill_for_intent(
        RouterIntent.WORK_PREFLIGHT,
        "use defuddle to parse this URL into clean markdown",
    )

    assert "obsidian-cli" not in {s.name for s in cli_matched}
    assert "defuddle" not in {s.name for s in defuddle_matched}


# ─────────────────────────── build_skill_prompt_block ───────────────────


def test_build_block_empty_skills_returns_empty() -> None:
    """空 skills 列表返回空字符串."""
    assert build_skill_prompt_block([]) == ""


def test_build_block_includes_header_and_skill(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """输出应当包含 marker 头 + 技能标题 + body 截断标记."""
    matched = match_skill_for_intent(RouterIntent.DEEP_CREATIVE, "做个内容营销方案")
    assert matched, "前提: 应该至少匹配上 brand-marketing"
    block = build_skill_prompt_block(matched, max_total_bytes=8192)
    assert "[AstrBot skill hints" in block
    assert "brand-marketing" in block
    assert "请直接基于这些内容回答" in block


def test_build_block_respects_byte_budget() -> None:
    """max_total_bytes 设很小时, 应当截断 (block 长度不超过预算 + 一些 marker 余量)."""

    # 造一个 fake SkillInfo, body 读不到 (path 不存在 → read_skill_card 返回 "")
    fake = SkillInfo(
        name="fake-skill",
        title="Fake Skill For Budget Test",
        description="x",
        trigger_phrases=(),
        keywords=(),
        path=Path("/nonexistent/SKILL.md"),  # read_skill_card 会 IO 失败 → ""
    )
    # body 为空 → 应当只剩 header, 没具体 skill 内容
    block = build_skill_prompt_block([fake], max_total_bytes=4096)
    assert "fake-skill" not in block
    # header 仍然存在 (没匹配上等于直接返回)
    assert "AstrBot skill hints" in block


def test_build_block_truncates_real_long_skill(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """设 max_total_bytes=200, 实际 block 长度不能超过预算."""
    matched = match_skill_for_intent(RouterIntent.DEEP_CREATIVE, "做个内容营销方案")
    block = build_skill_prompt_block(matched, max_total_bytes=200)
    assert len(block.encode("utf-8")) <= 200
    assert "(truncated" in block or len(block) < 200


def test_build_block_enforces_byte_budget_for_chinese_skill(tmp_path: Path) -> None:
    """UTF-8 multi-byte content must not exceed the prompt byte budget."""
    skill_dir = tmp_path / "zh-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: zh-skill
description: 中文技能
---

"""
        + ("内容营销品牌策略" * 200),
        encoding="utf-8",
    )
    info = SkillInfo(
        name="zh-skill",
        title="中文技能",
        description="中文技能",
        trigger_phrases=(),
        keywords=("营销",),
        path=skill_md,
    )

    block = build_skill_prompt_block([info], max_total_bytes=500)

    assert len(block.encode("utf-8")) <= 500
    assert "truncated" in block


# ─────────────────────────── read_skill_card + cache ────────────────────


def test_read_skill_card_basic(
    use_fake_root: Path,  # noqa: ARG001
) -> None:
    """读真实 SKILL.md, 应当返回 frontmatter + body 截断格式."""
    path = use_fake_root / "brand-marketing" / "SKILL.md"
    text = read_skill_card(path)
    assert "Brand Marketing Planning" in text
    assert "品牌推广" in text or "marketing" in text


def test_read_skill_card_preloads_agent_skill_references(tmp_path: Path) -> None:
    """Agent Skills-style ``references/*.md`` links are loaded into the card."""
    skill_dir = tmp_path / "obsidian-markdown"
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: obsidian-markdown
description: Create Obsidian markdown notes with wikilinks and callouts.
---

# Obsidian Markdown

Use properties and callouts. See [Properties](references/PROPERTIES.md).
""",
        encoding="utf-8",
    )
    (refs_dir / "PROPERTIES.md").write_text(
        "# Properties\n\nUse tags, aliases, dates, and status fields.",
        encoding="utf-8",
    )
    (refs_dir / "UNLINKED.md").write_text(
        "unlinked reference",
        encoding="utf-8",
    )

    text = read_skill_card(skill_md, max_chars=DEFAULT_SKILL_CARD_CHARS)

    assert "Referenced skill files" in text
    assert "references/PROPERTIES.md" in text
    assert "Use tags, aliases, dates" in text
    assert "unlinked reference" not in text
    assert "reference truncated" not in text
    assert "see SKILL.md for full content" not in text


def test_real_obsidian_markdown_preloads_direct_references(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    """The installed kepano Obsidian Markdown skill preloads linked references."""
    path = REAL_SKILLS_ROOT / "obsidian-markdown" / "SKILL.md"

    text = read_skill_card(path, max_chars=4096)

    assert "Referenced skill files" in text
    assert "references/PROPERTIES.md" in text
    assert "references/EMBEDS.md" in text
    assert "references/CALLOUTS.md" in text


def test_real_json_canvas_preloads_layout_reference(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    """The installed JSON Canvas skill preloads layout authoring guidance."""
    path = REAL_SKILLS_ROOT / "json-canvas" / "SKILL.md"

    text = read_skill_card(path, max_chars=4096)

    assert "Referenced skill files" in text
    assert "references/LAYOUTS.md" in text
    assert "JSON Canvas Layout Patterns" in text


def test_real_obsidian_authoring_skills_include_upstream_license(
    real_skills_root: Path,  # noqa: ARG001
) -> None:
    for skill_name in (
        "obsidian-markdown",
        "obsidian-bases",
        "json-canvas",
        "mermaid-visualizer",
    ):
        license_path = REAL_SKILLS_ROOT / skill_name / "LICENSE"
        assert license_path.is_file()
        assert "MIT License" in license_path.read_text(encoding="utf-8")


def test_read_skill_card_rejects_references_outside_skill_dir(tmp_path: Path) -> None:
    """A crafted skill cannot preload markdown outside its own directory."""
    secret = tmp_path / "SECRET.md"
    secret.write_text("do not inject this", encoding="utf-8")
    skill_dir = tmp_path / "unsafe-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: unsafe-skill
description: Unsafe path probe.
---

# Unsafe

Do not follow [secret](../SECRET.md).
""",
        encoding="utf-8",
    )

    text = read_skill_card(skill_md)

    assert "Referenced skill files" not in text
    assert "do not inject this" not in text


def test_read_skill_card_rejects_parent_traversal_inside_skill_dir(
    tmp_path: Path,
) -> None:
    """Parent traversal links are ignored even when they resolve inside the skill."""
    skill_dir = tmp_path / "strict-skill"
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True)
    (skill_dir / "LOCAL.md").write_text("local traversal target", encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        """---
name: strict-skill
description: Strict path probe.
---

# Strict

Do not follow [local](references/../LOCAL.md).
""",
        encoding="utf-8",
    )

    text = read_skill_card(skill_md)

    assert "Referenced skill files" not in text
    assert "local traversal target" not in text


def test_read_skill_card_cache_invalidates_when_reference_changes(
    tmp_path: Path,
) -> None:
    """Reference file mtimes participate in the cache key."""
    clear_skill_cache()
    skill_dir = tmp_path / "agent-skill"
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    ref_md = refs_dir / "DETAILS.md"
    skill_md.write_text(
        """---
name: agent-skill
description: Uses a referenced detail file.
---

See [Details](references/DETAILS.md).
""",
        encoding="utf-8",
    )
    ref_md.write_text("first version", encoding="utf-8")
    first = read_skill_card(skill_md)

    ref_md.write_text("second version", encoding="utf-8")
    second = read_skill_card(skill_md)

    assert "first version" in first
    assert "second version" in second


def test_read_skill_card_respects_max_chars_across_cache_hits(tmp_path: Path) -> None:
    """The cache key includes max_chars so a larger read cannot leak into a smaller one."""
    clear_skill_cache()
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("x" * 200, encoding="utf-8")

    larger = read_skill_card(skill_md, max_chars=100)
    smaller = read_skill_card(skill_md, max_chars=10)

    assert len(larger.encode("utf-8")) <= 100
    assert len(smaller.encode("utf-8")) <= 10


def test_read_skill_card_caches_under_same_mtime(tmp_path: Path) -> None:
    """同一 mtime + 30s 内, 不重复 IO (patch path.read_text 计数)."""
    clear_skill_cache()
    fake_path = tmp_path / "skill_cache_test.md"
    fake_path.write_text("---\nname: x\n---\nbody", encoding="utf-8")

    with patch.object(Path, "read_text", wraps=fake_path.read_text) as spy:
        _ = read_skill_card(fake_path)
        _ = read_skill_card(fake_path)  # 2nd call should hit cache
        assert spy.call_count == 1, f"cache miss — read_text 被调 {spy.call_count} 次"


def test_read_skill_card_returns_empty_for_missing_file() -> None:
    """不存在的 path 返回 "" 而不是抛异常."""
    assert read_skill_card(Path("/no/such/file.md")) == ""


# ─────────────────────────── SkillInfo.matches ──────────────────────────


def test_skill_info_matches_trigger_case_insensitive() -> None:
    info = SkillInfo(
        name="x",
        title="x",
        description="",
        trigger_phrases=("Marketing Plan",),
        keywords=(),
        path=Path("/dev/null"),
    )
    assert info.matches("please make a marketing plan for me")
    assert info.matches("MARKETING PLAN")  # case-insensitive
    assert not info.matches("unrelated text")


def test_skill_info_matches_keyword_zh() -> None:
    info = SkillInfo(
        name="x",
        title="x",
        description="",
        trigger_phrases=(),
        keywords=("营销",),
        path=Path("/dev/null"),
    )
    assert info.matches("做个内容营销方案")
    assert not info.matches("做个 logo 设计")


def test_skill_info_matches_empty_text() -> None:
    info = SkillInfo(
        name="x",
        title="x",
        description="",
        trigger_phrases=("a",),
        keywords=(),
        path=Path("/dev/null"),
    )
    assert not info.matches("")


# ─────────────────────────── 默认 MAX_PROMPT_BYTES ──────────────────────


def test_default_max_prompt_bytes_is_4k() -> None:
    """公开常量保持稳定 (下游 plugin 依赖此值)."""
    assert DEFAULT_MAX_PROMPT_BYTES == 4096
