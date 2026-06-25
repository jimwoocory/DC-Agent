"""Scan ``data/skills/`` and inject matched SKILL.md content into LLM prompts.

Why this exists (2026-06-11):
    普通 aihubmix LLM 在收到 "请基于 brand-marketing skill 帮我做营销方案" 这类
    任务时, 经常 hallucinate 说 "我正使用 astrbot_file_read_tool 读取 brand-
    marketing..." 但**不真的调 tool** (截图里看到的假工具调用). 让 LLM 真的能
    拿到 skill 内容的办法: 在 routing 阶段 (LLM 调用前) 主动预读匹配 skill 的
    SKILL.md 全文, 注入到 system_prompt. 这样 LLM 拿到真东西, hallucinate
    tool use 自动消除.

设计要点:
    1. **.yaml 解析不依赖 pyyaml**: SKILL.md 的 frontmatter 是简单 YAML 块,
       字段固定 (name, description, version), 自己写 30 行 parser 更稳.
    2. **trigger phrases + 关键词 fallback**: "Trigger phrases: " 后面跟的是
       显式列表; description 里的关键词也作为 fallback. 中英文混合场景下
       description 第一句的关键词更稳.
    3. **intent 门控**: 不是所有 intent 都需要 skill. CASUAL / FALLBACK /
       REALTIME / PUBLIC_OPINION 这类不需要. DEEP_*/INSIGHT/CREATIVE 这类才注入.
    4. **缓存**: 按 (path, mtime_ns) 缓存读过的 SKILL.md 内容, 避免重复 IO.
    5. **大小上限**: 注入到 system_prompt 的总大小不能爆, 默认 4KB.

公开 API:
    ``list_available_skills()``  — 扫盘, 返回所有 SkillInfo
    ``parse_skill_frontmatter()``  — 解析 SKILL.md 的 YAML frontmatter
    ``match_skill_for_intent()``  — intent + text → 匹配的 SkillInfo 列表
    ``read_skill_card()``  — 读 SKILL.md 的 frontmatter + 第一段正文
    ``build_skill_prompt_block()``  — 把匹配的 skill 拼成 system_prompt 段
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dc_router_core.taxonomy import RouterIntent

# 默认 skills 根目录, 跟 AstrBot 共享 (astrbot_path.get_astrbot_skills_path()).
# 这里硬编码 fallback, 测试可临时 patch _SKILLS_ROOT.
_DEFAULT_SKILLS_ROOT = Path("data") / "skills"
_SKILLS_ROOT: Path | None = None

# intent → 哪些 skill 目录名适合. 没列出的目录 (如 document-intake) 走
# 关键词匹配或 attachment 检测.
_INTENT_ALLOWED_SKILLS: dict[RouterIntent, frozenset[str]] = {
    RouterIntent.DEEP_CREATIVE: frozenset(
        {
            "brand-marketing",
            "creative-copywriting",
            "creative-design",
            "event-planning",
            "planning-writing",
        }
    ),
    RouterIntent.DEEP_INSIGHT: frozenset(
        {
            "data-analytics",
            "budget-management",
            "pr-management",
            "client-project-management",
        }
    ),
    RouterIntent.CREATIVE: frozenset(
        {"creative-copywriting", "creative-design", "planning-writing"}
    ),
    RouterIntent.INSIGHT: frozenset({"data-analytics"}),
    RouterIntent.WORK_PREFLIGHT: frozenset(
        {"client-project-management", "pr-management"}
    ),
    RouterIntent.OPS_WRITING: frozenset({"creative-copywriting"}),
}

_GLOBAL_MATCH_SKILLS: frozenset[str] = frozenset(
    {
        "obsidian-markdown",
        "obsidian-bases",
        "json-canvas",
        "mermaid-visualizer",
    }
)

_GLOBAL_MATCH_ANCHORS: dict[str, tuple[str, ...]] = {
    "obsidian-markdown": (
        "obsidian note",
        "obsidian notes",
        "obsidian markdown",
        "wikilink",
        "wikilinks",
        "obsidian callout",
        "obsidian callouts",
        "obsidian frontmatter",
        "[[",
        "]]",
        "> [!",
    ),
    "obsidian-bases": (
        "obsidian bases",
        "obsidian base",
        ".base",
        "bases table",
        "bases view",
    ),
    "json-canvas": (
        "json canvas",
        "obsidian canvas",
        ".canvas",
    ),
    "mermaid-visualizer": (
        "mermaid",
        "flowchart",
        "sequence diagram",
        "state diagram",
        "mindmap",
        "mind map",
        "visualize",
        "diagram",
    ),
}

# intent → 完全跳过 skill 注入的清单 (闲聊 / 实时 / 兜底).
_INTENT_SKIP_SKILLS: frozenset[RouterIntent] = frozenset(
    {
        RouterIntent.CASUAL,
        RouterIntent.REALTIME,
        RouterIntent.PUBLIC_OPINION,
        RouterIntent.SIMPLE_CODE,
        RouterIntent.FALLBACK,
    }
)

# 默认注入到 system_prompt 的总大小上限 (4 KB). 超过的 skill 末尾会带
# "...(truncated)" 标记.
DEFAULT_MAX_PROMPT_BYTES = 4096
DEFAULT_SKILL_CARD_CHARS = 1800
DEFAULT_REFERENCE_CHARS = 450
DEFAULT_MAX_REFERENCES = 3

# 缓存层: (path, mtime_ns) → (cached_text, cached_at)
_skill_card_cache: dict[
    tuple[str, int, int, tuple[tuple[str, int], ...]], tuple[str, float]
] = {}
_CACHE_TTL_SEC = 30.0  # 文件没改的话 30s 复用缓存


# ─────────────────────────── 公共数据结构 ────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """一个可用 skill 的元数据。"""

    name: str  # 目录名, e.g. "brand-marketing"
    title: str  # SKILL.md frontmatter name, e.g. "Brand Marketing Planning"
    description: str  # 解析后的 description 全文
    trigger_phrases: tuple[str, ...]  # 从 description 抽出的 trigger 关键词
    keywords: tuple[str, ...]  # description 里补充的关键词 (用于匹配)
    path: Path  # SKILL.md 路径

    def matches(self, text: str) -> bool:
        """text 是否命中这个 skill 的 trigger phrases 或 keywords。

        大小写不敏感, 词组 (含空格) 也匹配."""
        if not text:
            return False
        lowered = text.lower()
        for phrase in self.trigger_phrases:
            if phrase.lower() in lowered:
                return True
        for kw in self.keywords:
            if len(kw) >= 2 and kw.lower() in lowered:
                return True
        return False


# ─────────────────────────── 路径/扫描 ──────────────────────────────────


def get_skills_root() -> Path:
    """返回 skills 根目录。

    优先用环境变量 ``DC_AGENT_SKILLS_ROOT`` 覆盖 (测试用), 否则
    ``data/skills/`` (项目根下的相对路径). 真实 AstrBot 部署时, 这个相对
    路径会被 cwd 解析到项目根.
    """
    global _SKILLS_ROOT
    if _SKILLS_ROOT is not None:
        return _SKILLS_ROOT
    env = os.environ.get("DC_AGENT_SKILLS_ROOT", "").strip()
    if env:
        _SKILLS_ROOT = Path(env).resolve()
    else:
        _SKILLS_ROOT = _DEFAULT_SKILLS_ROOT.resolve()
    return _SKILLS_ROOT


def reset_skills_root_for_testing() -> None:
    """测试钩子: 清除缓存的 skills 根目录, 让下一次 ``get_skills_root()``
    重新读 env 变量."""
    global _SKILLS_ROOT
    _SKILLS_ROOT = None


def list_available_skills() -> list[SkillInfo]:
    """扫 ``data/skills/*/SKILL.md``, 解析每个 frontmatter + description.

    没有 SKILL.md 的目录或 frontmatter 解析失败的目录会被静默跳过.
    """
    root = get_skills_root()
    if not root.exists() or not root.is_dir():
        return []

    out: list[SkillInfo] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            info = parse_skill_frontmatter(skill_md)
        except _SkillParseError:
            continue
        if info is None:
            continue
        out.append(info)
    return out


# ─────────────────────────── SKILL.md 解析 ──────────────────────────────


class _SkillParseError(Exception):
    """SKILL.md frontmatter 解析失败 (格式非预期)."""


_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
# trigger phrases 通常是 "Trigger phrases: " 后面的 quoted list, e.g.
#   Trigger phrases: "marketing plan", "brand strategy"
_TRIG_RE = re.compile(
    r"Trigger\s+phrases?:\s*(.+?)(?:\.\s*$|\.\s*\n|\Z)", re.IGNORECASE | re.DOTALL
)
_QUOTED_RE = re.compile(r'"([^"]+)"')


def parse_skill_frontmatter(path: Path) -> SkillInfo | None:
    """解析 SKILL.md 的 YAML frontmatter, 返回 SkillInfo.

    YAML 不解析完整语法 (不依赖 pyyaml), 只取 ``name`` / ``description`` /
    ``version`` 三个字段. description 可能是单行或 ``>`` 折叠块.

    注意 (2026-06-11): SKILL.md 的 frontmatter description 通常是英文模板句
    (e.g. "This skill should be used when..."), 真正的中文关键词在 body
    (e.g. "专业的品牌推广策划技能..."). 我们会把 frontmatter + body 头
    2000 字符都喂给 keyword 抽取, 但 ``description`` 字段本身只暴露
    frontmatter 那个 (因为它是 LLM 看 "这个 skill 是干嘛的" 的简介).
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise _SkillParseError(str(exc)) from exc

    m = _FRONT_RE.match(text)
    if not m:
        return _build_skill_info(
            name=path.parent.name,
            title=path.parent.name,
            description="",
            raw_text=text,
            path=path,
        )

    fm = m.group(1)
    body = m.group(2)
    name = _extract_scalar(fm, "name") or path.parent.name
    desc_block = _extract_description(fm) or ""
    # 反转义 frontmatter 里残留的 HTML 实体 (典型: &quot; 来自 > 折叠块里的
    # 双引号转义, &amp; 来自 &)
    desc_block = _unescape_html(desc_block)
    return _build_skill_info(
        name=path.parent.name,
        title=name,
        description=desc_block,
        raw_text=body,
        path=path,
    )


def _build_skill_info(
    *,
    name: str,
    title: str,
    description: str,
    raw_text: str,
    path: Path,
) -> SkillInfo:
    """从 description + body 抽 trigger_phrases 和 keywords.

    frontmatter 的 description 经常只有英文模板句 ("This skill should be
    used when..."), 真正的中文关键词在 body. 我们把 description 和 body
    前 2000 字符拼起来, 一起喂给 keyword 抽取.
    """
    # 拼 frontmatter description + body 头, 一起抽 trigger / keyword
    combined = (description or "") + "\n" + (raw_text or "")[:2000]
    phrases, keywords = _extract_triggers_and_keywords(combined)
    # trigger phrases 还要尝试从 description 单抽 (因为 description 是
    # "This skill should be used when ... Trigger phrases: a, b, c." 格式),
    # combined 拼上 body 头不会破坏.
    return SkillInfo(
        name=name,
        title=title,
        description=description.strip(),
        trigger_phrases=tuple(phrases),
        keywords=tuple(keywords),
        path=path,
    )


_HTML_ENTITIES = {
    "quot": '"',
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "apos": "'",
    "nbsp": " ",
}


def _unescape_html(text: str) -> str:
    """把 ``&quot;`` / ``&amp;`` / ``&lt;`` / ``&gt;`` / ``&apos;`` / ``&nbsp;``
    反转义回原始字符. SKILL.md frontmatter 的 ``>`` 折叠块里常出现 quoted 段.
    """
    if "&" not in text:
        return text
    return re.sub(
        r"&(quot|amp|lt|gt|apos|nbsp);",
        lambda m: _HTML_ENTITIES.get(m.group(1), m.group(0)),
        text,
    )


def _truncate_utf8(text: str, max_bytes: int, suffix: str = "") -> str:
    """Truncate text to a UTF-8 byte budget without splitting characters."""
    if max_bytes <= 0:
        return ""
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) > max_bytes:
        return suffix_bytes[:max_bytes].decode("utf-8", errors="ignore")
    encoded = text.encode("utf-8")
    if len(encoded) + len(suffix_bytes) <= max_bytes:
        return text
    body_budget = max_bytes - len(suffix_bytes)
    return encoded[:body_budget].decode("utf-8", errors="ignore") + suffix


def _extract_scalar(fm: str, key: str) -> str | None:
    """抽 YAML frontmatter 里的单行字段 (name / version)."""
    m = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", fm, re.M)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    if val in {">", "|"} or val.startswith("&") or val.startswith("|"):
        return None
    return val


def _extract_description(fm: str) -> str | None:
    """抽 ``description:`` 字段. 支持单行 和 ``>`` 折叠块."""
    # 单行
    m = re.search(r'^description:\s*"?(.+?)"?\s*$', fm, re.M)
    if m and m.group(1) not in {">", "|"} and not m.group(1).startswith("&"):
        return m.group(1)
    # > / | 折叠块. Some local fixtures HTML-escape the marker as &gt;.
    m = re.search(r"^description:\s*(?:&gt;|>|\|)\s*\n((?:  .*\n?)+)", fm, re.M)
    if m:
        lines = [line.strip() for line in m.group(1).splitlines() if line.strip()]
        return " ".join(lines)
    return None


def _extract_triggers_and_keywords(desc: str) -> tuple[list[str], list[str]]:
    """从 description 抽 trigger phrases (引号列表) 和 fallback keywords.

    - trigger_phrases: 显式 quoted 列表, 比如 "marketing plan", "brand strategy"
    - keywords: 整句里过滤掉停用词后剩下的有意义 token. 用两个 regex:
      * 英文: 3+ 字符的单词
      * 中文: 2+ 字符的连续汉字 (主), 加 2 字符 sliding window (兜底, 防止
        "内容营销" 把 "营销" 这个独立词吞掉)
    """
    if not desc:
        return [], []

    phrases: list[str] = []
    m = _TRIG_RE.search(desc)
    if m:
        quoted = _QUOTED_RE.findall(m.group(1))
        if quoted:
            phrases = [p.strip() for p in quoted if p.strip()]
        else:
            # 没引号的话, 用逗号 split
            raw = m.group(1).rstrip(".").strip()
            phrases = [p.strip() for p in raw.split(",") if p.strip()]

    keywords: list[str] = []
    # 抽 description 里的 "重要名词短语" — 简单规则:
    # - 跳过停用词
    # - 保留 3+ 字符的英文 token
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "use",
        "when",
        "user",
        "any",
        "are",
        "from",
        "have",
        "has",
        "should",
        "would",
        "could",
        "create",
        "make",
        "want",
        "needs",
        "asks",
        "asking",
        "their",
        "your",
        "skill",
        "used",
        "based",
        "through",
        "into",
        "about",
        "such",
        "same",
        "than",
        "more",
        "less",
        "most",
        "least",
        "only",
        "own",
        "other",
    }
    for tok in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", desc):
        if tok.lower() in stop:
            continue
        keywords.append(tok)

    # 中文: 2+ 连续汉字 (主)
    cn_long = re.findall(r"[\u4e00-\u9fff]{2,}", desc)
    # 中文 2 字符 sliding window (兜底: "内容营销" 抓完后, 还要 "内容" "营销"
    # 这种独立 2 字词). 限制输出, 避免噪声.
    cn_short: list[str] = []
    for tok in cn_long:
        if len(tok) >= 4:
            # 从 4+ 字 token 里抽 2 字 sliding window
            for i in range(len(tok) - 1):
                pair = tok[i : i + 2]
                if pair not in cn_short:
                    cn_short.append(pair)
    keywords.extend(cn_long)
    keywords.extend(cn_short)

    # 去重保序
    seen: set[str] = set()
    keywords_dedup: list[str] = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            keywords_dedup.append(kw)
    return phrases, keywords_dedup


# ─────────────────────────── 匹配 ──────────────────────────────────────


def _global_match_anchor_hit(skill_name: str, text: str) -> bool:
    """Return whether a global authoring skill has an explicit domain anchor."""
    anchors = _GLOBAL_MATCH_ANCHORS.get(skill_name, ())
    if not anchors or not text:
        return False
    lowered = text.lower()
    return any(anchor in lowered for anchor in anchors)


def match_skill_for_intent(
    intent: RouterIntent | str,
    text: str,
    *,
    has_attachments: bool = False,
    max_matches: int = 2,
) -> list[SkillInfo]:
    """根据 intent + 文本匹配最相关的 skill 目录.

    匹配流程:
    1. intent 在 ``_INTENT_SKIP_SKILLS`` → 返回空 (闲聊/实时不要 skill)
    2. intent 在 ``_INTENT_ALLOWED_SKILLS`` → 限制候选范围
    3. 对每个候选 SkillInfo, 调 ``matches(text)``:
       - 先看 trigger phrases 是否命中
       - 再看 keywords 是否命中
    4. 按 (trigger_hits, keyword_hits) 排序, 取 top ``max_matches``

    ``has_attachments=True`` 时, 强制把 ``document-intake`` 加进候选 (文件
    类附件命中).
    """
    # 收窄到 RouterIntent, 接受 str (来自 event.get_extra) 或 enum (来自分类器)
    intent_enum: RouterIntent | None
    if isinstance(intent, RouterIntent):
        intent_enum = intent
    elif isinstance(intent, str) and intent:
        try:
            intent_enum = RouterIntent(intent)
        except ValueError:
            intent_enum = None
    else:
        intent_enum = None

    if intent_enum is not None and intent_enum in _INTENT_SKIP_SKILLS:
        return []

    allowed_dirs = (
        _INTENT_ALLOWED_SKILLS.get(intent_enum, frozenset())
        if intent_enum
        else frozenset()
    )

    candidates: list[SkillInfo] = []
    for skill in list_available_skills():
        is_global_match_skill = skill.name in _GLOBAL_MATCH_SKILLS
        # intent 限制
        if (
            allowed_dirs
            and skill.name not in allowed_dirs
            and not is_global_match_skill
        ):
            # 关键词很重时, 也允许跨 intent (兜底)
            if not has_attachments or skill.name != "document-intake":
                continue
        if has_attachments and skill.name == "document-intake":
            candidates.append(skill)
            continue
        if is_global_match_skill and not _global_match_anchor_hit(skill.name, text):
            continue
        if skill.matches(text):
            candidates.append(skill)

    # 排序: trigger 命中数 desc, keyword 命中数 desc
    lowered = (text or "").lower()
    scored: list[tuple[int, int, SkillInfo]] = []
    for skill in candidates:
        trig_hits = sum(1 for p in skill.trigger_phrases if p.lower() in lowered)
        kw_hits = sum(1 for k in skill.keywords if len(k) >= 2 and k.lower() in lowered)
        scored.append((trig_hits, kw_hits, skill))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for _, _, s in scored[:max_matches]]


# ─────────────────────────── 读取 + 拼装 ────────────────────────────────


_MD_LINK_RE = re.compile(r"\]\(([^)]+?\.md(?:#[^)]+)?)\)")


def _extract_reference_paths(text: str, skill_md_path: Path) -> list[Path]:
    """Return safe same-skill markdown references declared from ``SKILL.md``.

    Agent Skills often keep detailed instructions under ``references/*.md`` and
    link to them from the main skill card. We preload a bounded subset, but only
    from inside the current skill directory so a crafted skill cannot read
    arbitrary workspace files.
    """
    if not text:
        return []

    skill_dir = skill_md_path.parent.resolve(strict=False)
    refs: list[Path] = []
    seen: set[Path] = set()
    for raw_ref in _MD_LINK_RE.findall(text):
        clean_ref = raw_ref.split("#", 1)[0].strip()
        if not clean_ref or "://" in clean_ref:
            continue
        if ".." in Path(clean_ref).parts:
            continue
        candidate = (skill_dir / clean_ref).resolve(strict=False)
        try:
            candidate.relative_to(skill_dir)
        except ValueError:
            continue
        if candidate == skill_md_path.resolve(strict=False):
            continue
        if candidate.suffix.lower() != ".md" or candidate in seen:
            continue
        refs.append(candidate)
        seen.add(candidate)
        if len(refs) >= DEFAULT_MAX_REFERENCES:
            break
    return refs


def _reference_cache_signature(
    reference_paths: Iterable[Path],
) -> tuple[tuple[str, int], ...]:
    signature: list[tuple[str, int]] = []
    for ref_path in reference_paths:
        try:
            signature.append((str(ref_path), ref_path.stat().st_mtime_ns))
        except OSError:
            signature.append((str(ref_path), -1))
    return tuple(signature)


def _cached_skill_card(
    *,
    path: Path,
    skill_mtime_ns: int,
    max_chars: int,
) -> str | None:
    resolved = str(path.resolve())
    now = time.time()
    for cache_key, cached in list(_skill_card_cache.items()):
        cached_path, cached_mtime_ns, cached_max_chars, reference_signature = cache_key
        if (
            cached_path != resolved
            or cached_mtime_ns != skill_mtime_ns
            or cached_max_chars != max_chars
        ):
            continue
        text, ts = cached
        if now - ts >= _CACHE_TTL_SEC:
            continue
        reference_paths = [Path(ref_path) for ref_path, _mtime in reference_signature]
        if _reference_cache_signature(reference_paths) == reference_signature:
            return text
    return None


def _read_reference_snippets(reference_paths: Iterable[Path]) -> str:
    parts: list[str] = []
    for ref_path in reference_paths:
        if not ref_path.is_file():
            continue
        try:
            text = ref_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = _unescape_html(text).strip()
        if not text:
            continue
        text = _truncate_utf8(
            text,
            DEFAULT_REFERENCE_CHARS,
            suffix="\n…(reference truncated)",
        )
        parts.append(f"### {ref_path.parent.name}/{ref_path.name}\n{text}")

    if not parts:
        return ""
    return "\n\n## Referenced skill files (preloaded)\n" + "\n\n".join(parts)


def read_skill_card(path: Path, max_chars: int = DEFAULT_SKILL_CARD_CHARS) -> str:
    """读 SKILL.md, 返回 ``--- frontmatter ---\\n<正文截断>`` 格式.

    缓存以 ``(path, mtime_ns)`` 为 key, 30s 内复用. 实际 IO 触发
    ``os.stat``, 几乎零成本.
    """
    try:
        stat = path.stat()
    except OSError:
        return ""

    cached_text = _cached_skill_card(
        path=path,
        skill_mtime_ns=stat.st_mtime_ns,
        max_chars=max_chars,
    )
    if cached_text is not None:
        return cached_text

    try:
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    raw_text = _unescape_html(raw_text)
    reference_paths = _extract_reference_paths(raw_text, path)
    reference_signature = _reference_cache_signature(reference_paths)
    cache_key = (str(path.resolve()), stat.st_mtime_ns, max_chars, reference_signature)
    cached = _skill_card_cache.get(cache_key)
    if cached is not None:
        text, ts = cached
        if time.time() - ts < _CACHE_TTL_SEC:
            return text

    text = raw_text
    reference_block = _read_reference_snippets(reference_paths)
    if reference_block:
        reference_budget = min(len(reference_block.encode("utf-8")), max_chars // 2)
        main_budget = max(0, max_chars - reference_budget)
        text = _truncate_utf8(
            text,
            main_budget,
            suffix="\n\n…(truncated, see SKILL.md for full content)",
        )
        text = text + reference_block

    text = _truncate_utf8(
        text,
        max_chars,
        suffix="\n\n…(truncated, see SKILL.md for full content)",
    )
    _skill_card_cache[cache_key] = (text, time.time())
    return text


def clear_skill_cache() -> None:
    """清缓存 (测试用)."""
    _skill_card_cache.clear()


def build_skill_prompt_block(
    skills: Iterable[SkillInfo],
    *,
    max_total_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
) -> str:
    """把匹配的 skill 拼成一个 system_prompt 段.

    格式::

        [AstrBot skill hints — 基于意图匹配自动注入, 不要假装使用 astrbot_file_read_tool]
        1. <skill_name> (— <title>): <body>
        2. <skill_name> (— <title>): <body>
        …

    总字节数受 ``max_total_bytes`` 限制, 超出截断.
    """
    skill_list = list(skills)
    if not skill_list:
        return ""

    header = (
        "\n\n[AstrBot skill hints — 以下技能库内容已预读注入, "
        "请直接基于这些内容回答, 不要假装调用 astrbot_file_read_tool]\n"
    )
    if len(header.encode("utf-8")) >= max_total_bytes:
        return _truncate_utf8(header, max_total_bytes)
    parts: list[str] = [header]
    budget = max_total_bytes - len(header.encode("utf-8"))
    for idx, skill in enumerate(skill_list, start=1):
        body = read_skill_card(skill.path)
        if not body:
            continue
        block = f"\n{idx}. {skill.name} — {skill.title}:\n{body}\n"
        block_bytes = len(block.encode("utf-8"))
        if block_bytes > budget:
            # 预算不够, 截断这个 skill 的 body
            prefix = f"\n{idx}. {skill.name} — {skill.title}:\n"
            suffix = "\n…(truncated, budget exceeded)\n"
            keep = max(0, budget - len(prefix.encode()) - len(suffix.encode()))
            if keep <= 0:
                break
            block = prefix + _truncate_utf8(body, keep) + suffix
            parts.append(block)
            break
        parts.append(block)
        budget -= block_bytes

    return "".join(parts)


__all__ = [
    "SkillInfo",
    "get_skills_root",
    "reset_skills_root_for_testing",
    "list_available_skills",
    "parse_skill_frontmatter",
    "match_skill_for_intent",
    "read_skill_card",
    "build_skill_prompt_block",
    "clear_skill_cache",
    "DEFAULT_MAX_PROMPT_BYTES",
    "DEFAULT_SKILL_CARD_CHARS",
]
