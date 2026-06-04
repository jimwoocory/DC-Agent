"""飞书 Interactive Card JSON 模板。

每个 builder 返回的 dict 都可以直接作为 lark message content（json.dumps 后）
发到 `im.v1.message.create` 或 `im.v1.message.patch`。

卡片头部 template 色板（飞书定义）:
- blue / wathet / turquoise / green / yellow / orange / red /
  carmine / violet / purple / indigo / grey

约定颜色：
- 进度中：indigo（思考冷静色）
- 成功完成：green
- 失败：red
- 日常渲染：blue（业务主调）
"""

from __future__ import annotations

import re
from typing import Any

WAITING_PULSE_FRAMES = ("◐", "◓", "◑", "◒")
WAITING_TRACK_DOTS = 4
# Feishu lark_md body text reliably renders named colors only. Hex values such
# as #940000/#008000 worked for design intent but were dropped in real cards.
WARNING_RED = "red"
SUCCESS_GREEN = "green"
KAMI_INK = "blue"
KAMI_MUTED = "grey"
CASUAL_STRUCTURE_RE = re.compile(
    r"(^|\n)\s*(#{1,6}\s+|[-*]\s+|\d+[.)]\s+|```|>|[^\n|]+\|[^\n|]+)"
)


def _safe_md(text: str, limit: int = 5000) -> str:
    """飞书 lark_md 单元素长度上限 ~10K 字符，留 buffer。"""
    if not text:
        return "_(空)_"
    if len(text) > limit:
        return text[:limit] + f"\n\n_... [已截断，共 {len(text)} 字符]_"
    return text


def waiting_pulse(elapsed_sec: float) -> str:
    """Return a small time-based pulse for cards that are patched in place."""
    frame = int(max(elapsed_sec, 0) // 3) % len(WAITING_PULSE_FRAMES)
    return WAITING_PULSE_FRAMES[frame]


def waiting_track(elapsed_sec: float) -> str:
    """Return a small patched animation track for waiting cards."""
    frame = int(max(elapsed_sec, 0) // 2) % WAITING_TRACK_DOTS
    dots = ["●" if idx == frame else "○" for idx in range(WAITING_TRACK_DOTS)]
    return " ".join(dots)


def _font(text: str, color: str) -> str:
    return f"<font color='{color}'>{text}</font>"


def _muted(text: str) -> str:
    return _font(text, KAMI_MUTED)


def _ink(text: str) -> str:
    return _font(text, KAMI_INK)


def _kami_lede(title: str, note: str = "") -> dict[str, Any]:
    content = _ink(f"**{title}**")
    if note:
        content += f"\n{_muted(note)}"
    return _md(content, "heading_2")


def _code_panel(text: str, limit: int = 1600) -> str:
    content = _safe_md(str(text or "").strip(), limit).replace("```", "'''")
    return f"```text\n{content}\n```"


def _display_panel(
    title: str, content: str, text_size: str = "normal"
) -> dict[str, Any]:
    return _md(f"**{title}**\n{_code_panel(content)}", text_size)


def _status_dot(text: str, color: str = WARNING_RED) -> str:
    return f"{_font('●', color)} {text}"


def _activity_line(action: str, detail: str = "", color: str = "red") -> dict[str, Any]:
    suffix = f" {_muted(detail)}" if detail else ""
    return _md(f"{_font(action, color)}{suffix} ›", "notation")


def _status_color(status: str) -> str:
    if status in {
        "已核验",
        "已归档",
        "已入库",
        "正常",
        "可直接用",
        "完整",
        "可用",
        "已完成",
    }:
        return SUCCESS_GREEN
    if status in {"部分待确认", "需要补充", "待审核", "降级", "需要确认", "已逾期"}:
        return WARNING_RED
    if status in {"执行失败", "入库失败", "异常", "不建议直接用"}:
        return "red"
    if status in {"示例/假设", "示例假设"}:
        return "violet"
    return "grey"


def _format_elapsed(elapsed_sec: float) -> str:
    total = int(max(elapsed_sec, 0))
    minutes, seconds = divmod(total, 60)
    if minutes:
        return f"{minutes} 分 {seconds:02d} 秒"
    return f"{seconds} 秒"


def _strip_table_cell(cell: str) -> str:
    return cell.strip().replace("\\|", "|") or " "


def _is_table_separator(line: str) -> bool:
    cells = [_strip_table_cell(cell) for cell in line.strip().strip("|").split("|")]
    if not cells:
        return False
    for cell in cells:
        normalized = cell.replace(":", "").replace("-", "").strip()
        if normalized:
            return False
        if "-" not in cell:
            return False
    return True


def _split_table_row(line: str) -> list[str]:
    return [_strip_table_cell(cell) for cell in line.strip().strip("|").split("|")]


def _is_markdown_table(block: str) -> bool:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    return (
        len(lines) >= 2
        and lines[0].startswith("|")
        and "|" in lines[0][1:]
        and _is_table_separator(lines[1])
    )


def _table_blocks_from_markdown(block: str) -> list[dict[str, Any]]:
    """Render compact markdown tables as centered card rows.

    飞书原生 lark_md 表格在卡片里经常字号和对齐不可控。这里把 2-4 列的
    常见表格转成 column_set，每个单元格显式居中；更宽的表格回退原生渲染，
    避免窄屏挤坏。
    """
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    header = _split_table_row(lines[0])
    rows = [_split_table_row(line) for line in lines[2:]]
    column_count = len(header)
    if column_count < 2 or column_count > 4:
        return [
            {
                "tag": "markdown",
                "content": block,
                "text_size": "normal",
                "text_align": "center",
            }
        ]

    normalized_rows = [header]
    for row in rows:
        padded = (row + [""] * column_count)[:column_count]
        normalized_rows.append(padded)

    elements: list[dict[str, Any]] = []
    for row_index, row in enumerate(normalized_rows):
        columns = []
        for cell in row:
            content = f"**{cell}**" if row_index == 0 else cell
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": _safe_md(content, 300),
                            "text_size": "notation" if row_index == 0 else "normal",
                            "text_align": "center",
                        }
                    ],
                }
            )
        elements.append({"tag": "column_set", "columns": columns})
        if row_index == 0:
            elements.append({"tag": "hr"})
    return elements


def _md_blocks_from_text(text: str, max_chars: int = 6000) -> list[dict[str, Any]]:
    """把 LLM 输出的 markdown 智能拆段，按段类型选 飞书 markdown element 字号。

    映射规则：
        # 标题       → text_size="heading_1" (22px 加粗)
        ## 标题      → text_size="heading_2" (18px 加粗)
        ###/#### 标题 → text_size="heading_3" (16px 加粗)
        > 引用       → normal 正文卡片化展示
        其他段落     → text_size="normal"    (14px 正常)
        ─────────   → 分隔线 hr
        表格 |..|    → 2-4 列转居中 column_set，宽表回退居中 markdown

    Returns:
        飞书 elements 数组
    """
    if not text:
        return []
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n_... [已截断，共 {len(text)} 字符]_"

    elements: list[dict[str, Any]] = []
    # 按双换行（段落）分段
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]

    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue

        # 分隔线
        if block in ("---", "***", "___") or block.startswith("──"):
            elements.append({"tag": "hr"})
            continue

        # 表格：优先转成显式居中的卡片列，避免飞书原生表格字号漂移
        if _is_markdown_table(block):
            elements.extend(_table_blocks_from_markdown(block))
            continue

        # 标题处理：只吃第一行作为标题，剩余正文作为独立段落
        # （之前 bug：整段被 wrap **xxx** 导致 markdown 渲染挂 + 字号失效）
        stripped = block.lstrip()
        heading_map = [
            ("# ", 2, "heading_1"),  # 22px 文档级标题
            ("## ", 3, "heading_2"),  # 18px 章节
            ("### ", 4, "heading_3"),  # 16px 小节
            ("#### ", 5, "heading_3"),  # 16px 四级
        ]
        matched_heading = False
        for prefix, skip, size in heading_map:
            if stripped.startswith(prefix):
                # 只吃第一行
                first_line, _, rest = stripped.partition("\n")
                title_text = first_line[skip:].strip()
                if title_text:
                    elements.append(
                        {
                            "tag": "markdown",
                            "content": title_text,  # 不再 wrap **，避免跨行 markdown 解析挂
                            "text_size": size,
                        }
                    )
                # 剩余内容（列表、段落等）按 normal 字号渲染为独立段落
                if rest.strip():
                    elements.append(
                        {
                            "tag": "markdown",
                            "content": rest.strip(),
                            "text_size": "normal",
                        }
                    )
                matched_heading = True
                break
        if matched_heading:
            continue

        # 引用（> 开头）→ normal 字号（不再小灰，避免老板看不清）
        if stripped.startswith("> "):
            quote_text = "\n".join(
                line.lstrip("> ").strip() for line in block.split("\n")
            )
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"💬 {quote_text}",
                    "text_size": "normal",
                }
            )
            continue

        # 普通段落 / 列表 / 行内加粗 → markdown normal 字号 (14px 舒适正文)
        elements.append(
            {
                "tag": "markdown",
                "content": block,
                "text_size": "normal",
            }
        )

    return elements


def progress_bar(elapsed_sec: float, estimated_sec: float = 180) -> str:
    """生成等待进度行：进度条 + 百分比 + 已等待时间。

    百分比基于估算时长，只表达“时间进展感”，运行中最多到 95%，避免
    误导用户以为已经精确完成。
    """
    safe_estimated = max(float(estimated_sec or 180), 1.0)
    percent = min(int(max(elapsed_sec, 0) / safe_estimated * 100), 95)
    filled = max(1, min(5, int(percent / 20) + (1 if percent % 20 else 0)))
    bar = "▰" * filled + "▱" * (5 - filled)
    return f"{bar} {percent}%"


def smart_thinking_hint(elapsed_sec: float) -> str:
    """根据已耗时返回阶段提示文字（防止用户以为机器人挂了）。"""
    if elapsed_sec < 3:
        return "任务思考中"
    if elapsed_sec < 8:
        return "任务分析中"
    if elapsed_sec < 20:
        return "任务推理中（这个问题稍复杂）"
    if elapsed_sec < 60:
        return "任务深度处理中"
    if elapsed_sec < 180:
        return "长任务处理中"
    return "超长任务处理中"


# ─────────────────────────── 1. 进度卡片（运行中）───────────────────────────


def build_progress_card(
    *,
    title: str,
    brief: str,
    model: str = "",  # 保留兼容但不再显示（用户觉得 GPT-5.5 xhigh 这种太技术）
    elapsed_sec: float = 0,
    estimated_sec: float = 180,  # 保留兼容，progress_bar 不再用
    current_stage: str | None = None,
    iterations: int | None = None,
    max_iterations: int = 60,
    reasoning_tier: str | None = None,  # 'medium' / 'high' / 'xhigh'
    queue_position: int | None = None,
    eta_text: str | None = None,
) -> dict[str, Any]:
    """长任务运行中的进度卡片（自适应进度条 + 推理级别友好显示）。"""
    bar = progress_bar(elapsed_sec, estimated_sec)
    hint = smart_thinking_hint(elapsed_sec)
    progress_line = (
        f"{waiting_pulse(elapsed_sec)} {_font(hint, WARNING_RED)} · "
        f"{_font('任务进度', 'grey')} **{bar}** · "
        f"{_font('⏱️已等待 ' + _format_elapsed(elapsed_sec), 'grey')}"
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**{_safe_md(title, 80)}**",
            "text_size": "heading_2",
        },
        _md(_muted(_safe_md(brief, 200)), "normal"),
        _md(_status_dot("等待卡已启动，后台持续刷新进度"), "notation"),
    ]

    # 推理级别 + 预估时间（用户友好，不显示底层 model 名）
    tier_display = (
        REASONING_TIER_DISPLAY.get(reasoning_tier) if reasoning_tier else None
    )
    if tier_display:
        icon, tier_name, eta = tier_display
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{tier_name}** · {_muted('参考耗时 ' + eta)}",
                "text_size": "notation",
            }
        )

    if queue_position is not None or eta_text:
        queue_bits = []
        if queue_position is not None:
            queue_bits.append(f"前面还有 {max(queue_position, 0)} 个任务")
        if eta_text:
            queue_bits.append(eta_text.rstrip("。"))
        elements.append(
            {
                "tag": "markdown",
                "content": "**排队状态**：" + _muted(" · ".join(queue_bits)),
                "text_size": "normal",
            }
        )

    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": progress_line,
                "text_size": "heading_3",
            },
        ]
    )

    if iterations is not None:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**推理步骤**：{iterations}/{max_iterations}",
                },
            }
        )

    if current_stage:
        elements.append(
            _activity_line("Running", _safe_md(current_stage, 200), WARNING_RED)
        )

    elements.append(
        {
            "tag": "markdown",
            "content": _muted("结果出来会自动更新这张卡片。"),
            "text_size": "notation",
        }
    )

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🚀 {title}"},
            "template": "indigo",
        },
        "body": {"elements": elements},
    }


def build_antigravity_queue_card(
    *,
    job_id: str,
    queue_position: int,
    eta_text: str,
    elapsed_sec: float = 0,
    primary_channel_name: str = "巅巅小助手",
    fallback_channel_name: str = "池池小助手",
    fallback_provider_id: str = "aihubmix/gemini-3-flash-preview",
    original_prompt: str = "",
    status: str = "queued",
) -> dict[str, Any]:
    """Antigravity 短排队卡：真实队列位置 + 备用小助手入口。

    provider_id 只放在按钮 value 里，员工可见文案不展示底层技术词。
    """
    is_fallback_running = status == "fallback_running"
    title = f"{fallback_channel_name}处理中" if is_fallback_running else "小助手排队中"
    status_line = (
        _status_dot(f"{fallback_channel_name}已接手处理", SUCCESS_GREEN)
        if is_fallback_running
        else _status_dot("已保留排队位置，等待卡会持续刷新", WARNING_RED)
    )
    progress_line = (
        f"{waiting_pulse(elapsed_sec)} {_font('等待中', WARNING_RED)} · "
        f"**{progress_bar(elapsed_sec, 120)}** · "
        f"{_font('⏱️已等待 ' + _format_elapsed(elapsed_sec), 'grey')}"
    )
    current_channel = (
        fallback_channel_name if is_fallback_running else primary_channel_name
    )
    elements: list[dict[str, Any]] = [
        _md(
            "哎呀，现在小助手太忙啦，当前同时接待人数已经超过 9 人。\n"
            "我已经帮您排好队了，请您耐心等待⌛️",
            "normal",
        ),
        _md(status_line, "notation"),
        _md(
            _aligned_fields(
                [
                    ("当前位置", f"第 {max(int(queue_position or 1), 1)} 位"),
                    ("预计等待", eta_text or "稍后"),
                    ("当前通道", current_channel),
                ]
            )
        ),
        {"tag": "hr"},
        _md(progress_line, "heading_3"),
    ]
    if is_fallback_running:
        elements.append(_md(_muted("好的，我马上请池池小助手先帮您处理～"), "notation"))
    else:
        elements.extend(
            [
                _md(_muted("您也可以不排队，先让池池小助手处理。"), "notation"),
                _button_row(
                    [
                        {
                            "text": f"不想排队，使用{fallback_channel_name}",
                            "type": "primary",
                            "value": {
                                "source": "antigravity_queue_card",
                                "action": "use_fallback",
                                "job_id": job_id,
                                "fallback_provider_id": fallback_provider_id,
                                "fallback_channel_name": fallback_channel_name,
                                "original_prompt": original_prompt,
                            },
                        }
                    ]
                ),
            ]
        )
    return _business_card(
        title=title,
        template="green" if is_fallback_running else "orange",
        elements=elements,
    )


# ─────────────────────────── 2. 最终成功卡片 ───────────────────────────


def build_final_card(
    *,
    title: str,
    result_md: str,
    elapsed_sec: float = 0,
    model: str = "",  # 保留兼容但不再显示
    reasoning_tier: str | None = None,
) -> dict[str, Any]:
    """长任务完成时的结果卡片（替换进度卡片）。

    内容用智能字号分段：H1/H2/H3/正文/notation 多档自动识别。
    """
    m, s = divmod(int(elapsed_sec), 60)
    time_str = f"{m} 分 {s} 秒" if m else f"{s} 秒"

    # footer 信息：推理级别（用户友好）+ 耗时
    tier_display = (
        REASONING_TIER_DISPLAY.get(reasoning_tier) if reasoning_tier else None
    )
    if tier_display:
        icon, tier_name, _eta = tier_display
        footer = f"{icon} {tier_name} · 实际耗时 {time_str}"
    else:
        footer = f"⏱️ 耗时 {time_str}"

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": footer,
            "text_size": "notation",
        },
        {"tag": "hr"},
    ]
    # 智能字号分段
    elements.extend(_md_blocks_from_text(result_md))

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ {title}"},
            "template": "green",
        },
        "body": {"elements": elements},
    }


# ─────────────────────────── 3. 失败卡片 ───────────────────────────


def build_error_card(
    *,
    title: str,
    error_msg: str = "",
    elapsed_sec: float = 0,
    retry_hint: str = "",
    error_message: str | None = None,
) -> dict[str, Any]:
    """任务失败时的卡片。"""
    error_text = error_msg or error_message or "未知错误"
    m, s = divmod(int(elapsed_sec), 60)
    time_str = f"{m} 分 {s} 秒" if m else f"{s} 秒"

    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**任务失败前耗时**：{time_str}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": _safe_md(error_text, 2000),
            "text_size": "normal",
        },
    ]
    if retry_hint:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "markdown",
                "content": f"💡 {retry_hint}",
                "text_size": "notation",
            }
        )

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"❌ {title}"},
            "template": "red",
        },
        "body": {"elements": elements},
    }


# ─────────────────────────── 4.5 思考中占位卡片（短任务专用）───────────────────────────


# 推理级别 → 用户友好展示（图标 + 中文名 + 预估时间）
REASONING_TIER_DISPLAY = {
    "medium": ("⚡", "中等推理", "30秒-1.5分钟"),
    "high": ("🧠", "高级推理", "1.5-3分钟"),
    "xhigh": ("🚀", "超深推理", "3-8分钟"),
}


def _resolve_tier_from_model(model_hint: str) -> tuple[str, str, str] | None:
    """从 model_hint（如 codex/gpt-5.5-high）反查推理级别展示。"""
    if "xhigh" in model_hint:
        return REASONING_TIER_DISPLAY["xhigh"]
    if "high" in model_hint:
        return REASONING_TIER_DISPLAY["high"]
    if "medium" in model_hint:
        return REASONING_TIER_DISPLAY["medium"]
    return None


def build_thinking_card(
    *,
    user_msg: str = "",
    model_hint: str = "巅池-Agent小助手",
    elapsed_sec: float = 0,
    reasoning_tier: str | None = None,  # 'medium' / 'high' / 'xhigh'
) -> dict[str, Any]:
    """日常 LLM 调用前发的占位卡片。

    统一复用工程级等待卡版式，避免真实链路和灰度样卡出现两套视觉。
    """
    tier = reasoning_tier
    if not tier:
        resolved = _resolve_tier_from_model(model_hint)
        if resolved:
            for candidate, display in REASONING_TIER_DISPLAY.items():
                if display == resolved:
                    tier = candidate
                    break
    brief = user_msg.strip() if user_msg else "正在处理你的请求"
    return build_progress_card(
        title="任务推理中",
        brief=brief,
        elapsed_sec=elapsed_sec,
        reasoning_tier=tier,
        current_stage=smart_thinking_hint(elapsed_sec),
    )


# ─────────────────────────── 4. 日常渲染卡片（级别 3）───────────────────────────


# ─────────────────────────── 5. Onboarding 卡片（按钮交互）───────────────────────────


def _button_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """组装一行按钮组（V2 schema 用 column_set 横排）。

    每个 button: {"text": str, "value": dict, "type": "primary"|"default"}
    """
    if not buttons:
        return {"tag": "markdown", "content": ""}

    def button_element(button: dict[str, Any]) -> dict[str, Any]:
        element = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": button["text"]},
            "type": button.get("type", "default"),
            "value": button["value"],
        }
        # Feishu buttons otherwise shrink to text width, which makes quiz
        # choices and paired actions look scattered. Fill keeps each column
        # visually balanced while the column weights control row rhythm.
        element["width"] = button.get("width", "fill")
        return element

    def spacer_column(weight: int = 1) -> dict[str, Any]:
        return {
            "tag": "column",
            "width": "weighted",
            "weight": weight,
            "elements": [{"tag": "markdown", "content": " ", "text_size": "notation"}],
        }

    # Center one/two buttons instead of letting them cling to the row edges.
    columns: list[dict[str, Any]] = []
    if len(buttons) == 1:
        columns.append(spacer_column(1))
    elif len(buttons) == 2:
        columns.append(spacer_column(1))

    for button in buttons:
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 2 if len(buttons) <= 2 else 1,
                "elements": [button_element(button)],
            }
        )

    if len(buttons) == 1:
        columns.append(spacer_column(1))
    elif len(buttons) == 2:
        columns.append(spacer_column(1))

    return {"tag": "column_set", "columns": columns}


def build_onboarding_dept_card(*, welcome_name: str = "") -> dict[str, Any]:
    """新员工首次接入推的第一张卡：欢迎 + 选择部门。"""
    greet = (
        f"欢迎你，**{welcome_name}**！"
        if welcome_name
        else "欢迎来到巅池-Agent 小助手！"
    )
    # 2026-05-22 按职能部组织架构调整为 7 部门。
    # ID 跟 dc_engines.department_workflows.defaults 对齐，方便后续部门工作流匹配
    departments = [
        ("总经办", "executive_office"),
        ("客户部", "client_dept"),
        ("策划", "planning"),
        ("品宣部", "brand_publicity"),
        ("执行运营", "execution_ops"),
        ("综合部", "general_affairs"),
        ("财务部", "finance"),
    ]
    # 飞书 action element 一行最多 4 个 button，7 个部门分为 4 + 3。
    row1 = [
        {
            "text": name,
            "value": {"action": "select_dept", "dept": code},
            "type": "primary",
        }
        for name, code in departments[:4]
    ]
    row2 = [
        {
            "text": name,
            "value": {"action": "select_dept", "dept": code},
            "type": "primary",
        }
        for name, code in departments[4:]
    ]
    return _business_card(
        title="入职引导 · 第 1 步",
        template="blue",
        elements=[
            _kami_lede(greet, "先完成身份登记，再进入短教程和答题。"),
            _md(_status_dot("等待选择部门", KAMI_INK), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("步骤", "1 / 3"),
                        ("状态", _ink("入职登记中")),
                        ("下一步", "选择所在部门"),
                    ]
                )
            ),
            {"tag": "hr"},
            _md("请选择你所在的部门。后续教程和测试题会按部门补充 1 道差异化题。"),
            _button_row(row1),
            _button_row(row2),
            _md(
                _muted("点错了不要紧，流程走完后随时发 `/重新入职` 可以从头重做。"),
                "notation",
            ),
        ],
    )


def build_onboarding_role_card(*, dept_name: str) -> dict[str, Any]:
    """选完部门后推第 2 张卡：选择角色。"""
    roles = [
        ("总监", "director"),
        ("经理", "manager"),
        ("专员", "specialist"),
        ("实习生", "intern"),
    ]
    buttons = [
        {
            "text": name,
            "value": {"action": "select_role", "role": code},
            "type": "primary",
        }
        for name, code in roles
    ]
    return _business_card(
        title="入职引导 · 第 2 步",
        template="blue",
        elements=[
            _kami_lede(f"{dept_name} 同事", "部门已记录，继续选择你的角色。"),
            _md(_status_dot("部门已记录", KAMI_INK), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("步骤", "2 / 3"),
                        ("部门", _ink(dept_name)),
                        ("下一步", "选择角色 / 职位"),
                    ]
                )
            ),
            {"tag": "hr"},
            _button_row(buttons),
        ],
    )


def build_onboarding_name_prompt_card(*, role_name: str) -> dict[str, Any]:
    """选完角色后推第 3 张卡：提示输入姓名。"""
    return _business_card(
        title="入职引导 · 第 3 步",
        template="blue",
        elements=[
            _kami_lede(
                f"{role_name}，最后一步", "留下真实姓名，后续我会用正式称呼与你协作。"
            ),
            _md(_status_dot("角色已记录", KAMI_INK), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("步骤", "3 / 3"),
                        ("角色", _ink(role_name)),
                        ("下一步", "输入真实姓名"),
                    ]
                )
            ),
            {"tag": "hr"},
            _md(
                "请直接发一条消息告诉我你的**真实姓名**（中文即可）。"
                "我只会把它用于系统称呼、培训记录和工作协作。"
            ),
            _md(_muted("例：直接发「您的姓名」"), "notation"),
        ],
    )


LESSON_SEQUENCE = [
    (1, "🎯 推理级别 · 让小助手「想深一点」", "lesson_reasoning"),
    (2, "📸 让小助手「看懂」图 / 视频 / 音频", "lesson_multimodal"),
    (3, "📥 真实性 · 上传资料让助手基于真实工作", "lesson_truth"),
    (4, "🎨 生图 · 一句话画对，失败自动备用", "lesson_image"),
    (5, "⚡ 日常 5 个高频场景速查", "lesson_daily"),
    (6, "🧩 你的部门常用 · 4 个场景", "lesson_dept_common"),
]


def build_onboarding_tutorial_list_card(
    *,
    display_name: str,
    dept_code: str | None = None,
) -> dict[str, Any]:
    """身份完成后推教程清单卡（5 节 + 申请测试）。

    dept_code 决定底部「测试 N 题」的题数提示：
    - None / 未识别 → 通用 5 题
    - 已知部门 → 5 通用 + 1 部门差异化 = 6 题
    """
    lesson_count = len(LESSON_SEQUENCE)
    quiz_total = len(get_quiz_for_dept(dept_code))
    lesson_buttons = [
        {
            "text": f"第 {num} 节",
            "value": {"action": "open_lesson", "lesson_id": code},
            "type": "default",
        }
        for num, _title, code in LESSON_SEQUENCE
    ]
    # 拆 2 行（每行 3 个）
    row1 = lesson_buttons[:3]
    row2 = lesson_buttons[3:]

    lesson_list_md = _field_list(
        [f"**第 {num} 节**：{title}" for num, title, _code in LESSON_SEQUENCE]
    )

    return _business_card(
        title="你的学习清单",
        template="blue",
        elements=[
            _kami_lede(
                f"{display_name}，登记完成", "下面是正式使用前需要完成的短教程。"
            ),
            _md(_status_dot("入职登记已完成", KAMI_INK), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("教程", f"{lesson_count} 节短教程"),
                        ("测试", f"{quiz_total} 题"),
                        ("规则", "全对通过"),
                    ]
                )
            ),
            _md(
                "每节教程大约 1-2 分钟。"
                "**测试通过后**，你会被邀请加入「DC-Agent · 内测群」开始正式协作。"
            ),
            {"tag": "hr"},
            _md(f"**教程目录**\n{lesson_list_md}"),
            _button_row(row1),
            _button_row(row2),
            {"tag": "hr"},
            _button_row(
                [
                    {
                        "text": "我学完了，开始测试",
                        "value": {"action": "start_quiz"},
                        "type": "primary",
                    }
                ]
            ),
            _md(
                _muted(f"测试 {quiz_total} 题 · 全对通过 · 答错可看完教程后重做"),
                "notation",
            ),
        ],
    )


# 6 节教程内容（5 节 universal + 1 节 dept-specific 占位）
# 注意：`lesson_dept_common` 的 body 由 get_dept_lesson_body(dept_code) 运行时生成
TUTORIALS: dict[str, dict[str, str]] = {
    "lesson_reasoning": {
        "title": "🎯 推理级别 · 让小助手「想深一点」",
        "body": """## 三档推理（自然语言切换）

| 前缀 | 级别 | 适合场景 | 耗时 |
|------|------|---------|------|
| 默认 | ⚡ 中等推理 | 闲聊、查询、简短分析 | 30s-1.5min |
| `#高` | 🧠 高级推理 | 多角度对比、战略思考 | 1-3min |
| `#超深` | 🚀 超深推理 | 完整方案、深度报告 | 3-8min |

### ✅ 对的用法
- 「**#超深** 分析端午活动方案，给出 3 个执行路径」
- 「**#高** 对比一下我们和五菱的端午营销策略」

### ❌ 错的用法
- 「分析端午活动方案」（默认中等，深度不够）
- 一句话就要"完整方案"（材料和深度都不够）

💡 记不住前缀也没关系：复杂问题直接说「**深度分析…**」/「**详细对比…**」，小助手会自动升级到高/超深。
""",
    },
    "lesson_multimodal": {
        "title": "📸 让小助手「看懂」图 / 视频 / 音频",
        "body": """## 三种素材，三种用法

| 素材 | 怎么用 | 典型场景 |
|------|-------|---------|
| 🖼 图片 | 直接发图 + 一句指令 | 产品图识别 / 海报抠点 / 截图问内容 |
| 🎬 视频 | 发视频文件 + 说要什么 | 口播提取 / 关键画面 / 视频总结 |
| 🔊 音频 | 发音频 + 说目标 | 会议录音转纪要 / 语音备忘整理 |

### ✅ 对的用法
- 发一张竞品海报，问：「**这张海报主打的卖点是什么？**」
- 发一段巡店视频：「**这视频里出现的产品有哪些？哪些画面有口播？**」
- 发一段会议录音：「**帮我提取里面的决议项**」

### ❌ 错的用法
- 同时发 5 个视频刷屏（**视频处理有日限额**，按需上传）
- 发图问「这是真的还是假的」（小助手不判真伪，只描述看到的）
- 把识别结果直接当公司结论（要看是「看到的」还是「猜的」，详见下一节）

### 限额提醒
视频/音频处理消耗资源大，**单人每日有限额**。如果排队了，小助手会给你一张排队卡告诉你位置和预计等待时间。**同样内容只上传一次**，重复传不会得到不同结果。
""",
    },
    "lesson_truth": {
        "title": "📥 真实性 · 上传资料让助手基于真实工作",
        "body": """## 给资料 = 给真实性

小助手**不会凭空编公司事实**。要让它给你真实可用的输出，必须给它**真实可核验的资料**。

### 三种给资料方式

| 方式 | 怎么做 | 适合 |
|------|-------|------|
| 📎 直接上传文件 | 发附件给小助手 | 一次性的素材、截图、音视频 |
| 🔗 飞书知识库 / 云文档 | 把链接发过来 | 公司知识库里已有的文档 |
| 🗂 公司文件库（NAS） | 文件放进公司文件库，助手自动检索 | 需要反复引用的稳定资料 |

### 三档「真实性」状态

| 标签 | 含义 |
|------|------|
| ✅ 已核验 | 基于你给的真实资料生成，可放心用 |
| ⚠️ 部分待确认 | 部分基于资料，部分需要你补充 |
| 🔵 需要补充 | 没给资料，无法基于事实生成，请补材料 |
| 🟣 示例/假设 | 你明确要求模板/示例，非真实结论 |

### ✅ 对的用法
- 写客户触达话术前，**先把客户上次的沟通记录发过来**
- 做竞品分析前，**先把竞品资料发上来**（不然会标"需要补充"）
- 跟小助手说「**这是我们公司的活动方案**」→ 它会把你给的当事实
- 生成文案/生图/视频后，**先确认来源、风险和审查清单，再外发**

### ❌ 错的用法
- 直接问「我们公司端午活动方案是什么？」（**没给资料 = 让它瞎编**）
- 用「猜测的内容」当作公司结论
- 把不确定的信息塞进长期记忆（错的记忆会污染后续所有任务）
- 资料不足时催它“先写一版正式稿”（会污染后续判断）

💡 **关键认知**：资料不足先补资料；确认后再外发；生成错了按记录回滚，不要继续沿用错稿。
""",
    },
    "lesson_image": {
        "title": "🎨 生图 · 一句话画对，失败自动备用",
        "body": """## 默认 GPT Image 2，中英文都能画

### 画质 · 用自然语言切换

| 你说的话 | 出图时间 | 适合 |
|---------|---------|------|
| 「**快速**画张草图」 | ~15s | 想法验证、初稿试错 |
| 「画一张…」（默认） | ~40s | 日常用图 |
| 「**精致**画…」/「**高清**…」 | ~2min | 海报、宣传图、客户用 |

### 画幅

| 你说的话 | 尺寸 | 适合 |
|---------|------|------|
| 默认（不说） | 1536×1024 横版 | 朋友圈横图、海报 |
| 「**方版**」/「1024×1024」/「小红书尺寸」 | 1024×1024 | 小红书、公众号头图 |
| 「**竖版**」 | 1024×1536 | 手机壁纸、抖音封面 |

### 失败自动备用
GPT Image 2 调用失败 / 排队超时时，**小助手会自动切到 Dreamina 即梦再画一次**，你不用手动重试也不用记切换指令。

如果你想试 Dreamina 的另一种风格，直接说：
- 「用 **dreamina** 画一张端午海报」

### ✅ 对的用法
- 「画一张**方版**的端午海报，主体是粽子和五菱缤果，**高清**」
- 「**快速**试一张创意草图」（验证想法）

### ❌ 错的用法
- 「画图」（没说画什么、什么尺寸、什么质量）
- 失败就重试很多次（**自动备用**，等着就行）
""",
    },
    "lesson_daily": {
        "title": "⚡ 日常 5 个高频场景速查",
        "body": """## 5 个员工每天都用的场景

### 1. 写文案
> 帮我写一段**端午节客户微信问候话术**，要正式但有温度

### 2. 列方案
> 列下**端午节我们公司可以做的 5 个营销动作**

### 3. 分析对比（用 `#高` 升级）
> **#高** 对比下我们和五菱的端午营销策略

### 4. 总结文档（自动读飞书知识库 / 上传文件）
> [飞书知识库链接] 帮我总结要点
> 或：发一段会议录音，「帮我提取决议项」

### 5. 深度战略（用 `#超深` 升级）
> **#超深** 我们公司端午节适合做什么内部活动方案，要含执行路径

### 通用提示
- **回复格式**：超过 80 字会自动用结构化卡片渲染，标题/表格/列表都漂亮
- **想看能做什么**：直接发「**你能帮我做什么**」
- **想看长期记忆**：发「**我们之前聊过什么**」

💡 你不会的指令，可以**直接用人话问小助手**：「我想…，应该怎么说？」
""",
    },
    "lesson_dept_common": {
        # 占位 title，实际渲染时会被部门名替换（如 "🧩 你的部门常用 · 市场"）
        "title": "🧩 你的部门常用 · 4 个场景",
        # body 由 get_dept_lesson_body(dept_code) 运行时生成
        "body": "",
    },
}


# ─────────────────────────── 5.1 部门差异化教程内容 ───────────────────────────
# 7 部门各 4 个常用场景。dept_code 缺失 / 未知 → 返回通用兜底文案。

_DEPT_LESSON_BODIES: dict[str, dict[str, str]] = {
    "executive_office": {
        "name": "总经办",
        "body": """## 总经办 · 4 个常用场景

### 1. 🗒 会议纪要整理
**录音转文字稿后** + `#高`，输出议题 / 决议 / Owner / 时间节点 的结构化纪要。
**关键**：小助手不直接处理音频文字，**先发录音让助手转文字稿**，再让它结构化。

> 「**#高** 这是会议录音的文字稿，帮我整理成纪要（议题 / 决议 / Owner / 时间）」

### 2. 📌 决议跟进
基于上次会议的纪要，**列出本周要催的事项**，按紧急度排序，含催的话术草稿。

> 「**#高** 这是上次的会议纪要，列下本周要催的事项 + Owner + 催的话术，按紧急度排序」

### 3. 📨 跨部门通知起草
给客户部 / 品宣部 / 执行运营起草正式通知邮件，含**活动时间 / 物料要求 / 提交截止**。

> 「帮我起草一份给客户部+品宣部的端午活动通知，含时间 / 物料要求 / 6 月 14 日截止」

### 4. 📊 月度运营简报（给老板看的核心产出）
**汇总各部门月度数据 + `#超深`**，输出执行摘要 / 关键数据 / 异常分析 / 下月计划。
**关键**：先把客户部、品宣部、执行运营三个部门的月度数据上传，越完整越准。

> 「**#超深** 这是 5 月份客户部/品宣部/执行运营的运营数据，帮我汇总成给老板的月度简报（亮点 / 风险 / 下月计划）」
""",
    },
    "client_dept": {
        "name": "客户部",
        "body": """## 客户部 · 4 个常用场景

### 1. 💬 客户触达话术
提供**受众身份 + 节日场景 + 业务亮点 + 触达渠道**，让小助手定向起草微信/私域/飞书消息话术。公司默认不使用客户邮件，除非你明确要求邮件格式。

> 「帮我起草端午节 VIP 老客户微信问候话术，带礼盒预告 + 试驾邀约入口」

### 2. 📞 客户邀约话术（试驾 / 到店 / 活动）
按**客户分层 + 邀约场景**生成话术，含开场白 / 主推卖点 / 异议处理 / 收尾。
**关键**：告诉小助手客户是哪类（新客 / 留资未成交 / 已购车），话术差别很大。

> 「帮我写一段试驾邀约话术，目标是 520 留资但没到店的客户，主推端午到店礼包」

### 3. 🔁 客户回访话术
针对特定客户分层整理回访话术（节日问候、活动通知、售后关怀）。

> 「帮我整理一段端午回访话术，针对去年端午买车的老客户，重点是售后关怀 + 新车试驾邀约」

### 4. 🎁 节日营销文案（多版本 + 配图建议）
**多渠道多版本**生成（朋友圈 / 微信群 / 公众号摘要），配生图建议。

> 「**#高** 端午节朋友圈推送，帮我写 3 版文案（情绪向 / 利益向 / 故事向）+ 配图建议」
""",
    },
    "brand_publicity": {
        "name": "品宣部",
        "body": """## 品宣部 · 4 个常用场景

### 1. 🎨 种草海报方案
**`#高` 出 3 个创意方向 + 文案备选**，选定后再生图（先方向后视觉）。
**关键**：直接说"画一张海报"出来的是猜的，要先沉淀方向再生图。

> 「**#高** 端午海报，产品=缤果，目标=年轻女性，出 3 个创意方向 + 文案备选」

### 2. 📔 小红书种草图文
**方版生图 + 200 字带情绪的种草文案**，组合输出，可直接发。

> 「画一张**方版**的端午种草图，配 200 字小红书文案，要带情绪 + 痛点 + 钩子」

### 3. 🎬 视频内容打标 / 摘要（用第 2 节看视频的能力）
把视频丢给小助手，**提取主题 / 卖点 / 适合的投放渠道**。

> （发视频）「这条视频的核心卖点是什么？哪些画面有口播？适合投在抖音/小红书/视频号哪个渠道？」

### 4. 📰 公众号推文起草（800-1500 字长文）
**`#高` + 先列好提纲（目标读者 / 阅读情绪 / 结尾引导）**，输出有节奏的长文（开头钩子 / 主体 3 段 / 结尾引导转化）。
**关键**：长文要有"读者旅程感"，告诉小助手目标读者是谁、希望他看完什么感觉、最后做什么动作。

> 「**#高** 帮我起草一篇公众号长文，主题=端午自驾游+缤果，目标读者=30-40 岁带娃家庭，1200 字左右，要有开头钩子和结尾试驾邀约」
""",
    },
    "planning": {
        "name": "策划",
        "body": """## 策划 · 4 个常用场景

### 1. 🔍 竞品洞察简报
**竞品材料 + `#超深`**，输出证据链 / 机会点 / 行动建议。

> 「**#超深** 这是 3 份竞品端午方案，提炼共同信号 + 我们的差异化机会点 + 3 条行动建议」

### 2. 📈 市场趋势分析
上传几篇行业报告，`#超深` 提炼共同信号 + 对我们的影响。

> 「**#超深** 这 5 份行业报告里有哪些共同的端午消费趋势？对我们五菱的代理业务有什么影响？」

### 3. 🛡 方案评审 / 风险扫描
帮别人写的方案**找潜在风险**，列优先级。

> 「**#高** 这份方案有哪些潜在风险？给我 3 个最值得警惕的 + 应对建议」

### 4. 📝 应标方案 / 客户提案起草（核心产出 ⭐）
**应标 / 客户提案是策划最大产值的活**。先**喂背景资料**（客户需求简介 / 我们的历史合作案例 / 公司能力清单），用 `#超深` 输出有结构的方案（洞察 / 策略 / 创意 / 执行 / 预算 / 团队）。
**关键**：方案的灵魂是**洞察**（看见客户没看见的痛点），不是套模板。给的资料越深，洞察越准。

> 「**#超深** 客户=XXX 4S 店集团，需求简介=618 季度营销代理招标，我们的优势=端午案例 + 内容矩阵能力。帮我起草应标方案（洞察 / 策略 / 创意 / 执行排期 / 预算 / 团队配置）」
""",
    },
    "execution_ops": {
        "name": "执行运营",
        "body": """## 执行运营 · 4 个常用场景

### 1. 🗓 活动排期
**任务清单 + 时间窗 + 负责人 + `#高`**，拆出每日动作 + 风险预案。

> 「**#高** 端午活动 6 月 17-19 日，这是任务清单和负责人，拆每日动作 + 关键里程碑 + 风险预案」

### 2. 📦 活动物料清单 / 检查表（事前必做）
按活动类型 **生成物料清单 + 检查项 + 责任人 + 截止时间**，避免现场缺东西。
**关键**：先告诉小助手活动类型（线下 vs 线上 vs 混合）+ 规模，物料差别很大。

> 「**#高** 端午到店活动（线下，预估 50 人），帮我列物料清单 + 检查表（含每项的责任人和截止时间）」

### 3. 🔔 催进度话术
跟同事 / 供应商催进度，**温和不伤关系**。

> 「帮我起草催进度话术，对象=设计师，事=端午海报 V3 拖了 2 天，要温和但要明确截止」

### 4. 📷 现场问题诊断（用第 2 节看现场照片的能力）
把现场照片 / 视频丢给小助手，**问问题在哪 + 怎么改**。

> （发现场照片）「这个布展有什么问题？哪些地方可以改进？按照视觉冲击力 / 客户动线 / 拍照效果 三个维度说」
""",
    },
    "general_affairs": {
        "name": "综合部",
        "body": """## 综合部 · 4 个常用场景

### 1. 🧾 行政通知整理
把行政安排、办公规则或内部流程整理成**清晰通知 + 执行要点 + FAQ**。
**关键**：制度类内容要提供原文，小助手只做结构化和表达优化。

> 「这是办公用品领用规则，帮我整理成一份内部通知，含申请入口 / 审批人 / 注意事项」

### 2. 🧩 跨部门事项协调
把多个部门反馈整理成待办清单，标出 Owner / 截止时间 / 风险。

> 「**#高** 这是各部门对团建安排的反馈，帮我整理成待确认事项 + 负责人 + 下一步」

### 3. 📚 资料归档说明
把活动资料、合同附件或会议材料整理成归档目录和命名规则。

> 「帮我把这些活动资料整理成归档清单，按合同 / 物料 / 照片 / 复盘分类，并给命名规则」

### 4. 🛎 内部服务答疑
把高频内部问题整理成标准回复，减少重复沟通。

> 「这是员工常问的 8 个行政问题，帮我整理成标准回复，语气礼貌但明确」
""",
    },
    "finance": {
        "name": "财务部",
        "body": """## 财务部 · 4 个常用场景

### 1. 🧾 发票 / 报销复核
**发票信息整理成文本** → 让助手按公司报销规则 + 缺项检查清单逐条复核。
**关键**：小助手 **不判真伪**（那是原件审核 + 人工的活），只做规则匹配 + 缺项检查。

> 「这是 5 张报销发票的信息，按公司报销规则帮我检查缺项 / 合规问题 / 金额阈值」

### 2. 📊 预算报表说明
把**月度数据写成简要说明**，给老板看，重点突出超支 / 异常。

> 「帮我把这份月度预算数据写成 200 字简要说明，重点=超支项 / 异常波动 / 下月预警」

### 3. ⚖️ 合规风险提示
看**合同 / 协议**找可能的财务合规问题，按重要度排序。

> 「**#高** 这份合同里有哪些可能影响财务合规的条款？给我列 3 条最关键的 + 修改建议」

### 4. ❓ 报销规则答疑（对内服务高频项 ⭐）
员工天天问财务"这能不能报"，让小助手**按公司报销规则批量起草标准回复**。
**关键**：把公司报销规则上传（PDF / 飞书知识库 / 文字粘贴都行），助手才能答得准。

> 「这是公司报销规则文档（附件）。员工问的几个常见问题：① 跨城打车能不能报？② 客户请客是否可全额？③ 学习资料上限多少？帮我每条起草一段对员工的标准回复」
""",
    },
}

# 历史 dept_code alias（跟 employee_onboarding DEPT_DISPLAY 对齐）
_DEPT_LESSON_ALIASES = {
    "marketing": "client_dept",
    "strategy": "planning",
    "branding": "brand_publicity",
    "exec_office": "executive_office",
    "operations": "execution_ops",
    "film": "brand_publicity",
    "client": "client_dept",
    "planning_dept": "planning",
    "general": "general_affairs",
    "comprehensive": "general_affairs",
}


def get_dept_lesson_body(dept_code: str | None) -> tuple[str, str]:
    """返回部门常用 4 场景的 (标题后缀, body markdown)。

    dept_code 缺失 / 未识别 → 返回通用兜底（不卡流程）。
    返回值：(部门名后缀, body markdown)。部门名用于在 header title 上加后缀，如 "· 客户部"。
    """
    if not dept_code:
        return "", _DEPT_LESSON_FALLBACK_BODY
    code = _DEPT_LESSON_ALIASES.get(dept_code, dept_code)
    dept = _DEPT_LESSON_BODIES.get(code)
    if not dept:
        return "", _DEPT_LESSON_FALLBACK_BODY
    return dept["name"], dept["body"]


_DEPT_LESSON_FALLBACK_BODY = """## 部门常用场景

未识别你的部门，可以先看通用场景：

- **写文案**：「帮我写一段…」
- **列方案**：「列下…可以做的 5 个动作」
- **分析对比**：「**#高** 对比…」
- **总结文档**：发飞书链接 / 文件，「帮我总结要点」
- **深度战略**：「**#超深** …」

💡 部门常用场景由管理员配置，如果你看到这里，请联系管理员把你的部门补上。
"""


# ─────────────────────────── 6. Quiz 测试题（5 道选择题）───────────────────────────

QUIZ_QUESTIONS = [
    {
        "id": 1,
        "question": "想让小助手做深度战略分析（含证据链 / 完整方案），应该加什么前缀？",
        "options": [("A", "#深"), ("B", "#高"), ("C", "#超深"), ("D", "不用加")],
        "correct": "C",
        "explain": "`#超深` 对应超深推理（3-8 分钟），适合完整战略报告；`#高` 适合多角度对比。",
        "lesson_id": "lesson_reasoning",
    },
    {
        "id": 2,
        "question": "同事发来一段巡店视频，想让小助手提取关键画面 + 视频里出现的产品，应该？",
        "options": [
            ("A", "把视频转 GIF 再发"),
            ("B", "直接发视频，问「这视频里出现的产品有哪些？哪些画面有口播？」"),
            ("C", "让小助手自己去摄像头拍"),
            ("D", "用文字描述视频内容再问小助手"),
        ],
        "correct": "B",
        "explain": "小助手能直接看视频，提取关键画面、口播文本、产品识别。**单人每日有限额**，不要刷屏式上传。",
        "lesson_id": "lesson_multimodal",
    },
    {
        "id": 3,
        "question": "老板让你写一份端午活动的复盘报告，正确做法是？",
        "options": [
            ("A", "直接问小助手「我们端午活动有什么效果」（让它自己编）"),
            ("B", "先把活动数据 + 现场照片 + 销售反馈发给小助手，再让它整理"),
            ("C", "把别人公司的活动方案改改"),
            ("D", "让小助手猜一个看着合理的报告"),
        ],
        "correct": "B",
        "explain": "小助手不会凭空编公司事实。给的资料越多越准，没给材料它会标「需要补充」，不能硬生成正式稿。",
        "lesson_id": "lesson_truth",
    },
    {
        "id": 4,
        "question": "想画一张朋友圈方版海报，如果 GPT Image 2 失败了，正确做法是？",
        "options": [
            ("A", "必须手动重试或自己切到 Dreamina"),
            ("B", "说「方版」让 GPT 画；失败小助手会自动切 Dreamina 备用"),
            ("C", "直接放弃，找设计师"),
            ("D", "必须重新打开对话"),
        ],
        "correct": "B",
        "explain": "两件事：① 画幅用「方版/竖版/横版」自然语言切换；② GPT 失败小助手自动 fallback 到 Dreamina，不打断工作流。",
        "lesson_id": "lesson_image",
    },
    {
        "id": 5,
        "question": "下面哪个**不是**小助手目前的能力？",
        "options": [
            ("A", "起草客户触达话术草稿"),
            ("B", "上传视频做摘要 + 口播识别"),
            ("C", "直接对接公司邮箱一键发送邮件"),
            ("D", "自动读飞书 wiki / 云文档"),
        ],
        "correct": "C",
        "explain": "客户触达话术 ✅、视频摘要 ✅、读飞书文档 ✅，但「一键对接公司邮箱发送」不是默认办公场景。",
        "lesson_id": "lesson_daily",
    },
]


# ─── 6.1 部门差异化题（每部门 1 道）─────────────────────────────────────────────
# 触发场景从 dc_engines/department_workflows/defaults.py 提炼。
# 每个员工实际答题 = QUIZ_QUESTIONS（5 道通用）+ 本部门 1 道差异化题 = 6 题。

QUIZ_QUESTIONS_BY_DEPT: dict[str, dict[str, Any]] = {
    "executive_office": {
        "question": "公司开月度战略复盘会，老板让你把会议录音整理成纪要。下面哪种用法最合适？",
        "options": [
            ("A", "直接把录音发给小助手，让它转写"),
            (
                "B",
                "提供录音转文字稿 + #高，让小助手整理成结构化纪要（议题/决议/Owner/时间）",
            ),
            ("C", "让小助手自己脑补会议内容"),
            ("D", "不用小助手，自己手抄"),
        ],
        "correct": "B",
        "explain": "小助手不直接处理音频，但拿到文字稿后用 #高 能很快整理出议题/决议/Owner/时间的结构化纪要。",
        "lesson_id": "lesson_dept_common",
    },
    "client_dept": {
        "question": "要给 VIP 客户发端午节微信/私域问候话术，怎么让小助手起草更准确？",
        "options": [
            ("A", "直接说「写一段」"),
            (
                "B",
                "提供 受众身份 + 节日场景 + 公司业务亮点 + 触达渠道，让小助手定向起草草稿",
            ),
            ("C", "把上次的话术改个名字"),
            ("D", "让小助手猜客户喜好"),
        ],
        "correct": "B",
        "explain": "客户沟通文案的精度，取决于你输入的「受众 / 场景 / 业务信息」密度。给得越具体，输出越对位。",
        "lesson_id": "lesson_dept_common",
    },
    "brand_publicity": {
        "question": "618 大促要做种草海报，怎么让小助手帮你出方案？",
        "options": [
            ("A", "直接说「画一张海报」"),
            (
                "B",
                "提供产品卖点 + 目标人群，用 #高 出 3 个创意方向 + 文案备选，再选定后生图",
            ),
            ("C", "让小助手自己看着办"),
            ("D", "跳过小助手，找设计师从零开始"),
        ],
        "correct": "B",
        "explain": "创意类任务先用 #高 出方向（卖点/人群/调性），再用 Dreamina 生图，效率最高也最不踩坑。",
        "lesson_id": "lesson_dept_common",
    },
    "planning": {
        "question": "老板让你出一份活动策划洞察简报，小助手怎么用最高效？",
        "options": [
            ("A", "把活动主题甩给它，让它自己脑补"),
            (
                "B",
                "提供客户需求/竞品材料 + #超深，让小助手输出证据链 / 机会点 / 行动建议",
            ),
            ("C", "直接复制百度搜索结果交差"),
            ("D", "让小助手脑补策划策略"),
        ],
        "correct": "B",
        "explain": "策划类任务的质量取决于输入资料密度。给材料 + #超深，小助手能输出有证据链的洞察简报。",
        "lesson_id": "lesson_dept_common",
    },
    "execution_ops": {
        "question": "端午活动要排期，怎么让小助手出一份可执行的排期表？",
        "options": [
            ("A", "直接说「帮我排个期」"),
            ("B", "提供任务清单 + 时间窗 + 负责人，用 #高 拆解出每日动作 + 风险预案"),
            ("C", "让小助手随便估时间"),
            ("D", "不用小助手，手画甘特图"),
        ],
        "correct": "B",
        "explain": "执行类任务靠拆解。给任务清单 + 时间窗 + 负责人 + #高，小助手能输出每日动作 + 风险预案。",
        "lesson_id": "lesson_dept_common",
    },
    "finance": {
        "question": "同事发来一批报销发票，要复核能不能报销。小助手怎么用？",
        "options": [
            ("A", "直接发图让它判真伪"),
            (
                "B",
                "把发票信息整理成文本，让小助手按公司报销规则 + 缺项检查清单逐条复核",
            ),
            ("C", "让小助手代查税务局"),
            ("D", "让小助手代发邮件给财务"),
        ],
        "correct": "B",
        "explain": "小助手不判真伪（那是 OCR + 人工的活），但能基于公司规则 + 检查清单做缺项 / 合规复核，效率比你逐条对快得多。",
        "lesson_id": "lesson_dept_common",
    },
    "general_affairs": {
        "question": "综合部要发一份办公规则通知，怎么让小助手处理最稳？",
        "options": [
            ("A", "直接让小助手自己写规则"),
            ("B", "提供制度原文 / 适用范围 / 执行时间，让小助手整理成通知 + FAQ"),
            ("C", "只发一句「写个通知」"),
            ("D", "让小助手替你审批"),
        ],
        "correct": "B",
        "explain": "行政制度和内部规则不能凭空编。给原文、范围和时间，小助手负责结构化表达和常见问题整理。",
        "lesson_id": "lesson_dept_common",
    },
}


# 历史 dept_code 兼容（与 employee_onboarding/main.py 的 DEPT_DISPLAY 对齐）
_DEPT_CODE_ALIASES = {
    "marketing": "client_dept",
    "strategy": "planning",
    "branding": "brand_publicity",
    "exec_office": "executive_office",
    "operations": "execution_ops",
    "film": "brand_publicity",  # 影视部已下线，归并到品宣部
    "client": "client_dept",
    "planning_dept": "planning",
    "general": "general_affairs",
    "comprehensive": "general_affairs",
}


def get_quiz_for_dept(dept_code: str | None) -> list[dict[str, Any]]:
    """返回该员工要答的所有题（通用 5 题 + 部门 1 道差异化题）。

    - dept_code 缺失 / 未识别 → 回退到通用 5 题（不卡流程）
    - legacy dept_code（branding / exec_office / operations / film）→ 走别名映射
    """
    base = [dict(q) for q in QUIZ_QUESTIONS]
    if not dept_code:
        return base
    code = _DEPT_CODE_ALIASES.get(dept_code, dept_code)
    dept_q = QUIZ_QUESTIONS_BY_DEPT.get(code)
    if not dept_q:
        return base
    appended = dict(dept_q)
    appended.setdefault("id", len(base) + 1)
    return base + [appended]


def build_quiz_question_card(
    *,
    q_num: int,
    total: int,
    questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """单题卡片。

    questions 默认走通用 5 题；传入则用 get_quiz_for_dept 返回的题库。
    """
    qs = questions if questions is not None else QUIZ_QUESTIONS
    q = qs[q_num - 1]
    option_lines = "\n".join(f"- **{label}**：{text}" for label, text in q["options"])
    option_buttons = [
        {
            "text": label,
            "value": {"action": "submit_quiz", "q_num": q_num, "choice": label},
            "type": "default",
        }
        for label, text in q["options"]
    ]
    return _business_card(
        title=f"测试 · 第 {q_num} / {total} 题",
        template="blue",
        elements=[
            _md(_status_dot("培训测试进行中", KAMI_INK), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("进度", f"{q_num} / {total}"),
                        ("题型", "单选题"),
                        ("状态", _ink("待作答")),
                    ]
                ),
                "notation",
            ),
            {"tag": "hr"},
            _md(_ink(f"**{q['question']}**"), "heading_2"),
            _md(f"**选项**\n{option_lines}"),
            _md(_muted("请选择一个答案："), "notation"),
            _button_row(option_buttons),
        ],
    )


def build_quiz_feedback_card(
    *,
    q_num: int,
    correct: bool,
    explain: str,
    next_q: int | None,
    total: int | None = None,
    lesson_id: str | None = None,
) -> dict[str, Any]:
    """答题反馈卡片。

    total 默认走通用 5 题；传入时按部门题量（通常 6）判断 next_q 是否越界。

    答对：单按钮「继续第 N+1 题」（最后一题无按钮，由调用方自动出结果）。
    答错：双按钮「📚 返回复习再答」+「继续下一题」（最后一题答错则改成「📊 看结果」）。
    返回复习功能需要传入 lesson_id（错题对应的教程）。
    """
    upper = total if total is not None else len(QUIZ_QUESTIONS)
    if correct:
        title_line = f"第 {q_num} 题答对了"
        header_title = "答对了"
        template = "blue"
        status = _font("正确", SUCCESS_GREEN)
    else:
        title_line = f"第 {q_num} 题还差一点"
        header_title = "看下解析"
        template = "orange"
        status = _font("需复习", WARNING_RED)

    elements: list[dict[str, Any]] = [
        _kami_lede(title_line, "答题结果会自动记录，不需要重复提交。"),
        _md(
            _status_dot("答题反馈", SUCCESS_GREEN if correct else WARNING_RED),
            "notation",
        ),
        _md(
            _aligned_fields(
                [
                    ("题目", str(q_num)),
                    ("结果", status),
                    (
                        "下一题",
                        str(next_q)
                        if next_q is not None and next_q <= upper
                        else "完成",
                    ),
                ]
            ),
            "notation",
        ),
        {"tag": "hr"},
        _md(f"**解析**：{explain}"),
    ]

    has_next = next_q is not None and next_q <= upper

    if correct:
        # 答对：单按钮 / 最后一题无按钮（调用方自动出结果）
        if has_next:
            elements.append({"tag": "hr"})
            elements.append(
                _button_row(
                    [
                        {
                            "text": f"继续第 {next_q} 题",
                            "value": {"action": "next_quiz", "q_num": next_q},
                            "type": "primary",
                        }
                    ]
                )
            )
    else:
        # 答错：双按钮（返回复习 + 继续/看结果），无 lesson_id 时降级为单按钮
        elements.append({"tag": "hr"})
        review_btn = None
        if lesson_id:
            review_btn = {
                "text": "📚 返回复习再答",
                "value": {
                    "action": "review_and_retry",
                    "q_num": q_num,
                    "lesson_id": lesson_id,
                },
                "type": "default",
            }
        if has_next:
            advance_btn = {
                "text": f"继续第 {next_q} 题",
                "value": {"action": "next_quiz", "q_num": next_q},
                "type": "primary",
            }
        else:
            # 最后一题答错：用「看结果」代替「下一题」
            advance_btn = {
                "text": "📊 看结果",
                "value": {"action": "show_result"},
                "type": "primary",
            }
        buttons = [review_btn, advance_btn] if review_btn else [advance_btn]
        elements.append(_button_row(buttons))

    return _business_card(
        title=header_title,
        template=template,
        elements=elements,
    )


def build_quiz_result_card(
    *,
    display_name: str,
    correct_count: int,
    total: int,
    invite_link: str | None = None,
    invite_note: str = "",
    missed_lesson_ids: list[str] | None = None,
) -> dict[str, Any]:
    """测试结果卡：通过 → 2 按钮（继续聊天 / 进入内测群）；不通过 → 提示复习重做。"""
    passed = correct_count == total
    invite_link = (invite_link or "").strip() or None
    # placeholder 链接视为无效（"待补真实链接" / "..." / "…"）
    if invite_link and (
        "…" in invite_link
        or "..." in invite_link
        or "（待补" in invite_link
        or "(待补" in invite_link
    ):
        invite_link = None

    score_line = f"**{display_name}**，测试成绩 {correct_count} / {total}"
    elements: list[dict[str, Any]] = [
        _kami_lede(score_line, "这张卡会作为你的培训通过记录。"),
        _md(
            _aligned_fields(
                [
                    ("成绩", f"**{correct_count} / {total}**"),
                    (
                        "状态",
                        _font("已通过", SUCCESS_GREEN)
                        if passed
                        else _font("未通过", WARNING_RED),
                    ),
                    ("下一步", "加入内测群" if passed else "复习后重做"),
                ]
            )
        ),
    ]
    if passed:
        elements.append({"tag": "hr"})
        elements.append(
            _md(
                "恭喜你！全对通过，你已经掌握小助手的核心用法。\n\n"
                "**接下来**：\n"
                "- 直接私聊我开始工作（提问 / 写文案 / 出方案）\n"
                "- 工作流（自动发邮件等）会在内测群里通知更新"
            )
        )
        # 2 按钮：💬 继续聊天 + 🚀 进入内测群
        chat_btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "💬 继续和小助手聊天"},
            "type": "default",
            "value": {"action": "noop"},
        }
        if invite_link:
            # 真实 link：进群按钮用 multi_url 直接跳转（飞书 v2 schema）
            join_btn = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🚀 进入内测群"},
                "type": "primary",
                "multi_url": {
                    "url": invite_link,
                    "android_url": invite_link,
                    "ios_url": invite_link,
                    "pc_url": invite_link,
                },
            }
        else:
            # 无 link：进群按钮降级为 noop + 底部小字解释
            join_btn = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🚀 进入内测群"},
                "type": "primary",
                "value": {"action": "noop"},
            }
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "column_set",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [chat_btn],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [join_btn],
                    },
                ],
            }
        )
        if not invite_link:
            elements.append(
                _md(
                    _muted("（群链接稍后开放，先继续聊天熟悉小助手吧）"),
                    "notation",
                )
            )
        elif invite_note:
            # 极少数业务自定义提示（如 auto-invite 失败的备注）
            elements.append(_md(_muted(invite_note), "notation"))
        header_color = "green"
        header_title = "测试通过 · 欢迎加入内测"
    else:
        lesson_by_id = {code: (num, title) for num, title, code in LESSON_SEQUENCE}
        missed_lines = []
        for lesson_id in missed_lesson_ids or []:
            item = lesson_by_id.get(lesson_id)
            if item:
                num, title = item
                missed_lines.append(f"- 第 {num} 节：{title}")
        elements.append(
            _md(
                "差一点点！再回去把对应教程看完，重新答这道题就行 —— "
                "不会从头来过，放心点。"
            )
        )
        if missed_lines:
            elements.append(
                _md("**建议优先复习**\n" + "\n".join(missed_lines)),
            )
        elements.append({"tag": "hr"})
        elements.append(
            _button_row(
                [
                    {
                        "text": "回学习清单",
                        "value": {"action": "show_tutorial_list"},
                        "type": "default",
                    },
                    {
                        "text": "重做测试",
                        "value": {"action": "start_quiz"},
                        "type": "primary",
                    },
                ]
            )
        )
        header_color = "orange"
        header_title = "还差一点，加油"

    return _business_card(
        title=header_title,
        template=header_color,
        elements=elements,
    )


def next_lesson_id(current_lesson_id: str) -> str | None:
    """按 LESSON_SEQUENCE 顺序找下一节；最后一节返回 None。

    入职引导链用，main.py 倒计时任务也会调它来算下一卡。
    """
    codes = [code for _num, _title, code in LESSON_SEQUENCE]
    if current_lesson_id not in codes:
        return None
    idx = codes.index(current_lesson_id)
    if idx + 1 >= len(codes):
        return None
    return codes[idx + 1]


def build_tutorial_lesson_card(
    *,
    lesson_id: str,
    auto_progress: bool = True,
    timeout_sec: int = 60,
    dept_code: str | None = None,
    retry_q_num: int | None = None,
) -> dict[str, Any] | None:
    """单节教程详情卡。

    三种模式：
    - **auto_progress=True**（默认 · 入职引导链模式）：底部显示「📖 继续学下一节」+「✅ 开始测试」
      + 「⏳ N 秒后自动继续/开始测试」。最后一节没有"继续"按钮，只有"开始测试" + 倒计时。
    - **auto_progress=False, retry_q_num=None**（复习模式 · 测试未通过后从清单挑节复习）：
      底部「📚 返回学习清单」。
    - **retry_q_num=N**（答错返回复习再答模式）：底部单按钮「✅ 已经复习好了，再答第 N 题」。
      优先级最高，无论 auto_progress 取值。

    特殊处理：`lesson_id == "lesson_dept_common"` 时，body 由 get_dept_lesson_body(dept_code)
    运行时生成；header 标题会带上部门名后缀。
    """
    lesson = TUTORIALS.get(lesson_id)
    if not lesson:
        return None
    # 部门差异化教程：运行时根据 dept_code 生成 body + title 后缀
    if lesson_id == "lesson_dept_common":
        dept_suffix, dept_body = get_dept_lesson_body(dept_code)
        # 浅拷贝避免污染原 dict
        lesson = {
            "title": (
                f"{lesson['title']} · {dept_suffix}" if dept_suffix else lesson["title"]
            ),
            "body": dept_body,
        }
    elements = _md_blocks_from_text(lesson["body"])
    # Header already carries the lesson title; keep the body compact and avoid
    # a repeated large title at the top of every tutorial card.
    if (
        elements
        and elements[0].get("tag") == "markdown"
        and elements[0].get("text_size") == "heading_2"
    ):
        elements = elements[1:]
    lesson_index = next(
        (num for num, _title, code in LESSON_SEQUENCE if code == lesson_id),
        0,
    )
    elements = [
        _md(_status_dot("培训教程", KAMI_INK), "notation"),
        _md(
            _aligned_fields(
                [
                    ("章节", f"第 {lesson_index} / {len(LESSON_SEQUENCE)} 节"),
                    ("状态", _ink("学习中")),
                    ("时长", "约 1-2 分钟"),
                ]
            ),
            "notation",
        ),
        {"tag": "hr"},
        *elements,
    ]
    elements.append({"tag": "hr"})

    if retry_q_num is not None:
        # 答错返回复习再答模式：单按钮回到错题
        elements.append(
            _button_row(
                [
                    {
                        "text": f"已经复习好了，再答第 {retry_q_num} 题",
                        "value": {"action": "retry_quiz", "q_num": retry_q_num},
                        "type": "primary",
                    }
                ]
            )
        )
    elif not auto_progress:
        # 复习模式：单按钮返回清单
        elements.append(
            _button_row(
                [
                    {
                        "text": "返回学习清单",
                        "value": {"action": "show_tutorial_list"},
                        "type": "default",
                    }
                ]
            )
        )
    else:
        next_id = next_lesson_id(lesson_id)
        buttons = []
        if next_id:
            buttons.append(
                {
                    "text": "继续学下一节",
                    "value": {
                        "action": "continue_tutorial",
                        "current_lesson_id": lesson_id,
                    },
                    "type": "primary",
                }
            )
            buttons.append(
                {
                    "text": "开始测试",
                    "value": {"action": "start_quiz"},
                    "type": "default",
                }
            )
            timeout_hint = f"**{timeout_sec} 秒**不点的话，自动继续下一节。"
        else:
            buttons.append(
                {
                    "text": "开始测试",
                    "value": {"action": "start_quiz"},
                    "type": "primary",
                }
            )
            timeout_hint = f"**{timeout_sec} 秒**不点的话，自动开始测试。"
        elements.append(_button_row(buttons))
        elements.append(
            {
                "tag": "markdown",
                "content": _muted(timeout_hint),
                "text_size": "notation",
            }
        )

    # 教程卡保留独立 schema 因为 body 是动态 markdown 长内容，不走 _business_card
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": lesson["title"]},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def _business_card(
    *,
    title: str,
    template: str,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": elements},
    }


def _md(content: str, text_size: str = "normal") -> dict[str, Any]:
    return {"tag": "markdown", "content": _safe_md(content), "text_size": text_size}


def _compact(text: Any, limit: int = 120) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _field_list(items: list[str]) -> str:
    if not items:
        return "- （暂无）"
    return "\n".join(f"- {item}" for item in items)


def _inline_list(items: list[str] | None, *, empty: str = "暂无") -> str:
    if not items:
        return empty
    return " · ".join(str(item).strip() for item in items if str(item).strip()) or empty


def _aligned_fields(pairs: list[tuple[str, str]]) -> str:
    labels = [label for label, _value in pairs]
    width = max((len(label) for label in labels), default=0)
    lines = []
    for label, value in pairs:
        padding = "　" * max(width - len(label), 0)
        lines.append(f"**{label}**{padding}：{value}")
    return "\n".join(lines)


def _count_summary(items: dict[str, int] | None) -> str:
    if not items:
        return "暂无"
    parts = []
    for name, count in items.items():
        unit = "段" if "文字" in name or "原文" in name or "粘贴" in name else "个"
        parts.append(f"{name} {count} {unit}")
    return " · ".join(parts)


def _inline_cell(value: Any, fallback: str = "-") -> str:
    text = str(value or fallback).strip() or fallback
    return text.replace("\n", " ").replace("|", "/")


def _metric_row(items: list[tuple[str, str]]) -> dict[str, Any]:
    columns = []
    for label, value in items:
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [
                    _md(f"{label}\n**{value}**", "normal"),
                ],
            }
        )
    return {"tag": "column_set", "columns": columns}


def _kv_row(label: str, value: str, *, text_size: str = "normal") -> dict[str, Any]:
    return {
        "tag": "column_set",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_md(f"**{label}**", "notation")],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "elements": [_md(value, text_size)],
            },
        ],
    }


def _truth_status_template(truth_status: str) -> str:
    return {
        "已核验": "green",
        "部分待确认": "red",
        "需要补充": "red",
        "示例/假设": "violet",
        "示例假设": "violet",
        "执行失败": "red",
    }.get(truth_status, "grey")


def _should_show_restart_button(next_action_hint: str) -> bool:
    if "重启" not in next_action_hint:
        return False
    return not any(
        word in next_action_hint for word in ("无需重启", "不用重启", "不需要重启")
    )


def build_truth_intake_request_card(
    *,
    task_title: str,
    task_id: str,
    missing_fields: list[str],
    task_brief: str = "",
    accept_hints: list[str] | None = None,
) -> dict[str, Any]:
    """公司真实任务缺少依据时，提示员工补充可核验资料。"""
    hints = accept_hints or ["飞书链接", "附件", "截图", "粘贴原文"]
    elements = [
        _md(f"**{_compact(task_title, 42) or '待补资料任务'}**", "heading_2"),
        _md(_status_dot("资料不足，暂不进入生成"), "notation"),
        _md(
            _muted("我先帮你把这件事稳住。为了不让内容失真，我还需要一点可核验的资料。")
        ),
        _md(
            _aligned_fields(
                [
                    ("真实性", _font("需要补充", WARNING_RED)),
                    ("状态", _font("`truth_intake / blocked`", WARNING_RED)),
                    ("编号", f"`#{task_id}`"),
                ]
            )
        ),
        _md(f"**需要补充**\n{_field_list(missing_fields)}"),
        _md(
            f"**可接受资料**：{_inline_list(hints)}",
            "notation",
        ),
    ]
    if task_brief:
        elements.extend(
            [
                {"tag": "hr"},
                _display_panel("原话", _compact(task_brief, 180), "notation"),
            ]
        )
    elements.extend(
        [
            _button_row(
                [
                    {
                        "text": "我现在补资料",
                        "value": {"action": "noop"},
                        "type": "primary",
                    }
                ]
            ),
            _md(_muted("有多少给多少，我会接着往下处理。"), "notation"),
        ]
    )
    return _business_card(
        title="需要真实资料",
        template="red",
        elements=elements,
    )


def build_truth_intake_received_card(
    *,
    task_id: str,
    sources_summary: dict[str, int],
    archive_path: str,
    next_route: str = "Router",
) -> dict[str, Any]:
    """真实资料接收并归档后的确认卡。"""
    return _business_card(
        title="资料已收到",
        template="green",
        elements=[
            _md("**资料已归档，继续处理**", "heading_2"),
            _md(_status_dot("资料链路已接通", SUCCESS_GREEN), "notation"),
            _md(
                _aligned_fields(
                    [
                        ("状态", _font("已归档", SUCCESS_GREEN)),
                        ("编号", f"`#{task_id}`"),
                        ("资料来源", _count_summary(sources_summary)),
                    ]
                )
            ),
            _display_panel("归档位置", archive_path, "notation"),
            _md(f"**下一步**：已交回 **{next_route}** 继续执行", "notation"),
        ],
    )


def build_source_trace_card(
    *,
    truth_status: str,
    sources: list[str],
    kb_names: list[str] | None = None,
    unconfirmed: list[str] | None = None,
    risk_note: str = "",
) -> dict[str, Any]:
    """事实类输出后的来源追踪卡。"""
    elements = [
        _md(
            f"**真实性状态**：**{_font(truth_status, _status_color(truth_status))}**",
            "heading_2",
        ),
        _md(_status_dot("来源链路已整理", _status_color(truth_status)), "notation"),
        _md(f"**来源**\n{_field_list(sources)}"),
    ]
    if kb_names:
        elements.append(_md(f"**知识库**：{_inline_list(kb_names)}"))
    if unconfirmed:
        elements.append(_md(f"**未确认项**\n{_field_list(unconfirmed)}"))
    if risk_note:
        elements.append(_md(f"**风险提示**：{_muted(risk_note)}", "notation"))
    return _business_card(
        title="来源追踪",
        template=_truth_status_template(truth_status),
        elements=elements,
    )


def build_devops_status_card(
    *,
    services: list[dict],
    queue_summary: dict | None = None,
    recent_errors: list[str] | None = None,
    next_action_hint: str = "",
) -> dict[str, Any]:
    """DevOps 服务状态、队列和错误摘要卡。"""
    statuses = [str(service.get("status", "")).strip() for service in services]
    if any(status == "异常" for status in statuses):
        template = "red"
    elif any(status == "降级" for status in statuses):
        template = "red"
    elif services and all(status == "正常" for status in statuses):
        template = "green"
    else:
        template = "grey"

    queue_labels = {
        "hermes": "Hermes 队列",
        "claude_cli": "Claude CLI",
        "codex": "Codex",
    }
    queue_parts = [
        f"{queue_labels.get(str(name), str(name))}：{count}"
        for name, count in (queue_summary or {}).items()
    ]
    elements = [
        _md("**服务状态**", "heading_2"),
        _md(
            _status_dot(
                "运行状态快照",
                _status_color("降级") if template == "red" else template,
            ),
            "notation",
        ),
    ]
    if services:
        service_lines = [
            (
                f"- **{_inline_cell(service.get('name'))}**："
                f"{_font(_inline_cell(service.get('status')), _status_color(_inline_cell(service.get('status'))))} · "
                f"PID `{_inline_cell(service.get('pid'))}` · "
                f"{_font(_compact(_inline_cell(service.get('metric')), 72), 'grey')}"
            )
            for service in services
        ]
        elements.append(_md("**服务列表**\n" + "\n".join(service_lines)))
    if queue_parts:
        elements.append(_md(f"**关键指标**：**{' · '.join(queue_parts)}**"))
    if recent_errors:
        errors = "\n".join(f"- {error}" for error in recent_errors[:3])
        elements.append(_md(f"**错误摘要**\n{errors}"))
    if next_action_hint:
        elements.append(_md(_muted(next_action_hint), "notation"))
    if _should_show_restart_button(next_action_hint):
        elements.append(
            _button_row(
                [
                    {
                        "text": "触发重启诊断",
                        "value": {"action": "devops_restart_prompt"},
                        "type": "default",
                    }
                ]
            )
        )
    return _business_card(
        title="DevOps 状态",
        template=template,
        elements=elements,
    )


def build_kb_archive_card(
    *,
    archived_items: dict[str, int],
    kb_name: str,
    status: str,
    archive_path: str,
    doc_id: str = "",
    error_hint: str = "",
) -> dict[str, Any]:
    """材料归档并同步知识库后的沉淀状态卡。"""
    status_text = {
        "已入库": "已入库",
        "待审核": "待审核",
        "入库失败": "入库失败",
    }.get(status, status)
    template = {
        "已入库": "green",
        "待审核": "red",
        "入库失败": "red",
    }.get(status, "grey")
    trace_line = archive_path
    if doc_id:
        trace_line += f"\n\ndoc_id: {doc_id}"

    elements = [
        _md(f"**{_font(status_text, _status_color(status))}**", "heading_2"),
        _md(_status_dot("知识库同步结果", _status_color(status)), "notation"),
        _md(f"**知识库**：{kb_name}\n**来源**：{_count_summary(archived_items)}"),
        _display_panel("可追溯路径", trace_line, "notation"),
    ]
    if status == "入库失败" and error_hint:
        elements.append(_md(f"**失败诊断**：{_muted(error_hint)}", "notation"))
    return _business_card(
        title="资料已沉淀",
        template=template,
        elements=elements,
    )


def build_employee_pending_card(
    *,
    task_title: str,
    pending_items: list[dict],
    current_draft_summary: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """业务草稿已生成但仍需员工确认字段时的提示卡。"""
    pending_lines = []
    for item in pending_items:
        field = str(item.get("field", "")).strip() or "待确认项"
        hint = str(item.get("hint", "")).strip() or "请补充"
        pending_lines.append(f"**{field}**：{_font(hint, WARNING_RED)}")

    elements = [
        _md(f"**{_compact(task_title, 42) or '待确认任务'}**", "heading_2"),
        _md(_status_dot("等待员工确认", WARNING_RED), "notation"),
        _md(
            _muted("我已经先整理到这一步了，还需要你确认几项，给我答案我就接着往下做。")
        ),
        _md(f"**待确认项**\n{_field_list(pending_lines)}"),
    ]
    if task_id:
        elements.append(_md(f"**task_id**：`#{task_id}`", "notation"))
    if current_draft_summary:
        elements.extend(
            [
                {"tag": "hr"},
                _display_panel("当前可用内容", current_draft_summary[:240]),
            ]
        )
    elements.extend(
        [
            _md(_muted("补充后我会自动继续生成"), "notation"),
            _button_row(
                [
                    {
                        "text": "我现在补",
                        "value": {"action": "noop"},
                        "type": "primary",
                    }
                ]
            ),
        ]
    )
    return _business_card(
        title="还差一点信息",
        template="red",
        elements=elements,
    )


def _status_template(status: str) -> str:
    if status in {"已完成", "已生成", "已理解", "已通过", "正常"}:
        return "green"
    if status in {"进行中", "生成中", "理解中", "处理中", "排队中"}:
        return "blue"
    if status in {"需要补充", "待确认", "已逾期", "阻塞", "失败", "执行失败"}:
        return "red"
    if status in {"已降级", "部分完成", "部分待确认"}:
        return "orange"
    return "grey"


def build_media_generation_card(
    *,
    task_title: str,
    media_type: str,
    status: str,
    prompt: str,
    engine: str = "",
    task_id: str = "",
    aspect_ratio: str = "",
    duration: str = "",
    output_url: str = "",
    fallback_note: str = "",
    error_hint: str = "",
    elapsed_sec: float = 0,
) -> dict[str, Any]:
    """媒体生成母版：生图 / 文生视频 / 图片转视频的状态与结果卡。"""
    type_label = {
        "image": "生图",
        "video": "生视频",
        "image_to_video": "图片转视频",
    }.get(media_type, media_type or "媒体生成")
    template = _status_template(status)
    fields: list[tuple[str, str]] = [
        ("类型", type_label),
        ("状态", _font(status, _status_color(status))),
    ]
    if engine:
        fields.append(("引擎", engine))
    if aspect_ratio:
        fields.append(("画幅", aspect_ratio))
    if duration:
        fields.append(("时长", duration))
    if task_id:
        fields.append(("编号", f"`#{task_id}`"))

    elements: list[dict[str, Any]] = [
        _md(f"**{_compact(task_title, 48) or type_label}**", "heading_2"),
        _md(_status_dot(f"{type_label}状态", _status_color(status)), "notation"),
        _md(_aligned_fields(fields)),
        _display_panel("生成描述", prompt, "notation"),
    ]
    if status in {"生成中", "进行中", "处理中", "排队中"}:
        elements.extend(
            [
                {"tag": "hr"},
                _md(
                    f"{waiting_pulse(elapsed_sec)} {_font('生成中', WARNING_RED)} · "
                    f"**{progress_bar(elapsed_sec, 240)}** · "
                    f"{_muted('⏱️已等待 ' + _format_elapsed(elapsed_sec))}",
                    "heading_3",
                ),
            ]
        )
    if fallback_note:
        elements.append(_md(f"**兜底状态**：{_muted(fallback_note)}", "notation"))
    if output_url:
        elements.append({"tag": "hr"})
        elements.append(_display_panel("输出链接", output_url, "notation"))
    if error_hint:
        elements.append(_md(f"**失败诊断**：{_muted(error_hint)}", "notation"))
    return _business_card(
        title=f"{type_label} · {status}",
        template=template,
        elements=elements,
    )


def build_multimodal_understanding_card(
    *,
    task_title: str,
    modality: str,
    status: str,
    files_summary: dict[str, int] | None = None,
    truth_status: str = "已核验",
    findings: list[str] | None = None,
    limits: list[str] | None = None,
    next_action_hint: str = "",
    task_id: str = "",
    elapsed_sec: float = 0,
) -> dict[str, Any]:
    """多模态理解母版：图片 / 视频 / 音频 / 混合附件理解状态卡。"""
    modality_label = {
        "image": "图片理解",
        "video": "视频理解",
        "audio": "音频理解",
        "mixed": "多模态理解",
    }.get(modality, modality or "多模态理解")
    template = _status_template(status)
    elements: list[dict[str, Any]] = [
        _md(f"**{_compact(task_title, 48) or modality_label}**", "heading_2"),
        _md(_status_dot(f"{modality_label}状态", _status_color(status)), "notation"),
        _md(
            _aligned_fields(
                [
                    ("类型", modality_label),
                    ("状态", _font(status, _status_color(status))),
                    ("真实性", _font(truth_status, _status_color(truth_status))),
                    ("材料", _count_summary(files_summary)),
                    ("编号", f"`#{task_id}`" if task_id else "未记录"),
                ]
            )
        ),
    ]
    if status in {"理解中", "处理中", "排队中"}:
        elements.append(
            _md(
                f"{waiting_pulse(elapsed_sec)} {_font('理解中', WARNING_RED)} · "
                f"**{progress_bar(elapsed_sec, 180)}** · "
                f"{_muted('⏱️已等待 ' + _format_elapsed(elapsed_sec))}",
                "heading_3",
            )
        )
    if findings:
        elements.append({"tag": "hr"})
        elements.append(_md(f"**已识别要点**\n{_field_list(findings[:6])}"))
    if limits:
        elements.append(
            _md(
                f"**仍需确认**\n{_field_list([_font(item, WARNING_RED) for item in limits[:6]])}"
            )
        )
    if next_action_hint:
        elements.append(_md(f"**下一步**：{_muted(next_action_hint)}", "notation"))
    return _business_card(
        title=f"{modality_label} · {status}",
        template=template,
        elements=elements,
    )


def build_case_overview_card(
    *,
    case_id: str,
    case_title: str,
    status: str,
    owner: str = "",
    progress: str = "",
    task_summary: dict[str, int] | None = None,
    risks: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Case / 项目聚合母版：把多个任务收敛成一个可扫描的项目卡。"""
    summary = task_summary or {}
    elements: list[dict[str, Any]] = [
        _md(f"**{_compact(case_title, 56) or '项目 Case'}**", "heading_2"),
        _md(_status_dot("Case 进度快照", _status_color(status)), "notation"),
        _md(
            _aligned_fields(
                [
                    ("状态", _font(status, _status_color(status))),
                    ("负责人", owner or "未指定"),
                    ("进度", progress or "待更新"),
                    ("编号", f"`#{case_id}`"),
                ]
            )
        ),
        _md(
            "**任务概况**："
            f"待办 {summary.get('todo', 0)} · "
            f"进行中 {summary.get('in_progress', 0)} · "
            f"已完成 {summary.get('done', 0)} · "
            f"阻塞 {summary.get('blocked', 0)}"
        ),
    ]
    if risks:
        elements.append(
            _md(
                f"**风险 / 阻塞**\n{_field_list([_font(item, WARNING_RED) for item in risks[:5]])}"
            )
        )
    if next_actions:
        elements.append(_md(f"**下一步动作**\n{_field_list(next_actions[:5])}"))
    return _business_card(
        title="Case 概览",
        template=_status_template(status),
        elements=elements,
    )


def build_task_reminder_card(
    *,
    task_id: str,
    task_title: str,
    assignee: str = "",
    due_text: str = "",
    status: str = "待处理",
    priority: str = "普通",
    source: str = "",
    next_actions: list[str] | None = None,
    overdue: bool = False,
) -> dict[str, Any]:
    """通用待办提醒母版：截止时间、负责人、确认/完成/稍后提醒入口。"""
    display_status = "已逾期" if overdue else status
    elements: list[dict[str, Any]] = [
        _md(f"**{_compact(task_title, 56) or '待办任务'}**", "heading_2"),
        _md(_status_dot("任务提醒", WARNING_RED if overdue else "blue"), "notation"),
        _md(
            _aligned_fields(
                [
                    ("状态", _font(display_status, _status_color(display_status))),
                    (
                        "优先级",
                        _font(priority, WARNING_RED)
                        if priority in {"高", "紧急"}
                        else priority,
                    ),
                    ("负责人", assignee or "未指定"),
                    ("截止", due_text or "未设置"),
                    ("来源", source or "未记录"),
                    ("编号", f"`#{task_id}`"),
                ]
            )
        ),
    ]
    if next_actions:
        elements.append(_md(f"**建议动作**\n{_field_list(next_actions[:5])}"))
    elements.append(
        _button_row(
            [
                {
                    "text": "我来处理",
                    "type": "primary",
                    "value": {
                        "source": "task_reminder_card",
                        "action": "ack",
                        "task_id": task_id,
                    },
                },
                {
                    "text": "标记完成",
                    "value": {
                        "source": "task_reminder_card",
                        "action": "done",
                        "task_id": task_id,
                    },
                },
                {
                    "text": "稍后提醒",
                    "value": {
                        "source": "task_reminder_card",
                        "action": "snooze",
                        "task_id": task_id,
                    },
                },
            ]
        )
    )
    return _business_card(
        title="任务提醒",
        template="red" if overdue else _status_template(status),
        elements=elements,
    )


def _skill_kind_title(kind: str) -> str:
    return "老板 Skill" if kind == "boss" else "同事 Skill"


def _skill_action(
    action: str,
    *,
    kind: str,
    slug: str,
    version: str = "",
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "source": "hermes_skill_card",
        "action": action,
        "kind": kind,
        "slug": slug,
    }
    if version:
        payload["version"] = version
    payload.update({k: v for k, v in extra.items() if v not in ("", None)})
    return payload


def _skill_primary_buttons(
    kind: str, slug: str, version: str = ""
) -> list[dict[str, Any]]:
    return [
        {
            "text": "查看",
            "type": "primary",
            "value": _skill_action("inspect", kind=kind, slug=slug, version=version),
        },
        {
            "text": "审阅",
            "value": _skill_action("review", kind=kind, slug=slug, version=version),
        },
        {
            "text": "纠错",
            "value": _skill_action(
                "correct_request", kind=kind, slug=slug, version=version
            ),
        },
    ]


def _skill_risk_buttons(
    kind: str, slug: str, version: str = ""
) -> list[dict[str, Any]]:
    return [
        {
            "text": "回滚",
            "value": _skill_action(
                "rollback_request", kind=kind, slug=slug, version=version
            ),
        },
        {
            "text": "删除",
            "value": _skill_action(
                "delete_request", kind=kind, slug=slug, version=version
            ),
        },
    ]


def build_skill_list_card(
    *,
    kind: str,
    skills: list[dict[str, Any]],
) -> dict[str, Any]:
    """Skill 列表卡：查看、审阅、纠错、回滚、删除入口。"""
    title = f"{_skill_kind_title(kind)} 列表"
    elements: list[dict[str, Any]] = [
        _md(f"**共 {len(skills)} 个可用 Skill**", "heading_2"),
        _md(_muted("按钮会携带具体 slug / version，后端仍会校验权限。"), "notation"),
    ]
    if not skills:
        elements.append(_md("暂无可用 Skill。"))
    for index, skill in enumerate(skills[:12], start=1):
        name = _compact(skill.get("name") or skill.get("slug") or "未命名 Skill", 42)
        slug = str(skill.get("slug") or "").strip()
        version = str(skill.get("version") or "unknown")
        rollback_version = str(skill.get("rollback_version") or "")
        summary = _aligned_fields(
            [
                ("编号", f"`{index}`"),
                ("名称", f"**{name}**"),
                ("slug", f"`{slug}`"),
                ("版本", f"`{version}`"),
                (
                    "可回滚",
                    f"`{rollback_version}`" if rollback_version else _muted("暂无"),
                ),
                ("纠错", str(skill.get("corrections_count", 0))),
                ("更新", _muted(_compact(skill.get("updated_at") or "unknown", 36))),
            ]
        )
        if index > 1:
            elements.append({"tag": "hr"})
        elements.extend(
            [
                _md(summary),
                _button_row(_skill_primary_buttons(kind, slug, version)),
                _button_row(_skill_risk_buttons(kind, slug, rollback_version)),
            ]
        )
    if len(skills) > 12:
        elements.append(
            _md(
                _muted(f"还有 {len(skills) - 12} 个未展示，请继续用命令分页。"),
                "notation",
            )
        )
    return _business_card(title=title, template="green", elements=elements)


def build_skill_detail_card(
    *,
    kind: str,
    skill: dict[str, Any],
) -> dict[str, Any]:
    """Skill 详情卡：展示元信息、文件与知识源。"""
    slug = str(skill.get("slug") or "").strip()
    version = str(skill.get("version") or "unknown")
    rollback_version = str(skill.get("rollback_version") or "")
    files = [str(item) for item in skill.get("files") or []]
    sources = [str(item) for item in skill.get("knowledge_sources") or []]
    elements = [
        _md(f"**{_compact(skill.get('name') or slug, 48)}**", "heading_2"),
        _md(
            _aligned_fields(
                [
                    ("类型", _skill_kind_title(kind)),
                    ("slug", f"`{slug}`"),
                    ("版本", f"`{version}`"),
                    (
                        "可回滚",
                        f"`{rollback_version}`" if rollback_version else _muted("暂无"),
                    ),
                    ("会话", str(skill.get("conversation_type") or "unknown")),
                    ("消息", str(skill.get("message_count", 0))),
                    ("说话人", str(skill.get("speaker_count", 0))),
                    ("纠错", str(skill.get("corrections_count", 0))),
                    (
                        "更新",
                        _muted(_compact(skill.get("updated_at") or "unknown", 42)),
                    ),
                ]
            )
        ),
        _md(f"**知识来源**\n{_field_list([f'`{item}`' for item in sources[:8]])}"),
        _md(f"**文件清单**\n{_field_list([f'`{item}`' for item in files[:12]])}"),
        _button_row(_skill_primary_buttons(kind, slug, version)[1:]),
        _button_row(_skill_risk_buttons(kind, slug, rollback_version)),
    ]
    return _business_card(
        title=f"{_skill_kind_title(kind)} 详情",
        template="green",
        elements=elements,
    )


def build_skill_review_card(
    *,
    kind: str,
    review: dict[str, Any],
) -> dict[str, Any]:
    """Skill 质量审阅卡：通过项、风险项和后续操作。"""
    slug = str(review.get("slug") or "").strip()
    version = str(review.get("version") or "unknown")
    rollback_version = str(review.get("rollback_version") or "")
    score = int(review.get("score") or 0)
    risks = [str(item) for item in review.get("risks") or []]
    passes = [str(item) for item in review.get("passes") or []]
    template = "green" if score >= 80 and not risks else "red"
    elements = [
        _md(f"**{_compact(review.get('name') or slug, 48)}**", "heading_2"),
        _md(
            _aligned_fields(
                [
                    ("slug", f"`{slug}`"),
                    ("版本", f"`{version}`"),
                    (
                        "评分",
                        f"**{_font(str(score) + '/100', SUCCESS_GREEN if score >= 80 else WARNING_RED)}**",
                    ),
                    ("纠错", str(review.get("corrections_count", 0))),
                ]
            )
        ),
        _md(f"**通过项**\n{_field_list(passes[:8])}"),
        _md(
            f"**风险项**\n{_field_list([_font(item, WARNING_RED) for item in risks[:8]] or ['暂无明显风险'])}"
        ),
        _button_row(_skill_primary_buttons(kind, slug, version)),
        _button_row(_skill_risk_buttons(kind, slug, rollback_version)),
    ]
    return _business_card(
        title=f"{_skill_kind_title(kind)} 质量审阅",
        template=template,
        elements=elements,
    )


def build_deleted_skill_list_card(
    *,
    kind: str,
    skills: list[dict[str, Any]],
) -> dict[str, Any]:
    """已删除 Skill 回收站卡：恢复入口。"""
    title = f"已删除{_skill_kind_title(kind)}"
    elements: list[dict[str, Any]] = [
        _md(f"**回收站共 {len(skills)} 个 Skill**", "heading_2"),
        _md(_muted("恢复也会走管理员权限校验。"), "notation"),
    ]
    if not skills:
        elements.append(_md("回收站为空。"))
    for index, skill in enumerate(skills[:12], start=1):
        slug = str(skill.get("slug") or "").strip()
        if index > 1:
            elements.append({"tag": "hr"})
        elements.extend(
            [
                _md(
                    _aligned_fields(
                        [
                            ("编号", f"`{index}`"),
                            ("名称", f"**{_compact(skill.get('name') or slug, 42)}**"),
                            ("slug", f"`{slug}`"),
                            (
                                "删除",
                                _muted(
                                    _compact(skill.get("deleted_at") or "unknown", 42)
                                ),
                            ),
                            ("路径", f"`{_compact(skill.get('path') or '', 80)}`"),
                        ]
                    )
                ),
                _button_row(
                    [
                        {
                            "text": "恢复",
                            "type": "primary",
                            "value": _skill_action("restore", kind=kind, slug=slug),
                        },
                        {
                            "text": "查看路径",
                            "value": _skill_action(
                                "inspect_deleted_path",
                                kind=kind,
                                slug=slug,
                                path=str(skill.get("path") or ""),
                            ),
                        },
                    ]
                ),
            ]
        )
    return _business_card(title=title, template="red", elements=elements)


def build_skill_confirm_card(
    *,
    operation: str,
    kind: str,
    slug: str,
    version: str = "",
    operator_id: str = "",
    risk_note: str = "",
) -> dict[str, Any]:
    """危险操作二次确认卡：删除和回滚只从这里进入执行。"""
    operation_name = "删除" if operation == "delete" else "回滚"
    confirm_action = "delete_confirm" if operation == "delete" else "rollback_confirm"
    elements = [
        _md(f"**确认{operation_name} {_skill_kind_title(kind)}**", "heading_2"),
        _md(_status_dot(f"{operation_name}属于危险操作", WARNING_RED), "notation"),
        _md(
            _aligned_fields(
                [
                    ("类型", _skill_kind_title(kind)),
                    ("slug", f"`{slug}`"),
                    ("版本", f"`{version}`" if version else "未指定"),
                    ("操作者", f"`{operator_id}`" if operator_id else "未知"),
                    (
                        "风险",
                        _font(
                            risk_note or "会修改当前 Skill 状态，请确认后再执行。",
                            WARNING_RED,
                        ),
                    ),
                ]
            )
        ),
        _button_row(
            [
                {
                    "text": f"确认{operation_name}",
                    "type": "primary",
                    "value": _skill_action(
                        confirm_action, kind=kind, slug=slug, version=version
                    ),
                },
                {
                    "text": "取消",
                    "value": _skill_action(
                        "cancel", kind=kind, slug=slug, version=version
                    ),
                },
            ]
        ),
    ]
    return _business_card(
        title=f"确认{operation_name}", template="red", elements=elements
    )


def build_daily_response_card(
    *,
    content_md: str,
    title: str | None = None,
    header_color: str = "blue",
    footer_hint: str | None = None,
) -> dict[str, Any]:
    """日常对话渲染卡片 —— 给 LLM 输出的 markdown 套一层飞书卡片。

    用智能字号分段：# / ## / ### 渲染成 22/18/16px 加粗大字，
    正文 14px，`> 引用` 12px 小灰字。
    """
    elements: list[dict[str, Any]] = _md_blocks_from_text(content_md)
    if footer_hint:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "markdown",
                "content": footer_hint,
                "text_size": "notation",
            }
        )

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "body": {"elements": elements},
    }
    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": header_color,
        }
    return card


def build_casual_response_card(
    *,
    content_md: str,
    user_msg: str = "",
    footer_hint: str | None = None,
) -> dict[str, Any]:
    """轻量闲聊卡片：不显示任务、模型、进度，只保留对话感。"""
    content = str(content_md or "").strip()
    has_structure = bool(CASUAL_STRUCTURE_RE.search(content))
    if len(content) <= 180 and not has_structure:
        elements: list[dict[str, Any]] = [_md(content, "normal")]
    else:
        elements = _md_blocks_from_text(content, max_chars=2400)

    if footer_hint:
        elements.append(_md(_muted(footer_hint), "notation"))
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "巅池-Agent小助手"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


# ─────────────────────────── 7. 文案/语气敏感型业务卡（Claude 负责）──────────────
# 与 §4.1-4.17 中 Codex 的数据/状态卡同属"业务输出"层，但这 3 张靠文案分层和
# 信息凝练取胜，复用同一组 helper（_business_card / _md / _field_list /
# _truth_status_template）保持视觉一致。


def build_email_draft_card(
    *,
    email_subject: str,
    sendable_body: str,
    truth_status: str = "已核验",
    modify_suggestions: list[str] | None = None,
    pending_items: list[str] | None = None,
    sources: list[str] | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    """邮件草稿卡 · §4.4 · 客户回复 / 内部通知 / 节日问候邮件。

    设计取舍：
    - 「可直接发送版」最显眼，员工可直接复制 → 卡片顶部位置
    - 「修改建议」「待确认项」是辅助层，正常字号但放在分隔线下
    - 收尾说明"一键发送在开发中"避免员工误解为已对接公司邮箱
    """
    elements = [
        _md("**邮件草稿**", "heading_2"),
        _md(f"**主题**：{email_subject}"),
        _md(f"**真实性状态**：{truth_status}"),
        {"tag": "hr"},
        _md("**可直接发送版**"),
        _md(sendable_body),
    ]
    if modify_suggestions:
        elements.extend(
            [
                {"tag": "hr"},
                _md(f"**修改建议**\n{_field_list(modify_suggestions)}"),
            ]
        )
    if pending_items:
        elements.append(
            _md(f"**待确认项**\n{_field_list(pending_items)}"),
        )
    if sources:
        elements.append(
            _md(f"**资料来源**\n{_field_list(sources)}", "notation"),
        )
    if task_id:
        elements.append(_md(f"**task_id**：`#{task_id}`", "notation"))
    elements.extend(
        [
            _button_row(
                [
                    {
                        "text": "📋 复制正文",
                        "value": {"action": "noop"},
                        "type": "primary",
                    },
                    {
                        "text": "✏️ 我要改",
                        "value": {"action": "noop"},
                        "type": "default",
                    },
                ]
            ),
            _md(
                "当前能力：起草草稿，请你 review 后复制到企业邮箱发送；一键发送对接中。",
                "notation",
            ),
        ]
    )
    return _business_card(
        title="✉️ 邮件草稿",
        template=_truth_status_template(truth_status),
        elements=elements,
    )


def build_copy_draft_card(
    *,
    copy_title: str,
    main_version: str,
    alternatives: list[str] | None = None,
    usage_hint: str = "",
    truth_status: str = "已核验",
    sources: list[str] | None = None,
    target_channel: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """普通文案卡 · §4.5 · 公告 / 推文 / 话术 / 宣传文案 / 营销文案。

    设计取舍：
    - 「主版本」最显眼，「备选版本」自动编号但放分隔线下（避免抢戏）
    - target_channel（朋友圈 / 小红书 / 微信群）影响后续文案长度认知
    - 使用提醒用 💡 前缀避免被当成正文
    """
    elements = [
        _md("**文案草稿**", "heading_2"),
        _md(f"**标题**：{copy_title}"),
        _md(f"**真实性状态**：{truth_status}"),
    ]
    if target_channel:
        elements.append(_md(f"**适用渠道**：{target_channel}"))
    elements.extend(
        [
            {"tag": "hr"},
            _md("**主版本**"),
            _md(main_version),
        ]
    )
    if alternatives:
        alt_md = "\n\n".join(
            f"**备选 {idx}**\n{alt}" for idx, alt in enumerate(alternatives, start=1)
        )
        elements.extend(
            [
                {"tag": "hr"},
                _md(alt_md),
            ]
        )
    if usage_hint:
        elements.append(_md(f"💡 **使用提醒**：{usage_hint}", "notation"))
    if sources:
        elements.append(
            _md(f"**资料来源**\n{_field_list(sources)}", "notation"),
        )
    if task_id:
        elements.append(_md(f"**task_id**：`#{task_id}`", "notation"))
    elements.append(
        _button_row(
            [
                {
                    "text": "📋 复制主版本",
                    "value": {"action": "noop"},
                    "type": "primary",
                },
            ]
        ),
    )
    return _business_card(
        title="📝 文案草稿",
        template=_truth_status_template(truth_status),
        elements=elements,
    )


def build_boss_quicklook_card(
    *,
    task_title: str,
    one_line_conclusion: str,
    usability: str,
    risks: list[str] | None = None,
    next_actions: list[str] | None = None,
    detail_summary: str = "",
    task_id: str = "",
) -> dict[str, Any]:
    """老板快看卡 · §4.16 · 凝练结论，不展开过程。

    设计取舍：
    - 语气稳重凝练（对员工温柔；对老板务实）
    - 「一句话结论」+「是否可直接用」+「风险」+「下一步」是 4 段固定结构
    - usability 映射颜色 + emoji 标签（可直接用/需要确认/不建议直接用）
    - detail_summary 是 fallback 摘要，notation 字号低调展示
    """
    usability_map = {
        "可直接用": ("✅ 可直接用", "green"),
        "需要确认": ("⚠️ 需要确认", "orange"),
        "不建议直接用": ("🛑 不建议直接用", "red"),
    }
    usability_label, template = usability_map.get(usability, (usability or "-", "grey"))
    elements = [
        _md(f"**{task_title}**", "heading_2"),
        _md(f"**一句话结论**\n{one_line_conclusion}"),
        _md(f"**是否可直接用**：{usability_label}"),
    ]
    if risks:
        elements.append(_md(f"**风险**\n{_field_list(risks)}"))
    if next_actions:
        elements.append(_md(f"**下一步**\n{_field_list(next_actions)}"))
    if detail_summary:
        elements.extend(
            [
                {"tag": "hr"},
                _md("**摘要**", "notation"),
                _md(detail_summary, "notation"),
            ]
        )
    if task_id:
        elements.append(_md(f"**task_id**：`#{task_id}`", "notation"))
    elements.append(
        _button_row(
            [
                {
                    "text": "📄 看详细分析",
                    "value": {"action": "noop"},
                    "type": "default",
                },
                {
                    "text": "✅ 老板已确认",
                    "value": {"action": "noop"},
                    "type": "primary",
                },
            ]
        ),
    )
    return _business_card(
        title="👀 老板快看",
        template=template,
        elements=elements,
    )
