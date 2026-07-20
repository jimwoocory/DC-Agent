"""Feishu cards for the deterministic assistant workbench."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from dc_engines.codex_capability import list_codex_capabilities

SOURCE = "assistant_workbench"

TASK_LABELS = {
    "copy": "写文案 / 方案",
    "image": "生成图片",
    "video": "生成视频",
    "research": "查资料 / 分析",
    "file": "处理文件",
    "quotation": "筹备组物料报价",
    "ai_cdr": "AI 转 CDR",
    "codex": "Codex 高级处理",
}

TASK_REQUIRED_FIELDS = {
    "copy": "task_request",
    "image": "visual_prompt",
    "video": "video_prompt",
    "research": "research_question",
    "file": "file_goal",
    "quotation": "project_name",
    "ai_cdr": "source_name",
    "codex": "task_request",
}

TASK_FIELD_LABELS = {
    "copy_type": "写作类型",
    "task_request": "核心要求",
    "audience": "受众 / 发布渠道",
    "source_material": "已有资料",
    "output_requirement": "交付规格",
    "image_template": "图片模板",
    "visual_prompt": "画面描述",
    "reference_source": "参考素材",
    "aspect_ratio": "画面比例",
    "image_count": "输出数量",
    "quality": "生成质量",
    "generation_mode": "生成方式",
    "video_prompt": "镜头描述",
    "duration": "视频时长",
    "video_quality": "视频质量",
    "research_question": "研究问题",
    "research_scope": "范围 / 时间",
    "source_requirement": "来源要求",
    "analysis_mode": "AI 分析方法",
    "research_depth": "分析深度",
    "source_policy": "资料使用范围",
    "output_format": "内容 / 输出格式",
    "delivery_format": "交付文件",
    "file_goal": "处理目标",
    "file_source": "文件来源",
    "operation": "处理方式",
    "processing_scope": "处理范围",
    "preserve_layout": "原结构处理",
    "source_language": "源语言",
    "target_language": "目标语言",
    "direction_mode": "翻译方向",
    "translation_style": "翻译方式",
    "professional_domain": "专业领域",
    "output_mode": "译文呈现",
    "glossary": "术语表",
    "translation_memory": "参考译法",
    "reasoning_depth": "分析深度",
    "model_choice": "模型选择",
    "image_format": "图片格式",
    "camera_motion": "镜头运动",
    "audio_mode": "声音",
    "project_name": "项目名称",
    "client_name": "客户 / 品牌",
    "delivery_date": "交付日期",
    "validity_days": "报价有效期",
    "quotation_items": "物料明细",
    "transport_fee": "运输费",
    "installation_fee": "安装费",
    "rush_fee": "加急费",
    "loss_rate": "损耗率",
    "loss_fee": "损耗费",
    "profit_rate": "利润率",
    "profit_fee": "利润额",
    "tax_rate": "税率",
    "tax_fee": "税费",
    "priced_items_subtotal": "已定价物料小计",
    "pre_tax_total": "未税合计",
    "grand_total": "含税合计",
    "pending_inquiry_count": "待询价项",
    "quotation_status": "报价状态",
    "quotation_notes": "报价备注",
}

TASK_VALUE_LABELS = {
    "official_account": "公众号推文",
    "activity_plan": "活动方案",
    "report": "汇报材料",
    "notice": "通知 / 公告",
    "social_copy": "社交媒体文案",
    "xiaohongshu_note": "小红书图文笔记",
    "douyin_script": "抖音短视频脚本",
    "xigua_script": "西瓜视频长视频脚本",
    "custom": "自定义",
    "brand_visual": "品牌主视觉",
    "infographic": "信息图",
    "social_cover": "社媒封面",
    "video_first_frame": "视频首帧",
    "none": "无参考素材",
    "recent_generated": "最近生成的图片",
    "recent_attachment": "最近生成的图片",
    "url": "图片链接（写在画面描述中）",
    "1": "1 张",
    "2": "2 张",
    "4": "4 张",
    "low": "快速草图",
    "medium": "标准质量",
    "high": "高质量",
    "text_to_video": "文生视频",
    "image_to_video": "图生视频",
    "5": "5 秒",
    "10": "10 秒",
    "15": "15 秒",
    "720p": "720p",
    "1080p": "1080p",
    "short_answer": "简短结论",
    "comparison": "对比表",
    "analysis_memo": "分析备忘录",
    "recommendation": "建议方案",
    "evidence_review": "证据查证",
    "synthesis": "综合归纳",
    "quick": "快速判断",
    "standard": "标准分析",
    "deep": "深度研究",
    "mixed": "内部资料 + 公开来源",
    "uploaded_only": "仅使用已上传资料",
    "public_only": "仅检索公开来源",
    "result_card": "飞书结果卡片",
    "docx": "Word 文档",
    "xlsx": "Excel 表格",
    "pdf": "PDF 文件",
    "txt": "文本文件",
    "feishu_doc": "飞书云文档",
    "summarize": "总结提炼",
    "extract": "信息提取",
    "rewrite": "改写润色",
    "convert": "格式转换",
    "compare": "文件对比",
    "organize": "分类整理",
    "translate": "翻译",
    "restructure": "按任务重新组织",
    "preserve": "尽量保留原结构",
    "business_clean": "商务简洁排版",
    "one_way": "单向翻译",
    "bidirectional": "双向互译",
    "faithful": "忠实翻译",
    "professional": "专业润色",
    "target_only": "仅译文",
    "bilingual": "双语对照",
    "general": "通用",
    "legal": "法律",
    "finance": "金融",
    "technology": "科技",
    "medical": "医疗",
    "marketing": "营销",
    "manufacturing": "制造",
    "Chinese": "中文",
    "English": "英文",
    "Japanese": "日文",
    "Korean": "韩文",
    "French": "法文",
    "German": "德文",
    "Spanish": "西班牙文",
    "Portuguese": "葡萄牙文",
    "Russian": "俄文",
    "Arabic": "阿拉伯文",
    "Thai": "泰文",
    "Vietnamese": "越南文",
    "Indonesian": "印尼文",
    "Italian": "意大利文",
    "codex_high": "深度分析",
    "codex_xhigh": "超深分析",
    "auto": "智能选择",
    "fast": "快速模式",
    "balanced": "均衡模式",
    "quality_first": "质量优先",
    "png": "PNG",
    "webp": "WEBP",
    "jpeg": "JPEG",
    "fixed": "固定镜头",
    "free": "自由运镜",
    "on": "生成声音",
    "off": "无声音",
}


def build_assistant_workspace_app_link(
    *,
    app_id: str,
    workspace_url: str,
    mode: str = "",
    reload: bool = False,
) -> str:
    """Build a Feishu sidebar AppLink for one trusted H5 workspace.

    Args:
        app_id: Feishu application ID used to open the web app.
        workspace_url: Absolute HTTP(S) capability URL for the workspace.
        mode: Optional H5 mode appended to the workspace query string.
        reload: Whether Feishu must reload the embedded page before opening it.

    Returns:
        A Feishu AppLink, or an empty string when required inputs are invalid.
    """
    normalized_app_id = str(app_id or "").strip()
    normalized_url = str(workspace_url or "").strip()
    parsed = urlsplit(normalized_url)
    if (
        not normalized_app_id
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        return ""
    if mode:
        normalized_url += ("&" if "?" in normalized_url else "?") + (
            f"mode={quote(str(mode), safe='')}"
        )
    return (
        "https://applink.feishu.cn/client/web_app/open"
        f"?appId={quote(normalized_app_id, safe='')}&mode=sidebar-semi"
        f"&reload={'true' if reload else 'false'}"
        f"&lk_target_url={quote(normalized_url, safe='')}"
    )


def _workspace_open_behavior(web_url: str, app_link: str) -> dict[str, Any]:
    """Build one multi-platform H5 navigation behavior.

    Args:
        web_url: Direct capability URL used by Feishu web browsers.
        app_link: Feishu AppLink used by native desktop and mobile clients.

    Returns:
        Card JSON 2.0 open-url behavior with explicit platform targets.
    """
    return {
        "type": "open_url",
        "default_url": web_url,
        "pc_url": app_link,
        "android_url": app_link,
        "ios_url": app_link,
    }


def _button(
    text: str,
    action: str,
    *,
    button_type: str = "default",
    task_type: str = "",
    width: str = "fill",
    extra_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a callback button shared by all workbench cards.

    Args:
        text: User-facing button label.
        action: Deterministic workbench action identifier.
        button_type: Feishu button visual style.
        task_type: Optional task category carried in the callback.
        width: Button width accepted by Feishu Card JSON 2.0.
        extra_value: Additional deterministic callback fields.

    Returns:
        A Feishu Card JSON 2.0 button component.
    """
    value = {"source": SOURCE, "action": action}
    if task_type:
        value["task_type"] = task_type
    if extra_value:
        value.update(extra_value)
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "width": width,
        "behaviors": [{"type": "callback", "value": value}],
    }


def _text_input(
    name: str,
    label: str,
    placeholder: str,
    *,
    required: bool = False,
    multiline: bool = False,
    max_length: int = 200,
) -> dict[str, Any]:
    """Build a Feishu Card JSON 2.0 text input.

    Args:
        name: Form field key returned by the callback.
        label: User-facing field label.
        placeholder: Task-specific example or guidance.
        required: Whether Feishu should require a value before submission.
        multiline: Whether the field should grow as multiline text.
        max_length: Maximum accepted text length.

    Returns:
        A card input component.
    """
    field: dict[str, Any] = {
        "tag": "input",
        "name": name,
        "required": required,
        "label": {"tag": "plain_text", "content": label},
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "max_length": max_length,
        "width": "fill",
    }
    if multiline:
        field.update(
            {
                "input_type": "multiline_text",
                "rows": 4,
                "max_rows": 8,
                "auto_resize": True,
            }
        )
    return field


def _select(
    name: str,
    label: str,
    placeholder: str,
    options: tuple[tuple[str, str], ...],
    *,
    required: bool = False,
) -> list[dict[str, Any]]:
    """Build a static select field for a task form.

    Args:
        name: Form field key returned by the callback.
        label: User-facing field label.
        placeholder: Prompt shown before a choice is made.
        options: Pairs of display text and stable value.
        required: Whether Feishu should require a choice.

    Returns:
        A visible field label followed by a card static-select component.
    """
    return [
        {
            "tag": "markdown",
            "content": f"**{label}**"
            + (" <font color='red'>*</font>" if required else ""),
        },
        {
            "tag": "select_static",
            "name": name,
            "required": required,
            "placeholder": {"tag": "plain_text", "content": placeholder},
            "options": [
                {
                    "text": {"tag": "plain_text", "content": text},
                    "value": value,
                }
                for text, value in options
            ],
            "width": "fill",
        },
    ]


def _button_row(*buttons: dict[str, Any]) -> dict[str, Any]:
    """Arrange callback buttons in an evenly weighted row.

    Args:
        buttons: Button components to display in the row.

    Returns:
        A Feishu column-set component.
    """
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def _card(
    title: str,
    subtitle: str,
    elements: list[dict[str, Any]],
    *,
    template: str = "blue",
) -> dict[str, Any]:
    """Wrap workbench elements in a Card JSON 2.0 envelope.

    Args:
        title: Main card title.
        subtitle: Short explanation below the title.
        elements: Card body elements.
        template: Feishu header color template.

    Returns:
        A complete Feishu Card JSON 2.0 payload.
    """
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "summary": {"content": title},
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
        },
        "body": {
            "padding": "16px",
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def build_codex_toolbox_card() -> dict[str, Any]:
    """Build the user-visible Codex Advanced Executor toolbox.

    Returns:
        A deterministic capability card with explicit user execution entries.
    """
    capability_lines = []
    for item in list_codex_capabilities():
        authority = "用户确认" if "user" in item.authorized_by else "系统主控"
        capability_lines.append(f"**{item.name}** · {authority}\n{item.description}")
    return _card(
        "Codex 高级工具",
        "复杂任务的高级执行器，不拥有定时调度或系统主控权",
        [
            {"tag": "markdown", "content": "\n\n".join(capability_lines)},
            _button_row(
                _button(
                    "发起深度分析",
                    "select_task_type",
                    task_type="codex",
                    button_type="primary",
                ),
                _button("生成图片", "select_task_type", task_type="image"),
            ),
            _button("查看任务", "open_task_list", width="default"),
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>打开与填写表单不会调用模型；只有确认执行后"
                    "才会调用相应工具。</font>"
                ),
                "text_size": "notation",
            },
        ],
        template="indigo",
    )


def build_assistant_attachment_demo_card(
    *,
    app_id: str,
    upload_url: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the single-entry attachment intake card.

    Args:
        app_id: Feishu application ID used to open the authenticated web app.
        upload_url: Capability-token-scoped H5 upload page URL.
        attachments: Sanitized local-file or cloud-document metadata.

    Returns:
        A Card JSON 2.0 payload for the initial or attached-file state.
    """
    attachment_rows = list(attachments or [])
    intake_app_link = (
        "https://applink.feishu.cn/client/web_app/open"
        f"?appId={quote(app_id, safe='')}&mode=sidebar-semi"
        f"&reload=false&lk_target_url={quote(upload_url, safe='')}"
    )
    intake_button = {
        "tag": "button",
        "type": "primary",
        "width": "fill",
        "text": {
            "tag": "plain_text",
            "content": "继续添加资料" if attachment_rows else "添加资料",
        },
        "behaviors": [_workspace_open_behavior(upload_url, intake_app_link)],
    }
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                "**任务资料（选填）**\n"
                "<font color='grey'>本地文件和飞书云文档统一从一个页面添加。</font>"
            ),
        },
        intake_button,
        {
            "tag": "markdown",
            "content": (
                "<font color='grey'>打开后选择一种资料来源；不会发送新的聊天消息。"
                "</font>"
            ),
            "text_size": "notation",
        },
    ]
    if attachment_rows:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"**已关联资料 · 共 {len(attachment_rows)} 项资料**",
                },
            ]
        )
        for item in attachment_rows[:5]:
            filename = str(item.get("filename") or "未命名附件")[:120]
            if str(item.get("type") or "") == "cloud_doc":
                detail = "飞书云文档"
                icon = "🔗"
            else:
                size_bytes = max(0, int(item.get("size_bytes") or 0))
                if size_bytes >= 1024 * 1024:
                    detail = f"{size_bytes / (1024 * 1024):.1f} MB"
                elif size_bytes >= 1024:
                    detail = f"{size_bytes / 1024:.1f} KB"
                else:
                    detail = f"{size_bytes} B"
                icon = "📄"
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"{icon}  **{filename}**\n"
                        f"<font color='grey'>{detail} · 已关联到本次任务</font>"
                    ),
                }
            )
        if len(attachment_rows) > 5:
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"<font color='grey'>另有 {len(attachment_rows) - 5} 项资料已关联。"
                        "</font>"
                    ),
                }
            )
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "<font color='green'>✓ 资料已就绪，确认任务时会保留真实关联。"
                    "</font>"
                ),
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "<font color='grey'>当前尚未关联附件。</font>",
            }
        )
    return _card(
        "写文案 / 方案",
        "统一资料入口",
        elements,
        template="turquoise",
    )


def build_assistant_task_workspace_card(
    *,
    task_type: str,
    app_id: str,
    workspace_url: str,
) -> dict[str, Any]:
    """Build the recoverable entry card for the fixed Feishu task sidebar.

    Args:
        task_type: Selected assistant task category.
        app_id: Feishu application ID used to open the web app.
        workspace_url: Capability-token-scoped unified workspace URL.

    Returns:
        A compact Card JSON 2.0 payload with one multi-platform action and one
        callback that replaces an expired capability URL in the same card.
    """
    normalized_type = task_type if task_type in TASK_LABELS else "copy"
    app_link = build_assistant_workspace_app_link(
        app_id=app_id,
        workspace_url=workspace_url,
    )
    entry_copy = {
        "research": (
            "检索、查证与分析",
            "**研究分析工作台**\n"
            "界定问题 · 核验来源 · 对比分析 · 形成建议\n"
            "<font color='grey'>研究问题 → 参考资料 → 分析交付</font>",
        ),
        "file": (
            "总结、提取、改写、转换与对比",
            "**文件处理工作台**\n"
            "选择真实文件 · 指定处理动作 · 定义输出格式\n"
            "<font color='grey'>处理目标 → 选择文件 → 输出规则</font>",
        ),
        "quotation": (
            "项目估价与合作公司价格入库",
            "**报价工具首页**\n"
            "项目估价测算 · 新增合作公司价格\n"
            "<font color='grey'>员工可手工录入单项 / 多项价格，或上传原始报价资料。</font>",
        ),
        "ai_cdr": (
            "批量上传、颜色校准与转换进度",
            "**AI 转 CDR 工具**\n"
            "批量 AI · CMYK 数值校准 · ICC 留档 · 逐份非空验收\n"
            "<font color='grey'>mini4 按队列顺序转换，页面实时显示百分比和耗时。</font>",
        ),
    }
    subtitle, workspace_intro = entry_copy.get(
        normalized_type,
        (
            "在右侧完成提词、资料和生成设置",
            "**统一任务工作台**\n"
            "<font color='grey'>先整理任务，保存后立即开始生成并直接交付。</font>",
        ),
    )
    return _card(
        TASK_LABELS[normalized_type],
        subtitle,
        [
            {
                "tag": "markdown",
                "content": workspace_intro,
            },
            {
                "tag": "button",
                "type": "primary",
                "width": "fill",
                "text": {
                    "tag": "plain_text",
                    "content": (
                        "打开报价工具"
                        if normalized_type == "quotation"
                        else (
                            "打开 AI 转 CDR 工具"
                            if normalized_type == "ai_cdr"
                            else "打开工作台"
                        )
                    ),
                },
                "behaviors": [_workspace_open_behavior(workspace_url, app_link)],
            },
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>如果安全链接已过期，直接刷新入口即可，"
                    "不用重新查找菜单。</font>"
                ),
                "text_size": "notation",
            },
            _button(
                "刷新安全入口",
                "select_task_type",
                task_type=normalized_type,
            ),
        ],
        template="turquoise",
    )


def build_assistant_task_submitted_card(
    *,
    task_type: str,
    task_data: dict[str, Any],
    app_id: str,
    workspace_url: str,
    deliverables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the non-blocking status card shown after one workspace save.

    Args:
        task_type: Selected assistant task category.
        task_data: Sanitized task values already accepted by the server.
        app_id: Feishu application ID used to reopen the sidebar.
        workspace_url: Capability-scoped workspace URL for another iteration.
        deliverables: Optional capability-scoped generated-file download URLs.

    Returns:
        A Card JSON 2.0 status card with no approval or finalization action.
    """
    normalized_type = task_type if task_type in TASK_LABELS else "copy"
    workspace_mode = ""
    if normalized_type in {"copy", "image", "video"}:
        workspace_mode = "revise"
    elif normalized_type == "ai_cdr":
        workspace_mode = "progress"
    elif normalized_type == "quotation":
        workspace_mode = "recalculate"
    app_link = build_assistant_workspace_app_link(
        app_id=app_id,
        workspace_url=workspace_url,
        mode=workspace_mode,
        reload=True,
    )
    web_url = workspace_url
    if workspace_mode:
        web_url += ("&" if "?" in web_url else "?") + (
            f"mode={quote(workspace_mode, safe='')}"
        )
    if normalized_type == "quotation":
        project_name = str(task_data.get("project_name") or "未命名项目")[:120]
        client_name = str(task_data.get("client_name") or "")[:80]
        validity_days = str(task_data.get("validity_days") or "15")[:3]
        estimate_status = str(task_data.get("quotation_status") or "估价方案")[:20]
        summary_lines = [
            f"**项目**：{project_name}",
            *([f"**客户 / 品牌**：{client_name}"] if client_name else []),
            f"**估价金额**：¥{str(task_data.get('grand_total') or '0.00')}",
            f"**有效期**：{validity_days} 天",
            f"**状态**：{estimate_status}",
        ]
        market_downloads = []
        for key, label in (
            ("market_docx", "下载 Word 估价"),
            ("market_pdf", "下载 PDF 估价"),
        ):
            download_url = str((deliverables or {}).get(key) or "")
            if not download_url.startswith(("http://", "https://")):
                continue
            market_downloads.append(
                {
                    "tag": "button",
                    "type": "default",
                    "width": "fill",
                    "text": {"tag": "plain_text", "content": label},
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": download_url,
                            "pc_url": download_url,
                            "android_url": download_url,
                            "ios_url": download_url,
                        }
                    ],
                }
            )
        return _card(
            "市场部估价结果",
            "筹备组测算结果 · 不含内部成本明细",
            [
                {"tag": "markdown", "content": "\n".join(summary_lines)},
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>本结果用于市场方案估价；正式采购金额以最终复核为准。</font>"
                    ),
                    "text_size": "notation",
                },
                *market_downloads,
                {
                    "tag": "button",
                    "type": "default",
                    "width": "fill",
                    "text": {"tag": "plain_text", "content": "返回筹备组重新测算"},
                    "behaviors": [_workspace_open_behavior(web_url, app_link)],
                },
                _button(
                    "刷新报价入口",
                    "select_task_type",
                    task_type=normalized_type,
                ),
            ],
            template="green",
        )
    summary_lines = []
    for key, value in task_data.items():
        if not value or key in {"model_choice", "quotation_items"}:
            continue
        label = TASK_FIELD_LABELS.get(key, key)
        display = TASK_VALUE_LABELS.get(str(value), str(value))
        summary_lines.append(f"**{label}**：{display[:180]}")
        if len(summary_lines) == 4:
            break
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": "\n".join(summary_lines)
            or f"**任务类型**：{TASK_LABELS[normalized_type]}",
        }
    ]
    if normalized_type in {"copy", "image", "video"}:
        elements.extend(
            [
                {
                    "tag": "markdown",
                    "content": (
                        "**当前进度**：机器校验 → 生成结果 → 直接发送\n"
                        "<font color='grey'>打开 H5 不会自动执行；系统会回填上次资料，"
                        "修改完成后请在最后一步点击“确认修改并再次生成”。</font>"
                    ),
                },
                {
                    "tag": "button",
                    "type": "default",
                    "width": "fill",
                    "text": {
                        "tag": "plain_text",
                        "content": "打开 H5 修改资料",
                    },
                    "behaviors": [_workspace_open_behavior(web_url, app_link)],
                },
            ]
        )
    elif normalized_type == "ai_cdr":
        elements.extend(
            [
                {
                    "tag": "markdown",
                    "content": (
                        "**当前进度**：已进入 mini4 顺序转换队列\n"
                        "<font color='grey'>重新打开页面只查看转换进度，不会重复提交任务。</font>"
                    ),
                },
                {
                    "tag": "button",
                    "type": "default",
                    "width": "fill",
                    "text": {"tag": "plain_text", "content": "查看转换进度"},
                    "behaviors": [_workspace_open_behavior(web_url, app_link)],
                },
            ]
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "**当前进度**：机器校验 → 执行任务 → 直接交付\n"
                    "<font color='grey'>当前任务会按已提交资料完成；如需新任务，请从对应菜单重新发起。</font>"
                ),
            }
        )
    elements.append(
        _button(
            "刷新安全入口",
            "select_task_type",
            task_type=normalized_type,
        )
    )
    subtitle = (
        f"{TASK_LABELS[normalized_type]} · 后台生成并直接交付"
        if normalized_type in {"copy", "image", "video"}
        else f"{TASK_LABELS[normalized_type]} · 后台执行并直接交付"
    )
    return _card(
        "任务已提交",
        subtitle,
        elements,
        template="blue",
    )


def _task_form_elements(task_type: str) -> list[dict[str, Any]]:
    """Build the dedicated fields for one task type.

    This function intentionally keeps each task's information model visible in
    one place. The branches differ in both field names and user guidance, which
    prevents the previous generic-form regression.

    Args:
        task_type: Normalized task type.

    Returns:
        Task-specific form components excluding the submit button.

    Raises:
        ValueError: If the task type has no dedicated form.
    """
    if task_type == "copy":
        return [
            *_select(
                "copy_type",
                "写作类型",
                "选择要交付的内容",
                (
                    ("公众号推文", "official_account"),
                    ("活动方案", "activity_plan"),
                    ("汇报材料", "report"),
                    ("通知 / 公告", "notice"),
                    ("社交媒体文案", "social_copy"),
                    ("小红书图文笔记", "xiaohongshu_note"),
                    ("抖音短视频脚本", "douyin_script"),
                    ("西瓜视频长视频脚本", "xigua_script"),
                    ("自定义", "custom"),
                ),
                required=True,
            ),
            _text_input(
                "task_request",
                "核心要求",
                "例如：围绕夏季新品写一篇面向老客户的公众号推文",
                required=True,
                multiline=True,
                max_length=800,
            ),
            _text_input(
                "audience",
                "受众 / 发布渠道",
                "例如：老客户；微信公众号",
            ),
            _text_input(
                "source_material",
                "已有资料",
                "最近附件、飞书文档链接，或需要采用的事实要点",
                max_length=400,
            ),
            _text_input(
                "output_requirement",
                "交付规格（选填）",
                "篇幅、语气、结构、截止时间等",
                max_length=300,
            ),
        ]
    if task_type == "image":
        return [
            *_select(
                "image_template",
                "图片模板",
                "从常见用途开始",
                (
                    ("品牌主视觉", "brand_visual"),
                    ("信息图", "infographic"),
                    ("社媒封面", "social_cover"),
                    ("视频首帧", "video_first_frame"),
                    ("自定义", "custom"),
                ),
                required=True,
            ),
            _text_input(
                "visual_prompt",
                "画面描述",
                "描述主体、用途、构图、光线、材质、文字区域和不要出现的元素",
                required=True,
                multiline=True,
                max_length=800,
            ),
            *_select(
                "reference_source",
                "参考图（选填）",
                "选择可用的参考来源",
                (
                    ("无参考图", "none"),
                    ("最近生成的图片", "recent_generated"),
                    ("图片链接（写在画面描述中）", "url"),
                ),
            ),
            *_select(
                "aspect_ratio",
                "画面比例",
                "选择交付比例",
                (("1:1", "1:1"), ("3:4", "3:4"), ("9:16", "9:16"), ("16:9", "16:9")),
                required=True,
            ),
            *_select(
                "image_count",
                "输出数量",
                "选择图片数量",
                (("1 张", "1"), ("2 张", "2"), ("4 张", "4")),
                required=True,
            ),
            *_select(
                "quality",
                "生成质量",
                "选择速度与质量",
                (("快速草图", "low"), ("标准质量", "medium"), ("高质量", "high")),
                required=True,
            ),
        ]
    if task_type == "video":
        return [
            *_select(
                "generation_mode",
                "生成方式",
                "选择视频起点",
                (
                    ("文生视频", "text_to_video"),
                    ("最近生成图片转视频", "image_to_video"),
                ),
                required=True,
            ),
            _text_input(
                "video_prompt",
                "镜头描述",
                "描述主体动作、镜头运动、节奏和场景变化",
                required=True,
                multiline=True,
                max_length=800,
            ),
            *_select(
                "reference_source",
                "参考素材（选填）",
                "图生视频需有最近生成的图片",
                (("无参考素材", "none"), ("最近生成的图片", "recent_generated")),
            ),
            *_select(
                "duration",
                "视频时长",
                "选择目标时长",
                (("5 秒", "5"), ("10 秒", "10"), ("15 秒", "15")),
                required=True,
            ),
            *_select(
                "aspect_ratio",
                "视频比例",
                "选择交付比例",
                (("9:16", "9:16"), ("16:9", "16:9"), ("1:1", "1:1")),
                required=True,
            ),
            *_select(
                "video_quality",
                "视频质量",
                "选择输出质量",
                (("720p", "720p"), ("1080p", "1080p")),
                required=True,
            ),
        ]
    if task_type == "research":
        return [
            _text_input(
                "research_question",
                "研究问题",
                "例如：比较三个本地生活竞品的定位，并给出进入建议",
                required=True,
                multiline=True,
                max_length=800,
            ),
            _text_input(
                "research_scope",
                "范围 / 时间",
                "地区、行业、对象和时间范围",
                max_length=300,
            ),
            _text_input(
                "source_requirement",
                "来源要求",
                "指定飞书资料、公开来源、数据口径或必须引用的文件",
                max_length=400,
            ),
            *_select(
                "output_format",
                "交付形式",
                "选择最适合决策的格式",
                (
                    ("简短结论", "short_answer"),
                    ("对比表", "comparison"),
                    ("分析备忘录", "analysis_memo"),
                    ("建议方案", "recommendation"),
                ),
                required=True,
            ),
            _text_input(
                "output_requirement",
                "补充要求（选填）",
                "深度、篇幅、截止时间和证据要求",
                max_length=300,
            ),
        ]
    if task_type == "file":
        return [
            _text_input(
                "file_goal",
                "处理目标",
                "例如：提取合同中的金额、日期、责任和风险条款",
                required=True,
                multiline=True,
                max_length=800,
            ),
            _text_input(
                "file_source",
                "文件来源",
                "最近附件或飞书文档链接",
                required=True,
                max_length=400,
            ),
            *_select(
                "operation",
                "处理方式",
                "选择主要操作",
                (
                    ("总结提炼", "summarize"),
                    ("信息提取", "extract"),
                    ("改写润色", "rewrite"),
                    ("格式转换", "convert"),
                    ("文件对比", "compare"),
                    ("分类整理", "organize"),
                ),
                required=True,
            ),
            _text_input(
                "output_format",
                "输出格式",
                "例如：Word、Excel、Markdown 或飞书文档",
                required=True,
            ),
            _text_input(
                "output_requirement",
                "补充要求（选填）",
                "命名、版式、截止时间或保留规则",
                max_length=300,
            ),
        ]
    if task_type == "codex":
        return [
            _text_input(
                "task_request",
                "需要深度处理的问题",
                "例如：复核这份方案的关键假设、风险和可执行建议",
                required=True,
                multiline=True,
                max_length=800,
            ),
            *_select(
                "reasoning_depth",
                "分析深度",
                "选择投入程度",
                (("深度分析", "codex_high"), ("超深分析", "codex_xhigh")),
                required=True,
            ),
            _text_input(
                "source_material",
                "依据材料（选填）",
                "飞书文档链接、附件说明或必须采用的事实",
                max_length=400,
            ),
            _text_input(
                "output_requirement",
                "交付要求（选填）",
                "例如：先给结论，再列证据、风险与下一步",
                max_length=300,
            ),
        ]
    raise ValueError(f"unsupported assistant task type: {task_type}")


def build_assistant_task_intake_card(task_type: str = "") -> dict[str, Any]:
    """Build the task-type picker or the selected task intake form.

    Args:
        task_type: Selected task category. Empty renders the category picker.

    Returns:
        A task picker or task detail form card.
    """
    if task_type == "media":
        return _card(
            "选择创作类型",
            "图片和视频使用不同参数，不再共用一张表单",
            [
                _button_row(
                    _button("生成图片", "select_task_type", task_type="image"),
                    _button("生成视频", "select_task_type", task_type="video"),
                ),
                _button("查看任务", "open_task_list", width="default"),
            ],
            template="turquoise",
        )
    if not task_type:
        return _card(
            "选择任务类型",
            "长期统一入口 · 每次点击都会生成新的安全工作台",
            [
                {
                    "tag": "markdown",
                    "content": "**你现在想完成什么？**",
                },
                _button_row(
                    _button("写文案 / 方案", "select_task_type", task_type="copy"),
                    _button("查资料 / 分析", "select_task_type", task_type="research"),
                ),
                _button_row(
                    _button("生成图片", "select_task_type", task_type="image"),
                    _button("生成视频", "select_task_type", task_type="video"),
                ),
                _button_row(
                    _button("处理文件", "select_task_type", task_type="file"),
                    _button(
                        "筹备组物料报价",
                        "select_task_type",
                        task_type="quotation",
                    ),
                ),
                _button("AI 转 CDR", "select_task_type", task_type="ai_cdr"),
            ],
            template="turquoise",
        )

    normalized_type = task_type if task_type in TASK_LABELS else "copy"
    task_label = TASK_LABELS[normalized_type]
    form_elements = _task_form_elements(normalized_type)
    form_elements.append(
        {
            "tag": "button",
            "name": "preview_task",
            "form_action_type": "submit",
            "text": {"tag": "plain_text", "content": "下一步：确认任务"},
            "type": "primary",
            "width": "fill",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "source": SOURCE,
                        "action": "preview_task",
                        "task_type": normalized_type,
                    },
                }
            ],
        }
    )
    return _card(
        task_label,
        "填写后先进入确认页，不会立即调用模型",
        [
            {
                "tag": "form",
                "name": "assistant_task_form",
                "vertical_spacing": "12px",
                "elements": form_elements,
            },
            _button("返回任务选择", "open_task_intake", width="default"),
        ],
        template="turquoise",
    )


def build_assistant_task_confirmation_card(
    task_type: str = "copy",
    task_request: str = "整理一份客户活动方案",
    output_requirement: str = "简洁、可直接发送",
    task_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a task confirmation or deterministic quotation result card.

    Args:
        task_type: Selected task category.
        task_request: User-provided task description.
        output_requirement: Optional delivery constraints.
        task_data: Structured values returned by the dedicated form.

    Returns:
        A result-only quotation card, or a confirmation card whose primary
        button is the LLM execution boundary for generative tasks.
    """
    normalized_type = task_type if task_type in TASK_LABELS else "copy"
    normalized_data = {
        str(key): str(value or "").strip()[
            : 6000 if str(key) == "quotation_items" else 800
        ]
        for key, value in (task_data or {}).items()
        if str(value or "").strip()
    }
    if not normalized_data:
        normalized_data = {
            "task_request": str(task_request or "").strip()[:800],
            "output_requirement": str(output_requirement or "").strip()[:300],
        }
    if normalized_type == "quotation":
        project_name = normalized_data.get("project_name", "未命名项目")
        client_name = normalized_data.get("client_name", "")
        result_lines = [
            f"**项目**：{project_name}",
            *([f"**客户 / 品牌**：{client_name}"] if client_name else []),
            f"**估价金额**：¥{normalized_data.get('grand_total', '0.00')}",
            f"**有效期**：{normalized_data.get('validity_days', '15')} 天",
            f"**状态**：{normalized_data.get('quotation_status', '估价方案')}",
        ]
        return _card(
            "市场部估价结果",
            "只展示价格结果，不展示筹备组内部明细",
            [
                {"tag": "markdown", "content": "\n".join(result_lines)},
                _button(
                    "返回内部测算",
                    "select_task_type",
                    task_type="quotation",
                    width="fill",
                ),
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>本页没有调用 LLM；正式采购金额以供应商复核为准。</font>"
                    ),
                    "text_size": "notation",
                },
            ],
            template="green",
        )
    content_lines = [f"**任务类型**：{TASK_LABELS[normalized_type]}"]
    for key, value in normalized_data.items():
        if not value:
            continue
        label = TASK_FIELD_LABELS.get(key, key)
        display_value = TASK_VALUE_LABELS.get(value, value)
        content_lines.append(f"**{label}**：{display_value}")
    content = "\n".join(content_lines)
    start_value = {
        "source": SOURCE,
        "action": "start_task",
        "task_type": normalized_type,
        "task_data": normalized_data,
    }
    start_button = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "确认并开始执行"},
        "type": "primary",
        "width": "fill",
        "behaviors": [{"type": "callback", "value": start_value}],
        "confirm": {
            "title": {"tag": "plain_text", "content": "开始执行任务？"},
            "text": {
                "tag": "plain_text",
                "content": "确认后才会按任务需要调用模型或工具。",
            },
        },
    }
    return _card(
        "确认后再开始",
        "这是模型调用与费用产生的明确边界",
        [
            {"tag": "markdown", "content": content},
            _button_row(
                start_button,
                _button("返回修改", "select_task_type", task_type=normalized_type),
            ),
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>到目前为止只使用固定路由和卡片，"
                    "没有调用 LLM。</font>"
                ),
                "text_size": "notation",
            },
        ],
        template="orange",
    )


def build_assistant_task_list_card(
    active_tasks: int = 0,
    pending_tasks: int = 0,
    completed_today: int = 0,
    has_recent_task: bool = False,
    tasks: list[dict[str, Any]] | None = None,
    status_filter: str = "",
) -> dict[str, Any]:
    """Build a truthful task-center card without fabricated task records.

    Args:
        active_tasks: Number of currently running tasks.
        pending_tasks: Number of tasks waiting for more material.
        completed_today: Number of tasks completed today.
        has_recent_task: Whether a recent task can be resumed.
        tasks: Truthful task rows loaded from the existing task store.
        status_filter: Optional menu filter: active, pending, or completed.

    Returns:
        The task-center card.
    """
    task_rows = list(tasks or [])
    if task_rows:
        active_tasks = sum(
            str(task.get("status") or "") in {"pending", "in_progress"}
            for task in task_rows
        )
        pending_tasks = sum(
            str(task.get("status") or "") in {"blocked", "review_required"}
            for task in task_rows
        )
        completed_today = sum(
            str(task.get("status") or "") == "completed" for task in task_rows
        )

    title = {
        "active": "进行中任务",
        "pending": "待补充任务",
        "completed": "已完成任务",
    }.get(status_filter, "任务中心")
    subtitle = {
        "active": "查看正在处理和等待执行的任务",
        "pending": "补充资料或完成必要确认",
        "completed": "查看最近完成的真实结果",
    }.get(status_filter, "看进度、补资料、继续或查看结果")
    elements: list[dict[str, Any]] = []
    if not status_filter:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**进行中** {active_tasks}　·　**待补充** {pending_tasks}　·　"
                    f"**已完成** {completed_today}"
                ),
            }
        )
    if task_rows:
        for task in task_rows[:3]:
            status = str(task.get("status_label") or task.get("status") or "未知")
            task_title = str(task.get("title") or "未命名任务")[:80]
            task_id = str(task.get("task_id") or "")
            updated_at = str(task.get("updated_at") or "")[:16]
            detail = f"**{task_title}**\n状态：{status}"
            if updated_at:
                detail += f"　·　更新：{updated_at}"
            elements.append({"tag": "markdown", "content": detail})
            if task_id:
                elements.append(
                    _button(
                        "查看任务",
                        "show_task",
                        width="default",
                        extra_value={"task_id": task_id},
                    )
                )
    else:
        empty_label = {
            "active": "现在没有进行中的任务。",
            "pending": "现在没有等待补充的任务。",
            "completed": "现在没有可展示的已完成任务。",
        }.get(status_filter, "现在没有可展示的任务。")
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**{empty_label}**\n任务中心只展示真实记录，不会生成占位任务。"
                ),
            }
        )
    if has_recent_task and status_filter != "completed":
        recent_task_id = str(task_rows[0].get("task_id") or "") if task_rows else ""
        elements.append(
            _button(
                "继续最近",
                "resume_recent",
                extra_value={"task_id": recent_task_id},
            )
        )
    elements.append(
        _button(
            "刷新",
            "open_task_list",
            width="default",
            extra_value={"status_filter": status_filter},
        )
    )
    elements.append(
        {
            "tag": "markdown",
            "content": (
                "<font color='grey'>新任务请从底部“创作”或“办公”菜单发起；"
                "查看任务不会调用 LLM。</font>"
            ),
            "text_size": "notation",
        }
    )
    return _card(
        title,
        subtitle,
        elements,
        template="indigo",
    )


def build_assistant_task_detail_card(task: dict[str, Any]) -> dict[str, Any]:
    """Build one truthful task detail card without invoking a model.

    Args:
        task: Sanitized task row loaded from the current user's Harness session.

    Returns:
        A deterministic task detail card.
    """
    task_id = str(task.get("task_id") or "")
    title = str(task.get("title") or "未命名任务")[:120]
    status = str(task.get("status_label") or task.get("status") or "未知")
    updated_at = str(task.get("updated_at") or "")[:19]
    lines = [
        f"**任务名称**：{title}",
        f"**当前状态**：{status}",
        f"**任务编号**：`{task_id}`",
    ]
    if updated_at:
        lines.append(f"**最近更新**：{updated_at}")
    return _card(
        "任务详情",
        "直接读取真实任务记录，不调用 LLM",
        [
            {"tag": "markdown", "content": "\n".join(lines)},
            _button("返回任务列表", "open_task_list", width="default"),
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>这里只查看状态；执行、重跑或补充资料会单独确认。"
                    "</font>"
                ),
                "text_size": "notation",
            },
        ],
        template="indigo",
    )


def build_assistant_session_choice_card(
    decision_id: str = "sample-decision",
    state: str = "pending",
) -> dict[str, Any]:
    """Build the post-task conversation-routing card.

    Args:
        decision_id: Durable server-side decision identifier.
        state: Pending or terminal session-decision state.

    Returns:
        A Card JSON 2.0 prompt or terminal projection without task approval.
    """
    if state == "new_conversation":
        return _card(
            "新对话已开启",
            "会话边界已生效",
            [
                {
                    "tag": "markdown",
                    "content": "你的下一条消息将从全新的短期上下文开始。",
                },
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>历史消息仍可查看，但不会自动带入新对话。"
                        "</font>"
                    ),
                    "text_size": "notation",
                },
            ],
            template="green",
        )
    if state in {"continue_current", "superseded_continue"}:
        note = (
            "已有更新的任务结果，本卡已自动关闭。"
            if state == "superseded_continue"
            else "你的下一条消息将继续沿用本次任务的上下文。"
        )
        return _card(
            "已继续当前对话",
            "当前上下文保持不变",
            [
                {"tag": "markdown", "content": note},
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>需要彻底切换主题时，可在后续任务完成后"
                        "选择开启新对话。</font>"
                    ),
                    "text_size": "notation",
                },
            ],
            template="green",
        )
    return _card(
        "这项任务已完成",
        "请选择后续消息使用的上下文",
        [
            {
                "tag": "markdown",
                "content": "接下来要开启新对话，还是继续当前对话？",
            },
            {
                "tag": "markdown",
                "content": (
                    "<font color='grey'>开启新对话后，历史消息仍可查看，"
                    "但后续消息不再自动带入上一项任务的短期上下文。</font>"
                ),
                "text_size": "notation",
            },
            _button_row(
                _button(
                    "开启新对话",
                    "open_new_session",
                    button_type="primary",
                    extra_value={"decision_id": decision_id},
                ),
                _button(
                    "继续当前对话",
                    "continue_current_session",
                    extra_value={"decision_id": decision_id},
                ),
            ),
        ],
        template="blue",
    )


__all__ = [
    "SOURCE",
    "TASK_LABELS",
    "build_assistant_attachment_demo_card",
    "build_assistant_task_confirmation_card",
    "build_assistant_task_detail_card",
    "build_assistant_task_intake_card",
    "build_assistant_task_list_card",
    "build_assistant_task_submitted_card",
    "build_assistant_task_workspace_card",
    "build_assistant_session_choice_card",
    "build_codex_toolbox_card",
]
