from __future__ import annotations

import asyncio
import hashlib
import html
import importlib.util
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from dc_engines.assistant_workbench_cards import (
    TASK_LABELS,
    TASK_REQUIRED_FIELDS,
    build_assistant_attachment_demo_card,
    build_assistant_task_submitted_card,
)
from dc_engines.card_style import apply_card_visual_system
from dc_engines.creative_memory import search_company_creative_memory
from dc_engines.material_quotation import (
    MAX_QUOTATION_IMAGES_PER_ITEM,
    normalize_material_quotation,
    search_material_prices,
)
from dc_engines.material_quotation_delivery import (
    generate_material_quotation_deliverables,
)
from dc_engines.supplier_price_intake import save_supplier_price_submission
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from lark_oapi.api.docx.v1 import (
    Block,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    CreateDocumentRequest,
    CreateDocumentRequestBody,
    Text,
    TextElement,
    TextRun,
)
from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_temp_path
from astrbot.dashboard.api.ai_cdr_h5 import build_ai_cdr_page
from astrbot.dashboard.api.multipart import UploadFileAdapter
from astrbot.dashboard.services.chat_service import ChatServiceError
from nas_sync.dc_memory_indexer import load_config as load_nas_config

router = APIRouter(prefix="/assistant-attachments", tags=["Assistant Attachments"])

MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_SUPPLIER_PRICE_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_QUOTATION_IMAGE_BYTES = 10 * 1024 * 1024
MAX_QUOTATION_IMAGE_PIXELS = 24_000_000
MAX_FILES = 10
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_AI_CDR_FILES = 20
MAX_AI_CDR_FILE_BYTES = 100 * 1024 * 1024
MAX_AI_CDR_TOTAL_BYTES = 500 * 1024 * 1024
MAX_AI_CDR_ICC_BYTES = 20 * 1024 * 1024
RESEARCH_ANALYSIS_MODES = {
    "evidence_review",
    "comparison",
    "synthesis",
    "recommendation",
}
RESEARCH_DEPTHS = {"quick", "standard", "deep"}
RESEARCH_SOURCE_POLICIES = {"mixed", "uploaded_only", "public_only"}
RESEARCH_CONTENT_FORMATS = {
    "short_answer",
    "comparison",
    "analysis_memo",
    "recommendation",
}
RESEARCH_DELIVERY_FORMATS = {"result_card", "docx", "xlsx", "pdf", "feishu_doc"}
FILE_OPERATIONS = {
    "summarize",
    "extract",
    "rewrite",
    "convert",
    "compare",
    "organize",
    "translate",
}
FILE_LAYOUT_MODES = {"restructure", "preserve", "business_clean"}
FILE_DELIVERY_FORMATS = {"docx", "xlsx", "pdf", "txt", "feishu_doc"}
TRANSLATION_LANGUAGES = {
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "French",
    "German",
    "Spanish",
    "Portuguese",
    "Russian",
    "Arabic",
    "Thai",
    "Vietnamese",
    "Indonesian",
    "Italian",
}
FEISHU_CONNECTOR_ENV = "FEISHU_UNIFIED_CONNECTOR_BASE_URL"
FEISHU_CONNECTOR_PROXY_ENV = "FEISHU_UNIFIED_CONNECTOR_PROXY_URL"
CREATIVE_PROMPT_SERVICE_ENV = "DIANCHI_CREATIVE_PROMPT_SERVICE_URL"
CREATIVE_PROMPT_SERVICE_TOKEN_ENV = "DIANCHI_CREATIVE_PROMPT_SERVICE_TOKEN"
DRAFT_ADMIN_TOKEN_ENV = "DC_ASSISTANT_H5_ADMIN_TOKEN"
_creative_prompt_backend: Any | None = None
_creative_prompt_backend_error = ""
WORKSPACE_ALLOWED_FIELDS = {
    "copy": {
        "copy_type",
        "task_request",
        "audience",
        "source_material",
        "output_requirement",
        "model_choice",
    },
    "image": {
        "image_template",
        "visual_prompt",
        "reference_source",
        "aspect_ratio",
        "image_count",
        "quality",
        "image_format",
        "model_choice",
    },
    "video": {
        "generation_mode",
        "video_prompt",
        "reference_source",
        "duration",
        "aspect_ratio",
        "video_quality",
        "camera_motion",
        "audio_mode",
        "output_format",
        "model_choice",
    },
    "research": {
        "research_question",
        "research_scope",
        "source_requirement",
        "source_material",
        "analysis_mode",
        "research_depth",
        "source_policy",
        "output_format",
        "delivery_format",
        "output_requirement",
        "model_choice",
    },
    "file": {
        "file_goal",
        "file_source",
        "operation",
        "processing_scope",
        "preserve_layout",
        "output_format",
        "output_requirement",
        "model_choice",
        "source_language",
        "target_language",
        "direction_mode",
        "translation_style",
        "professional_domain",
        "output_mode",
        "glossary",
        "translation_memory",
    },
    "quotation": {
        "project_name",
        "client_name",
        "delivery_date",
        "validity_days",
        "quotation_items",
        "transport_fee",
        "installation_fee",
        "rush_fee",
        "loss_rate",
        "profit_rate",
        "tax_rate",
        "quotation_notes",
        "model_choice",
    },
    "codex": {
        "task_request",
        "reasoning_depth",
        "source_material",
        "output_requirement",
        "model_choice",
    },
}
WORKSPACE_SCHEMAS = {
    "copy": {
        "prompt_label": "要写什么",
        "prompt_placeholder": "写清主题、目的和必须包含的信息",
        "material_field": "source_material",
        "settings": [
            {
                "name": "copy_type",
                "label": "内容类型",
                "type": "select",
                "options": [
                    ["公众号推文", "official_account"],
                    ["活动方案", "activity_plan"],
                    ["汇报材料", "report"],
                    ["小红书图文", "xiaohongshu_note"],
                    ["抖音短视频脚本", "douyin_script"],
                    ["西瓜长视频脚本", "xigua_script"],
                ],
            },
            {"name": "audience", "label": "受众 / 渠道", "type": "text"},
            {
                "name": "output_requirement",
                "label": "篇幅 / 语气 / 结构",
                "type": "text",
            },
        ],
    },
    "image": {
        "prompt_label": "画面描述",
        "prompt_placeholder": "主体、用途、构图、光线、材质和不要出现的元素",
        "material_field": "reference_source",
        "settings": [
            {
                "name": "image_template",
                "label": "图片模板",
                "type": "select",
                "options": [
                    ["品牌主视觉", "brand_visual"],
                    ["信息图", "infographic"],
                    ["社媒封面", "social_cover"],
                    ["视频首帧", "video_first_frame"],
                ],
            },
            {
                "name": "aspect_ratio",
                "label": "尺寸 / 比例",
                "type": "select",
                "options": [
                    ["1:1 方图", "1:1"],
                    ["3:4 竖图", "3:4"],
                    ["4:3 横图", "4:3"],
                    ["16:9 横版", "16:9"],
                    ["9:16 竖版", "9:16"],
                ],
            },
            {
                "name": "image_count",
                "label": "数量",
                "type": "select",
                "options": [["1 张", "1"], ["2 张", "2"], ["4 张", "4"]],
            },
            {
                "name": "quality",
                "label": "生成质量",
                "type": "select",
                "options": [
                    ["标准质量", "medium"],
                    ["高质量", "high"],
                    ["快速草图", "low"],
                ],
            },
            {
                "name": "image_format",
                "label": "图片格式",
                "type": "select",
                "options": [["PNG", "png"], ["WEBP", "webp"], ["JPEG", "jpeg"]],
            },
        ],
    },
    "video": {
        "prompt_label": "镜头描述",
        "prompt_placeholder": "人物或主体、动作、镜头运动、场景变化和节奏",
        "material_field": "reference_source",
        "settings": [
            {
                "name": "generation_mode",
                "label": "生成方式",
                "type": "select",
                "options": [
                    ["文生视频", "text_to_video"],
                    ["图生视频", "image_to_video"],
                ],
            },
            {
                "name": "duration",
                "label": "时长",
                "type": "select",
                "options": [
                    ["5 秒", "5"],
                    ["8 秒", "8"],
                    ["10 秒", "10"],
                    ["15 秒", "15"],
                ],
            },
            {
                "name": "video_quality",
                "label": "清晰度",
                "type": "select",
                "options": [["720p", "720p"], ["1080p", "1080p"]],
            },
            {
                "name": "aspect_ratio",
                "label": "画面比例",
                "type": "select",
                "options": [
                    ["16:9 横版", "16:9"],
                    ["9:16 竖版", "9:16"],
                    ["1:1 方形", "1:1"],
                    ["4:3", "4:3"],
                    ["3:4", "3:4"],
                    ["21:9 宽银幕", "21:9"],
                ],
            },
            {
                "name": "camera_motion",
                "label": "镜头",
                "type": "select",
                "options": [["自由运镜", "free"], ["固定镜头", "fixed"]],
            },
            {
                "name": "audio_mode",
                "label": "声音",
                "type": "select",
                "options": [["生成声音", "on"], ["无声音", "off"]],
            },
        ],
    },
    "research": {
        "eyebrow": "DC · 研究分析",
        "summary": "LLM 先读资料和来源，再围绕问题查证、比较并形成可追溯结论",
        "tabs": ["研究目标", "准备资料", "分析与交付"],
        "section_titles": [
            "告诉 AI 要解决的问题",
            "先准备研究资料",
            "选择 AI 工作方式与交付",
        ],
        "prompt_label": "研究问题",
        "prompt_placeholder": "要查清什么、用于什么决策、结论需要多具体",
        "material_field": "source_material",
        "material_label": "来源范围与补充说明",
        "material_placeholder": "指定网站、内部资料、数据口径，或需要排除的来源",
        "empty_material": "可上传内部资料，也可直接检索公开来源",
        "workflow_steps": ["准备资料", "提出问题", "AI 查证分析", "生成交付"],
        "material_guide": "先放入内部文件、飞书云文档或来源链接。没有内部资料时，也可以在下一步要求 AI 检索公开来源。",
        "prompt_guide": "问题里最好包含使用场景，例如“用于供应商选择”或“用于下周经营会决策”。",
        "settings_guide": "内容结构决定 AI 怎么组织答案；交付文件决定最后收到卡片、Word、Excel、PDF 还是飞书云文档。",
        "quick_goals": [
            [
                "核实事实",
                "核实资料中的关键事实，列出支持证据、冲突信息和暂时无法确认的部分。",
            ],
            [
                "对比选择",
                "对关键选项进行同口径对比，指出差异、优缺点、风险和适用条件。",
            ],
            ["归纳趋势", "归纳资料中的变化、共同规律和异常信号，并区分事实与推断。"],
            ["形成建议", "基于证据形成可执行建议，说明依据、优先级、风险和下一步。"],
        ],
        "save_label": "开始分析并直接交付",
        "next_label": "下一步：填写研究问题",
        "settings": [
            {
                "name": "analysis_mode",
                "label": "AI 分析方法",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "AI 怎么分析",
                "help": "决定模型是侧重查证、对比、归纳还是给出建议。",
                "options": [
                    ["证据查证", "evidence_review"],
                    ["同口径对比", "comparison"],
                    ["综合归纳", "synthesis"],
                    ["决策建议", "recommendation"],
                ],
            },
            {
                "name": "research_depth",
                "label": "分析深度",
                "type": "select",
                "group": "AI 怎么分析",
                "options": [
                    ["快速判断", "quick"],
                    ["标准分析", "standard"],
                    ["深度研究", "deep"],
                ],
            },
            {
                "name": "source_policy",
                "label": "资料使用范围",
                "type": "select",
                "group": "AI 怎么分析",
                "options": [
                    ["内部资料 + 公开来源", "mixed"],
                    ["仅使用已上传资料", "uploaded_only"],
                    ["仅检索公开来源", "public_only"],
                ],
            },
            {
                "name": "research_scope",
                "label": "时间 / 地区 / 对象范围",
                "type": "text",
                "group": "AI 怎么分析",
                "help": "选填，例如“2025 年以来，华南区域，三家供应商”。",
            },
            {
                "name": "source_requirement",
                "label": "来源与证据要求",
                "type": "text",
                "group": "AI 怎么分析",
                "help": "选填，例如“优先官方来源，所有关键结论附出处”。",
            },
            {
                "name": "output_format",
                "label": "内容结构",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "结果怎么呈现",
                "help": "这四项控制 LLM 的组织方式，不是假装成文件格式。",
                "options": [
                    ["简短结论", "short_answer"],
                    ["对比表", "comparison"],
                    ["分析备忘录", "analysis_memo"],
                    ["建议方案", "recommendation"],
                ],
            },
            {
                "name": "delivery_format",
                "label": "交付文件",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "结果怎么呈现",
                "help": "选择后会真实生成对应文件或飞书云文档。",
                "options": [
                    ["飞书结果卡片", "result_card"],
                    ["Word 文档 (.docx)", "docx"],
                    ["Excel 表格 (.xlsx)", "xlsx"],
                    ["PDF 文件 (.pdf)", "pdf"],
                    ["飞书云文档", "feishu_doc"],
                ],
            },
            {
                "name": "output_requirement",
                "label": "其他交付要求",
                "type": "text",
                "group": "结果怎么呈现",
                "help": "选填，例如“控制在 3 页内，先结论后证据”。",
            },
        ],
    },
    "file": {
        "eyebrow": "DC · 文件处理",
        "summary": "LLM 先理解真实文件，再完成总结、提取、改写、对比、转换；全文件翻译走专用文档链路",
        "tabs": ["处理目标", "上传文件", "处理与交付"],
        "section_titles": [
            "告诉 AI 最终要得到什么",
            "先选择待处理文件",
            "选择 AI 动作与交付文件",
        ],
        "prompt_label": "处理目标",
        "prompt_placeholder": "说明要从文件得到什么结果",
        "material_field": "file_source",
        "material_label": "文件链接或补充说明",
        "material_placeholder": "可粘贴飞书文档链接，或说明页码、工作表和处理范围",
        "empty_material": "尚未选择待处理文件",
        "material_requirement": "至少上传一个文件、选择一份云文档，或填写文件链接",
        "workflow_steps": ["上传文件", "说明目标", "AI 理解处理", "生成交付"],
        "material_guide": "先把要处理的文件放进来。需要对比时请至少上传两份文件，或提供两个有效链接。",
        "prompt_guide": "先选择处理任务。翻译时必须选目标语言，可进一步指定专业领域、双语对照和术语。",
        "settings_guide": "按源文件类型只提供可真实生成的交付格式；Excel 翻译固定交付 Excel 以保留公式和格式。",
        "quick_goals": [
            ["抓重点", "提炼文件的核心结论、关键数据、风险和待办事项。"],
            ["提取字段", "按统一字段提取文件中的信息，缺失项留空并标注来源位置。"],
            ["对比差异", "逐项比较文件的相同点、差异、冲突和可能影响。"],
            ["改写成稿", "保留原始事实和数据，将内容改写为可直接使用的正式成稿。"],
            [
                "翻译文件",
                "翻译整个文件，保留标题、段落、列表、表格或 Excel 单元格结构。",
            ],
        ],
        "save_label": "开始处理并直接交付",
        "next_label": "下一步：说明处理目标",
        "settings": [
            {
                "name": "operation",
                "label": "处理方式",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "AI 怎么处理",
                "stage": "prompt",
                "help": "LLM 会先理解文件内容，再执行所选动作。",
                "options": [
                    ["总结提炼", "summarize"],
                    ["信息提取", "extract"],
                    ["改写润色", "rewrite"],
                    ["内容转换并重排", "convert"],
                    ["文件对比", "compare"],
                    ["分类整理", "organize"],
                    ["翻译", "translate"],
                ],
            },
            {
                "name": "source_language",
                "label": "源语言",
                "type": "select",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "options": [
                    ["自动识别", "auto"],
                    ["中文", "Chinese"],
                    ["英文", "English"],
                    ["日文", "Japanese"],
                    ["韩文", "Korean"],
                    ["法文", "French"],
                    ["德文", "German"],
                    ["西班牙文", "Spanish"],
                    ["葡萄牙文", "Portuguese"],
                    ["俄文", "Russian"],
                    ["阿拉伯文", "Arabic"],
                    ["泰文", "Thai"],
                    ["越南文", "Vietnamese"],
                    ["印尼文", "Indonesian"],
                    ["意大利文", "Italian"],
                ],
            },
            {
                "name": "target_language",
                "label": "目标语言",
                "type": "select",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "required_when": ["operation", "translate"],
                "options": [
                    ["请选择目标语言", ""],
                    ["中文", "Chinese"],
                    ["英文", "English"],
                    ["日文", "Japanese"],
                    ["韩文", "Korean"],
                    ["法文", "French"],
                    ["德文", "German"],
                    ["西班牙文", "Spanish"],
                    ["葡萄牙文", "Portuguese"],
                    ["俄文", "Russian"],
                    ["阿拉伯文", "Arabic"],
                    ["泰文", "Thai"],
                    ["越南文", "Vietnamese"],
                    ["印尼文", "Indonesian"],
                    ["意大利文", "Italian"],
                ],
            },
            {
                "name": "direction_mode",
                "label": "翻译方向",
                "type": "select",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "options": [
                    ["单向翻译（主要）", "one_way"],
                    ["双向互译（高级）", "bidirectional"],
                ],
            },
            {
                "name": "translation_style",
                "label": "翻译方式",
                "type": "select",
                "stage": "prompt",
                "wide": True,
                "presentation": "cards",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "options": [
                    ["忠实翻译", "faithful"],
                    ["专业润色", "professional"],
                ],
            },
            {
                "name": "professional_domain",
                "label": "专业领域",
                "type": "select",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "options": [
                    ["通用", "general"],
                    ["法律", "legal"],
                    ["金融", "finance"],
                    ["科技", "technology"],
                    ["医疗", "medical"],
                    ["营销", "marketing"],
                    ["制造", "manufacturing"],
                ],
            },
            {
                "name": "output_mode",
                "label": "译文呈现",
                "type": "select",
                "stage": "prompt",
                "wide": True,
                "presentation": "cards",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "options": [
                    ["仅译文", "target_only"],
                    ["双语对照", "bilingual"],
                ],
            },
            {
                "name": "glossary",
                "label": "术语表",
                "type": "textarea",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "help": "选填，每行“原文=译文”；专业词会作为 Qwen-MT 术语约束。",
            },
            {
                "name": "translation_memory",
                "label": "参考译法",
                "type": "textarea",
                "stage": "prompt",
                "group": "翻译设置",
                "visible_when": ["operation", "translate"],
                "help": "选填，每行“原句=参考译句”，用于统一历史译法。",
            },
            {
                "name": "processing_scope",
                "label": "处理范围",
                "type": "text",
                "group": "AI 怎么处理",
                "stage": "prompt",
                "visible_unless": ["operation", "translate"],
                "help": "选填，例如“第 2-5 页”“工作表：报价明细”“只处理正文”。",
            },
            {
                "name": "preserve_layout",
                "label": "原结构处理",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "AI 怎么处理",
                "options": [
                    ["按任务重新组织", "restructure"],
                    ["尽量保留原结构", "preserve"],
                    ["商务简洁排版", "business_clean"],
                ],
            },
            {
                "name": "output_format",
                "label": "交付文件",
                "type": "select",
                "wide": True,
                "presentation": "cards",
                "group": "结果怎么交付",
                "help": "选择后会真实生成对应文件或飞书云文档。",
                "options": [
                    ["Word 文档 (.docx)", "docx"],
                    ["Excel 表格 (.xlsx)", "xlsx"],
                    ["PDF 文件 (.pdf)", "pdf"],
                    ["文本文件 (.txt)", "txt"],
                    ["飞书云文档", "feishu_doc"],
                ],
            },
            {
                "name": "output_requirement",
                "label": "其他要求",
                "type": "text",
                "group": "结果怎么交付",
                "help": "选填，例如“表格列顺序为姓名、部门、金额”。",
            },
        ],
    },
    "quotation": {
        "prompt_label": "项目名称",
        "prompt_placeholder": "例如：柳州用户共创会活动物料",
        "material_field": "quotation_notes",
        "settings": [],
    },
    "codex": {
        "prompt_label": "分析任务",
        "prompt_placeholder": "要复核的问题、关键假设和期望结论",
        "material_field": "source_material",
        "settings": [
            {
                "name": "reasoning_depth",
                "label": "分析深度",
                "type": "select",
                "options": [["深度分析", "codex_high"], ["超深分析", "codex_xhigh"]],
            },
            {"name": "output_requirement", "label": "交付要求", "type": "text"},
        ],
    },
}
SUPPORTED_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".markdown",
    ".md",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".rtf",
    ".svg",
    ".tif",
    ".tiff",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}
_ticket_cache: dict[str, tuple[str, float]] = {}
_ticket_cache_lock = threading.Lock()


@dataclass(slots=True)
class AttachmentDraft:
    """One short-lived attachment upload capability.

    Attributes:
        token: Unguessable capability token embedded in the H5 URL.
        platform_id: Feishu platform that owns the target card.
        upload_url: Absolute URL opened from the Feishu card.
        expires_at: Unix timestamp after which the capability is rejected.
        message_id: Feishu message patched after a successful upload.
        attachments: Sanitized local-file and cloud-document metadata.
        oauth_state: One-time anti-forgery state for the current OAuth attempt.
        user_access_token: Short-lived token used to list the user's Drive files.
        user_access_token_expires_at: Unix timestamp for the user token expiry.
        oauth_grant_token: Opaque connector grant used for check, refresh, and revoke.
        task_type: Optional task category that enables the unified workspace.
        task_data: Sanitized prompt, material notes, and generation settings.
        session_id: Original Feishu private user or group conversation ID.
        message_type: Original AstrBot message type used for automatic submit.
        sender_id: Original Feishu sender open ID.
        sender_name: Original sender display name when available.
        group_id: Original Feishu group ID for group conversations.
        deliverables: Capability-scoped download URLs for generated files.
        delivery_version: Latest generated quotation revision.
        feishu_doc_url: Optional sanitized market-result cloud document URL.
    """

    token: str
    platform_id: str
    upload_url: str
    expires_at: float
    message_id: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    oauth_state: str = ""
    user_access_token: str = ""
    user_access_token_expires_at: float = 0.0
    oauth_grant_token: str = ""
    task_type: str = ""
    task_data: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    message_type: str = "FriendMessage"
    sender_id: str = ""
    sender_name: str = ""
    group_id: str = ""
    deliverables: dict[str, str] = field(default_factory=dict)
    delivery_version: int = 0
    feishu_doc_url: str = ""


class AttachmentDraftStore:
    """Keep bounded, expiring attachment capabilities across service restarts."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 30 * 60,
        storage_path: Path | None = None,
    ) -> None:
        """Initialize the short-lived capability store.

        Args:
            ttl_seconds: Lifetime of each draft capability in seconds.
            storage_path: Optional runtime JSON file used to survive restarts.
        """
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.storage_path = storage_path
        self._drafts: dict[str, AttachmentDraft] = {}
        self._lock = threading.Lock()
        if self.storage_path is not None and self.storage_path.exists():
            try:
                payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
                now = time.time()
                self._drafts = {
                    str(item["token"]): AttachmentDraft(**item)
                    for item in payload
                    if isinstance(item, dict)
                    and float(item.get("expires_at") or 0) > now
                }
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                self._drafts = {}

    def _persist_locked(self) -> None:
        """Persist live drafts while the store lock is held."""
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.storage_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(
                [asdict(draft) for draft in self._drafts.values()],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(self.storage_path)

    def save(self) -> None:
        """Persist direct OAuth field updates made to live draft objects."""
        with self._lock:
            self._persist_locked()

    def create(
        self,
        *,
        platform_id: str,
        upload_base_url: str,
        task_type: str = "",
        session_id: str = "",
        message_type: str = "FriendMessage",
        sender_id: str = "",
        sender_name: str = "",
        group_id: str = "",
    ) -> AttachmentDraft:
        """Create one upload capability.

        Args:
            platform_id: Feishu platform that will patch the original card.
            upload_base_url: Public or local HTTP origin serving this router.
            task_type: Optional task category for the unified task workspace.
            session_id: Original Feishu user or group conversation identifier.
            message_type: Original AstrBot message type value.
            sender_id: Original Feishu sender open ID.
            sender_name: Original sender display name when available.
            group_id: Original Feishu group identifier when applicable.

        Returns:
            The newly stored attachment draft.

        Raises:
            ValueError: If the upload origin is not an absolute HTTP URL.
        """
        base_url = upload_base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upload_base_url must be an absolute HTTP URL")
        token = secrets.token_urlsafe(32)
        normalized_task_type = task_type if task_type in TASK_LABELS else ""
        normalized_message_type = (
            message_type
            if message_type in {"FriendMessage", "GroupMessage", "OtherMessage"}
            else "FriendMessage"
        )
        draft = AttachmentDraft(
            token=token,
            platform_id=platform_id.strip() or "巅池-Agent小助手",
            upload_url=f"{base_url}/api/v1/assistant-attachments/{token}",
            expires_at=time.time() + self.ttl_seconds,
            task_type=normalized_task_type,
            session_id=str(session_id or "").strip()[:300],
            message_type=normalized_message_type,
            sender_id=str(sender_id or "").strip()[:120],
            sender_name=str(sender_name or "").strip()[:120],
            group_id=str(group_id or "").strip()[:120],
        )
        with self._lock:
            now = time.time()
            self._drafts = {
                key: item for key, item in self._drafts.items() if item.expires_at > now
            }
            self._drafts[token] = draft
            self._persist_locked()
        return draft

    def save_task_data(
        self,
        token: str,
        task_data: dict[str, str],
    ) -> AttachmentDraft:
        """Store sanitized workspace fields for a live task draft.

        Args:
            token: Capability token from the workspace URL.
            task_data: Server-validated task fields ready for confirmation.

        Returns:
            The updated task draft.

        Raises:
            KeyError: If the capability is missing or expired.
        """
        draft = self.get(token)
        if draft is None:
            raise KeyError(token)
        with self._lock:
            draft.task_data = dict(task_data)
            self._persist_locked()
        return draft

    def save_quotation_delivery(
        self,
        token: str,
        *,
        deliverables: dict[str, str],
        version: int,
        feishu_doc_url: str = "",
    ) -> AttachmentDraft:
        """Persist generated quotation links and optional cloud-document state.

        Args:
            token: Capability token from the quotation workspace URL.
            deliverables: Sanitized download URLs keyed by delivery format.
            version: Positive generated revision number.
            feishu_doc_url: Optional Feishu cloud-document URL for this result.

        Returns:
            The updated quotation draft.

        Raises:
            KeyError: If the capability is missing or expired.
            ValueError: If the draft is not a quotation workspace.
        """
        draft = self.get(token)
        if draft is None:
            raise KeyError(token)
        if draft.task_type != "quotation":
            raise ValueError("draft is not a quotation workspace")
        with self._lock:
            draft.deliverables = {
                str(key): str(value)
                for key, value in deliverables.items()
                if str(key) in {"internal_xlsx", "market_docx", "market_pdf"}
                and str(value).startswith(("http://", "https://", "/"))
            }
            draft.delivery_version = max(0, int(version))
            draft.feishu_doc_url = str(feishu_doc_url or "")[:500]
            self._persist_locked()
        return draft

    def get(self, token: str) -> AttachmentDraft | None:
        """Return a live draft or remove an expired one.

        Args:
            token: Capability token from the request path.

        Returns:
            The live draft, or None when missing or expired.
        """
        with self._lock:
            draft = self._drafts.get(token)
            if draft is None:
                return None
            if draft.expires_at <= time.time():
                self._drafts.pop(token, None)
                self._persist_locked()
                return None
            return draft

    def bind_message(self, token: str, message_id: str) -> AttachmentDraft:
        """Bind a Feishu message to a live capability.

        Args:
            token: Capability token returned during draft creation.
            message_id: Feishu message to patch after upload.

        Returns:
            The updated draft.

        Raises:
            KeyError: If the capability is missing or expired.
            ValueError: If appending would exceed the attachment limit.
            ValueError: If the message ID is empty.
        """
        draft = self.get(token)
        if draft is None:
            raise KeyError(token)
        if not message_id.strip():
            raise ValueError("message_id is required")
        with self._lock:
            draft.message_id = message_id.strip()
            self._persist_locked()
        return draft

    def attach_many(
        self,
        token: str,
        attachments: list[dict[str, Any]],
    ) -> AttachmentDraft:
        """Append sanitized attachment metadata to a live capability.

        Args:
            token: Capability token from the upload request.
            attachments: Metadata returned by ChatService or docsPicker.

        Returns:
            The updated draft.

        Raises:
            KeyError: If the capability is missing or expired.
        """
        draft = self.get(token)
        if draft is None:
            raise KeyError(token)
        with self._lock:
            if len(draft.attachments) + len(attachments) > MAX_FILES:
                raise ValueError(f"最多关联 {MAX_FILES} 项资料")
            draft.attachments.extend(dict(item) for item in attachments)
            self._persist_locked()
        return draft


draft_store = AttachmentDraftStore(
    ttl_seconds=int(os.environ.get("DC_ASSISTANT_H5_TTL_SECONDS", str(30 * 60))),
    storage_path=Path(get_astrbot_temp_path()) / "assistant_attachment_drafts.json",
)


class CreateDraftRequest(BaseModel):
    platform_id: str = "巅池-Agent小助手"
    upload_base_url: str
    task_type: str = ""
    session_id: str = ""
    message_type: str = "FriendMessage"
    sender_id: str = ""
    sender_name: str = ""
    group_id: str = ""


class BindDraftRequest(BaseModel):
    message_id: str


class CloudDocItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_name: str = Field(alias="fileName")
    file_path: str = Field(alias="filePath")
    file_type: str = Field(default="docx", alias="fileType")


class CloudDocsRequest(BaseModel):
    files: list[CloudDocItem]


class WorkspaceRequest(BaseModel):
    task_data: dict[str, str]


class PromptCandidatesRequest(BaseModel):
    prompt: str
    task_data: dict[str, str] = Field(default_factory=dict)


def build_jsapi_signature(
    *,
    ticket: str,
    nonce: str,
    timestamp: int,
    url: str,
) -> str:
    """Build the SHA-1 signature required by Feishu h5sdk.config.

    Args:
        ticket: Cached Feishu JSAPI ticket.
        nonce: Per-request random string.
        timestamp: Millisecond Unix timestamp.
        url: Exact H5 URL without a fragment.

    Returns:
        Lowercase hexadecimal SHA-1 signature.
    """
    verify = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(verify.encode()).hexdigest()


def build_media_player_page(
    *,
    source_url: str,
    title: str = "视频预览",
    poster_url: str = "",
    engine: str = "",
    aspect_ratio: str = "",
    duration: str = "",
    task_id: str = "",
) -> str:
    """Render a cinematic dependency-free player for Feishu's H5 sidebar.

    Args:
        source_url: Browser-accessible HTTP(S) video URL.
        title: User-facing result title.
        poster_url: Optional browser-accessible poster image URL.
        engine: Optional media engine label.
        aspect_ratio: Optional delivery aspect ratio.
        duration: Optional requested duration label.
        task_id: Optional user-facing task identifier.

    Returns:
        Complete UTF-8 HTML with custom playback controls.
    """
    config_json = json.dumps(
        {
            "sourceUrl": source_url,
            "title": title[:120] or "视频预览",
            "posterUrl": poster_url,
            "engine": engine[:80],
            "aspectRatio": aspect_ratio[:24],
            "duration": duration[:24],
            "taskId": task_id[:80],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    page = r"""<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#46513a">
  <link rel="icon" href="data:,">
  <title>视频预览</title>
  <style>
    :root { color-scheme:light; --ink:#25281f; --muted:#777a6d; --line:#dedfd6; --paper:#ffffff; --panel:#ffffff; --olive:#46513a; --olive-2:#657158; --signal:#d96b32; --signal-soft:#fff0e6; --ok:#347558; --warn:#a75225; --accent:var(--signal); --accent-soft:var(--signal-soft); --accent-ink:#ffffff; --danger:var(--warn); }
    * { box-sizing:border-box; }
    html,body { width:100%; height:100%; margin:0; overflow:hidden; }
    body { font-family:"Microsoft YaHei","PingFang SC",sans-serif; color:var(--ink); background:var(--paper); }
    button,input { font:inherit; }
    button { color:inherit; }
    .shell { position:relative; isolation:isolate; width:100%; height:100%; min-height:420px; display:grid; grid-template-rows:auto minmax(0,1fr) auto; overflow:hidden; background:var(--paper); }
    .topbar { width:100%; min-width:0; display:flex; align-items:center; justify-content:space-between; gap:18px; padding:14px 16px 10px; }
    .identity { display:flex; min-width:0; align-items:center; gap:11px; }
    .monogram { width:32px; height:32px; display:grid; flex:0 0 auto; place-items:center; border:1px solid var(--olive); border-radius:10px; color:#ffffff; background:var(--olive); font:900 11px/1 "Songti SC","STSong",serif; letter-spacing:.08em; }
    .heading { min-width:0; }
    .kicker { margin:0 0 3px; color:var(--signal); font:900 8px/1.2 "Microsoft YaHei","PingFang SC",sans-serif; letter-spacing:.16em; text-transform:uppercase; }
    h1 { overflow:hidden; margin:0; font:600 clamp(15px,2.6vw,21px)/1.2 "Songti SC","STSong",serif; letter-spacing:-.035em; text-overflow:ellipsis; white-space:nowrap; }
    .window-actions,.transport,.utility { display:flex; align-items:center; gap:5px; }
    .window-actions .icon-button { border-color:var(--line); color:var(--olive); background:var(--paper); box-shadow:0 8px 22px rgba(61,59,43,.07); }
    .window-actions .icon-button:hover { border-color:#d8c5ae; color:var(--signal); background:var(--signal-soft); }
    .icon-button { width:34px; height:34px; display:grid; flex:0 0 auto; place-items:center; padding:0; border:1px solid var(--line); border-radius:10px; background:#ffffff; cursor:pointer; transition:border-color .2s ease,background .2s ease,transform .2s ease; }
    .icon-button:hover { border-color:#d8c5ae; background:var(--signal-soft); transform:translateY(-1px); }
    .icon-button:focus-visible,.play-orbit:focus-visible,.speed:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
    .icon-button svg,.play-orbit svg { width:16px; height:16px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .stage-wrap { width:100%; min-width:0; min-height:0; display:grid; place-items:center; padding:6px 14px 12px; }
    .stage { position:relative; width:min(100%,1120px); aspect-ratio:16/9; max-height:100%; overflow:hidden; border:1px solid var(--line); border-radius:clamp(14px,2.4vw,24px); background:#1f211b; box-shadow:0 24px 64px rgba(61,59,43,.16),0 1px 0 rgba(255,255,255,.72) inset; }
    .stage::after { content:""; position:absolute; inset:0; pointer-events:none; border-radius:inherit; box-shadow:inset 0 0 0 1px rgba(255,255,255,.035),inset 0 -100px 100px rgba(0,0,0,.18); }
    video { width:100%; height:100%; display:block; object-fit:contain; background:#020302; }
    .ambient { position:absolute; inset:8% 12%; z-index:-1; background:var(--poster) center/cover no-repeat; filter:blur(42px) saturate(.72); opacity:.22; transform:scale(1.16); }
    .center-state { position:absolute; inset:0; z-index:2; display:grid; place-items:center; pointer-events:none; transition:opacity .24s ease; }
    .play-orbit { position:relative; width:72px; height:72px; display:grid; place-items:center; padding:0; border:1px solid rgba(255,255,255,.48); border-radius:50%; color:var(--accent-ink); background:var(--accent); box-shadow:0 12px 44px rgba(0,0,0,.28),0 0 0 10px rgba(255,255,255,.12); pointer-events:auto; cursor:pointer; transition:transform .24s cubic-bezier(.2,.8,.2,1),box-shadow .24s ease; }
    .play-orbit::after { content:""; position:absolute; inset:-13px; border:1px solid rgba(255,255,255,.28); border-radius:50%; animation:breathe 2.4s ease-in-out infinite; }
    .play-orbit:hover { transform:scale(1.06); box-shadow:0 16px 54px rgba(0,0,0,.34),0 0 0 13px rgba(255,255,255,.14); }
    .play-orbit svg { width:24px; height:24px; margin-left:3px; fill:currentColor; stroke:none; }
    .stage.playing .center-state { opacity:0; }
    .loading { position:absolute; inset:0; z-index:4; display:none; place-items:center; background:rgba(3,5,4,.32); backdrop-filter:blur(5px); }
    .stage.waiting .loading { display:grid; }
    .loader { width:42px; height:42px; border:2px solid rgba(255,255,255,.15); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
    .error { position:absolute; inset:0; z-index:5; display:none; place-items:center; padding:30px; color:var(--ink); text-align:center; background:var(--paper); }
    .stage.failed .error { display:grid; }
    .error-card { max-width:380px; }
    .error-mark { width:42px; height:42px; display:grid; margin:0 auto 15px; place-items:center; border:1px solid rgba(255,139,120,.32); border-radius:50%; color:var(--danger); font:700 20px serif; }
    .error h2 { margin:0 0 8px; font:600 20px "Songti SC","STSong",serif; }
    .error p { margin:0 0 16px; color:var(--muted); font-size:11px; line-height:1.7; }
    .retry { min-height:36px; padding:0 16px; border:1px solid #d8c5ae; border-radius:10px; color:var(--signal); background:var(--signal-soft); font-weight:900; cursor:pointer; }
    .deck { width:min(100%,1120px); min-width:0; margin:0 auto; padding:0 16px calc(14px + env(safe-area-inset-bottom)); transition:opacity .25s ease,transform .25s ease; }
    .timeline { position:relative; height:22px; display:flex; align-items:center; }
    .range { --fill:0%; width:100%; height:4px; margin:0; border:0; border-radius:99px; appearance:none; background:#dcded6; accent-color:var(--signal); cursor:pointer; }
    .range::-webkit-slider-thumb { width:13px; height:13px; border:3px solid #ffffff; border-radius:50%; appearance:none; background:var(--signal); box-shadow:0 0 0 2px rgba(217,107,50,.2); }
    .range::-moz-range-thumb { width:9px; height:9px; border:3px solid #ffffff; border-radius:50%; background:var(--accent); }
    .control-row { display:flex; min-width:0; align-items:center; justify-content:space-between; gap:12px; }
    .primary-play { color:var(--accent); }
    .time { min-width:88px; color:var(--olive-2); font:800 10px/1 "Microsoft YaHei","PingFang SC",sans-serif; letter-spacing:.04em; }
    .volume { width:72px; }
    .speed { height:30px; min-width:43px; padding:0 9px; border:1px solid var(--line); border-radius:9px; color:var(--olive); background:#ffffff; font:900 9px "Microsoft YaHei","PingFang SC",sans-serif; cursor:pointer; }
    .meta-strip { display:flex; min-width:0; align-items:center; gap:10px; }
    .meta-item { position:relative; color:var(--muted); font-size:9px; white-space:nowrap; }
    .meta-item+ .meta-item { padding-left:11px; }
    .meta-item+ .meta-item::before { content:""; position:absolute; left:0; top:50%; width:2px; height:2px; border-radius:50%; background:var(--olive-2); }
    .meta-item strong { color:var(--olive); font-weight:800; }
    .hint { overflow:hidden; color:var(--muted); font-size:8px; text-overflow:ellipsis; white-space:nowrap; }
    .stage.idle.playing~.deck { opacity:0; transform:translateY(7px); pointer-events:none; }
    @keyframes spin { to { transform:rotate(360deg); } }
    @keyframes breathe { 50% { transform:scale(1.08); opacity:.55; } }
    @media (max-width:760px) {
      .meta-strip,.hint { display:none; }
    }
    @media (max-width:560px) {
      .topbar { padding:11px 11px 7px; }
      .monogram { width:29px; height:29px; border-radius:9px; }
      .stage-wrap { padding:4px 9px 9px; }
      .stage { border-radius:14px; }
      .deck { padding-inline:11px; }
      .volume { display:none; }
      .play-orbit { width:60px; height:60px; }
      .time { min-width:78px; font-size:9px; }
    }
    @media (max-height:520px) {
      .shell { min-height:0; grid-template-rows:auto minmax(0,1fr) auto; }
      .topbar { padding-block:7px 4px; }
      .stage-wrap { padding-block:2px 4px; }
      .deck { padding-bottom:6px; }
      .kicker,.meta-strip,.hint { display:none; }
    }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto!important; animation:none!important; transition:none!important; } }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="identity">
        <div class="monogram">DC</div>
        <div class="heading"><p class="kicker">Dianchi Screening Room</p><h1 id="title">视频预览</h1></div>
      </div>
      <div class="window-actions">
        <button class="icon-button" id="expandTop" type="button" aria-label="全屏播放" title="全屏播放">
          <svg viewBox="0 0 24 24"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>
        </button>
        <button class="icon-button" id="close" type="button" aria-label="关闭播放器" title="关闭播放器">
          <svg viewBox="0 0 24 24"><path d="M5 5l14 14M19 5L5 19"/></svg>
        </button>
      </div>
    </header>
    <div class="stage-wrap">
      <section class="stage waiting" id="stage" aria-label="视频播放器">
        <div class="ambient" id="ambient"></div>
        <video id="video" playsinline preload="metadata"></video>
        <div class="center-state"><button class="play-orbit" id="centerPlay" type="button" aria-label="播放视频"><svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg></button></div>
        <div class="loading" aria-label="视频加载中"><span class="loader"></span></div>
        <div class="error"><div class="error-card"><div class="error-mark">!</div><h2>视频暂时无法加载</h2><p>链接可能已过期或网络暂时不可用。可以重试；生成结果仍保留在飞书原生视频消息中。</p><button class="retry" id="retry" type="button">重新加载</button></div></div>
      </section>
    </div>
    <footer class="deck" id="deck">
      <div class="timeline"><input class="range" id="progress" type="range" min="0" max="1000" value="0" aria-label="播放进度"></div>
      <div class="control-row">
        <div class="transport">
          <button class="icon-button primary-play" id="play" type="button" aria-label="播放或暂停" title="播放 / 暂停">
            <svg id="playIcon" viewBox="0 0 24 24"><path d="M8 5l11 7-11 7z"/></svg>
          </button>
          <button class="icon-button" id="mute" type="button" aria-label="静音" title="静音">
            <svg id="volumeIcon" viewBox="0 0 24 24"><path d="M5 9v6h4l5 4V5L9 9H5zM18 9c1.3 1.7 1.3 4.3 0 6"/></svg>
          </button>
          <input class="range volume" id="volume" type="range" min="0" max="1" step=".05" value=".9" aria-label="音量">
          <span class="time"><span id="current">00:00</span> / <span id="total">00:00</span></span>
        </div>
        <div class="meta-strip" id="meta"></div>
        <div class="utility">
          <span class="hint">Space 播放 · ← → 快进 · F 全屏</span>
          <button class="speed" id="speed" type="button" aria-label="播放速度">1.0×</button>
          <button class="icon-button" id="pip" type="button" aria-label="画中画" title="画中画">
            <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><rect x="12" y="11" width="7" height="5" rx="1"/></svg>
          </button>
          <button class="icon-button" id="fullscreen" type="button" aria-label="全屏播放" title="全屏播放">
            <svg viewBox="0 0 24 24"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/></svg>
          </button>
        </div>
      </div>
    </footer>
  </main>
  <script>
    const config=__PLAYER_CONFIG__;
    const stage=document.getElementById('stage');
    const video=document.getElementById('video');
    const title=document.getElementById('title');
    const play=document.getElementById('play');
    const centerPlay=document.getElementById('centerPlay');
    const playIcon=document.getElementById('playIcon');
    const progress=document.getElementById('progress');
    const current=document.getElementById('current');
    const total=document.getElementById('total');
    const mute=document.getElementById('mute');
    const volume=document.getElementById('volume');
    const speed=document.getElementById('speed');
    const pip=document.getElementById('pip');
    const meta=document.getElementById('meta');
    let idleTimer=0;
    let seeking=false;
    const speedSteps=[1,1.25,1.5,2,.75];
    const formatTime=value=>{if(!Number.isFinite(value))return '00:00';const seconds=Math.max(0,Math.floor(value));const hours=Math.floor(seconds/3600);const minutes=Math.floor((seconds%3600)/60);const rest=seconds%60;return hours?`${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')}:${String(rest).padStart(2,'0')}`:`${String(minutes).padStart(2,'0')}:${String(rest).padStart(2,'0')}`;};
    const icon=playing=>{playIcon.innerHTML=playing?'<path d="M8 5v14M16 5v14"/>':'<path d="M8 5l11 7-11 7z"/>';stage.classList.toggle('playing',playing);};
    const showControls=()=>{stage.classList.remove('idle');clearTimeout(idleTimer);if(!video.paused)idleTimer=setTimeout(()=>stage.classList.add('idle'),2400);};
    const togglePlay=async()=>{try{if(video.paused)await video.play();else video.pause();}catch(error){stage.classList.add('failed');}};
    const toggleFullscreen=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await stage.requestFullscreen();}catch(error){showControls();}};
    title.textContent=config.title||'视频预览';document.title=title.textContent;
    if(config.posterUrl){video.poster=config.posterUrl;document.getElementById('ambient').style.setProperty('--poster',`url("${config.posterUrl.replaceAll('"','%22')}")`);}
    video.src=config.sourceUrl;video.volume=.9;
    const metaValues=[[config.engine,'引擎'],[config.aspectRatio,'比例'],[config.duration,'时长'],[config.taskId?`#${config.taskId}`:'','任务']].filter(item=>item[0]);
    metaValues.forEach(([value,label])=>{const item=document.createElement('span');item.className='meta-item';item.innerHTML=`${label} <strong></strong>`;item.querySelector('strong').textContent=value;meta.append(item);});
    video.addEventListener('loadedmetadata',()=>{stage.classList.remove('waiting','failed');total.textContent=formatTime(video.duration);});
    video.addEventListener('canplay',()=>stage.classList.remove('waiting'));
    video.addEventListener('waiting',()=>stage.classList.add('waiting'));
    video.addEventListener('playing',()=>{stage.classList.remove('waiting','failed');icon(true);showControls();});
    video.addEventListener('pause',()=>{icon(false);showControls();});
    video.addEventListener('ended',()=>{icon(false);stage.classList.remove('idle');});
    video.addEventListener('error',()=>{stage.classList.remove('waiting');stage.classList.add('failed');icon(false);});
    video.addEventListener('timeupdate',()=>{if(seeking||!Number.isFinite(video.duration))return;const value=Math.round((video.currentTime/video.duration)*1000)||0;progress.value=String(value);progress.style.setProperty('--fill',`${value/10}%`);current.textContent=formatTime(video.currentTime);});
    progress.addEventListener('input',()=>{seeking=true;progress.style.setProperty('--fill',`${Number(progress.value)/10}%`);current.textContent=formatTime((Number(progress.value)/1000)*(video.duration||0));});
    progress.addEventListener('change',()=>{if(Number.isFinite(video.duration))video.currentTime=(Number(progress.value)/1000)*video.duration;seeking=false;});
    volume.addEventListener('input',()=>{video.volume=Number(volume.value);video.muted=video.volume===0;volume.style.setProperty('--fill',`${Number(volume.value)*100}%`);});
    mute.addEventListener('click',()=>{video.muted=!video.muted;if(!video.muted&&video.volume===0)video.volume=.7;volume.value=video.muted?'0':String(video.volume);volume.style.setProperty('--fill',`${Number(volume.value)*100}%`);});
    speed.addEventListener('click',()=>{const index=speedSteps.indexOf(video.playbackRate);video.playbackRate=speedSteps[(index+1)%speedSteps.length];speed.textContent=`${video.playbackRate.toFixed(video.playbackRate%1?2:1).replace(/0$/,'')}×`;});
    pip.addEventListener('click',async()=>{try{if(document.pictureInPictureElement)await document.exitPictureInPicture();else if(document.pictureInPictureEnabled)await video.requestPictureInPicture();}catch(error){showControls();}});
    play.addEventListener('click',togglePlay);centerPlay.addEventListener('click',togglePlay);video.addEventListener('click',togglePlay);
    document.getElementById('fullscreen').addEventListener('click',toggleFullscreen);document.getElementById('expandTop').addEventListener('click',toggleFullscreen);
    document.getElementById('retry').addEventListener('click',()=>{stage.classList.remove('failed');stage.classList.add('waiting');video.load();});
    document.getElementById('close').addEventListener('click',async()=>{try{if(window.LarkAPI?.webview?.close){await window.LarkAPI.webview.close();return;}}catch(error){}if(history.length>1)history.back();else window.close();});
    ['mousemove','pointermove','pointerdown','touchstart'].forEach(name=>stage.addEventListener(name,showControls,{passive:true}));
    document.addEventListener('keydown',event=>{if(['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName))return;if(event.code==='Space'){event.preventDefault();togglePlay();}else if(event.key==='ArrowRight')video.currentTime=Math.min(video.duration||0,video.currentTime+5);else if(event.key==='ArrowLeft')video.currentTime=Math.max(0,video.currentTime-5);else if(event.key.toLowerCase()==='m')mute.click();else if(event.key.toLowerCase()==='f')toggleFullscreen();showControls();});
    volume.style.setProperty('--fill','90%');showControls();
  </script>
</body>
</html>"""
    return page.replace("__PLAYER_CONFIG__", config_json)


def build_material_quotation_page(draft: AttachmentDraft) -> str:
    """Render the preparation calculation and market estimate surfaces.

    Args:
        draft: Live quotation capability and any previously saved draft state.

    Returns:
        A dependency-free Feishu H5 workspace that keeps internal costing details
        separate from the result-only estimate sent to the market department.
    """
    try:
        quotation_items = json.loads(draft.task_data.get("quotation_items", "[]"))
    except json.JSONDecodeError:
        quotation_items = []
    if not isinstance(quotation_items, list):
        quotation_items = []
    config_json = json.dumps(
        {
            "projectName": draft.task_data.get("project_name", ""),
            "clientName": draft.task_data.get("client_name", ""),
            "deliveryDate": draft.task_data.get("delivery_date", ""),
            "validityDays": draft.task_data.get("validity_days", "15"),
            "items": quotation_items[:20],
            "transportFee": draft.task_data.get("transport_fee", "0"),
            "installationFee": draft.task_data.get("installation_fee", "0"),
            "rushFee": draft.task_data.get("rush_fee", "0"),
            "lossRate": draft.task_data.get("loss_rate", "0"),
            "profitRate": draft.task_data.get("profit_rate", "0"),
            "taxRate": draft.task_data.get("tax_rate", "0"),
            "notes": draft.task_data.get("quotation_notes", ""),
            "deliverables": draft.deliverables,
            "deliveryVersion": draft.delivery_version,
            "feishuDocUrl": draft.feishu_doc_url,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    page = r"""<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#46513a">
  <link rel="icon" href="data:,">
  <title>筹备组物料报价台</title>
  <style>
    :root { color-scheme:light; --ink:#25281f; --muted:#777a6d; --line:#dedfd6; --paper:#ffffff; --panel:#ffffff; --olive:#46513a; --olive-2:#657158; --signal:#d96b32; --signal-soft:#fff0e6; --ok:#347558; --warn:#a75225; }
    * { box-sizing:border-box; }
    html,body { width:100%; height:100%; margin:0; overflow:hidden; }
    body { color:var(--ink); background:#ffffff; font-family:"Microsoft YaHei","PingFang SC",sans-serif; }
    button,input,textarea,select { font:inherit; }
    button { cursor:pointer; }
    .app { height:100%; background:#ffffff; }
    .tool-home { height:100%; overflow:auto; padding:24px 16px; background:#ffffff; }
    .tool-home[hidden],.calculator-surface[hidden],.supplier-surface[hidden],.market-result[hidden] { display:none; }
    .home-shell { width:min(100%,540px); margin:0 auto; }
    .home-mark { display:inline-grid; place-items:center; width:34px; height:34px; border-radius:10px; color:#ffffff; background:var(--olive); font:900 11px/1 "Songti SC","STSong",serif; }
    .home-kicker { margin:16px 0 7px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.16em; }
    .tool-home h1 { margin:0; font-size:26px; }
    .home-intro { margin:8px 0 20px; color:var(--muted); font-size:10px; line-height:1.65; }
    .tool-grid { display:grid; gap:11px; }
    .tool-card { padding:16px; border:1px solid var(--line); border-radius:14px; background:#ffffff; box-shadow:0 8px 24px rgba(61,59,43,.06); }
    .tool-number { color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.12em; }
    .tool-card h2 { margin:7px 0 5px; font:800 18px/1.2 "Songti SC","STSong",serif; }
    .tool-card p { margin:0; color:var(--muted); font-size:9px; line-height:1.55; }
    .tool-actions { display:grid; grid-template-columns:1fr 1fr; gap:7px; margin-top:13px; }
    .tool-actions.one { grid-template-columns:1fr; }
    .tool-actions button { min-height:40px; border:0; border-radius:9px; font-size:10px; font-weight:900; }
    .tool-primary { color:#ffffff; background:var(--signal); }
    .tool-secondary { color:var(--olive); background:#f0f2eb; }
    .home-footnote { margin:14px 1px 0; padding-top:12px; border-top:1px solid var(--line); color:var(--muted); font-size:8px; line-height:1.55; }
    .calculator-surface { height:100%; display:grid; grid-template-rows:auto auto minmax(0,1fr) auto; background:#ffffff; }
    .hero { position:relative; overflow:hidden; padding:14px 16px 13px; border-bottom:1px solid var(--line); background:#ffffff; }
    .hero-top { display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .back-home { flex:0 0 auto; min-height:28px; padding:0 8px; border:1px solid var(--line); border-radius:8px; color:var(--olive); background:#ffffff; font-size:8px; font-weight:900; }
    .kicker { position:relative; z-index:1; margin:0 0 5px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.16em; }
    h1 { position:relative; z-index:1; margin:0; font:800 22px/1.15 "Songti SC","STSong",serif; letter-spacing:-.04em; }
    .hero-row { position:relative; z-index:1; display:flex; gap:7px; align-items:center; margin-top:7px; }
    .hero-copy { margin:0; color:var(--muted); font-size:9px; line-height:1.45; }
    .source-chip { flex:0 0 auto; padding:3px 7px; border:1px solid #d8c5ae; border-radius:99px; color:#865126; background:#fff7ed; font-size:8px; font-weight:800; }
    .tabs { display:grid; grid-template-columns:repeat(3,1fr); padding:0 12px; border-bottom:1px solid var(--line); background:#ffffff; }
    .tab { min-height:42px; border:0; border-bottom:2px solid transparent; color:#85877c; background:transparent; font-size:11px; font-weight:800; }
    .tab.active { color:var(--olive); border-color:var(--signal); }
    .content { min-height:0; overflow:auto; padding:12px 13px 24px; background:#ffffff; }
    .panel { display:none; animation:rise .18s ease-out; }
    .panel.active { display:block; }
    @keyframes rise { from { opacity:.3; transform:translateY(4px); } to { opacity:1; transform:none; } }
    .section-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:10px; }
    .section-head h2 { margin:0; font-size:15px; letter-spacing:-.03em; }
    .section-head p { margin:3px 0 0; color:var(--muted); font-size:9px; line-height:1.45; }
    .badge { flex:0 0 auto; padding:3px 7px; border-radius:99px; color:#6d5937; background:#eee7d2; font-size:8px; font-weight:800; }
    .form-grid { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
    .field { min-width:0; }
    .field.wide { grid-column:1/-1; }
    label { display:block; margin:0 0 5px; color:#5e6256; font-size:9px; font-weight:800; }
    input,textarea,select { width:100%; border:1px solid var(--line); border-radius:9px; outline:none; color:var(--ink); background:#fff; }
    input,select { min-height:38px; padding:0 9px; }
    textarea { min-height:68px; padding:9px 10px; resize:vertical; line-height:1.5; }
    input:focus,textarea:focus,select:focus { border-color:var(--signal); box-shadow:0 0 0 3px rgba(217,107,50,.1); }
    .truth-note { margin-top:12px; padding:10px 11px; border-left:3px solid var(--signal); border-radius:0 9px 9px 0; color:#665b49; background:var(--signal-soft); font-size:9px; line-height:1.55; }
    .items { display:grid; gap:9px; }
    .quote-row { overflow:hidden; border:1px solid var(--line); border-radius:12px; background:#fff; box-shadow:0 5px 16px rgba(61,59,43,.05); }
    .row-head { display:flex; align-items:center; justify-content:space-between; padding:8px 10px; border-bottom:1px solid #ecece5; background:var(--panel); }
    .row-index { font-size:10px; font-weight:900; }
    .row-state { margin-left:6px; padding:2px 6px; border-radius:99px; color:var(--warn); background:#fff0e6; font-size:8px; font-weight:800; }
    .row-state.priced { color:var(--ok); background:#eaf6ef; }
    .remove { min-height:26px; padding:0 8px; border:0; border-radius:7px; color:#8b4a31; background:#f7e9e2; font-size:9px; font-weight:800; }
    .row-body { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:10px; }
    .row-body .wide { grid-column:1/-1; }
    .lookup { display:flex; gap:7px; align-items:center; }
    .lookup input { min-width:0; }
    .lookup-button { min-height:38px; flex:0 0 auto; padding:0 10px; border:0; border-radius:9px; color:white; background:var(--olive); font-size:9px; font-weight:900; }
    .item-image-upload { display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:8px; align-items:center; padding:7px; border:1px dashed #cfd2c7; border-radius:10px; background:#fafbf8; }
    .item-image-button { min-height:34px; padding:0 11px; border:0; border-radius:8px; color:white; background:var(--olive); font-size:9px; font-weight:900; }
    .item-image-button:disabled { color:#8b8d83; background:#dcddd5; }
    .item-image-meta { min-width:0; }
    .item-image-name { min-width:0; overflow:hidden; color:var(--muted); font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
    .item-image-hint { display:block; margin-top:2px; color:#96998f; font-size:8px; }
    .item-image-clear { min-height:30px; padding:0 8px; border:0; border-radius:7px; color:#8b4a31; background:#f7e9e2; font-size:8px; font-weight:800; }
    .item-image-clear[hidden] { display:none; }
    .item-image-previews { display:flex; grid-column:1/-1; gap:5px; overflow:auto; padding-top:1px; }
    .item-image-preview-button { width:42px; height:42px; flex:0 0 auto; overflow:hidden; padding:0; border:1px solid var(--line); border-radius:7px; background:#ffffff; cursor:zoom-in; transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease; }
    .item-image-preview-button:hover { transform:translateY(-1px); border-color:#aeb59f; box-shadow:0 4px 10px rgba(47,48,38,.14); }
    .item-image-preview-button:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
    .item-image-preview-button img { display:block; width:100%; height:100%; object-fit:cover; }
    .item-image-previews[hidden] { display:none; }
    .image-lightbox { position:fixed; inset:0; z-index:100; display:grid; place-items:center; padding:18px; background:rgba(31,32,25,.84); backdrop-filter:blur(7px); }
    .image-lightbox[hidden] { display:none; }
    .image-lightbox-panel { position:relative; display:grid; justify-items:center; width:min(100%,920px); }
    .image-lightbox-image { display:block; max-width:100%; max-height:calc(100vh - 96px); border:1px solid rgba(255,255,255,.24); border-radius:13px; object-fit:contain; background:#ffffff; box-shadow:0 24px 70px rgba(0,0,0,.4); animation:lightbox-in .18s ease-out; }
    .image-lightbox-close { position:absolute; top:10px; right:10px; min-width:34px; min-height:34px; padding:0 10px; border:1px solid rgba(255,255,255,.34); border-radius:9px; color:#ffffff; background:rgba(31,32,25,.78); font-size:9px; font-weight:900; }
    .image-lightbox-close:focus-visible { outline:2px solid #ffffff; outline-offset:2px; }
    .image-lightbox-hint { margin:9px 0 0; color:rgba(255,255,255,.8); font-size:8px; }
    @keyframes lightbox-in { from { opacity:.4; transform:scale(.97); } to { opacity:1; transform:none; } }
    .line-total { display:flex; align-items:end; justify-content:flex-end; color:var(--olive); font-size:14px; font-weight:900; }
    .matches { display:none; grid-column:1/-1; gap:6px; padding-top:2px; }
    .matches.open { display:grid; }
    .match { width:100%; padding:8px 9px; border:1px solid #ded9c9; border-radius:9px; color:var(--ink); background:#faf8ee; text-align:left; }
    .match strong,.match small { display:block; }
    .match strong { font-size:9px; }
    .match small { margin-top:3px; color:var(--muted); font-size:8px; line-height:1.4; }
    .match.empty { cursor:default; color:var(--muted); }
    .add { width:100%; min-height:40px; margin-top:10px; border:1px dashed #9fa58f; border-radius:10px; color:var(--olive); background:#f7f8f2; font-size:10px; font-weight:900; }
    .fees { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
    .ledger { margin-top:12px; overflow:hidden; border:1px solid #d7d7cd; border-radius:12px; background:white; }
    .ledger-row { display:flex; justify-content:space-between; gap:12px; padding:8px 11px; border-top:1px solid #ecece5; color:#606357; font-size:9px; }
    .ledger-row:first-child { border-top:0; }
    .ledger-row strong { color:var(--ink); }
    .ledger-row.total { padding-block:11px; color:white; background:var(--olive); font-size:11px; }
    .ledger-row.total strong { color:white; font-size:16px; }
    .pending-line { color:var(--warn); }
    .footer { display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px; align-items:center; padding:9px 13px 11px; border-top:1px solid var(--line); background:#ffffff; box-shadow:0 -9px 24px rgba(47,48,38,.06); }
    .footer-total small,.footer-total strong { display:block; }
    .footer-total small { color:var(--muted); font-size:8px; }
    .footer-total strong { margin-top:2px; color:var(--olive); font-size:18px; }
    .save { min-height:43px; border:0; border-radius:11px; color:white; background:var(--signal); font-size:11px; font-weight:900; box-shadow:0 8px 18px rgba(217,107,50,.2); }
    .save:disabled { color:#8b8d83; background:#dcddd5; box-shadow:none; }
    .status { grid-column:1/-1; min-height:0; color:var(--muted); font-size:8px; }
    .status.bad { color:#b14523; font-weight:800; }
    .status.ok { color:var(--ok); font-weight:800; }
    .internal-delivery { display:flex; grid-column:1/-1; align-items:center; justify-content:space-between; gap:8px; padding-top:1px; }
    .internal-delivery[hidden] { display:none; }
    .internal-delivery span { color:var(--muted); font-size:8px; }
    .internal-delivery a { color:var(--olive); font-size:9px; font-weight:900; text-decoration:none; }
    .supplier-surface { height:100%; display:grid; grid-template-rows:auto minmax(0,1fr) auto; background:#ffffff; }
    .supplier-content { min-height:0; overflow:auto; padding:12px 13px 24px; }
    .supplier-meta { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
    .upload-box { display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px; align-items:center; margin:12px 0; padding:11px; border:1px dashed #abae9f; border-radius:11px; background:#f8f8f4; }
    .upload-box button { min-height:38px; padding:0 12px; border:0; border-radius:9px; color:#ffffff; background:var(--olive); font-size:9px; font-weight:900; }
    .upload-copy strong,.upload-copy small { display:block; }
    .upload-copy strong { overflow:hidden; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
    .upload-copy small { margin-top:3px; color:var(--muted); font-size:8px; line-height:1.4; }
    .supplier-file { position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
    .supplier-items { display:grid; gap:8px; }
    .supplier-row { overflow:hidden; border:1px solid var(--line); border-radius:11px; background:#ffffff; }
    .supplier-row .row-body { grid-template-columns:1fr 1fr; }
    .supplier-footer { display:grid; gap:7px; padding:9px 13px 11px; border-top:1px solid var(--line); background:#ffffff; box-shadow:0 -9px 24px rgba(47,48,38,.06); }
    .supplier-footer .save { width:100%; }
    .market-result { height:100%; overflow:auto; padding:26px 18px; background:#ffffff; }
    .market-shell { width:min(100%,520px); margin:0 auto; padding-top:clamp(8px,7vh,58px); }
    .market-kicker { margin:0 0 8px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.14em; }
    .market-result h2 { margin:0; font:800 24px/1.2 "Songti SC","STSong",serif; letter-spacing:-.04em; }
    .market-summary { margin:8px 0 20px; color:var(--muted); font-size:10px; line-height:1.6; }
    .estimate-card { padding:19px; border:0; border-radius:14px; color:white; background:var(--olive); box-shadow:0 16px 38px rgba(47,48,38,.18); }
    .estimate-row { display:flex; justify-content:space-between; gap:14px; padding:9px 0; border-top:1px solid rgba(255,255,255,.18); color:#d8dece; font-size:10px; }
    .estimate-row:first-child { border-top:0; }
    .estimate-row strong { color:white; text-align:right; }
    .estimate-amount { margin:18px 0 4px; color:#d8dece; font-size:9px; }
    .estimate-amount strong { display:block; margin-top:5px; color:white; font-size:32px; letter-spacing:-.04em; }
    .result-actions { display:grid; grid-template-columns:1fr 1.5fr; gap:9px; margin-top:14px; }
    .result-actions button { min-height:43px; border:0; border-radius:11px; font-size:11px; font-weight:900; }
    .back-estimate { color:var(--olive); background:#f1f3ed; }
    .send-estimate { color:#ffffff; background:var(--signal); }
    .market-note { margin:12px 1px 0; color:var(--muted); font-size:9px; line-height:1.55; }
    .market-deliveries { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:10px; }
    .market-deliveries[hidden] { display:none; }
    .market-deliveries a,.market-deliveries button { min-height:40px; display:grid; place-items:center; border:1px solid var(--line); border-radius:10px; color:var(--olive); background:#ffffff; font-size:9px; font-weight:900; text-align:center; text-decoration:none; }
    .market-deliveries .sync-doc { grid-column:1/-1; color:#ffffff; border-color:var(--olive); background:var(--olive); }
    @media (min-width:480px) { .content { padding-inline:18px; } .hero { padding-inline:20px; } .footer { padding-inline:18px; } }
  </style>
</head>
<body>
  <main class="app">
    <section class="tool-home" data-surface="home" id="homeSurface">
      <div class="home-shell">
        <span class="home-mark">DC</span>
        <p class="home-kicker">PREP TEAM · PRICE TOOLS</p>
        <h1>报价工具首页</h1>
        <p class="home-intro">项目估价和合作公司价格入库是两条独立流程。选择现在要完成的工作。</p>
        <div class="tool-grid">
          <article class="tool-card">
            <span class="tool-number">TOOL 01</span>
            <h2>项目估价测算</h2>
            <p>筹备组计算内部成本，最后只把估价金额结果交给市场部。</p>
            <div class="tool-actions one"><button class="tool-primary" id="openCalculator" type="button">进入项目估价</button></div>
          </article>
          <article class="tool-card">
            <span class="tool-number">TOOL 02</span>
            <h2>新增合作公司价格</h2>
            <p>不需要先建立项目。可手工单项 / 多项录入，也可上传原始报价资料作为凭证。</p>
            <div class="tool-actions">
              <button class="tool-secondary" id="openSupplierManual" type="button">手工单项 / 多项录入</button>
              <button class="tool-primary" id="openSupplierUpload" type="button">上传报价资料</button>
            </div>
          </article>
        </div>
        <p class="home-footnote">新增价格保存后直接进入 NAS / Obsidian 历史报价库，并统一标记“待复核”。</p>
      </div>
    </section>
    <section class="calculator-surface" data-surface="calculator" id="calculatorSurface" hidden>
    <header class="hero">
      <div class="hero-top"><p class="kicker">PREP TEAM · QUOTATION DESK</p><button class="back-home" id="calculatorBackHome" type="button">返回工具首页</button></div>
      <h1>筹备组内部测算台</h1>
      <div class="hero-row"><p class="hero-copy">逐项算清内部成本，再生成不含成本明细的市场估价。</p><span class="source-chip">Obsidian 历史价</span></div>
    </header>
    <nav class="tabs" aria-label="报价步骤">
      <button class="tab active" data-tab="project" type="button">项目信息</button>
      <button class="tab" data-tab="items" type="button">物料明细</button>
      <button class="tab" data-tab="fees" type="button">费用汇总</button>
    </nav>
    <section class="content">
      <section class="panel active" data-panel="project">
        <div class="section-head"><div><h2>建立内部测算</h2><p>先明确项目和交付时间，再逐项录入物料。</p></div><span class="badge">筹备组</span></div>
        <div class="form-grid">
          <div class="field wide"><label for="projectName">项目名称 *</label><input id="projectName" maxlength="120" placeholder="例如：柳州用户共创会活动物料"></div>
          <div class="field"><label for="clientName">客户 / 品牌</label><input id="clientName" maxlength="80" placeholder="例如：五菱"></div>
          <div class="field"><label for="deliveryDate">交付日期</label><input id="deliveryDate" type="date"></div>
          <div class="field"><label for="validityDays">估价有效期（天）</label><input id="validityDays" type="number" min="1" max="365" value="15"></div>
          <div class="field wide"><label for="notes">内部备注</label><textarea id="notes" maxlength="800" placeholder="税口径、交付地点、开票要求或其他采购边界"></textarea></div>
        </div>
        <div class="truth-note">历史报价只用于预算初算。系统会保留供应商和 Obsidian 来源；正式采购前仍需确认含税、运输、安装、起订量和交付周期。</div>
      </section>
      <section class="panel" data-panel="items">
        <div class="section-head"><div><h2>逐项录入物料</h2><p>输入物料名后可匹配 Obsidian；Excel 会预留图示列，并带出每项物料备注。</p></div><span class="badge" id="itemCount">0 项</span></div>
        <div class="items" id="items"></div>
        <button class="add" id="addItem" type="button">＋ 新增物料</button>
      </section>
      <section class="panel" data-panel="fees">
        <div class="section-head"><div><h2>分列附加费用</h2><p>所有费用单独计算，不暗中并入物料单价。</p></div><span class="badge">实时测算</span></div>
        <div class="fees">
          <div class="field"><label for="transportFee">运输费（元）</label><input class="fee-input" id="transportFee" type="number" min="0" step="0.01"></div>
          <div class="field"><label for="installationFee">安装费（元）</label><input class="fee-input" id="installationFee" type="number" min="0" step="0.01"></div>
          <div class="field"><label for="rushFee">加急费（元）</label><input class="fee-input" id="rushFee" type="number" min="0" step="0.01"></div>
          <div class="field"><label for="lossRate">损耗率（%）</label><input class="fee-input" id="lossRate" type="number" min="0" max="100" step="0.01"></div>
          <div class="field"><label for="profitRate">利润率（%）</label><input class="fee-input" id="profitRate" type="number" min="0" max="100" step="0.01"></div>
          <div class="field"><label for="taxRate">税率（%）</label><input class="fee-input" id="taxRate" type="number" min="0" max="100" step="0.01"></div>
        </div>
        <div class="ledger">
          <div class="ledger-row"><span>已定价物料小计</span><strong id="itemsSubtotal">¥0.00</strong></div>
          <div class="ledger-row"><span>损耗费</span><strong id="lossFee">¥0.00</strong></div>
          <div class="ledger-row"><span>运输 + 安装 + 加急</span><strong id="otherFees">¥0.00</strong></div>
          <div class="ledger-row"><span>利润额</span><strong id="profitFee">¥0.00</strong></div>
          <div class="ledger-row"><span>未税合计</span><strong id="preTaxTotal">¥0.00</strong></div>
          <div class="ledger-row"><span>税费</span><strong id="taxFee">¥0.00</strong></div>
          <div class="ledger-row pending-line"><span>待询价（不计入合计）</span><strong id="pendingCount">0 项</strong></div>
          <div class="ledger-row total"><span>内部测算总额</span><strong id="grandTotal">¥0.00</strong></div>
        </div>
      </section>
    </section>
    <footer class="footer">
      <div class="footer-total"><small>内部含税合计</small><strong id="footerTotal">¥0.00</strong></div>
      <button class="save" id="save" type="button">生成估价结果</button>
      <div class="status" id="status">金额由服务端复算；生成与发送全程不调用模型。</div>
      <div class="internal-delivery" id="internalDelivery" hidden><span>筹备组留档</span><a id="internalExcel" target="_blank" rel="noopener">下载成本清单 Excel</a></div>
    </footer>
    </section>
    <section class="supplier-surface" id="supplierSurface" data-surface="supplier" hidden>
      <header class="hero">
        <div class="hero-top"><p class="kicker">SUPPLIER · PRICE INTAKE</p><button class="back-home" id="supplierBackHome" type="button">返回工具首页</button></div>
        <h1>新增合作公司价格</h1>
        <div class="hero-row"><p class="hero-copy">录入完成即可参与后续历史报价匹配；所有新价格先标记待复核。</p><span class="source-chip">NAS / Obsidian</span></div>
      </header>
      <section class="supplier-content">
        <div class="section-head"><div><h2>合作公司信息</h2><p>报价资料和价格行会归档到同一份供应商记录。</p></div><span class="badge">员工录入</span></div>
        <div class="supplier-meta">
          <div class="field wide"><label for="supplierName">合作公司名称 *</label><input id="supplierName" maxlength="80" placeholder="例如：柳州新伙伴广告有限公司"></div>
          <div class="field"><label for="supplierCategory">价格分类</label><select id="supplierCategory"><option>搭建类</option><option>礼品类</option><option>印刷类</option><option>制作类</option><option>其他类</option></select></div>
          <div class="field"><label for="supplierQuoteDate">报价日期</label><input id="supplierQuoteDate" type="date"></div>
        </div>
        <div class="upload-box">
          <button id="chooseSupplierFile" type="button">上传报价资料</button>
          <div class="upload-copy"><strong id="supplierFileName">尚未选择原始资料</strong><small>Excel / CSV 可自动读取价格行；PDF、Word、图片作为原始凭证保存，价格需在下方录入。</small></div>
          <input class="supplier-file" id="supplierFile" type="file" accept=".xlsx,.xlsm,.csv,.pdf,.docx,.png,.jpg,.jpeg,.webp">
        </div>
        <div class="section-head"><div><h2>价格明细</h2><p>单位必须填写真实计价口径；历史资料未记录单位时会保持空白。</p></div><span class="badge" id="supplierItemCount">1 项</span></div>
        <div class="supplier-items" id="supplierItems"></div>
        <button class="add" id="addSupplierItem" type="button">＋ 新增一项价格</button>
        <div class="field wide" style="margin-top:10px"><label for="supplierNotes">录入备注</label><textarea id="supplierNotes" maxlength="800" placeholder="例如：含税、不含运输、有效期或联系人说明"></textarea></div>
        <div class="truth-note">单位不明确时不要填写“待确认”。请向合作公司确认后填写“㎡、个、套、米、份”等真实单位再保存。</div>
      </section>
      <footer class="supplier-footer">
        <button class="save" id="saveSupplierPrices" type="button">保存到历史报价库</button>
        <div class="status" id="supplierStatus">保存位置：NAS / Obsidian；状态：待复核。</div>
      </footer>
    </section>
    <section class="market-result" id="resultSurface" data-surface="result" hidden>
      <div class="market-shell">
        <p class="market-kicker">MARKET ESTIMATE</p>
        <h2>市场部估价结果</h2>
        <p class="market-summary">仅展示项目估价、有效期和状态，可直接发送给市场部使用。</p>
        <div class="estimate-card">
          <div class="estimate-row"><span>项目</span><strong id="resultProject">-</strong></div>
          <div class="estimate-row"><span>客户 / 品牌</span><strong id="resultClient">-</strong></div>
          <div class="estimate-amount">估价金额<strong id="resultAmount">¥0.00</strong></div>
          <div class="estimate-row"><span>有效期</span><strong id="resultValidity">15 天</strong></div>
          <div class="estimate-row"><span>状态</span><strong>估价方案</strong></div>
        </div>
        <div class="result-actions">
          <button class="back-estimate" id="backEstimate" type="button">返回调整</button>
          <button class="send-estimate" id="sendEstimate" type="button">发送估价结果</button>
        </div>
        <div class="market-deliveries" id="marketDeliveries" hidden>
          <a id="marketWord" target="_blank" rel="noopener">下载 Word 估价</a>
          <a id="marketPdf" target="_blank" rel="noopener">下载 PDF 估价</a>
          <button class="sync-doc" id="syncDoc" type="button">同步到飞书云文档</button>
        </div>
        <div class="status" id="resultStatus"></div>
        <p class="market-note">正式采购金额以最终复核为准。</p>
      </div>
    </section>
    <div class="image-lightbox" id="imageLightbox" role="dialog" aria-modal="true" aria-label="物料图片放大预览" hidden>
      <div class="image-lightbox-panel">
        <button class="image-lightbox-close" id="imageLightboxClose" type="button" aria-label="关闭图片预览">关闭</button>
        <img class="image-lightbox-image" id="imageLightboxImage" alt="物料图片放大预览">
        <p class="image-lightbox-hint">点击空白处或按 Esc 关闭</p>
      </div>
    </div>
  </main>
  <script>
    const config=__QUOTATION_CONFIG__;
    const base=window.location.pathname.replace(/\/$/,'');
    const homeSurface=document.getElementById('homeSurface');
    const items=document.getElementById('items');
    const save=document.getElementById('save');
    const status=document.getElementById('status');
    const calculatorSurface=document.getElementById('calculatorSurface');
    const supplierSurface=document.getElementById('supplierSurface');
    const resultSurface=document.getElementById('resultSurface');
    const sendEstimate=document.getElementById('sendEstimate');
    const resultStatus=document.getElementById('resultStatus');
    const internalDelivery=document.getElementById('internalDelivery');
    const internalExcel=document.getElementById('internalExcel');
    const marketDeliveries=document.getElementById('marketDeliveries');
    const marketWord=document.getElementById('marketWord');
    const marketPdf=document.getElementById('marketPdf');
    const syncDoc=document.getElementById('syncDoc');
    const imageLightbox=document.getElementById('imageLightbox');
    const imageLightboxImage=document.getElementById('imageLightboxImage');
    const imageLightboxClose=document.getElementById('imageLightboxClose');
    let pendingTaskData=null;
    let feishuDocUrl=config.feishuDocUrl||'';
    let imageLightboxTrigger=null;
    const money=value=>`¥${(Number(value)||0).toFixed(2)}`;
    const number=value=>Math.max(0,Number(value)||0);
    const commonUnits=['㎡','米','延米','张','份','本','册','页','P','联','令','色','款','块','面','幅','卷','个','件','套','盒','包','箱','支','根','台','批','项','天','工时'];
    const unitOptionsHtml=`<option value="">请选择单位</option><optgroup label="印刷纸品">${['张','份','本','册','页','P','联','令','色','款'].map(unit=>`<option value="${unit}">${unit}</option>`).join('')}</optgroup><optgroup label="广告制作 / 搭建">${['㎡','米','延米','块','面','幅','卷'].map(unit=>`<option value="${unit}">${unit}</option>`).join('')}</optgroup><optgroup label="通用">${['个','件','套','盒','包','箱','支','根','台','批','项','天','工时'].map(unit=>`<option value="${unit}">${unit}</option>`).join('')}</optgroup>`;
    function showToolSurface(name){
      homeSurface.hidden=name!=='home';calculatorSurface.hidden=name!=='calculator';supplierSurface.hidden=name!=='supplier';resultSurface.hidden=name!=='result';
    }
    const openImageLightbox=(source,label,trigger)=>{imageLightboxTrigger=trigger;imageLightboxImage.src=source;imageLightboxImage.alt=label;imageLightbox.hidden=false;imageLightboxClose.focus();};
    const closeImageLightbox=()=>{if(imageLightbox.hidden)return;imageLightbox.hidden=true;imageLightboxImage.removeAttribute('src');imageLightboxTrigger?.focus();imageLightboxTrigger=null;};
    imageLightboxClose.addEventListener('click',closeImageLightbox);
    imageLightbox.addEventListener('click',event=>{if(event.target===imageLightbox)closeImageLightbox();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!imageLightbox.hidden)closeImageLightbox();});
    document.getElementById('openCalculator').addEventListener('click',()=>showToolSurface('calculator'));
    document.getElementById('openSupplierManual').addEventListener('click',()=>showToolSurface('supplier'));
    document.getElementById('openSupplierUpload').addEventListener('click',()=>{showToolSurface('supplier');document.getElementById('supplierFile').click();});
    document.getElementById('calculatorBackHome').addEventListener('click',()=>showToolSurface('home'));
    document.getElementById('supplierBackHome').addEventListener('click',()=>showToolSurface('home'));
    document.getElementById('projectName').value=config.projectName||'';
    document.getElementById('clientName').value=config.clientName||'';
    document.getElementById('deliveryDate').value=config.deliveryDate||'';
    document.getElementById('validityDays').value=config.validityDays||'15';
    document.getElementById('notes').value=config.notes||'';
    for(const [id,value] of Object.entries({transportFee:config.transportFee,installationFee:config.installationFee,rushFee:config.rushFee,lossRate:config.lossRate,profitRate:config.profitRate,taxRate:config.taxRate})){document.getElementById(id).value=value||'0';}
    document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(item=>item.classList.toggle('active',item===tab));document.querySelectorAll('.panel').forEach(panel=>panel.classList.toggle('active',panel.dataset.panel===tab.dataset.tab));}));
    function recalculate(){
      let subtotal=0,pending=0;
      document.querySelectorAll('.quote-row').forEach((row,index)=>{row.querySelector('.row-index').textContent=`物料 ${index+1}`;const quantity=number(row.querySelector('.quantity').value);const unitPrice=row.querySelector('.unit-price').value.trim();const state=row.querySelector('.row-state');if(unitPrice===''){pending+=1;state.textContent='待询价';state.className='row-state';row.querySelector('.line-total').textContent='待询价';}else{const line=quantity*number(unitPrice);subtotal+=line;state.textContent=row.querySelector('.source-path').value?'历史价待复核':'已定价';state.className='row-state priced';row.querySelector('.line-total').textContent=money(line);}});
      const transport=number(document.getElementById('transportFee').value);const installation=number(document.getElementById('installationFee').value);const rush=number(document.getElementById('rushFee').value);const loss=subtotal*number(document.getElementById('lossRate').value)/100;const costBase=subtotal+loss+transport+installation+rush;const profit=costBase*number(document.getElementById('profitRate').value)/100;const preTax=costBase+profit;const tax=preTax*number(document.getElementById('taxRate').value)/100;const grand=preTax+tax;
      document.getElementById('itemsSubtotal').textContent=money(subtotal);document.getElementById('lossFee').textContent=money(loss);document.getElementById('otherFees').textContent=money(transport+installation+rush);document.getElementById('profitFee').textContent=money(profit);document.getElementById('preTaxTotal').textContent=money(preTax);document.getElementById('taxFee').textContent=money(tax);document.getElementById('pendingCount').textContent=`${pending} 项`;document.getElementById('grandTotal').textContent=money(grand);document.getElementById('footerTotal').textContent=money(grand);document.getElementById('itemCount').textContent=`${document.querySelectorAll('.quote-row').length} 项`;
    }
    function addRow(item={}){
      if(document.querySelectorAll('.quote-row').length>=20){status.textContent='一次最多填写 20 项物料。';status.className='status bad';return;}
      const row=document.createElement('article');row.className='quote-row';row.innerHTML=`<div class="row-head"><div><span class="row-index"></span><span class="row-state">待询价</span></div><button class="remove" type="button">删除</button></div><div class="row-body"><div class="field wide"><label>物料名称 *</label><div class="lookup"><input class="item-name" maxlength="80" placeholder="例如：背胶、X 展架、礼盒"><button class="lookup-button" type="button">匹配历史价</button></div></div><div class="field wide"><label>规格 / 工艺</label><input class="specification" maxlength="160" placeholder="尺寸、材质、覆膜、定制要求"></div><div class="field wide"><label>图示（进入 Excel）</label><div class="item-image-upload"><button class="item-image-button" type="button">上传图片</button><div class="item-image-meta"><span class="item-image-name">未上传</span><small class="item-image-hint">单张或批量，最多 6 张</small></div><button class="item-image-clear" type="button" hidden>清空图片</button><div class="item-image-previews" hidden></div><input class="item-image-input" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><input class="image-paths" type="hidden" value="[]"></div></div><div class="field wide"><label>物料备注（进入 Excel）</label><input class="remark" maxlength="300" placeholder="例如：官网优惠价、含包装搬运、正式采购前复核"></div><div class="field"><label>数量 *</label><input class="quantity" type="number" min="0.01" step="0.01"></div><div class="field"><label>单位 *</label><select class="unit">${unitOptionsHtml}</select></div><div class="field"><label>单价（元）</label><input class="unit-price" type="number" min="0" step="0.01" placeholder="空白=待询价"></div><div class="line-total"></div><div class="field wide"><label>供应商</label><input class="supplier" maxlength="80" placeholder="采用历史价时自动带入"></div><input class="source-path" type="hidden"><input class="source-status" type="hidden"><div class="matches"></div></div>`;
      let previewUrls=[];const readImagePaths=()=>{try{return JSON.parse(row.querySelector('.image-paths').value||'[]');}catch(error){return [];}};const updateRowImages=paths=>{const normalized=paths.slice(0,6);row.querySelector('.image-paths').value=JSON.stringify(normalized);row.querySelector('.item-image-name').textContent=normalized.length?`已上传 ${normalized.length} 张`:'未上传';row.querySelector('.item-image-button').textContent=normalized.length?'继续添加':'上传图片';row.querySelector('.item-image-clear').hidden=!normalized.length;const previews=row.querySelector('.item-image-previews');previews.replaceChildren();for(const [previewIndex,previewUrl] of previewUrls.entries()){const previewButton=document.createElement('button');previewButton.className='item-image-preview-button';previewButton.type='button';previewButton.setAttribute('aria-label',`放大查看物料图片 ${previewIndex+1}`);previewButton.title='点击放大';const preview=document.createElement('img');preview.src=previewUrl;preview.alt=`物料图示 ${previewIndex+1}`;previewButton.addEventListener('click',()=>openImageLightbox(previewUrl,preview.alt,previewButton));previewButton.append(preview);previews.append(previewButton);}previews.hidden=!previewUrls.length;};const initialImagePaths=Array.isArray(item.image_paths)?item.image_paths:(item.image_path?[item.image_path]:[]);updateRowImages(initialImagePaths);
      row.querySelector('.item-name').value=item.item_name||'';row.querySelector('.specification').value=item.specification||'';row.querySelector('.remark').value=item.remark||'';row.querySelector('.quantity').value=item.quantity||'1';const initialUnit=({'平方米':'㎡','m²':'㎡','m2':'㎡','延长米':'延米'}[item.unit]||item.unit||'');if(initialUnit&&!commonUnits.includes(initialUnit)){const option=document.createElement('option');option.value=initialUnit;option.textContent=`${initialUnit}（历史单位）`;row.querySelector('.unit').append(option);}row.querySelector('.unit').value=initialUnit;row.querySelector('.unit-price').value=item.unit_price||'';row.querySelector('.supplier').value=item.supplier||'';row.querySelector('.source-path').value=item.source_path||'';row.querySelector('.source-status').value=item.source_status||'';
      row.querySelectorAll('input').forEach(input=>input.addEventListener('input',recalculate));
      row.querySelector('.item-image-button').addEventListener('click',()=>row.querySelector('.item-image-input').click());
      row.querySelector('.item-image-input').addEventListener('change',async()=>{const input=row.querySelector('.item-image-input');const selectedFiles=[...input.files];if(!selectedFiles.length)return;const currentPaths=readImagePaths();if(currentPaths.length+selectedFiles.length>6){status.textContent=`每项物料最多上传 6 张，当前已有 ${currentPaths.length} 张。`;status.className='status bad';input.value='';return;}if(selectedFiles.some(file=>file.size>10*1024*1024)){status.textContent='单张图片不能超过 10 MB。';status.className='status bad';input.value='';return;}const button=row.querySelector('.item-image-button');button.disabled=true;button.textContent=`上传 ${selectedFiles.length} 张…`;const form=new FormData();for(const selectedFile of selectedFiles)form.append('files',selectedFile);try{const response=await fetch(`${base}/quotation-item-image`,{method:'POST',body:form});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'图片上传失败');const uploadedImages=Array.isArray(payload.images)?payload.images:[payload];previewUrls.push(...selectedFiles.map(file=>URL.createObjectURL(file)));updateRowImages([...currentPaths,...uploadedImages.map(image=>image.image_path)]);status.textContent=`已上传 ${uploadedImages.length} 张图片到 NAS / knowledge，并会排入 Excel 图示列。`;status.className='status ok';}catch(error){updateRowImages(currentPaths);status.textContent=error.message||'图片上传失败，请重试';status.className='status bad';}finally{button.disabled=false;button.textContent=readImagePaths().length?'继续添加':'上传图片';input.value='';}});
      row.querySelector('.item-image-clear').addEventListener('click',()=>{previewUrls.forEach(url=>URL.revokeObjectURL(url));previewUrls=[];updateRowImages([]);status.textContent='已从当前物料中清空图片。';status.className='status';});
      row.querySelector('.remove').addEventListener('click',()=>{if(document.querySelectorAll('.quote-row').length===1){row.querySelectorAll('input:not([type=hidden])').forEach(input=>input.value='');row.querySelector('.quantity').value='1';row.querySelector('.unit').value='';row.querySelector('.source-path').value='';row.querySelector('.source-status').value='';previewUrls.forEach(url=>URL.revokeObjectURL(url));previewUrls=[];updateRowImages([]);}else{previewUrls.forEach(url=>URL.revokeObjectURL(url));row.remove();}recalculate();});
      row.querySelector('.lookup-button').addEventListener('click',async()=>{const query=row.querySelector('.item-name').value.trim();const matches=row.querySelector('.matches');if(!query){status.textContent='先填写物料名称，再匹配历史价。';status.className='status bad';row.querySelector('.item-name').focus();return;}matches.className='matches open';matches.replaceChildren();const waiting=document.createElement('button');waiting.className='match empty';waiting.type='button';waiting.textContent='正在检索 Obsidian 历史报价…';matches.append(waiting);try{const response=await fetch(`${base}/quotation-search?query=${encodeURIComponent(query)}`);const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'历史价检索失败');matches.replaceChildren();if(!payload.matches.length){const empty=document.createElement('button');empty.className='match empty';empty.type='button';empty.textContent='没有匹配价格，保留为待询价';matches.append(empty);return;}for(const match of payload.matches){const button=document.createElement('button');button.type='button';button.className='match';const title=document.createElement('strong');title.textContent=match.unit?`${match.item_name} · ¥${match.unit_price}/${match.unit}`:`${match.item_name} · ¥${match.unit_price}（单位未记录）`;const detail=document.createElement('small');detail.textContent=`${match.supplier}｜${match.specification||'无补充规格'}｜${match.source_status}`;button.append(title,detail);button.addEventListener('click',()=>{row.querySelector('.item-name').value=match.item_name;row.querySelector('.specification').value=match.specification||row.querySelector('.specification').value;const matchedUnit=({'平方米':'㎡','m²':'㎡','m2':'㎡','延长米':'延米'}[match.unit]||match.unit||'');if(matchedUnit&&!commonUnits.includes(matchedUnit)&&![...row.querySelector('.unit').options].some(option=>option.value===matchedUnit)){const option=document.createElement('option');option.value=matchedUnit;option.textContent=`${matchedUnit}（历史单位）`;row.querySelector('.unit').append(option);}row.querySelector('.unit').value=matchedUnit;row.querySelector('.unit-price').value=match.unit_price;row.querySelector('.supplier').value=match.supplier;row.querySelector('.source-path').value=match.source_path;row.querySelector('.source-status').value=match.source_status;matches.className='matches';if(matchedUnit){status.textContent=`已采用 ${match.supplier} 的历史价，正式采购前需复核。`;status.className='status ok';}else{status.textContent='该历史资料未记录单位，请从下拉列表选择真实计价单位。';status.className='status bad';row.querySelector('.unit').focus();}recalculate();});matches.append(button);}}catch(error){matches.replaceChildren();const failed=document.createElement('button');failed.className='match empty';failed.type='button';failed.textContent=error.message||'历史价检索失败';matches.append(failed);}});
      items.append(row);recalculate();
    }
    document.getElementById('addItem').addEventListener('click',()=>addRow());document.querySelectorAll('.fee-input').forEach(input=>input.addEventListener('input',recalculate));
    for(const item of config.items||[])addRow(item);if(!document.querySelector('.quote-row'))addRow();
    const supplierItems=document.getElementById('supplierItems');
    const supplierStatus=document.getElementById('supplierStatus');
    const supplierFile=document.getElementById('supplierFile');
    const saveSupplierPrices=document.getElementById('saveSupplierPrices');
    document.getElementById('supplierQuoteDate').value=new Date().toLocaleDateString('sv-SE');
    function addSupplierRow(){
      if(document.querySelectorAll('.supplier-row').length>=50){supplierStatus.textContent='一次最多录入 50 项价格。';supplierStatus.className='status bad';return;}
      const row=document.createElement('article');row.className='supplier-row';row.innerHTML=`<div class="row-head"><span class="row-index"></span><button class="remove" type="button">删除</button></div><div class="row-body"><div class="field wide"><label>物料名称 *</label><input class="supplier-item-name" maxlength="80" placeholder="例如：背胶、X 展架、礼盒"></div><div class="field wide"><label>规格 / 工艺</label><input class="supplier-specification" maxlength="160" placeholder="尺寸、材质、覆膜、定制要求"></div><div class="field"><label>单位 *</label><select class="supplier-unit">${unitOptionsHtml}</select></div><div class="field"><label>单价（元）*</label><input class="supplier-unit-price" type="number" min="0.01" step="0.01" placeholder="0.00"></div></div>`;
      row.querySelector('.remove').addEventListener('click',()=>{if(document.querySelectorAll('.supplier-row').length===1){row.querySelectorAll('input').forEach(input=>input.value='');row.querySelector('.supplier-unit').value='';}else row.remove();[...document.querySelectorAll('.supplier-row')].forEach((item,index)=>item.querySelector('.row-index').textContent=`价格 ${index+1}`);document.getElementById('supplierItemCount').textContent=`${document.querySelectorAll('.supplier-row').length} 项`;});
      supplierItems.append(row);[...document.querySelectorAll('.supplier-row')].forEach((item,index)=>item.querySelector('.row-index').textContent=`价格 ${index+1}`);document.getElementById('supplierItemCount').textContent=`${document.querySelectorAll('.supplier-row').length} 项`;
    }
    document.getElementById('addSupplierItem').addEventListener('click',addSupplierRow);
    document.getElementById('chooseSupplierFile').addEventListener('click',()=>supplierFile.click());
    supplierFile.addEventListener('change',()=>{const file=supplierFile.files[0];document.getElementById('supplierFileName').textContent=file?`${file.name} · ${(file.size/1024/1024).toFixed(1)} MB`:'尚未选择原始资料';supplierStatus.textContent=file?'已选择原始报价资料，保存时会一并归档。':'保存位置：NAS / Obsidian；状态：待复核。';supplierStatus.className='status';});
    saveSupplierPrices.addEventListener('click',async()=>{
      const supplierName=document.getElementById('supplierName').value.trim();
      if(supplierName.length<2){supplierStatus.textContent='请填写合作公司名称。';supplierStatus.className='status bad';document.getElementById('supplierName').focus();return;}
      const submittedItems=[];
      for(const [index,row] of [...document.querySelectorAll('.supplier-row')].entries()){
        const itemName=row.querySelector('.supplier-item-name').value.trim();const specification=row.querySelector('.supplier-specification').value.trim();const unit=row.querySelector('.supplier-unit').value.trim();const unitPrice=row.querySelector('.supplier-unit-price').value.trim();
        if(!itemName&&!specification&&!unit&&!unitPrice)continue;
        if(!itemName){supplierStatus.textContent=`请填写第 ${index+1} 项物料名称。`;supplierStatus.className='status bad';row.querySelector('.supplier-item-name').focus();return;}
        if(!unit||['待确认','待定','未知','-'].includes(unit)){supplierStatus.textContent=`请填写第 ${index+1} 项真实计价单位。`;supplierStatus.className='status bad';row.querySelector('.supplier-unit').focus();return;}
        if(number(unitPrice)<=0){supplierStatus.textContent=`请填写第 ${index+1} 项有效单价。`;supplierStatus.className='status bad';row.querySelector('.supplier-unit-price').focus();return;}
        submittedItems.push({item_name:itemName,specification,unit,unit_price:unitPrice});
      }
      const sourceFile=supplierFile.files[0];
      if(!submittedItems.length&&!sourceFile){supplierStatus.textContent='请至少录入一项价格，或上传可识别的 Excel / CSV。';supplierStatus.className='status bad';return;}
      if(sourceFile&&sourceFile.size>20*1024*1024){supplierStatus.textContent='原始报价资料不能超过 20 MB。';supplierStatus.className='status bad';return;}
      const form=new FormData();form.append('payload',JSON.stringify({supplier_name:supplierName,category:document.getElementById('supplierCategory').value,quote_date:document.getElementById('supplierQuoteDate').value,notes:document.getElementById('supplierNotes').value.trim(),items:submittedItems}));if(sourceFile)form.append('file',sourceFile);
      saveSupplierPrices.disabled=true;saveSupplierPrices.textContent='正在保存…';supplierStatus.textContent='正在写入 NAS / Obsidian 历史报价库';supplierStatus.className='status';
      try{const response=await fetch(`${base}/supplier-prices`,{method:'POST',body:form});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'保存失败');supplierStatus.textContent=`已保存 ${payload.item_count} 项价格，已进入历史报价库（待复核）。`;supplierStatus.className='status ok';supplierItems.replaceChildren();addSupplierRow();supplierFile.value='';document.getElementById('supplierFileName').textContent='尚未选择原始资料';document.getElementById('supplierNotes').value='';}
      catch(error){supplierStatus.textContent=error.message||'保存失败，请重试';supplierStatus.className='status bad';}
      finally{saveSupplierPrices.disabled=false;saveSupplierPrices.textContent='保存到历史报价库';}
    });
    addSupplierRow();
    function collectTaskData(){
      const projectName=document.getElementById('projectName').value.trim();
      if(!projectName){status.textContent='请先填写项目名称。';status.className='status bad';document.querySelector('[data-tab="project"]').click();document.getElementById('projectName').focus();return null;}
      const quotationItems=[];
      for(const [index,row] of [...document.querySelectorAll('.quote-row')].entries()){
        const itemName=row.querySelector('.item-name').value.trim();const quantity=row.querySelector('.quantity').value.trim();const unit=row.querySelector('.unit').value.trim();
        if(!itemName||number(quantity)<=0){status.textContent=`请补全第 ${index+1} 项的物料名称和有效数量。`;status.className='status bad';document.querySelector('[data-tab="items"]').click();return null;}
        if(!unit){status.textContent=`请填写第 ${index+1} 项的真实计价单位。`;status.className='status bad';document.querySelector('[data-tab="items"]').click();row.querySelector('.unit').focus();return null;}
        if(row.querySelector('.unit-price').value.trim()===''){status.textContent=`第 ${index+1} 项仍待询价，补齐价格后才能生成市场部估价结果。`;status.className='status bad';document.querySelector('[data-tab="items"]').click();row.querySelector('.unit-price').focus();return null;}
        const imagePaths=JSON.parse(row.querySelector('.image-paths').value||'[]');quotationItems.push({item_name:itemName,specification:row.querySelector('.specification').value.trim(),remark:row.querySelector('.remark').value.trim(),image_path:imagePaths[0]||'',image_paths:imagePaths,quantity,unit,unit_price:row.querySelector('.unit-price').value.trim(),supplier:row.querySelector('.supplier').value.trim(),source_path:row.querySelector('.source-path').value,source_status:row.querySelector('.source-status').value});
      }
      return {project_name:projectName,client_name:document.getElementById('clientName').value.trim(),delivery_date:document.getElementById('deliveryDate').value,validity_days:document.getElementById('validityDays').value||'15',quotation_items:JSON.stringify(quotationItems),transport_fee:document.getElementById('transportFee').value||'0',installation_fee:document.getElementById('installationFee').value||'0',rush_fee:document.getElementById('rushFee').value||'0',loss_rate:document.getElementById('lossRate').value||'0',profit_rate:document.getElementById('profitRate').value||'0',tax_rate:document.getElementById('taxRate').value||'0',quotation_notes:document.getElementById('notes').value.trim(),model_choice:'auto'};
    }
    save.addEventListener('click',()=>{
      pendingTaskData=collectTaskData();if(!pendingTaskData)return;recalculate();
      document.getElementById('resultProject').textContent=pendingTaskData.project_name;
      document.getElementById('resultClient').textContent=pendingTaskData.client_name||'-';
      document.getElementById('resultAmount').textContent=document.getElementById('footerTotal').textContent;
      document.getElementById('resultValidity').textContent=`${pendingTaskData.validity_days} 天`;
      status.textContent='内部测算已完成，可检查市场部估价结果。';status.className='status ok';resultStatus.textContent='确认金额无误后发送给市场部。';resultStatus.className='status';showToolSurface('result');
    });
    document.getElementById('backEstimate').addEventListener('click',()=>showToolSurface('calculator'));
    function renderDeliverables(deliverables){
      const files=deliverables||{};
      if(/^https?:\/\//.test(files.internal_xlsx||'')){internalExcel.href=files.internal_xlsx;internalDelivery.hidden=false;}
      if(/^https?:\/\//.test(files.market_docx||'')){marketWord.href=files.market_docx;marketDeliveries.hidden=false;}
      if(/^https?:\/\//.test(files.market_pdf||'')){marketPdf.href=files.market_pdf;marketDeliveries.hidden=false;}
      if(feishuDocUrl){syncDoc.textContent='已同步 · 打开飞书云文档';}
    }
    syncDoc.addEventListener('click',async()=>{
      if(feishuDocUrl){window.open(feishuDocUrl,'_blank','noopener');return;}
      syncDoc.disabled=true;syncDoc.textContent='正在同步…';resultStatus.textContent='正在创建脱敏后的飞书云文档';resultStatus.className='status';
      try{const response=await fetch(`${base}/quotation-sync-doc`,{method:'POST'});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'同步失败');feishuDocUrl=payload.feishu_doc_url;syncDoc.textContent='已同步 · 打开飞书云文档';resultStatus.textContent='飞书云文档已生成，可点击打开。';resultStatus.className='status ok';syncDoc.disabled=false;}
      catch(error){resultStatus.textContent=error.message||'同步失败，请重试';resultStatus.className='status bad';syncDoc.disabled=false;syncDoc.textContent='同步到飞书云文档';}
    });
    sendEstimate.addEventListener('click',async()=>{
      if(!pendingTaskData)return;sendEstimate.disabled=true;sendEstimate.textContent='正在发送…';resultStatus.textContent='服务端正在复算并发送估价结果';resultStatus.className='status';
      try{const response=await fetch(`${base}/workspace`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_data:pendingTaskData})});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'发送失败');feishuDocUrl=payload.feishu_doc_url||'';renderDeliverables(payload.deliverables);resultStatus.textContent=`估价结果已发送到飞书，V${payload.version} 文件已生成并归档 NAS / knowledge。`;resultStatus.className='status ok';sendEstimate.textContent='已发送';}
      catch(error){resultStatus.textContent=error.message||'发送失败，请重试';resultStatus.className='status bad';sendEstimate.disabled=false;sendEstimate.textContent='发送估价结果';}
    });
    renderDeliverables(config.deliverables);
    recalculate();
  </script>
</body>
</html>"""
    return page.replace("__QUOTATION_CONFIG__", config_json)


def build_task_workspace_page(draft: AttachmentDraft) -> str:
    """Render the responsive task-aware workspace.

    Args:
        draft: Live capability containing the selected task and staged state.

    Returns:
        A dependency-free H5 workspace for Feishu's sidebar and web browser.

    Raises:
        ValueError: If the draft does not contain a supported task type.
    """
    if draft.task_type == "quotation":
        return build_material_quotation_page(draft)
    if draft.task_type == "ai_cdr":
        return build_ai_cdr_page(draft.token)
    schema = WORKSPACE_SCHEMAS.get(draft.task_type)
    if schema is None:
        raise ValueError(f"unsupported assistant task type: {draft.task_type}")
    required_field = TASK_REQUIRED_FIELDS[draft.task_type]
    attachment_items = [
        {
            "filename": str(item.get("filename") or "未命名资料")[:120],
            "source": "飞书云文档" if item.get("type") == "cloud_doc" else "本地文件",
        }
        for item in draft.attachments
    ]
    config = {
        "taskType": draft.task_type,
        "taskLabel": TASK_LABELS[draft.task_type],
        "eyebrow": schema.get("eyebrow", "DC · 任务工作台"),
        "summary": schema.get("summary", "先准备资料，再填写任务要求和交付设置"),
        "tabs": schema.get("tabs", ["提词", "素材", "设置"]),
        "sectionTitles": schema.get(
            "section_titles", ["任务提词", "任务素材", "生成设置"]
        ),
        "requiredField": required_field,
        "promptLabel": schema["prompt_label"],
        "promptPlaceholder": schema["prompt_placeholder"],
        "materialField": schema["material_field"],
        "materialLabel": schema.get("material_label", "资料补充说明"),
        "materialPlaceholder": schema.get(
            "material_placeholder", "链接、页码、必须采用或忽略的信息"
        ),
        "emptyMaterial": schema.get("empty_material", "暂未添加资料"),
        "materialRequirement": schema.get("material_requirement", ""),
        "workflowSteps": schema.get(
            "workflow_steps", ["准备资料", "说明任务", "AI 执行", "直接交付"]
        ),
        "materialGuide": schema.get(
            "material_guide", "先准备任务需要的文件、云文档或来源链接。"
        ),
        "promptGuide": schema.get(
            "prompt_guide", "说明目标、使用场景和必须保留的信息。"
        ),
        "settingsGuide": schema.get("settings_guide", "选择生成方式和最后交付规格。"),
        "quickGoals": schema.get("quick_goals", []),
        "saveLabel": schema.get("save_label", "开始生成并直接交付"),
        "canRevise": draft.task_type in {"copy", "image", "video"},
        "nextLabel": schema.get("next_label", f"下一步：填写{schema['prompt_label']}"),
        "officeMode": draft.task_type in {"research", "file"},
        "settings": schema["settings"],
        "modelLabel": "生图模型选择" if draft.task_type == "image" else "模型选择",
        "modelSummary": (
            "默认智能选择，可固定 Image2 或即梦"
            if draft.task_type == "image"
            else "默认智能选择模型"
        ),
        "modelOptions": (
            [
                ["自动（Image2 优先，即梦兜底）", "auto"],
                ["Image2（仅使用）", "image2"],
                ["即梦（仅使用）", "dreamina"],
            ]
            if draft.task_type == "image"
            else [
                ["智能选择", "auto"],
                ["快速模式", "fast"],
                ["均衡模式", "balanced"],
                ["质量优先", "quality_first"],
            ]
        ),
        "taskData": draft.task_data,
        "attachments": attachment_items,
    }
    config_json = json.dumps(config, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    page = r"""<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#46513a">
  <link rel="icon" href="data:,">
  <title>任务工作台</title>
  <style>
    :root { color-scheme:light; --ink:#25281f; --muted:#777a6d; --line:#dedfd6; --paper:#ffffff; --panel:#ffffff; --olive:#46513a; --olive-2:#657158; --signal:#d96b32; --signal-soft:#fff0e6; --ok:#347558; --warn:#a75225; --forest:var(--olive); --mint:var(--signal); --danger:var(--warn); }
    * { box-sizing:border-box; }
    html,body { margin:0; width:100%; height:100%; overflow:hidden; }
    body { font-family:"Microsoft YaHei","PingFang SC",sans-serif; color:var(--ink); background:#ffffff; }
    button,input,select,textarea { font:inherit; }
    button { cursor:pointer; }
    .app { height:100%; display:grid; grid-template-rows:auto auto minmax(0,1fr) auto; background:#ffffff; }
    .hero { padding:14px 18px 11px; border-bottom:1px solid var(--line); color:var(--ink); background:var(--paper); }
    .hero-top { display:flex; gap:12px; align-items:flex-start; justify-content:space-between; }
    .eyebrow { margin:0 0 4px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.16em; }
    h1 { margin:0; font:700 21px/1.18 "Songti SC","STSong",serif; letter-spacing:-.035em; }
    .llm-chip { flex:0 0 auto; margin-top:2px; padding:5px 8px; border:1px solid #d8c5ae; border-radius:99px; color:#865126; background:#fff7ed; font-size:8px; font-weight:900; letter-spacing:.04em; }
    .hero-summary { margin:6px 0 0; color:var(--muted); font-size:10px; line-height:1.55; }
    .tabs { position:relative; display:grid; grid-template-columns:repeat(3,1fr); padding:0 10px; border-bottom:1px solid var(--line); background:#ffffff; }
    .step { position:relative; display:grid; grid-template-columns:22px minmax(0,1fr); gap:6px; align-items:center; min-height:52px; padding:8px 5px; border:0; color:var(--muted); background:#ffffff; text-align:left; }
    .step::after { content:""; position:absolute; right:-8px; top:25px; width:16px; height:1px; background:var(--line); }
    .step:last-child::after { display:none; }
    .step-number { display:grid; width:22px; height:22px; place-items:center; border:1px solid #cfd2c7; border-radius:50%; color:var(--olive-2); background:#ffffff; font-size:9px; font-weight:900; }
    .step-copy strong,.step-copy small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .step-copy strong { font-size:10px; }
    .step-copy small { margin-top:2px; font-size:8px; font-weight:500; }
    .step.active,.step.complete { color:var(--forest); }
    .step.active .step-number { border-color:var(--signal); color:#ffffff; background:var(--signal); }
    .step.complete .step-number { border-color:var(--forest); color:#ffffff; background:var(--forest); }
    .content { min-height:0; overflow:auto; padding:16px 18px 26px; background:#ffffff; scroll-behavior:smooth; }
    .panel { display:none; }
    .panel.active { display:block; animation:stage-in .18s ease-out; }
    @keyframes stage-in { from { opacity:.35; transform:translateY(5px); } to { opacity:1; transform:none; } }
    .section-title { display:flex; gap:8px; align-items:center; justify-content:space-between; margin-bottom:6px; }
    .section-title h2 { margin:0; font-size:18px; letter-spacing:-.035em; }
    .badge { flex:0 0 auto; padding:4px 8px; border:0; border-radius:99px; color:#6d5937; background:#eee7d2; font-size:8px; font-weight:800; }
    .guide { margin:0 0 14px; padding:9px 10px; border-left:3px solid var(--signal); border-radius:0 9px 9px 0; color:#665b49; background:var(--signal-soft); font-size:10px; line-height:1.65; }
    label { display:block; margin:0 0 7px; color:#5e6256; font-size:10px; font-weight:800; }
    textarea,input,select { width:100%; border:1px solid var(--line); border-radius:10px; outline:none; color:var(--ink); background:#ffffff; }
    textarea:focus,input:focus,select:focus { border-color:var(--signal); box-shadow:0 0 0 3px rgba(217,107,50,.1); }
    textarea { min-height:128px; padding:12px 13px; resize:vertical; font-size:12px; line-height:1.65; }
    input,select { min-height:42px; padding:0 11px; font-size:11px; }
    .prompt-tools { display:flex; justify-content:flex-end; margin-top:8px; }
    .secondary { min-height:36px; padding:0 12px; border:1px solid #cfd2c7; border-radius:9px; color:var(--olive); background:#f7f8f2; font-size:10px; font-weight:900; }
    .quick-title { margin:15px 0 8px; color:var(--olive-2); font-size:9px; font-weight:900; }
    .quick-goals { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .quick-goal { min-height:58px; padding:9px 10px; border:1px solid var(--line); border-radius:10px; color:var(--olive); background:#ffffff; font-size:9px; line-height:1.4; text-align:left; box-shadow:0 5px 16px rgba(61,59,43,.04); }
    .quick-goal:active { border-color:var(--signal); }
    .quick-goal strong,.quick-goal span { display:block; }
    .quick-goal strong { font-size:10px; }
    .quick-goal span { margin-top:4px; color:var(--muted); font-size:8px; }
    .candidates { display:grid; gap:7px; margin-top:10px; }
    .candidate { padding:10px 11px; border:1px solid #ded9c9; border-radius:9px; color:var(--olive); background:#faf8ee; font-size:10px; line-height:1.5; text-align:left; }
    .candidate strong,.candidate small,.candidate span { display:block; }
    .candidate strong { color:var(--forest); font-size:11px; }
    .candidate small { margin-top:3px; color:var(--olive-2); font-size:9px; }
    .candidate span { max-height:84px; margin-top:7px; overflow:hidden; color:var(--muted); font-size:9px; white-space:pre-wrap; }
    .memory-hits { padding:9px 10px; border:1px solid #d8c5ae; border-radius:9px; color:#665b49; background:var(--signal-soft); font-size:9px; line-height:1.55; }
    .memory-hits strong,.memory-hits span { display:block; }
    .memory-hits span { margin-top:3px; color:var(--muted); }
    .material-actions { display:grid; grid-template-columns:1fr 1fr; gap:9px; margin-top:11px; }
    .source { display:grid; grid-template-columns:30px minmax(0,1fr); gap:8px; align-items:center; min-height:58px; padding:8px 9px; border:1px solid #cfd2c7; border-radius:11px; color:var(--olive); background:#f7f8f2; text-align:left; }
    .source-icon { display:grid; width:30px; height:30px; place-items:center; border:1px solid var(--olive); border-radius:9px; color:#ffffff; background:var(--olive); font-size:16px; font-weight:400; }
    .source strong,.source small { display:block; }
    .source strong { font-size:10px; }
    .source small { margin-top:2px; color:var(--muted); font-size:7px; font-weight:500; }
    input[type=file] { position:absolute; opacity:0; pointer-events:none; }
    .attached { display:grid; gap:7px; margin:12px 0 0; }
    .attached.empty { display:grid; min-height:78px; place-items:center; padding:12px; border:1px dashed #cfd2c7; border-radius:11px; color:#8b8d83; background:#fafbf8; font-size:10px; text-align:center; }
    .attached-row { position:relative; padding:9px 10px 9px 32px; border:1px solid var(--line); border-radius:10px; background:#ffffff; }
    .attached-row::before { content:"✓"; position:absolute; left:10px; top:10px; display:grid; width:14px; height:14px; place-items:center; border-radius:50%; color:#ffffff; background:var(--ok); font-size:8px; }
    .attached-row strong,.attached-row small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .attached-row strong { font-size:10px; } .attached-row small { margin-top:3px; color:var(--olive-2); font-size:8px; }
    .note { margin-top:14px; }
    .note textarea { min-height:76px; }
    .requirement { margin:9px 0 0; padding-left:9px; border-left:2px solid var(--signal); color:#665b49; background:#ffffff; font-size:9px; line-height:1.5; }
    .requirement:empty { display:none; }
    .cloud-picker { display:none; margin-top:10px; overflow:hidden; border:1px solid var(--line); border-radius:10px; background:white; }
    .cloud-picker.open { display:block; }
    .cloud-head { padding:8px 10px; border-bottom:1px solid var(--line); color:var(--olive); background:#faf8ee; font-size:9px; }
    .cloud-list { max-height:210px; overflow:auto; }
    .cloud-row { display:flex; width:100%; gap:8px; align-items:center; padding:8px 10px; border:0; border-top:1px solid #edf1ef; color:var(--ink); background:white; text-align:left; }
    .cloud-row input { width:auto; min-height:0; accent-color:var(--mint); }
    .cloud-name { display:block; overflow:hidden; font-size:10px; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
    .cloud-type { display:block; color:#84918d; font-size:8px; }
    .run-summary { display:grid; gap:7px; margin-bottom:15px; padding:11px 12px; border:1px solid #ded9c9; border-radius:11px; background:#faf8ee; }
    .run-summary-title { display:flex; align-items:center; justify-content:space-between; color:var(--olive); font-size:10px; font-weight:900; }
    .run-summary-title span { color:var(--mint); font-size:8px; }
    .run-summary-row { display:grid; grid-template-columns:54px minmax(0,1fr); gap:8px; color:var(--muted); font-size:9px; line-height:1.5; }
    .run-summary-row strong { color:var(--olive); }
    .run-summary-row span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .settings { display:grid; gap:11px; }
    .advanced { border:1px solid var(--line); border-radius:10px; background:#ffffff; }
    .advanced summary { display:flex; align-items:center; justify-content:space-between; min-height:42px; padding:0 11px; color:var(--olive); font-size:10px; font-weight:800; cursor:pointer; list-style:none; }
    .advanced summary::-webkit-details-marker { display:none; }
    .advanced summary span { color:#7d8b87; font-size:8px; font-weight:500; }
    .advanced .field { padding:0 11px 11px; }
    .setting-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
    .setting-group { grid-column:1/-1; margin:7px 0 -2px; padding-bottom:6px; border-bottom:1px solid var(--line); color:var(--olive); font-size:10px; font-weight:900; }
    .field.wide { grid-column:1/-1; }
    .field-help { display:block; margin:5px 1px 0; color:#788682; font-size:8px; line-height:1.45; }
    .choice-field select { display:none; }
    .choice-grid { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
    .choice-option { position:relative; min-height:41px; padding:8px 26px 8px 10px; border:1px solid var(--line); border-radius:9px; color:var(--olive-2); background:#ffffff; font-size:9px; font-weight:700; line-height:1.35; text-align:left; }
    .choice-option.active { border:2px solid var(--signal); padding:7px 25px 7px 9px; color:var(--olive); }
    .choice-option.active::after { content:"✓"; position:absolute; right:8px; top:50%; display:grid; width:16px; height:16px; place-items:center; transform:translateY(-50%); border-radius:50%; color:#ffffff; background:var(--signal); font-size:8px; }
    .status { min-height:17px; margin-top:8px; color:var(--muted); font-size:9px; line-height:1.45; }
    .status.ok { color:var(--ok); font-weight:700; } .status.bad { color:var(--danger); }
    .footer { padding:8px 14px 12px; border-top:1px solid var(--line); background:#ffffff; box-shadow:0 -9px 24px rgba(47,48,38,.06); }
    .footer-meta { display:flex; align-items:center; justify-content:space-between; min-height:20px; color:#76847f; font-size:8px; }
    .footer-meta strong { color:var(--olive); font-size:9px; }
    .footer-actions { display:grid; grid-template-columns:auto minmax(0,1fr); gap:8px; }
    .footer-actions.single { grid-template-columns:1fr; }
    .back { min-width:74px; min-height:43px; padding:0 12px; border:1px solid #cfd2c7; border-radius:10px; color:var(--olive); background:#f7f8f2; font-size:10px; font-weight:900; }
    .back[hidden] { display:none; }
    .save { width:100%; min-height:43px; border:0; border-radius:10px; color:#ffffff; background:var(--signal); font-size:11px; font-weight:900; box-shadow:0 8px 18px rgba(217,107,50,.2); }
    .save:disabled,.source:disabled { border-color:var(--line); color:#8b8d83; background:#dcddd5; box-shadow:none; cursor:not-allowed; }
    .return-link { display:none; min-height:34px; padding:0 11px; border:1px solid #cfd2c7; border-radius:9px; color:var(--olive); background:#ffffff; font-size:10px; font-weight:900; }
    .completion { grid-area:content; display:grid; min-height:0; place-items:center; overflow:auto; padding:30px 22px; background:#ffffff; }
    .completion[hidden] { display:none; }
    .completion-card { width:min(100%,560px); padding:28px 24px; border:1px solid var(--line); border-radius:18px; background:#ffffff; text-align:center; box-shadow:0 18px 50px rgba(47,48,38,.08); }
    .completion-mark { display:grid; width:54px; height:54px; margin:0 auto 17px; place-items:center; border-radius:50%; color:#ffffff; background:var(--ok); font-size:24px; font-weight:900; }
    .completion-kicker { margin:0 0 7px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.14em; }
    .completion h2 { margin:0; font:700 24px/1.2 "Songti SC","STSong",serif; }
    .completion-copy { margin:11px auto 20px; color:var(--muted); font-size:11px; line-height:1.75; }
    .return-primary { width:100%; min-height:45px; border:0; border-radius:10px; color:#ffffff; background:var(--olive); font-size:11px; font-weight:900; }
    .completion-note { margin:10px 0 0; color:var(--muted); font-size:9px; line-height:1.5; }
    @media (min-width:430px) { .hero { padding-inline:20px; } .content { padding-inline:20px; } .footer { padding-inline:20px; } }
    @media (min-width:760px) {
      html,body { overflow:hidden; }
      body { padding:24px; background:#f2f1ea; }
      .app { grid-template-columns:278px minmax(0,1fr); grid-template-rows:auto minmax(0,1fr) auto; grid-template-areas:"hero content" "tabs content" "tabs footer"; width:min(1180px,100%); height:calc(100vh - 48px); margin:0 auto; overflow:hidden; border:1px solid #d5d7cd; border-radius:22px; box-shadow:0 24px 70px rgba(47,48,38,.12); }
      .hero { grid-area:hero; padding:32px 28px 25px; border:0; border-right:1px solid var(--line); background:#f7f8f2; }
      .hero-top { display:block; }
      .eyebrow { margin-bottom:9px; font-size:10px; }
      h1 { font-size:31px; line-height:1.12; }
      .llm-chip { display:inline-block; margin-top:18px; padding:7px 10px; font-size:9px; }
      .hero-summary { margin-top:15px; font-size:12px; line-height:1.75; }
      .return-link { display:inline-flex; align-items:center; justify-content:center; margin-top:22px; }
      .tabs { grid-area:tabs; display:flex; flex-direction:column; justify-content:flex-start; padding:8px 18px 24px; border:0; border-right:1px solid var(--line); background:#f7f8f2; }
      .step { grid-template-columns:34px minmax(0,1fr); gap:12px; min-height:72px; padding:10px; border-radius:12px; background:transparent; }
      .step:hover { background:#ffffff; }
      .step::after { left:26px; right:auto; top:53px; width:1px; height:39px; }
      .step-number { width:34px; height:34px; font-size:11px; }
      .step-copy strong { font-size:13px; }
      .step-copy small { margin-top:4px; font-size:10px; }
      .content { grid-area:content; padding:40px 48px 56px; }
      .panel { max-width:780px; margin:0 auto; }
      .section-title { margin-bottom:10px; }
      .section-title h2 { font-size:27px; }
      .badge { padding:6px 10px; font-size:10px; }
      .guide { margin-bottom:24px; padding:13px 15px; border-radius:0 12px 12px 0; font-size:12px; }
      label { margin-bottom:9px; font-size:12px; }
      textarea { min-height:180px; padding:16px 17px; border-radius:12px; font-size:14px; }
      input,select { min-height:48px; padding-inline:14px; border-radius:11px; font-size:13px; }
      .material-actions { gap:14px; margin-top:16px; }
      .source { grid-template-columns:42px minmax(0,1fr); gap:13px; min-height:82px; padding:13px 15px; border-radius:14px; }
      .source-icon { width:42px; height:42px; border-radius:11px; font-size:20px; }
      .source strong { font-size:13px; }
      .source small { margin-top:4px; font-size:10px; }
      .attached { gap:10px; margin-top:16px; }
      .attached.empty { min-height:104px; font-size:12px; }
      .attached-row { padding:13px 14px 13px 42px; }
      .attached-row::before { left:14px; top:14px; width:18px; height:18px; }
      .attached-row strong { font-size:12px; }
      .attached-row small { font-size:10px; }
      .note { margin-top:20px; }
      .note textarea { min-height:108px; }
      .requirement,.memory-hits,.candidate,.quick-goal,.field-help { font-size:11px; }
      .quick-goals { gap:12px; }
      .quick-goal { min-height:74px; padding:13px 14px; }
      .quick-goal strong { font-size:12px; }
      .quick-goal span { font-size:10px; }
      .setting-grid { gap:15px; }
      .choice-grid { grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
      .choice-option { min-height:48px; padding:10px 30px 10px 12px; font-size:11px; }
      .run-summary { gap:10px; padding:16px 18px; }
      .run-summary-title { font-size:12px; }
      .run-summary-row { grid-template-columns:66px minmax(0,1fr); font-size:11px; }
      .footer { grid-area:footer; padding:13px 48px 18px; box-shadow:0 -8px 28px rgba(47,48,38,.05); }
      .footer-meta { font-size:10px; }
      .footer-meta strong { font-size:11px; }
      .back,.save { min-height:48px; font-size:12px; }
      .status { font-size:10px; }
      .completion { padding:48px; }
      .completion-card { padding:48px 52px; border-radius:22px; }
      .completion-mark { width:68px; height:68px; font-size:30px; }
      .completion-kicker { font-size:10px; }
      .completion h2 { font-size:32px; }
      .completion-copy { font-size:13px; }
      .return-primary { min-height:50px; font-size:13px; }
      .completion-note { font-size:10px; }
    }
  </style>
</head>
<body>
  <main class="app">
    <header class="hero">
      <div class="hero-top"><div><p class="eyebrow" id="eyebrow"></p><h1 id="title"></h1></div><span class="llm-chip">LLM 执行</span></div>
      <p class="hero-summary" id="summary"></p>
      <button class="return-link" id="returnToAssistant" type="button">← 返回飞书小助手</button>
    </header>
    <nav class="tabs" aria-label="资料、任务与设置">
      <button class="step active" data-tab="material" id="materialTab" type="button"><span class="step-number">1</span><span class="step-copy"><strong id="materialStepLabel"></strong><small>添加来源</small></span></button>
      <button class="step" data-tab="prompt" id="promptTab" type="button"><span class="step-number">2</span><span class="step-copy"><strong id="promptStepLabel"></strong><small>说清目标</small></span></button>
      <button class="step" data-tab="settings" id="settingsTab" type="button"><span class="step-number">3</span><span class="step-copy"><strong id="settingsStepLabel"></strong><small>选择结果</small></span></button>
    </nav>
    <section class="content" id="workspaceContent">
      <section class="panel active" data-panel="material">
        <div class="section-title"><h2 id="materialTitle"></h2><span class="badge" id="materialCount">0 项</span></div>
        <p class="guide" id="materialGuide"></p>
        <div class="material-actions">
          <button class="source" id="local" type="button"><span class="source-icon">＋</span><span><strong>上传本地文件</strong><small>PDF / Word / Excel</small></span></button>
          <button class="source" id="docs" type="button"><span class="source-icon">⌁</span><span><strong>飞书云文档</strong><small>选择已有文档</small></span></button>
        </div>
        <input id="files" name="files" type="file" multiple accept=".doc,.docx,.pdf,.ppt,.pptx,.xls,.xlsx,.csv,.txt,.md,.markdown,.rtf,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.svg,.mp3,.wav,.m4a,.mp4,.mov,.avi,.mkv,.webm,.zip,.rar,.7z">
        <div class="attached empty" id="attached"></div>
        <div class="cloud-picker" id="cloudPicker"><div class="cloud-head" id="cloudHead">最近编辑的云文档</div><div class="cloud-list" id="cloudList"></div></div>
        <p class="requirement" id="materialRequirement"></p>
        <div class="note"><label for="materialNote" id="materialLabel"></label><textarea id="materialNote"></textarea></div>
        <div class="status" id="materialStatus"></div>
      </section>
      <section class="panel" data-panel="prompt">
        <div class="section-title"><h2 id="promptTitle"></h2><span class="badge">提交后自动生成</span></div>
        <p class="guide" id="promptGuide"></p>
        <label id="promptLabel" for="prompt"></label>
        <textarea id="prompt"></textarea>
        <p class="quick-title" id="quickTitle">常用目标 · 点击即可带入</p>
        <div class="quick-goals" id="quickGoals"></div>
        <div class="prompt-tools"><button class="secondary" id="suggest" type="button">整理 3 个提词版本</button></div>
        <div class="candidates" id="candidates"></div>
        <div class="settings prompt-settings"><div class="setting-grid" id="promptSettingGrid"></div></div>
      </section>
      <section class="panel" data-panel="settings">
        <div class="section-title"><h2 id="settingsTitle"></h2><span class="badge" id="modelBadge">模型由服务端管理</span></div>
        <p class="guide" id="settingsGuide"></p>
        <div class="run-summary" id="runSummary"></div>
        <div class="settings"><div class="setting-grid" id="settingGrid"></div><details class="advanced"><summary>高级设置 <span id="modelSummary"></span></summary><div class="field"><label for="model_choice" id="modelLabel"></label><select id="model_choice"></select></div></details></div>
      </section>
    </section>
    <section class="completion" id="completion" hidden>
      <div class="completion-card"><div class="completion-mark">✓</div><p class="completion-kicker">TASK ACCEPTED</p><h2>任务已经交给小助手</h2><p class="completion-copy">工作台已保存你的资料和设置，生成结果会继续发送到飞书对话。现在可以安全返回小助手。</p><button class="return-primary" id="completionReturn" type="button">关闭工作台，返回小助手</button><p class="completion-note" id="completionNote">若浏览器未自动关闭，请点击上方按钮。</p></div>
    </section>
    <footer class="footer" id="workspaceFooter"><div class="footer-meta"><strong id="stepStatus"></strong><span id="footerHint"></span></div><div class="footer-actions"><button class="back" id="back" type="button" hidden>上一步</button><button class="save" id="save" type="button"></button></div><div class="status" id="status"></div></footer>
  </main>
  <script>
    const config=__WORKSPACE_CONFIG__;
    const updateDisplayMode=()=>{document.documentElement.dataset.displayMode=window.matchMedia('(min-width:760px)').matches?'web':'sidebar';};
    updateDisplayMode();window.addEventListener('resize',updateDisplayMode);
    const base=window.location.pathname.replace(/\/$/,'');
    const revisionMode=config.canRevise&&new URLSearchParams(window.location.search).get('mode')==='revise';
    const finalSaveLabel=revisionMode?'确认修改并再次生成':config.saveLabel;
    const taskData=config.taskData||{};
    const selectedDocs=new Map();
    let stagedCount=config.attachments.length;
    const stageOrder=['material','prompt','settings'];
    let currentStage='material';
    const prompt=document.getElementById('prompt');
    const save=document.getElementById('save');
    const back=document.getElementById('back');
    const status=document.getElementById('status');
    const materialStatus=document.getElementById('materialStatus');
    const attached=document.getElementById('attached');
    const materialCount=document.getElementById('materialCount');
    const input=document.getElementById('files');
    const local=document.getElementById('local');
    const docs=document.getElementById('docs');
    const cloudPicker=document.getElementById('cloudPicker');
    const cloudList=document.getElementById('cloudList');
    const cloudHead=document.getElementById('cloudHead');
    async function returnToAssistant(){
      try{if(window.LarkAPI?.webview?.close){await window.LarkAPI.webview.close();return;}}catch(error){console.debug('Lark close fallback',error);}
      try{if(window.opener&&!window.opener.closed){window.opener.focus();window.close();return;}}catch(error){console.debug('Browser opener fallback',error);}
      window.close();setTimeout(()=>{const note=document.getElementById('completionNote');if(note)note.textContent='浏览器未允许自动关闭，请关闭当前工作台标签；原飞书小助手仍在上一页。';else{status.textContent='浏览器未允许自动关闭，请关闭当前工作台标签返回飞书。';status.className='status bad';}},220);
    }
    document.getElementById('returnToAssistant').addEventListener('click',()=>returnToAssistant());
    document.getElementById('completionReturn').addEventListener('click',()=>returnToAssistant());
    document.getElementById('eyebrow').textContent=config.eyebrow;
    document.getElementById('title').textContent=revisionMode?`修改${config.taskLabel}`:config.taskLabel;
    document.getElementById('summary').textContent=revisionMode?'已回填上次资料。打开页面不会自动执行；修改完成后，请到最后一步确认再次生成。':config.summary;
    document.getElementById('materialStepLabel').textContent=config.tabs[1];
    document.getElementById('promptStepLabel').textContent=config.tabs[0];
    document.getElementById('settingsStepLabel').textContent=config.tabs[2];
    document.getElementById('promptTitle').textContent=config.sectionTitles[0];
    document.getElementById('materialTitle').textContent=config.sectionTitles[1];
    document.getElementById('settingsTitle').textContent=config.sectionTitles[2];
    document.getElementById('promptLabel').textContent=config.promptLabel+' *';
    prompt.placeholder=config.promptPlaceholder;
    prompt.value=taskData[config.requiredField]||'';
    const materialNote=document.getElementById('materialNote');
    materialNote.value=taskData[config.materialField]||'';
    materialNote.placeholder=config.materialPlaceholder;
    document.getElementById('materialLabel').textContent=config.materialLabel;
    document.getElementById('materialRequirement').textContent=config.materialRequirement;
    document.getElementById('materialGuide').textContent=config.materialGuide;
    document.getElementById('promptGuide').textContent=config.promptGuide;
    document.getElementById('settingsGuide').textContent=config.settingsGuide;
    document.getElementById('modelLabel').textContent=config.modelLabel;
    document.getElementById('modelSummary').textContent=config.modelSummary;
    document.getElementById('modelBadge').textContent=config.taskType==='image'?'可指定生图模型':'模型由服务端管理';
    const modelChoice=document.getElementById('model_choice');
    for(const option of config.modelOptions){const node=document.createElement('option');node.textContent=option[0];node.value=option[1];modelChoice.append(node);}
    function setActiveStage(stage){
      currentStage=stage;const currentIndex=stageOrder.indexOf(stage);
      document.querySelectorAll('.step').forEach((item,index)=>{item.classList.toggle('active',item.dataset.tab===stage);item.classList.toggle('complete',index<currentIndex);});
      document.querySelectorAll('.panel').forEach(panel=>panel.classList.toggle('active',panel.dataset.panel===stage));
      document.querySelector('.content').scrollTop=0;back.hidden=currentIndex===0;back.parentElement.classList.toggle('single',currentIndex===0);
      document.getElementById('stepStatus').textContent=`第 ${currentIndex+1} 步，共 3 步`;
      document.getElementById('footerHint').textContent=['先把资料准备好','再说明想得到什么',revisionMode?'确认修改后再次生成':'确认交付方式后直接执行'][currentIndex];
      save.textContent=currentIndex===0?config.nextLabel:(currentIndex===1?'下一步：选择交付':finalSaveLabel);
      if(stage==='settings')renderRunSummary();
    }
    function hasLinkedMaterial(){try{const linkedFile=new URL(materialNote.value.trim());return ['http:','https:'].includes(linkedFile.protocol);}catch(error){return false;}}
    function validateStage(stage){
      if(stage==='material'&&config.materialRequirement&&stagedCount+selectedDocs.size===0&&!hasLinkedMaterial()){setActiveStage('material');status.textContent=config.materialRequirement;status.className='status bad';materialNote.focus();return false;}
      if(stage==='prompt'&&!prompt.value.trim()){setActiveStage('prompt');status.textContent=`请先填写${config.promptLabel}`;status.className='status bad';prompt.focus();return false;}
      if(stage==='prompt'&&document.getElementById('operation')?.value==='translate'&&!document.getElementById('target_language')?.value){setActiveStage('prompt');status.textContent='请选择目标语言';status.className='status bad';document.getElementById('target_language').focus();return false;}
      status.textContent='';status.className='status';return true;
    }
    document.querySelectorAll('.step').forEach(step=>step.addEventListener('click',()=>{
      const targetIndex=stageOrder.indexOf(step.dataset.tab);const currentIndex=stageOrder.indexOf(currentStage);
      if(targetIndex<=currentIndex){setActiveStage(step.dataset.tab);return;}
      if(targetIndex===currentIndex+1&&validateStage(currentStage)){setActiveStage(step.dataset.tab);return;}
      status.textContent='请按顺序完成当前步骤。';status.className='status bad';
    }));
    function renderAttachments(){
      materialCount.textContent=`${stagedCount+selectedDocs.size} 项`;
      attached.replaceChildren();
      const items=[...config.attachments,...selectedDocs.values()].slice(0,10);
      if(!items.length){attached.className='attached empty';attached.textContent=config.emptyMaterial;return;}
      attached.className='attached';
      items.forEach(item=>{const row=document.createElement('div');row.className='attached-row';const name=document.createElement('strong');name.textContent=item.filename||item.fileName;const source=document.createElement('small');source.textContent=item.source||'飞书云文档 · 待确认';row.append(name,source);attached.append(row);});syncTranslationDelivery();
    }
    const quickGoals=document.getElementById('quickGoals');
    if(!config.quickGoals.length){document.getElementById('quickTitle').hidden=true;quickGoals.hidden=true;}
    config.quickGoals.forEach(goal=>{const button=document.createElement('button');button.type='button';button.className='quick-goal';const title=document.createElement('strong');title.textContent=goal[0];const detail=document.createElement('span');detail.textContent=goal[1];button.append(title,detail);button.addEventListener('click',()=>{prompt.value=goal[1];status.textContent=`已带入“${goal[0]}”，可继续补充具体对象和用途。`;status.className='status ok';prompt.focus();});quickGoals.append(button);});
    const grid=document.getElementById('settingGrid');const promptGrid=document.getElementById('promptSettingGrid');let currentGroupByStage={prompt:'',settings:''};
    for(const field of config.settings){
      const targetGrid=field.stage==='prompt'?promptGrid:grid;const stageKey=field.stage==='prompt'?'prompt':'settings';
      if(field.group&&field.group!==currentGroupByStage[stageKey]){currentGroupByStage[stageKey]=field.group;const heading=document.createElement('div');heading.className='setting-group';heading.textContent=field.group;targetGrid.append(heading);}
      const wrapper=document.createElement('div');wrapper.className='field'+(field.type==='text'||field.wide?' wide':'')+(field.presentation==='cards'?' choice-field':'');
      const label=document.createElement('label');label.htmlFor=field.name;label.textContent=field.label;
      let control;
      if(field.type==='select'){control=document.createElement('select');for(const option of field.options){const node=document.createElement('option');node.textContent=option[0];node.value=option[1];control.append(node);}}
      else if(field.type==='textarea'){control=document.createElement('textarea');control.placeholder='选填';}
      else{control=document.createElement('input');control.type='text';control.placeholder='选填';}
      control.id=field.name;control.value=taskData[field.name]||field.default||(field.options?.[0]?.[1]||'');wrapper.append(label,control);
      if(field.visible_when){wrapper.dataset.visibleField=field.visible_when[0];wrapper.dataset.visibleValue=field.visible_when[1];}
      if(field.visible_unless){wrapper.dataset.hiddenField=field.visible_unless[0];wrapper.dataset.hiddenValue=field.visible_unless[1];}
      if(field.presentation==='cards'){
        const choices=document.createElement('div');choices.className='choice-grid';
        for(const option of field.options){const choice=document.createElement('button');choice.type='button';choice.className='choice-option';choice.dataset.value=option[1];choice.textContent=option[0];choice.classList.toggle('active',control.value===option[1]);choice.setAttribute('aria-pressed',String(control.value===option[1]));choice.addEventListener('click',()=>{control.value=option[1];choices.querySelectorAll('.choice-option').forEach(item=>{const active=item===choice;item.classList.toggle('active',active);item.setAttribute('aria-pressed',String(active));});control.dispatchEvent(new Event('change'));});choices.append(choice);}
        wrapper.append(choices);
      }
      if(field.help){const help=document.createElement('small');help.className='field-help';help.textContent=field.help;wrapper.append(help);}control.addEventListener('change',()=>{refreshConditionalFields();syncTranslationDelivery();renderRunSummary();});targetGrid.append(wrapper);
    }
    modelChoice.value=taskData.model_choice||'auto';
    const translationDeliveryMatrix={'.xlsx':['xlsx'],'.docx':['docx','pdf','txt','feishu_doc'],'.pdf':['docx','pdf','txt','feishu_doc'],'.txt':['docx','pdf','txt','feishu_doc'],'.md':['docx','pdf','txt','feishu_doc'],'.markdown':['docx','pdf','txt','feishu_doc'],cloud_doc:['docx','pdf','txt','feishu_doc']};
    function refreshConditionalFields(){for(const wrapper of document.querySelectorAll('[data-visible-field],[data-hidden-field]')){let visible=true;if(wrapper.dataset.visibleField)visible=document.getElementById(wrapper.dataset.visibleField)?.value===wrapper.dataset.visibleValue;if(wrapper.dataset.hiddenField&&document.getElementById(wrapper.dataset.hiddenField)?.value===wrapper.dataset.hiddenValue)visible=false;wrapper.hidden=!visible;}const direction=document.getElementById('direction_mode');const outputMode=document.getElementById('output_mode');if(direction?.value==='bidirectional'&&outputMode){outputMode.value='bilingual';for(const choice of outputMode.parentElement.querySelectorAll('.choice-option')){const active=choice.dataset.value==='bilingual';choice.classList.toggle('active',active);choice.setAttribute('aria-pressed',String(active));}}}
    function selectedTranslationSourceType(){const names=[...config.attachments.map(item=>item.filename||''),...selectedDocs.values()].map(item=>item.filename||item.fileName||'');if(selectedDocs.size)return 'cloud_doc';let name=names[0]||'';if(!name&&hasLinkedMaterial()){try{name=new URL(materialNote.value.trim()).pathname;}catch(error){name='';}}const match=name.toLowerCase().match(/\.(docx|xlsx|pdf|txt|md|markdown)$/);return match?`.${match[1]}`:'cloud_doc';}
    function syncTranslationDelivery(){const operation=document.getElementById('operation');const delivery=document.getElementById('output_format');if(!operation||!delivery)return;const allowed=operation.value==='translate'?(translationDeliveryMatrix[selectedTranslationSourceType()]||[]):Array.from(delivery.options).map(option=>option.value);for(const option of delivery.options){const enabled=allowed.includes(option.value);option.disabled=!enabled;option.hidden=!enabled;}if(!allowed.includes(delivery.value)){const next=Array.from(delivery.options).find(option=>!option.disabled);if(next)delivery.value=next.value;}for(const choice of delivery.parentElement.querySelectorAll('.choice-option')){choice.hidden=!allowed.includes(choice.dataset.value);const active=choice.dataset.value===delivery.value;choice.classList.toggle('active',active);choice.setAttribute('aria-pressed',String(active));}if(operation.value==='translate'){const layout=document.getElementById('preserve_layout');if(layout&&layout.value==='restructure'){layout.value='preserve';for(const choice of layout.parentElement.querySelectorAll('.choice-option')){const active=choice.dataset.value==='preserve';choice.classList.toggle('active',active);choice.setAttribute('aria-pressed',String(active));}}}}
    function renderRunSummary(){
      const summary=document.getElementById('runSummary');summary.replaceChildren();
      const heading=document.createElement('div');heading.className='run-summary-title';heading.innerHTML='<strong>本次任务概览</strong><span>提交后直接执行</span>';summary.append(heading);
      const deliveryField=config.settings.find(field=>field.name==='delivery_format')||config.settings.find(field=>field.name==='output_format');
      const deliveryControl=deliveryField?document.getElementById(deliveryField.name):null;
      const sourceCount=stagedCount+selectedDocs.size;const materialSummary=sourceCount?`${sourceCount} 项${materialNote.value.trim()?' · 含补充说明':''}`:(hasLinkedMaterial()?'1 个链接':(materialNote.value.trim()?'含补充说明':'使用公开来源'));
      const rows=[['资料',materialSummary],['目标',prompt.value.trim()||'尚未填写'],['交付',deliveryControl?.selectedOptions?.[0]?.textContent||'按默认设置']];
      for(const row of rows){const line=document.createElement('div');line.className='run-summary-row';const label=document.createElement('strong');label.textContent=row[0];const value=document.createElement('span');value.textContent=row[1];line.append(label,value);summary.append(line);}
    }
    const suggest=document.getElementById('suggest');
    if(!['copy','image','video'].includes(config.taskType)){suggest.hidden=true;}
    suggest.addEventListener('click',async()=>{
      const idea=prompt.value.trim();
      if(!idea){status.textContent='先写一句任务意图，再整理提词。';status.className='status bad';prompt.focus();return;}
      const candidates=document.getElementById('candidates');candidates.replaceChildren();
      const values={[config.materialField]:materialNote.value.trim()};for(const field of config.settings){values[field.name]=document.getElementById(field.name).value.trim();}
      suggest.disabled=true;suggest.textContent='正在生成专业提词…';status.textContent='正在调用 NAS 高级提词器';status.className='status';
      try{const response=await fetch(`${base}/prompt-candidates`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:idea,task_data:values})});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'专业提词生成失败');const sources=payload.company_memory_sources||[];const memory=document.createElement('div');memory.className='memory-hits';const memoryTitle=document.createElement('strong');memoryTitle.textContent=payload.company_memory_message||'公司 Obsidian 原文案参考';const memoryDetail=document.createElement('span');memoryDetail.textContent=sources.map(item=>`${item.title} · ${item.source_status}`).join('；')||'本次没有匹配到相关公司原文';memory.append(memoryTitle,memoryDetail);candidates.append(memory);for(const candidate of payload.candidates){const button=document.createElement('button');button.type='button';button.className='candidate';const title=document.createElement('strong');title.textContent=candidate.title;const summary=document.createElement('small');summary.textContent=candidate.summary;const content=document.createElement('span');content.textContent=candidate.prompt;button.append(title,summary,content);button.addEventListener('click',()=>{prompt.value=candidate.prompt;status.textContent=`已采用“${candidate.title}”专业提词。`;status.className='status ok';prompt.focus();});candidates.append(button);}status.textContent=payload.company_memory_status==='matched'?`已参考 ${sources.length} 条公司 Obsidian 原文，生成 ${payload.candidates.length} 个专业版本。`:`${payload.company_memory_message} 已生成 ${payload.candidates.length} 个专业版本。`;status.className=payload.company_memory_status==='unavailable'?'status bad':'status ok';}
      catch(error){status.textContent=error.message||'专业提词服务暂不可用';status.className='status bad';}
      finally{suggest.disabled=false;suggest.textContent='整理 3 个提词版本';}
    });
    async function uploadFiles(files){
      const selected=Array.from(files||[]).slice(0,10);if(!selected.length)return;
      local.disabled=true;local.querySelector('strong').textContent=`上传中 ${selected.length} 个…`;materialStatus.textContent='正在上传';materialStatus.className='status';
      const data=new FormData();selected.forEach(file=>data.append('files',file));
      try{const response=await fetch(base,{method:'POST',body:data});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'上传失败');stagedCount=Number(payload.total_attachments||stagedCount+payload.attachments.length);config.attachments.push(...payload.attachments.map(item=>({filename:item.filename,source:'本地文件 · 待确认'})));materialStatus.textContent=`已添加 ${payload.attachments.length} 个本地文件。`;materialStatus.className='status ok';local.querySelector('strong').textContent='继续上传';input.value='';renderAttachments();}
      catch(error){materialStatus.textContent=error.message||'上传失败，请重试';materialStatus.className='status bad';local.querySelector('strong').textContent='重新上传';}
      finally{local.disabled=false;}
    }
    async function loadCloudFiles(folderToken=''){
      docs.disabled=true;docs.querySelector('strong').textContent='读取中…';materialStatus.textContent='';
      try{const query=folderToken?`?folder_token=${encodeURIComponent(folderToken)}`:'';const response=await fetch(`${base}/cloud-files${query}`);if(response.status===401){window.location.href=`${base}/oauth/start`;return;}const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'读取云文档失败');cloudList.replaceChildren();cloudPicker.classList.add('open');cloudHead.textContent=folderToken?'文件夹内容':'最近编辑的云文档';if(!payload.files.length){const empty=document.createElement('div');empty.className='cloud-row';empty.textContent='这里暂时没有可选文档';cloudList.append(empty);}for(const file of payload.files){const row=document.createElement(file.isFolder?'button':'label');row.className='cloud-row';if(file.isFolder){row.type='button';row.addEventListener('click',()=>loadCloudFiles(file.fileToken));}else{const box=document.createElement('input');box.type='checkbox';box.checked=selectedDocs.has(file.filePath);box.addEventListener('change',()=>{if(box.checked)selectedDocs.set(file.filePath,file);else selectedDocs.delete(file.filePath);renderAttachments();});row.append(box);}const text=document.createElement('span');text.style.minWidth='0';const name=document.createElement('span');name.className='cloud-name';name.textContent=file.fileName;const type=document.createElement('span');type.className='cloud-type';type.textContent=file.isFolder?'文件夹 ›':file.fileType;text.append(name,type);row.append(text);cloudList.append(row);}}
      catch(error){materialStatus.textContent=error.message||'读取云文档失败';materialStatus.className='status bad';}
      finally{docs.disabled=false;docs.querySelector('strong').textContent='飞书云文档';}
    }
    input.addEventListener('change',()=>uploadFiles(input.files));local.addEventListener('click',()=>input.click());docs.addEventListener('click',()=>loadCloudFiles());materialNote.addEventListener('input',()=>{syncTranslationDelivery();renderRunSummary();});
    back.addEventListener('click',()=>{const currentIndex=stageOrder.indexOf(currentStage);if(currentIndex>0)setActiveStage(stageOrder[currentIndex-1]);});
    save.addEventListener('click',async()=>{
      const currentIndex=stageOrder.indexOf(currentStage);
      if(currentStage!=='settings'){if(validateStage(currentStage))setActiveStage(stageOrder[currentIndex+1]);return;}
      if(!validateStage('material')||!validateStage('prompt'))return;
      const materialValue=materialNote.value.trim();const value=prompt.value.trim();
      save.disabled=true;save.textContent='正在提交…';status.textContent='正在进入后台生成队列';status.className='status';
      try{if(selectedDocs.size){const response=await fetch(`${base}/cloud-docs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:Array.from(selectedDocs.values())})});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'云文档关联失败');stagedCount=Number(payload.total_attachments||stagedCount+selectedDocs.size);}
        const values={[config.requiredField]:value,[config.materialField]:materialValue,model_choice:document.getElementById('model_choice').value};for(const field of config.settings){values[field.name]=document.getElementById(field.name).value.trim();}
        const response=await fetch(`${base}/workspace`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_data:values})});const payload=await response.json();if(!response.ok)throw new Error(payload.detail||'提交失败');status.textContent='任务已提交，结果会直接发送到飞书。';status.className='status ok';save.textContent='已开始生成';document.getElementById('workspaceContent').hidden=true;document.getElementById('workspaceFooter').hidden=true;document.getElementById('completion').hidden=false;setTimeout(()=>returnToAssistant(),900);}
      catch(error){status.textContent=error.message||'提交失败，请重试';status.className='status bad';save.disabled=false;save.textContent=finalSaveLabel;}
    });
    refreshConditionalFields();syncTranslationDelivery();renderAttachments();setActiveStage('material');if(new URLSearchParams(window.location.search).get('cloud')==='1'){setActiveStage('material');loadCloudFiles();}
  </script>
</body>
</html>"""
    return page.replace("__WORKSPACE_CONFIG__", config_json)


def build_attachment_upload_page(draft: AttachmentDraft) -> str:
    """Render the dependency-free attachment upload H5 page.

    Args:
        draft: Live upload capability and any previously attached file.

    Returns:
        Complete UTF-8 HTML suitable for Feishu's in-app WebView.
    """
    if draft.task_type:
        return build_task_workspace_page(draft)
    attached_items = "".join(
        (
            '<div class="attached"><span class="attached-dot"></span><div>'
            f"<strong>{html.escape(str(item.get('filename') or '未命名资料'))}</strong>"
            f"<small>{'飞书云文档' if item.get('type') == 'cloud_doc' else '本地文件'}"
            " · 待确认</small></div></div>"
        )
        for item in draft.attachments
    )
    attached_block = (
        f'<div class="attached-list" id="attached"><h2>待确认 {len(draft.attachments)} 项</h2>'
        f"{attached_items}</div>"
        if attached_items
        else '<div class="attached-list empty" id="attached">暂未添加资料</div>'
    )
    return f"""<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#46513a">
  <link rel="icon" href="data:,">
  <title>添加任务资料</title>
  <style>
    :root {{ color-scheme:light; --ink:#25281f; --muted:#777a6d; --line:#dedfd6; --paper:#ffffff; --panel:#ffffff; --olive:#46513a; --olive-2:#657158; --signal:#d96b32; --signal-soft:#fff0e6; --ok:#347558; --warn:#a75225; --forest:var(--olive); --mint:var(--signal); --mint-dark:#b95426; --mint-soft:var(--signal-soft); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; height:100vh; overflow:hidden; font-family:"Microsoft YaHei","PingFang SC",sans-serif; color:var(--ink); background:#ffffff; }}
    main {{ height:100vh; padding:10px; background:#ffffff; }}
    .workspace {{ width:100%; height:100%; display:grid; grid-template-columns:172px minmax(0,1fr); overflow:hidden; border:1px solid var(--line); border-radius:18px; background:var(--paper); box-shadow:0 16px 48px rgba(61,59,43,.09); }}
    .intro {{ position:relative; overflow:hidden; padding:24px 20px; border-right:1px solid var(--line); color:var(--ink); background:#ffffff; }}
    .eyebrow {{ margin:0 0 14px; color:var(--signal); font-size:9px; font-weight:900; letter-spacing:.16em; }}
    h1 {{ margin:0; font:700 28px/1.08 "Songti SC","STSong",serif; letter-spacing:-.04em; }}
    .lead {{ margin:13px 0 0; color:var(--muted); font-size:11px; line-height:1.65; }}
    .source-panel {{ display:flex; min-width:0; flex-direction:column; margin:10px; padding:18px; overflow:hidden; border:1px solid var(--line); border-radius:14px; background:#ffffff; box-shadow:0 8px 26px rgba(61,59,43,.05); }}
    .source-panel.drag {{ border-color:var(--signal); background:var(--signal-soft); box-shadow:0 0 0 4px rgba(217,107,50,.1); }}
    .panel-head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }}
    .panel-title {{ margin:0; font-size:19px; letter-spacing:-.03em; }}
    .source-actions {{ display:grid; grid-template-columns:1fr 1fr; gap:9px; }}
    .source-help {{ margin:9px 1px 0; color:var(--muted); font-size:10px; }}
    input[type=file] {{ position:absolute; opacity:0; pointer-events:none; }}
    button {{ min-height:42px; border:0; border-radius:10px; color:white; background:var(--olive); font:900 12px "Microsoft YaHei","PingFang SC",sans-serif; cursor:pointer; box-shadow:0 7px 18px rgba(61,59,43,.15); transition:background .18s ease,transform .18s ease; }}
    button:hover:not(:disabled) {{ background:var(--olive-2); transform:translateY(-1px); }}
    button:disabled {{ cursor:not-allowed; color:#8b8d83; background:#dcddd5; box-shadow:none; }}
    .attached-list {{ max-height:105px; margin:0 0 11px; padding:10px; overflow:auto; border-radius:10px; background:#faf8ee; font-size:11px; }}
    .attached-list.empty {{ display:grid; min-height:44px; place-items:center; color:var(--muted); }}
    .attached-list h2 {{ margin:0 0 7px; font-size:11px; }}
    .attached {{ display:flex; gap:8px; align-items:center; margin-bottom:5px; padding:7px 9px; border-radius:8px; color:var(--olive); background:var(--signal-soft); }}
    .attached-dot {{ width:8px; height:8px; flex:0 0 auto; border-radius:50%; background:var(--ok); }}
    .attached strong,.attached small {{ display:block; }} .attached strong {{ font-size:11px; }} .attached small {{ margin-top:2px; color:var(--olive-2); font-size:9px; }}
    .cloud-picker {{ display:none; min-height:0; margin-top:10px; overflow:hidden; border:1px solid var(--line); border-radius:10px; }}
    .cloud-picker.open {{ display:flex; flex:1; flex-direction:column; }}
    .cloud-head {{ padding:8px 10px; color:var(--olive); background:#faf8ee; font-size:10px; }}
    .cloud-list {{ min-height:0; overflow:auto; }}
    .cloud-row {{ display:flex; gap:9px; align-items:center; padding:9px 11px; border-top:1px solid #edf1ef; cursor:pointer; }}
    .cloud-row:hover {{ background:var(--signal-soft); }} .cloud-row input {{ accent-color:var(--signal); }}
    .cloud-name {{ overflow:hidden; font-size:11px; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }}
    .cloud-type {{ color:var(--muted); font-size:9px; }}
    .status {{ min-height:17px; margin:8px 1px 0; color:var(--muted); font-size:10px; line-height:1.5; }}
    .status.ok {{ color:var(--ok); font-weight:700; }} .status.bad {{ color:var(--warn); }}
    .footer {{ display:flex; align-items:center; gap:12px; margin-top:auto; padding-top:10px; }}
    .confirm {{ width:150px; flex:0 0 auto; background:var(--signal); box-shadow:0 8px 18px rgba(217,107,50,.2); }}
    .confirm:hover:not(:disabled) {{ background:#b95426; }}
    .footer-note {{ flex:1; color:var(--muted); font-size:9px; }}
    @media (max-width:680px) {{
      main {{ padding:8px; }}
      .workspace {{ grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); border-radius:16px; }}
      .intro {{ min-height:62px; padding:12px 16px; border-right:0; border-bottom:1px solid var(--line); }}
      .eyebrow {{ margin:0 0 5px; font-size:8px; }}
      h1 {{ font-size:21px; }}
      .lead,.footer-note {{ display:none; }}
      .source-panel {{ min-height:0; margin:7px; padding:12px; border-radius:12px; }}
      .panel-head {{ margin-bottom:8px; }}
      .panel-title {{ font-size:16px; }}
      .source-actions {{ gap:7px; }}
      button {{ min-height:36px; font-size:11px; }}
      .attached-list {{ max-height:72px; margin-bottom:8px; padding:7px; }}
      .attached-list.empty {{ min-height:32px; }}
      .source-help {{ margin-top:6px; font-size:9px; }}
      .cloud-picker {{ margin-top:7px; }}
      .cloud-head {{ padding:6px 8px; }}
      .cloud-row {{ gap:7px; padding:7px 8px; }}
      .cloud-name {{ font-size:10px; }}
      .status {{ min-height:14px; margin-top:5px; }}
      .footer {{ gap:7px; padding-top:6px; }}
      .confirm {{ width:100%; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="workspace">
      <header class="intro">
        <p class="eyebrow">DC · 任务资料</p>
        <h1>添加资料</h1>
        <p class="lead">选好后确认，自动返回原卡片。</p>
      </header>
      <section class="source-panel" id="drop">
        <div class="panel-head"><h2 class="panel-title">选择来源</h2></div>
        {attached_block}
        <div class="source-actions">
          <button class="source-button" id="local" type="button">上传本地文件</button>
          <button class="source-button" id="docs" type="button">选择飞书云文档</button>
        </div>
        <input id="files" name="files" type="file" multiple accept=".doc,.docx,.pdf,.ppt,.pptx,.xls,.xlsx,.csv,.txt,.md,.markdown,.rtf,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.svg,.mp3,.wav,.m4a,.mp4,.mov,.avi,.mkv,.webm,.zip,.rar,.7z">
        <p class="source-help">最多 10 项 · 本地文件单个不超过 80 MB</p>
        <section class="cloud-picker" id="cloudPicker">
          <div class="cloud-head" id="cloudHead">最近编辑的云文档</div>
          <div class="cloud-list" id="cloudList"></div>
        </section>
        <div class="status" id="status"></div>
        <div class="footer">
          <span class="footer-note">不会发送新的聊天消息</span>
          <button class="confirm" id="confirm" type="button" {"disabled" if not draft.attachments else ""}>确认关联</button>
        </div>
      </section>
    </section>
  </main>
  <script>
    const base=window.location.pathname.replace(/\\/$/,'');
    const input=document.getElementById('files');
    const local=document.getElementById('local');
    const drop=document.getElementById('drop');
    const docs=document.getElementById('docs');
    const confirmButton=document.getElementById('confirm');
    const cloudPicker=document.getElementById('cloudPicker');
    const cloudList=document.getElementById('cloudList');
    const cloudHead=document.getElementById('cloudHead');
    const status=document.getElementById('status');
    let stagedCount={len(draft.attachments)};
    const selectedDocs=new Map();
    function refreshConfirm(){{ confirmButton.disabled=stagedCount+selectedDocs.size===0; }}
    async function uploadFiles(files){{
      const selected=Array.from(files||[]).slice(0,10);
      if(!selected.length)return;
      local.disabled=true; local.textContent=`正在上传 ${{selected.length}} 个文件…`; status.textContent='正在上传'; status.className='status';
      const data=new FormData(); selected.forEach(file=>data.append('files',file));
      try{{
        const response=await fetch(base,{{method:'POST',body:data}});
        const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||'上传失败');
        stagedCount=Number(payload.total_attachments||stagedCount+payload.attachments.length);
        status.textContent=`已添加 ${{payload.attachments.length}} 个本地文件，等待确认。`; status.className='status ok';
        local.textContent='继续上传本地文件'; input.value=''; refreshConfirm();
      }}catch(error){{ status.textContent=error.message||'上传失败，请重试'; status.className='status bad'; local.textContent='重新上传本地文件'; }}
      finally{{ local.disabled=false; }}
    }}
    async function loadCloudFiles(folderToken=''){{
      docs.disabled=true; docs.textContent='正在读取云文档…'; status.textContent='';
      try{{
        const query=folderToken?`?folder_token=${{encodeURIComponent(folderToken)}}`:'';
        const response=await fetch(`${{base}}/cloud-files${{query}}`);
        if(response.status===401){{ window.location.href=`${{base}}/oauth/start`; return; }}
        const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||'读取云文档失败');
        cloudList.replaceChildren(); cloudPicker.classList.add('open');
        cloudHead.textContent=folderToken?'文件夹内容':'最近编辑的云文档';
        if(!payload.files.length){{ const empty=document.createElement('div'); empty.className='cloud-row'; empty.textContent='这里暂时没有可选文档'; cloudList.append(empty); }}
        for(const file of payload.files){{
          const row=document.createElement(file.isFolder?'button':'label'); row.className='cloud-row';
          if(file.isFolder){{ row.type='button'; row.addEventListener('click',()=>loadCloudFiles(file.fileToken)); }}
          else{{
            const box=document.createElement('input'); box.type='checkbox'; box.checked=selectedDocs.has(file.filePath);
            box.addEventListener('change',()=>{{ if(box.checked)selectedDocs.set(file.filePath,file);else selectedDocs.delete(file.filePath); refreshConfirm(); status.textContent=`已选择 ${{selectedDocs.size}} 个云文档。`; }});
            row.append(box);
          }}
          const text=document.createElement('span'); text.style.minWidth='0';
          const name=document.createElement('span'); name.className='cloud-name'; name.textContent=file.fileName;
          const type=document.createElement('span'); type.className='cloud-type'; type.textContent=file.isFolder?'文件夹 ›':file.fileType;
          text.append(name,type); row.append(text); cloudList.append(row);
        }}
      }}catch(error){{ status.textContent=error.message||'读取云文档失败'; status.className='status bad'; }}
      finally{{ docs.disabled=false; docs.textContent='选择飞书云文档'; }}
    }}
    input.addEventListener('change',()=>uploadFiles(input.files));
    local.addEventListener('click',()=>input.click());
    docs.addEventListener('click',()=>loadCloudFiles());
    drop.addEventListener('dragover',event=>{{ event.preventDefault(); drop.classList.add('drag'); }});
    drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
    drop.addEventListener('drop',event=>{{ event.preventDefault(); drop.classList.remove('drag'); uploadFiles(event.dataTransfer.files); }});
    confirmButton.addEventListener('click',async()=>{{
      confirmButton.disabled=true; confirmButton.textContent='正在确认…'; status.textContent='正在更新原卡片'; status.className='status';
      try{{
        if(selectedDocs.size){{
          const response=await fetch(`${{base}}/cloud-docs`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{files:Array.from(selectedDocs.values())}})}});
          const payload=await response.json(); if(!response.ok)throw new Error(payload.detail||'云文档关联失败'); stagedCount=Number(payload.total_attachments||stagedCount+selectedDocs.size);
        }}
        const response=await fetch(`${{base}}/confirm`,{{method:'POST'}}); const payload=await response.json();
        if(!response.ok)throw new Error(payload.detail||'确认失败');
        status.textContent=`已关联 ${{payload.total_attachments}} 项资料，正在返回卡片…`; status.className='status ok'; confirmButton.textContent='已确认';
        try{{ if(window.LarkAPI?.webview?.close){{ await window.LarkAPI.webview.close(); return; }} }}catch(error){{ console.debug('Lark close fallback',error); }}
        window.close();
      }}catch(error){{ status.textContent=error.message||'确认失败，请重试'; status.className='status bad'; confirmButton.textContent='确认关联'; refreshConfirm(); }}
    }});
    refreshConfirm();
    if(new URLSearchParams(window.location.search).get('cloud')==='1')loadCloudFiles();
  </script>
</body>
</html>"""


def _ai_cdr_queue_root() -> Path:
    """Resolve the mounted NAS queue used by both H5 and mini4.

    Returns:
        Existing AI-to-CDR shared queue root.

    Raises:
        HTTPException: If the configured NAS queue is unavailable.
    """
    configured = os.environ.get("DC_AI_CDR_QUEUE_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        nas_mount = os.environ.get("DC_NAS_MOUNT_POINT", "").strip()
        if nas_mount:
            root = (Path(nas_mount) / "AI转CDR工具" / "AI转CDR共享").resolve()
        else:
            root = (Path.home() / "nas_kb" / "AI转CDR工具" / "AI转CDR共享").resolve()
    required = ("inbox", "processing", "outbox", "failed", "reports")
    if not root.is_dir() or any(not (root / name).is_dir() for name in required):
        raise HTTPException(status_code=503, detail="AI 转 CDR 的 NAS 队列当前未挂载")
    try:
        (root / "color_profiles").mkdir(parents=True, exist_ok=True)
        (root / "progress").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="AI 转 CDR 的 NAS 队列不可写"
        ) from exc
    return root


def _normalize_ai_cdr_calibration(
    raw_text: str,
    filenames: list[str],
) -> list[list[dict[str, Any]]]:
    """Validate per-file CMYK mappings submitted by the H5 page.

    Args:
        raw_text: JSON string containing one entry per uploaded AI file.
        filenames: Sanitized uploaded filenames in multipart order.

    Returns:
        Normalized mappings aligned with ``filenames``.

    Raises:
        HTTPException: If the JSON structure, order, scope, or CMYK value is invalid.
    """
    try:
        raw = json.loads(raw_text or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="颜色校准 JSON 无法读取") from exc
    raw_files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(raw_files, list) or len(raw_files) != len(filenames):
        raise HTTPException(status_code=400, detail="颜色校准与 AI 文件数量不一致")
    normalized_files: list[list[dict[str, Any]]] = []
    for file_index, (filename, raw_file) in enumerate(
        zip(filenames, raw_files, strict=True),
        start=1,
    ):
        if not isinstance(raw_file, dict) or raw_file.get("name") != filename:
            raise HTTPException(
                status_code=400,
                detail=f"第 {file_index} 份颜色校准与 AI 文件名不一致",
            )
        raw_mappings = raw_file.get("mappings", [])
        if not isinstance(raw_mappings, list) or len(raw_mappings) > 64:
            raise HTTPException(
                status_code=400,
                detail=f"{filename} 最多允许 64 条颜色映射",
            )
        mappings: list[dict[str, Any]] = []
        for mapping_index, raw_mapping in enumerate(raw_mappings, start=1):
            if not isinstance(raw_mapping, dict):
                raise HTTPException(status_code=400, detail="颜色映射格式错误")
            apply_to = raw_mapping.get("apply_to", "all")
            if apply_to not in {"all", "fill", "stroke"}:
                raise HTTPException(status_code=400, detail="颜色映射应用范围无效")
            tolerance = raw_mapping.get("tolerance", 0.1)
            if (
                isinstance(tolerance, bool)
                or not isinstance(tolerance, int | float)
                or not 0 <= tolerance <= 5
            ):
                raise HTTPException(status_code=400, detail="颜色容差必须是 0–5")
            normalized_mapping: dict[str, Any] = {
                "label": str(raw_mapping.get("label") or f"颜色映射 {mapping_index}")[
                    :120
                ],
                "apply_to": apply_to,
                "tolerance": float(tolerance),
            }
            for group in ("source", "target"):
                raw_color = raw_mapping.get(group)
                if not isinstance(raw_color, dict):
                    raise HTTPException(status_code=400, detail="CMYK 颜色格式错误")
                color: dict[str, float] = {}
                for component in ("c", "m", "y", "k"):
                    value = raw_color.get(component)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int | float)
                        or not 0 <= value <= 100
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"{filename} 第 {mapping_index} 条映射的 CMYK "
                                "必须全部在 0–100"
                            ),
                        )
                    color[component] = float(value)
                normalized_mapping[group] = color
            mappings.append(normalized_mapping)
        normalized_files.append(mappings)
    return normalized_files


def _ai_cdr_draft_jobs(draft: AttachmentDraft) -> list[dict[str, Any]]:
    """Read the current batch job records stored on one H5 capability.

    Args:
        draft: Live AI-to-CDR workspace draft.

    Returns:
        Sanitized job dictionaries, or an empty list for a new workspace.
    """
    try:
        raw = json.loads(draft.task_data.get("ai_cdr_jobs", "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)][:MAX_AI_CDR_FILES]


def _read_ai_cdr_progress(
    queue_root: Path,
    submitted_at_by_source: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Read current-submission mini4 progress for each requested source.

    Args:
        queue_root: Mounted shared conversion queue.
        submitted_at_by_source: Uploaded AI names and their submission times.

    Returns:
        Progress dictionaries keyed by source filename.
    """
    newest: dict[str, dict[str, Any]] = {}
    newest_updated_at: dict[str, datetime] = {}
    for progress_path in (queue_root / "progress").glob("*.json"):
        try:
            payload = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source_name = str(payload.get("source_name") or "")
        submitted_at = submitted_at_by_source.get(source_name)
        if submitted_at is None:
            continue
        try:
            progress_updated_at = datetime.fromisoformat(
                str(payload.get("updated_at") or "")
            )
            submission_started_at = datetime.fromisoformat(submitted_at)
        except (TypeError, ValueError):
            continue
        if progress_updated_at < submission_started_at:
            continue
        if (
            source_name not in newest_updated_at
            or progress_updated_at > newest_updated_at[source_name]
        ):
            newest[source_name] = payload
            newest_updated_at[source_name] = progress_updated_at
    return newest


def _require_draft_administration(request: Request) -> None:
    """Authenticate local or explicitly trusted draft administration calls.

    Args:
        request: Incoming FastAPI request.

    Raises:
        HTTPException: If the peer is remote and the shared token is invalid.
    """
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1"}:
        return
    expected_token = os.environ.get(DRAFT_ADMIN_TOKEN_ENV, "").strip()
    provided_token = str(
        request.headers.get("X-DC-Assistant-Admin-Token") or ""
    ).strip()
    if (
        expected_token
        and provided_token
        and secrets.compare_digest(provided_token, expected_token)
    ):
        return
    raise HTTPException(
        status_code=403,
        detail="draft administration credentials are invalid",
    )


async def _patch_attachment_card(request: Request, draft: AttachmentDraft) -> None:
    """Patch the Feishu prototype card with the draft's current attachments.

    The patch sequence is kept outside the two intake routes because both local
    uploads and OAuth Drive selections must converge on the same truthful card.

    Args:
        request: FastAPI request exposing the running AstrBot context.
        draft: Bound attachment draft containing the target message ID.

    Raises:
        HTTPException: If the Feishu platform or card update is unavailable.
    """
    context = request.app.state.core_lifecycle.star_context
    platform = context.get_platform_inst(draft.platform_id)
    client = getattr(platform, "lark_api", None)
    if client is None:
        raise HTTPException(status_code=503, detail="飞书卡片更新通道不可用")
    config = getattr(platform, "config", None) or {}
    app_id = str(config.get("app_id") or "") if isinstance(config, dict) else ""
    if not app_id:
        raise HTTPException(status_code=503, detail="小助手网页应用凭证不可用")
    if draft.task_type:
        card = apply_card_visual_system(
            build_assistant_task_submitted_card(
                task_type=draft.task_type,
                task_data=draft.task_data,
                app_id=app_id,
                workspace_url=draft.upload_url,
                deliverables=draft.deliverables,
            )
        )
    else:
        card = apply_card_visual_system(
            build_assistant_attachment_demo_card(
                app_id=app_id,
                upload_url=draft.upload_url,
                attachments=draft.attachments,
            )
        )
    body = (
        PatchMessageRequestBody.builder()
        .content(json.dumps(card, ensure_ascii=False))
        .build()
    )
    patch_request = (
        PatchMessageRequest.builder()
        .message_id(draft.message_id)
        .request_body(body)
        .build()
    )
    response = await client.im.v1.message.apatch(patch_request)
    if not response.success():
        raise HTTPException(
            status_code=502,
            detail=f"资料已关联，但卡片更新失败：{response.msg}",
        )


async def _enqueue_workspace_task(request: Request, draft: AttachmentDraft) -> None:
    """Submit one saved workspace as a trusted internal Feishu task event.

    The H5 save action is the user's single execution boundary. The synthetic
    card callback reuses the existing trusted ``start_task`` route, so task
    prompts, source boundaries, media routing, and Codex authorization remain
    centralized instead of being duplicated in the dashboard API.

    Args:
        request: FastAPI request exposing the running AstrBot context.
        draft: Bound workspace draft with sanitized task and session metadata.

    Raises:
        HTTPException: If the original session or Feishu event queue is missing.
    """
    if not draft.session_id or not draft.sender_id:
        raise HTTPException(status_code=409, detail="任务会话已失效，请重新打开工作台")
    context = request.app.state.core_lifecycle.star_context
    platform = context.get_platform_inst(draft.platform_id)
    create_event = getattr(platform, "create_event", None)
    commit_event = getattr(platform, "commit_event", None)
    if not callable(create_event) or not callable(commit_event):
        raise HTTPException(status_code=503, detail="任务执行队列暂时不可用")

    from astrbot.core.message.components import File, Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType

    action_payload = {
        "value": {
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": draft.task_type,
            "task_data": dict(draft.task_data),
        },
        "form_value": {},
        "source": "assistant_workbench_auto_submit",
    }
    message_text = "__card_action__:" + json.dumps(
        action_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    message = AstrBotMessage()
    message.type = MessageType(draft.message_type)
    message.self_id = draft.platform_id
    message.session_id = draft.session_id
    message.message_id = f"workspace_{uuid.uuid4().hex}"
    message.sender = MessageMember(
        user_id=draft.sender_id,
        nickname=draft.sender_name or draft.sender_id[:8],
    )
    message_parts = [Plain(message_text)]
    chat_service = getattr(getattr(request.app.state, "services", None), "chat", None)
    resolve_attachment = getattr(chat_service, "resolve_attachment_file", None)
    use_linked_attachments = not (
        draft.task_type == "research"
        and draft.task_data.get("source_policy") == "public_only"
    )
    if callable(resolve_attachment) and use_linked_attachments:
        for attachment in draft.attachments:
            attachment_id = str(attachment.get("attachment_id") or "")
            if not attachment_id or attachment.get("type") == "cloud_doc":
                continue
            try:
                file_path, _mime_type = await resolve_attachment(attachment_id)
            except Exception:
                continue
            if file_path:
                message_parts.append(
                    File(
                        name=str(attachment.get("filename") or "任务资料")[:120],
                        file=file_path,
                    )
                )
    message.message = message_parts
    message.message_str = message_text
    message.raw_message = action_payload
    message.group_id = draft.group_id
    message.is_card_action = True
    message.card_action_payload = action_payload
    event = create_event(message)
    event.is_card_action = True
    event.is_wake = True
    event.is_at_or_wake_command = True
    event.set_extra("assistant_workbench_auto_submit", True)
    event.set_extra("assistant_workbench_workspace_token", draft.token)
    event.set_extra("assistant_workbench_workspace_url", draft.upload_url)
    commit_event(event)


@router.post("/drafts")
async def create_attachment_draft(
    payload: CreateDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """Create a short-lived upload capability from the local host.

    Args:
        payload: Platform identifier and LAN origin for the H5 page.
        request: FastAPI request used to enforce loopback administration.

    Returns:
        Capability token, absolute upload URL, and expiry timestamp.

    Raises:
        HTTPException: If the caller is remote or the origin is invalid.
    """
    _require_draft_administration(request)
    try:
        draft = draft_store.create(
            platform_id=payload.platform_id,
            upload_base_url=payload.upload_base_url,
            task_type=payload.task_type,
            session_id=payload.session_id,
            message_type=payload.message_type,
            sender_id=payload.sender_id,
            sender_name=payload.sender_name,
            group_id=payload.group_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "token": draft.token,
        "upload_url": draft.upload_url,
        "expires_at": draft.expires_at,
        "task_type": draft.task_type,
    }


@router.post("/drafts/{token}/bind")
async def bind_attachment_draft(
    token: str,
    payload: BindDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """Bind the locally sent Feishu card to its upload capability.

    Args:
        token: Live capability token returned during draft creation.
        payload: Feishu message identifier to patch after attachment changes.
        request: FastAPI request used to enforce loopback administration.

    Returns:
        Bound capability token and Feishu message identifier.

    Raises:
        HTTPException: If the caller is remote, the token expired, or the
            message identifier is empty.
    """
    _require_draft_administration(request)
    try:
        draft = draft_store.bind_message(token, payload.message_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="upload capability not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"token": draft.token, "message_id": draft.message_id}


@router.get("/media-player", response_class=HTMLResponse)
async def get_media_player_page(
    src: str,
    title: str = "视频预览",
    poster: str = "",
    engine: str = "",
    ratio: str = "",
    duration: str = "",
    task_id: str = "",
) -> HTMLResponse:
    """Serve the cinematic H5 player used by Feishu video result cards.

    Args:
        src: Browser-accessible HTTP(S) video source URL.
        title: User-facing result title.
        poster: Optional HTTP(S) poster image URL.
        engine: Optional generation engine label.
        ratio: Optional delivery aspect ratio.
        duration: Optional requested duration label.
        task_id: Optional user-facing task identifier.

    Returns:
        Non-cacheable H5 player page with a restrictive security policy.

    Raises:
        HTTPException: If a media or poster URL is not a bounded HTTP(S) URL.
    """
    source = urlsplit(src.strip())
    if source.scheme not in {"http", "https"} or not source.netloc or len(src) > 6000:
        raise HTTPException(status_code=400, detail="视频地址无效")
    poster_value = poster.strip()
    if poster_value:
        parsed_poster = urlsplit(poster_value)
        if (
            parsed_poster.scheme not in {"http", "https"}
            or not parsed_poster.netloc
            or len(poster_value) > 3000
        ):
            raise HTTPException(status_code=400, detail="封面地址无效")
    return HTMLResponse(
        build_media_player_page(
            source_url=src.strip(),
            title=title,
            poster_url=poster_value,
            engine=engine,
            aspect_ratio=ratio,
            duration=duration,
            task_id=task_id,
        ),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; media-src http: https: blob:; "
                "img-src http: https: data:; connect-src 'none'; "
                "frame-ancestors 'self' https://*.feishu.cn https://*.larksuite.com"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{token}", response_class=HTMLResponse)
async def get_attachment_upload_page(token: str) -> HTMLResponse:
    """Serve the upload page for one live capability token.

    Args:
        token: Capability token embedded in the card button URL.

    Returns:
        Non-cacheable H5 upload page with a restrictive security policy.

    """
    draft = draft_store.get(token)
    if draft is None:
        return HTMLResponse(
            """<!doctype html>
<html lang="zh-CN" data-ui-system="material-quotation">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="theme-color" content="#46513a">
  <title>工作台入口已更新</title>
  <style>
    :root{--ink:#25281f;--muted:#777a6d;--line:#dedfd6;--paper:#ffffff;--olive:#46513a;--signal:#d96b32;--signal-soft:#fff0e6}
    *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",sans-serif}
    main{max-width:520px;margin:18vh auto;padding:28px;border:1px solid var(--line);border-radius:14px;background:#fff;text-align:center;box-shadow:0 16px 38px rgba(47,48,38,.1)}
    h1{font:800 24px/1.25 "Songti SC","STSong",serif;letter-spacing:-.04em;margin:0 0 14px}p{color:var(--muted);line-height:1.7;margin:8px 0}
    strong{color:var(--signal)}
  </style>
</head>
<body><main>
  <h1>这张旧卡片的入口已失效</h1>
  <p>请返回与巅池-Agent小助手的聊天，点击最新的<strong>「选择任务类型」</strong>卡片。</p>
  <p>长期入口每次点击都会自动生成新的安全工作台，不需要重新下载或配置。</p>
</main></body></html>""",
            status_code=410,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "frame-ancestors 'self' https://*.feishu.cn "
                    "https://*.larksuite.com"
                ),
                "Referrer-Policy": "no-referrer",
            },
        )
    return HTMLResponse(
        build_attachment_upload_page(draft),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data: blob:"
            ),
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/{token}/ai-cdr/jobs")
async def submit_ai_cdr_jobs(
    token: str,
    files: list[UploadFile] = File(...),
    calibration: str = Form(...),
    icc_profile: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Atomically publish one H5 batch into the NAS conversion queue.

    Args:
        token: Live Feishu H5 capability token.
        files: Illustrator files selected in multipart order.
        calibration: Per-file CMYK mapping JSON aligned with ``files``.
        icc_profile: Optional shared ICC or ICM file for validation and audit.

    Returns:
        Queued job records used by the progress UI.

    Raises:
        HTTPException: If the capability, AI batch, calibration, ICC, or NAS fails.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="AI 转 CDR 工作台已失效")
    if draft.task_type != "ai_cdr" or not draft.message_id:
        raise HTTPException(status_code=409, detail="AI 转 CDR 工作台尚未绑定飞书卡片")
    if not files or len(files) > MAX_AI_CDR_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多上传 {MAX_AI_CDR_FILES} 份 AI",
        )

    filenames: list[str] = []
    declared_total = 0
    for upload in files:
        filename = Path(upload.filename or "").name
        if not filename or Path(filename).suffix.casefold() != ".ai":
            raise HTTPException(status_code=415, detail=f"不是 AI 文件：{filename}")
        size = max(0, int(getattr(upload, "size", 0) or 0))
        if size > MAX_AI_CDR_FILE_BYTES:
            raise HTTPException(
                status_code=413, detail=f"AI 文件超过 100 MB：{filename}"
            )
        declared_total += size
        filenames.append(filename)
    if declared_total > MAX_AI_CDR_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="整批 AI 超过 500 MB")
    normalized_mappings = _normalize_ai_cdr_calibration(calibration, filenames)
    queue_root = _ai_cdr_queue_root()
    inbox = queue_root / "inbox"

    profile_relative: str | None = None
    profile_path: Path | None = None
    if icc_profile is not None and icc_profile.filename:
        profile_name = Path(icc_profile.filename).name
        if Path(profile_name).suffix.casefold() not in {".icc", ".icm"}:
            raise HTTPException(
                status_code=415, detail="ICC 文件扩展名必须是 .icc 或 .icm"
            )
        profile_data = await icc_profile.read(MAX_AI_CDR_ICC_BYTES + 1)
        if len(profile_data) > MAX_AI_CDR_ICC_BYTES:
            raise HTTPException(status_code=413, detail="ICC 文件超过 20 MB")
        if len(profile_data) < 128 or profile_data[36:40] != b"acsp":
            raise HTTPException(status_code=400, detail="ICC 文件未通过标准签名校验")
        stored_profile_name = f"{uuid.uuid4().hex}-{profile_name}"
        profile_path = queue_root / "color_profiles" / stored_profile_name
        try:
            profile_path.write_bytes(profile_data)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail="ICC 文件写入 NAS 失败"
            ) from exc
        profile_relative = str(Path("color_profiles") / stored_profile_name)

    reserved_names: set[str] = set()
    staged: list[tuple[Path, Path, list[dict[str, Any]]]] = []
    created_sidecars: list[Path] = []
    published_sources: list[Path] = []
    actual_total = 0
    try:
        for upload, filename, mappings in zip(
            files,
            filenames,
            normalized_mappings,
            strict=True,
        ):
            destination = inbox / filename
            if (
                destination.exists()
                or destination.with_suffix(".color.json").exists()
                or destination.name in reserved_names
            ):
                destination = inbox / (
                    f"{Path(filename).stem}-{uuid.uuid4().hex[:8]}.ai"
                )
            reserved_names.add(destination.name)
            uploading = inbox / f".{uuid.uuid4().hex}.uploading"
            copied = 0
            with uploading.open("wb") as output_file:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    actual_total += len(chunk)
                    if copied > MAX_AI_CDR_FILE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"AI 文件超过 100 MB：{filename}",
                        )
                    if actual_total > MAX_AI_CDR_TOTAL_BYTES:
                        raise HTTPException(
                            status_code=413, detail="整批 AI 超过 500 MB"
                        )
                    output_file.write(chunk)
            if copied == 0:
                raise HTTPException(status_code=400, detail=f"AI 文件为空：{filename}")
            staged.append((uploading, destination, mappings))

        submitted_at = datetime.now(UTC).isoformat()
        jobs: list[dict[str, Any]] = []
        for uploading, destination, mappings in staged:
            if mappings or profile_relative:
                sidecar = destination.with_suffix(".color.json")
                payload: dict[str, Any] = {
                    "version": 1,
                    "source_name": destination.name,
                    "mappings": mappings,
                }
                if profile_relative:
                    payload["icc_profile"] = profile_relative
                temporary_sidecar = sidecar.with_name(f".{sidecar.name}.uploading")
                temporary_sidecar.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary_sidecar.replace(sidecar)
                created_sidecars.append(sidecar)
            uploading.replace(destination)
            published_sources.append(destination)
            jobs.append(
                {
                    "submission_id": uuid.uuid4().hex,
                    "source_name": destination.name,
                    "submitted_at": submitted_at,
                    "status": "queued",
                    "stage": "queued",
                    "percentage": 8,
                    "message": "已上传到 NAS，等待 mini4",
                    "elapsed_seconds": 0,
                }
            )
        draft_store.save_task_data(
            token,
            {
                "ai_cdr_jobs": json.dumps(
                    jobs, ensure_ascii=False, separators=(",", ":")
                )
            },
        )
    except HTTPException:
        for uploading, _, _ in staged:
            uploading.unlink(missing_ok=True)
        for path in published_sources + created_sidecars:
            path.unlink(missing_ok=True)
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        for uploading, _, _ in staged:
            uploading.unlink(missing_ok=True)
        for path in published_sources + created_sidecars:
            path.unlink(missing_ok=True)
        if profile_path is not None:
            profile_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail="AI 文件写入 NAS 失败") from exc
    return {
        "jobs": jobs,
        "queue_mode": "single_worker_sequential",
        "icc_mode": "registered_only" if profile_relative else None,
    }


@router.get("/{token}/ai-cdr/jobs")
async def list_ai_cdr_jobs(token: str) -> dict[str, Any]:
    """Return truthful queue and mini4 progress for one H5 batch.

    Args:
        token: Live Feishu H5 capability token.

    Returns:
        Current job rows with percentages, elapsed time, errors, and downloads.

    Raises:
        HTTPException: If the workspace expired or is not an AI-to-CDR task.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="AI 转 CDR 工作台已失效")
    if draft.task_type != "ai_cdr":
        raise HTTPException(status_code=409, detail="当前工作台不是 AI 转 CDR")
    jobs = _ai_cdr_draft_jobs(draft)
    if not jobs:
        return {"jobs": [], "queue_mode": "single_worker_sequential"}
    queue_root = _ai_cdr_queue_root()
    submitted_at_by_source = {
        str(job.get("source_name") or ""): str(job.get("submitted_at") or "")
        for job in jobs
    }
    progress = _read_ai_cdr_progress(queue_root, submitted_at_by_source)
    inbox_order = [
        path.name
        for path in sorted(
            (queue_root / "inbox").glob("*.ai"),
            key=lambda item: item.stat().st_mtime,
        )
    ]
    now = datetime.now(UTC)
    current_jobs: list[dict[str, Any]] = []
    for index, job in enumerate(jobs):
        source_name = str(job.get("source_name") or "")
        snapshot = progress.get(source_name)
        if snapshot is not None:
            current = {
                **job,
                "status": str(snapshot.get("status") or "in_progress"),
                "stage": str(snapshot.get("stage") or "preparing"),
                "percentage": int(snapshot.get("percentage") or 0),
                "message": str(snapshot.get("message") or "mini4 正在处理"),
                "elapsed_seconds": float(snapshot.get("elapsed_seconds") or 0),
                "error": str(snapshot.get("error") or "") or None,
            }
            if current["status"] == "succeeded":
                current["download_url"] = (
                    f"/api/v1/assistant-attachments/{token}/ai-cdr/jobs/"
                    f"{index}/download"
                )
                current["download_name"] = f"{Path(source_name).stem}.cdr"
            current_jobs.append(current)
            continue
        try:
            submitted = datetime.fromisoformat(str(job.get("submitted_at") or ""))
            elapsed = max(0.0, (now - submitted).total_seconds())
        except ValueError:
            elapsed = 0.0
        if source_name in inbox_order:
            position = inbox_order.index(source_name)
            message = (
                "排队中，马上开始"
                if position == 0
                else f"排队中，前面还有 {position} 份"
            )
        else:
            message = "mini4 已认领，正在准备转换"
        current_jobs.append(
            {
                **job,
                "status": "queued",
                "stage": "queued",
                "percentage": 8,
                "message": message,
                "elapsed_seconds": round(elapsed, 1),
                "error": None,
            }
        )
    return {"jobs": current_jobs, "queue_mode": "single_worker_sequential"}


@router.get("/{token}/ai-cdr/jobs/{job_index}/download")
async def download_ai_cdr_result(token: str, job_index: int) -> FileResponse:
    """Download one completed CDR without exposing an absolute NAS path.

    Args:
        token: Live Feishu H5 capability token.
        job_index: Zero-based job index owned by the current capability.

    Returns:
        Completed CDR file response.

    Raises:
        HTTPException: If the job is unauthorized, incomplete, or unavailable.
    """
    draft = draft_store.get(token)
    if draft is None or draft.task_type != "ai_cdr":
        raise HTTPException(status_code=404, detail="AI 转 CDR 工作台已失效")
    jobs = _ai_cdr_draft_jobs(draft)
    if job_index < 0 or job_index >= len(jobs):
        raise HTTPException(status_code=404, detail="转换任务不存在")
    source_name = str(jobs[job_index].get("source_name") or "")
    queue_root = _ai_cdr_queue_root()
    snapshot = _read_ai_cdr_progress(
        queue_root,
        {source_name: str(jobs[job_index].get("submitted_at") or "")},
    ).get(source_name)
    if snapshot is None or snapshot.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail="CDR 尚未转换完成")
    job_id = str(snapshot.get("job_id") or "")
    try:
        report = json.loads(
            (queue_root / "reports" / f"{job_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="转换报告暂时不可用") from exc
    output_name = Path(str(report.get("output_cdr") or "")).name
    output_path = (queue_root / "outbox" / output_name).resolve()
    if (
        not output_name.lower().endswith(".cdr")
        or not output_path.is_relative_to((queue_root / "outbox").resolve())
        or not output_path.is_file()
    ):
        raise HTTPException(status_code=404, detail="CDR 结果文件不存在")
    return FileResponse(
        output_path,
        media_type="application/octet-stream",
        filename=f"{Path(source_name).stem}.cdr",
    )


@router.get("/{token}/jsapi-config")
async def get_jsapi_config(
    token: str,
    url: str,
    request: Request,
) -> dict[str, Any]:
    """Return scoped h5sdk.config parameters for docsPicker.

    Args:
        token: Live attachment capability token.
        url: Exact current H5 page URL without a fragment.
        request: FastAPI request exposing platform credentials.

    Returns:
        App ID, timestamp, nonce, and SHA-1 signature.

    Raises:
        HTTPException: If the URL, platform credentials, or Feishu ticket fails.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    requested = urlsplit(url)
    expected = urlsplit(draft.upload_url)
    if (
        requested.scheme != expected.scheme
        or requested.netloc != expected.netloc
        or requested.path != expected.path
    ):
        raise HTTPException(status_code=400, detail="JSAPI URL does not match draft")
    context = request.app.state.core_lifecycle.star_context
    platform = context.get_platform_inst(draft.platform_id)
    config = getattr(platform, "config", None) or {}
    app_id = str(config.get("app_id") or "") if isinstance(config, dict) else ""
    app_secret = str(config.get("app_secret") or "") if isinstance(config, dict) else ""
    if not app_id or not app_secret:
        raise HTTPException(status_code=503, detail="小助手 H5 凭证不可用")

    with _ticket_cache_lock:
        ticket, expires_at = _ticket_cache.get(app_id, ("", 0.0))
    if not ticket or expires_at <= time.time() + 60:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_payload = token_response.json()
            tenant_token = str(token_payload.get("tenant_access_token") or "")
            if token_response.status_code != 200 or not tenant_token:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取飞书访问凭证失败：{token_payload.get('msg') or 'unknown'}",
                )
            ticket_response = await client.post(
                "https://open.feishu.cn/open-apis/jssdk/ticket/get",
                headers={"Authorization": f"Bearer {tenant_token}"},
                json={},
            )
            ticket_payload = ticket_response.json()
            ticket_data = ticket_payload.get("data") or {}
            ticket = str(ticket_data.get("ticket") or "")
            if ticket_response.status_code != 200 or not ticket:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取 JSAPI 凭证失败：{ticket_payload.get('msg') or 'unknown'}",
                )
            expires_in = max(300, int(ticket_data.get("expire_in") or 7200))
            with _ticket_cache_lock:
                _ticket_cache[app_id] = (ticket, time.time() + expires_in - 60)
    nonce = secrets.token_urlsafe(12)
    timestamp = int(time.time() * 1000)
    return {
        "app_id": app_id,
        "timestamp": timestamp,
        "noncestr": nonce,
        "signature": build_jsapi_signature(
            ticket=ticket,
            nonce=nonce,
            timestamp=timestamp,
            url=url,
        ),
    }


@router.get("/{token}/oauth/start")
async def start_cloud_docs_oauth(token: str, request: Request) -> RedirectResponse:
    """Start purpose-bound connector OAuth for cloud-document access.

    Args:
        token: Live attachment capability token.
        request: FastAPI request retained for route compatibility.

    Returns:
        Redirect to Feishu's user authorization page.

    Raises:
        HTTPException: If the draft or connector configuration is unavailable.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    _ = request
    connector_base = os.getenv(FEISHU_CONNECTOR_ENV, "").strip().rstrip("/")
    if not connector_base.startswith("https://"):
        raise HTTPException(status_code=503, detail="统一飞书授权连接器未配置")

    if draft.oauth_grant_token:
        try:
            await _connector_post(
                "/api/oauth/grant/revoke",
                {"grant_token": draft.oauth_grant_token},
            )
        except HTTPException:
            pass
        _clear_cloud_docs_authorization(draft)

    state = secrets.token_urlsafe(32)
    draft.oauth_state = state
    draft_store.save()
    return_to = f"{draft.upload_url}/oauth/callback?" + urlencode({"state": state})
    authorization_url = (
        connector_base
        + "/api/auth/feishu/start?"
        + urlencode(
            {
                "app": os.getenv("FEISHU_ATTACHMENT_OAUTH_APP", "agent").strip()
                or "agent",
                "purpose": "cloud_docs",
                "return_to": return_to,
            }
        )
    )
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/{token}/oauth/callback")
async def finish_cloud_docs_oauth(
    token: str,
    request: Request,
    state: str = "",
    handoff_code: str = "",
    error: str = "",
) -> RedirectResponse:
    """Consume a connector handoff and return to the original draft.

    Args:
        token: Live attachment capability token.
        request: FastAPI request retained for route compatibility.
        state: Exact draft nonce returned through the connector.
        handoff_code: One-time connector code that never carries a long token in URL.
        error: Feishu authorization error, if the user denied access.

    Returns:
        Redirect to the same H5 page with its cloud picker opened.

    Raises:
        HTTPException: If state validation or connector handoff exchange fails.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if error:
        raise HTTPException(status_code=400, detail=f"飞书云文档授权失败：{error}")
    if (
        not handoff_code
        or not state
        or not secrets.compare_digest(state, draft.oauth_state)
    ):
        raise HTTPException(status_code=400, detail="飞书云文档授权状态无效")
    _ = request
    payload = await _connector_post(
        "/api/oauth/handoff/exchange",
        {"code": handoff_code},
    )
    access_token = str(payload.get("access_token") or "")
    grant_token = str(payload.get("grant_token") or "")
    if not access_token or not grant_token:
        raise HTTPException(
            status_code=502,
            detail="统一连接器未返回完整云文档授权",
        )
    draft.oauth_state = ""
    draft.user_access_token = access_token
    draft.user_access_token_expires_at = float(
        payload.get("access_token_expires_at") or 0
    )
    draft.oauth_grant_token = grant_token
    draft_store.save()
    return RedirectResponse(f"{draft.upload_url}?cloud=1", status_code=302)


async def _connector_post(path: str, payload: dict[str, str]) -> dict[str, Any]:
    """Call one server-side unified connector endpoint.

    Args:
        path: Fixed connector API path.
        payload: Opaque handoff or grant payload.

    Returns:
        Parsed connector response.

    Raises:
        HTTPException: If connector configuration, transport, or authorization fails.
    """
    connector_base = os.getenv(FEISHU_CONNECTOR_ENV, "").strip().rstrip("/")
    if not connector_base.startswith("https://"):
        raise HTTPException(status_code=503, detail="统一飞书授权连接器未配置")
    connector_proxy = os.getenv(FEISHU_CONNECTOR_PROXY_ENV, "").strip()
    if connector_proxy and not connector_proxy.startswith(("http://", "https://")):
        raise HTTPException(status_code=503, detail="统一飞书授权连接器代理配置无效")
    try:
        async with httpx.AsyncClient(
            timeout=10,
            proxy=connector_proxy or None,
        ) as client:
            response = await client.post(connector_base + path, json=payload)
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503, detail="统一飞书授权连接器暂不可用"
        ) from exc
    if response.status_code != 200 or not result.get("ok"):
        error = str(result.get("error") or "connector_request_failed")
        status = (
            response.status_code
            if response.status_code in {400, 401, 403, 410}
            else 502
        )
        raise HTTPException(status_code=status, detail=f"统一飞书授权失败：{error}")
    return result


def _clear_cloud_docs_authorization(draft: AttachmentDraft) -> None:
    """Clear all purpose-bound cloud-document credentials from one draft."""
    draft.oauth_state = ""
    draft.user_access_token = ""
    draft.user_access_token_expires_at = 0.0
    draft.oauth_grant_token = ""
    draft_store.save()


async def _refresh_cloud_docs_authorization(draft: AttachmentDraft) -> bool:
    """Rotate the connector grant and update the draft access token."""
    if not draft.oauth_grant_token:
        return False
    try:
        payload = await _connector_post(
            "/api/oauth/grant/refresh",
            {"grant_token": draft.oauth_grant_token},
        )
    except HTTPException as exc:
        if exc.status_code < 500:
            _clear_cloud_docs_authorization(draft)
            return False
        raise
    access_token = str(payload.get("access_token") or "")
    grant_token = str(payload.get("grant_token") or "")
    if not access_token or not grant_token:
        _clear_cloud_docs_authorization(draft)
        return False
    draft.user_access_token = access_token
    draft.user_access_token_expires_at = float(
        payload.get("access_token_expires_at") or 0
    )
    draft.oauth_grant_token = grant_token
    draft_store.save()
    return True


@router.get("/{token}/cloud-files")
async def list_cloud_documents(
    token: str,
    folder_token: str = "",
) -> dict[str, Any]:
    """List the current user's cloud files without relying on docsPicker.

    Args:
        token: Live attachment capability token.
        folder_token: Optional folder token used for one-level navigation.

    Returns:
        Sanitized folders and selectable cloud-document metadata.

    Raises:
        HTTPException: If authorization is missing or Feishu rejects the request.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if not draft.oauth_grant_token:
        raise HTTPException(status_code=401, detail="请先授权访问飞书云文档")

    try:
        await _connector_post(
            "/api/oauth/grant/check",
            {"grant_token": draft.oauth_grant_token},
        )
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise HTTPException(
                status_code=503,
                detail="统一飞书授权连接器暂不可用，现有授权已保留",
            ) from exc
        _clear_cloud_docs_authorization(draft)
        detail = (
            "员工身份已停用，云文档授权已撤销"
            if exc.status_code == 403
            else "飞书授权已失效，请重新授权"
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc

    if (
        not draft.user_access_token
        or draft.user_access_token_expires_at <= time.time() + 30
    ) and not await _refresh_cloud_docs_authorization(draft):
        raise HTTPException(status_code=401, detail="飞书授权已过期，请重新授权")

    params: dict[str, str | int] = {
        "page_size": 100,
        "order_by": "EditedTime",
        "direction": "DESC",
    }
    if folder_token.strip():
        params["folder_token"] = folder_token.strip()
    response = None
    payload: dict[str, Any] = {}
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                "https://open.feishu.cn/open-apis/drive/v1/files",
                headers={
                    "Authorization": f"Bearer {draft.user_access_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                params=params,
            )
        payload = response.json()
        unauthorized = (
            response.status_code == 401 or int(payload.get("code") or 0) == 1061005
        )
        if unauthorized and attempt == 0:
            if await _refresh_cloud_docs_authorization(draft):
                continue
        if unauthorized:
            _clear_cloud_docs_authorization(draft)
            raise HTTPException(status_code=401, detail="飞书授权已过期，请重新授权")
        break
    assert response is not None
    if response.status_code != 200 or int(payload.get("code") or 0) != 0:
        raise HTTPException(
            status_code=502,
            detail=f"读取飞书云文档失败：{payload.get('msg') or 'unknown'}",
        )

    files: list[dict[str, Any]] = []
    for item in (payload.get("data") or {}).get("files") or []:
        file_type = str(item.get("type") or "")
        file_token = str(item.get("token") or "")
        file_url = str(item.get("url") or "")
        is_folder = file_type == "folder"
        if not file_token or (not is_folder and not file_url.startswith("https://")):
            continue
        files.append(
            {
                "fileName": str(item.get("name") or "未命名云文档")[:120],
                "filePath": file_url,
                "fileType": file_type or "云文档",
                "fileToken": file_token,
                "isFolder": is_folder,
            }
        )
    return {"files": files}


@router.post("/{token}")
async def upload_attachment(
    token: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Save multiple uploaded files into the draft pending confirmation.

    Args:
        token: Live capability token from the H5 page URL.
        request: FastAPI request exposing the upload service and Feishu client.
        files: Local files selected in the H5 page.

    Returns:
        Sanitized attachment metadata and the staged attachment count.

    Raises:
        HTTPException: If the draft is invalid, a file violates limits, saving
            fails, or the draft limit is exceeded.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="card message is not bound yet")
    if not files or len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_FILES} 个文件")
    if len(draft.attachments) + len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"最多关联 {MAX_FILES} 项资料")
    prepared: list[tuple[UploadFile, str, int]] = []
    for file in files:
        original_name = Path(file.filename or "attachment").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail=f"不支持文件格式：{original_name}",
            )
        size_bytes = max(0, int(getattr(file, "size", 0) or 0))
        if size_bytes > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413, detail=f"文件超过 80 MB：{original_name}"
            )
        prepared.append((file, original_name, size_bytes))
    if sum(item[2] for item in prepared) > MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="所选文件总计超过 200 MB")

    attachments: list[dict[str, Any]] = []
    for file, original_name, size_bytes in prepared:
        try:
            saved = await request.app.state.services.chat.save_uploaded_file(
                UploadFileAdapter(file)
            )
        except ChatServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        attachments.append(
            {
                "attachment_id": str(saved.get("attachment_id") or ""),
                "filename": str(saved.get("filename") or original_name),
                "type": str(saved.get("type") or "file"),
                "size_bytes": size_bytes,
            }
        )
    try:
        draft_store.attach_many(token, attachments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "attachments": attachments,
        "total_attachments": len(draft.attachments),
        "card_updated": False,
    }


@router.post("/{token}/cloud-docs")
async def link_cloud_documents(
    token: str,
    payload: CloudDocsRequest,
    request: Request,
) -> dict[str, Any]:
    """Stage multiple OAuth Drive selections in the shared attachment draft.

    Args:
        token: Live capability token from the H5 page URL.
        payload: Feishu cloud documents selected from the Drive file list.
        request: FastAPI request retained for the shared route contract.

    Returns:
        Sanitized cloud-document metadata and the staged attachment count.

    Raises:
        HTTPException: If the draft is invalid, document limits are exceeded,
            or a URL is not a Feishu document.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="card message is not bound yet")
    if not payload.files or len(payload.files) > MAX_FILES:
        raise HTTPException(
            status_code=400, detail=f"一次最多选择 {MAX_FILES} 个云文档"
        )
    attachments: list[dict[str, Any]] = []
    for item in payload.files:
        file_url = item.file_path.strip()
        parsed = urlsplit(file_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or not (host.endswith(".feishu.cn") or host.endswith(".larksuite.com"))
        ):
            raise HTTPException(status_code=400, detail="云文档地址不属于飞书")
        attachments.append(
            {
                "attachment_id": (
                    "cloud_" + hashlib.sha256(file_url.encode()).hexdigest()[:16]
                ),
                "filename": item.file_name.strip()[:120] or "未命名云文档",
                "type": "cloud_doc",
                "cloud_file_type": item.file_type.strip().lower()[:40] or "docx",
                "url": file_url,
                "size_bytes": 0,
            }
        )
    try:
        draft_store.attach_many(token, attachments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "attachments": attachments,
        "total_attachments": len(draft.attachments),
        "card_updated": False,
    }


@router.post("/{token}/confirm")
async def confirm_attachments(token: str, request: Request) -> dict[str, Any]:
    """Confirm staged attachments, patch the card, and allow H5 to close.

    Args:
        token: Live attachment capability token.
        request: FastAPI request exposing the Feishu card client.

    Returns:
        Confirmed attachment count and card-update status.

    Raises:
        HTTPException: If the draft has no attachments or card binding.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="card message is not bound yet")
    if not draft.attachments:
        raise HTTPException(status_code=400, detail="请先添加至少一项资料")
    await _patch_attachment_card(request, draft)
    return {"total_attachments": len(draft.attachments), "card_updated": True}


@router.get("/{token}/quotation-search")
async def search_quotation_prices(token: str, query: str) -> dict[str, Any]:
    """Return deterministic historical price matches for one quotation draft.

    Args:
        token: Live material-quotation capability token.
        query: Material, specification, or supplier name from the active row.

    Returns:
        Structured Obsidian and NAS price matches safe for direct selection.

    Raises:
        HTTPException: If the draft is invalid, is not a quotation workspace,
            or the query is outside the supported boundary.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="报价链接已失效，请重新打开")
    if draft.task_type != "quotation":
        raise HTTPException(status_code=400, detail="该链接不是物料报价系统")
    try:
        matches = search_material_prices(
            query,
            db_path=Path(get_astrbot_data_path()) / "nas_memory.db",
            limit=10,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "matches": matches,
        "source": "Obsidian / NAS 历史报价索引",
        "price_policy": "历史价待复核；缺价保持待询价",
    }


@router.post("/{token}/supplier-prices")
async def save_supplier_prices(
    token: str,
    payload: str = Form(...),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """Archive one employee supplier-price submission and index it immediately.

    Args:
        token: Live material-quotation capability token.
        payload: JSON supplier metadata and optional manually entered price rows.
        file: Optional spreadsheet, document, or image kept as source evidence.

    Returns:
        Saved Obsidian note path, item count, review status, and source label.

    Raises:
        HTTPException: If the capability, JSON, file, or price rows are invalid.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="报价链接已失效，请重新打开")
    if draft.task_type != "quotation":
        raise HTTPException(status_code=400, detail="该链接不是物料报价系统")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="报价工具尚未绑定飞书消息")
    try:
        submission = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="合作公司价格数据格式无效") from exc
    if not isinstance(submission, dict):
        raise HTTPException(status_code=400, detail="合作公司价格数据格式无效")

    attachment_name = ""
    attachment_bytes = b""
    if file is not None and file.filename:
        attachment_name = Path(file.filename).name[:160]
        allowed_suffixes = {
            ".csv",
            ".docx",
            ".jpeg",
            ".jpg",
            ".pdf",
            ".png",
            ".webp",
            ".xlsm",
            ".xlsx",
        }
        if Path(attachment_name).suffix.lower() not in allowed_suffixes:
            raise HTTPException(
                status_code=400,
                detail="支持 Excel、CSV、PDF、Word 和常见图片格式",
            )
        attachment_bytes = await file.read(MAX_SUPPLIER_PRICE_UPLOAD_BYTES + 1)
        if len(attachment_bytes) > MAX_SUPPLIER_PRICE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="原始报价资料不能超过 20 MB")

    try:
        nas_config = load_nas_config()
        nas_path = str((nas_config.get("nas") or {}).get("mount_point") or "")
        result = save_supplier_price_submission(
            submission,
            submitted_by=draft.sender_name or draft.sender_id or "飞书员工",
            vault_path=Path(get_astrbot_data_path()).parent / "ObsidianVault",
            nas_path=nas_path,
            db_path=Path(get_astrbot_data_path()) / "nas_memory.db",
            attachment_name=attachment_name,
            attachment_bytes=attachment_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "source": "NAS / Obsidian 历史报价库",
        "review_policy": "员工新增价格统一标记待复核",
    }


@router.post("/{token}/quotation-item-image")
async def upload_quotation_item_image(
    token: str,
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    """Normalize one or several material images and archive them in NAS knowledge.

    Args:
        token: Live material-quotation capability token.
        file: Legacy single PNG, JPEG, or WebP image field.
        files: Single or batch image field used by the current quotation page.

    Returns:
        Ordered normalized PNG metadata plus legacy first-image fields.

    Raises:
        HTTPException: If the quotation draft, NAS mount, or image is invalid.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="报价链接已失效，请重新打开")
    if draft.task_type != "quotation":
        raise HTTPException(status_code=400, detail="该链接不是物料报价系统")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="报价工具尚未绑定飞书消息")

    uploads = ([file] if file is not None and hasattr(file, "read") else []) + (
        files if isinstance(files, list) else []
    )
    if not uploads:
        raise HTTPException(status_code=400, detail="请选择要上传的图片")
    if len(uploads) > MAX_QUOTATION_IMAGES_PER_ITEM:
        raise HTTPException(
            status_code=413,
            detail=f"每项物料最多上传 {MAX_QUOTATION_IMAGES_PER_ITEM} 张图片",
        )

    normalized_uploads: list[tuple[str, bytes]] = []
    for upload in uploads:
        original_name = Path(upload.filename or "").name[:160]
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".jpeg", ".jpg", ".png", ".webp"}:
            raise HTTPException(status_code=415, detail="图示支持 PNG、JPEG 和 WebP")
        image_bytes = await upload.read(MAX_QUOTATION_IMAGE_BYTES + 1)
        if not image_bytes:
            raise HTTPException(status_code=400, detail="上传图片为空")
        if len(image_bytes) > MAX_QUOTATION_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="单张图片不能超过 10 MB")

        try:
            with Image.open(BytesIO(image_bytes)) as source_image:
                source_image.load()
                if (
                    source_image.width * source_image.height
                    > MAX_QUOTATION_IMAGE_PIXELS
                ):
                    raise HTTPException(status_code=413, detail="图片像素尺寸过大")
                normalized_image = ImageOps.exif_transpose(source_image)
                normalized_image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                target_mode = "RGBA" if "A" in normalized_image.getbands() else "RGB"
                normalized_image = normalized_image.convert(target_mode)
                normalized_bytes = BytesIO()
                normalized_image.save(normalized_bytes, format="PNG", optimize=True)
        except HTTPException:
            raise
        except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as exc:
            raise HTTPException(status_code=400, detail="图片内容无效或已损坏") from exc
        safe_stem = re.sub(r"[\\/:*?\"<>|]+", "_", Path(original_name).stem)
        safe_stem = safe_stem.strip(" .")[:60] or "物料图片"
        normalized_uploads.append((safe_stem, normalized_bytes.getvalue()))

    try:
        nas_config = load_nas_config()
        nas_path = str((nas_config.get("nas") or {}).get("mount_point") or "")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="NAS knowledge 配置不可用") from exc
    nas_root = Path(nas_path)
    if not nas_path or not nas_root.is_dir():
        raise HTTPException(status_code=503, detail="NAS knowledge 当前未挂载")

    relative_dir = Path("projects") / "筹备组物料报价" / draft.token[:16] / "images"
    image_dir = nas_root / relative_dir
    image_results: list[dict[str, str]] = []
    written_paths: list[Path] = []
    try:
        image_dir.mkdir(parents=True, exist_ok=True)
        for safe_stem, normalized_bytes in normalized_uploads:
            image_filename = f"{uuid.uuid4().hex[:12]}_{safe_stem}.png"
            saved_path = image_dir / image_filename
            saved_path.write_bytes(normalized_bytes)
            written_paths.append(saved_path)
            image_results.append(
                {
                    "image_path": (relative_dir / image_filename).as_posix(),
                    "image_filename": image_filename,
                }
            )
    except OSError as exc:
        for written_path in written_paths:
            written_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503, detail="图片写入 NAS knowledge 失败"
        ) from exc
    first_image = image_results[0]
    return {
        "image_path": first_image["image_path"],
        "image_filename": first_image["image_filename"],
        "images": image_results,
        "archive": "NAS knowledge",
    }


@router.get("/{token}/quotation-deliverables/{kind}")
async def download_quotation_deliverable(
    token: str,
    kind: str,
    version: int,
) -> FileResponse:
    """Download one generated quotation artifact through its capability URL.

    Args:
        token: Unguessable quotation workspace capability token.
        kind: Allowed delivery key identifying Excel, Word, or PDF.
        version: Positive quotation revision selected by the generated link.

    Returns:
        A non-inline response for the exact generated office file.

    Raises:
        HTTPException: If the token, version, kind, manifest, or file is invalid.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
        raise HTTPException(status_code=404, detail="报价文件不存在")
    if kind not in {"internal_xlsx", "market_docx", "market_pdf"}:
        raise HTTPException(status_code=404, detail="报价文件不存在")
    if version < 1 or version > 10000:
        raise HTTPException(status_code=404, detail="报价版本不存在")
    output_root = Path(get_astrbot_data_path()) / "output" / "material_quotations"
    version_dir = output_root / token / f"v{version}"
    manifest_path = version_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        filename = str(manifest.get("files", {}).get(kind) or "")
    except (OSError, AttributeError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="报价文件不存在") from None
    file_path = version_dir / filename
    if (
        not filename
        or Path(filename).name != filename
        or not file_path.is_file()
        or file_path.parent.resolve() != version_dir.resolve()
    ):
        raise HTTPException(status_code=404, detail="报价文件不存在")
    media_types = {
        "internal_xlsx": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "market_docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "market_pdf": "application/pdf",
    }
    return FileResponse(
        file_path,
        media_type=media_types[kind],
        filename=filename,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/{token}/quotation-sync-doc")
async def sync_quotation_to_feishu_doc(
    token: str,
    request: Request,
) -> dict[str, Any]:
    """Create an optional Feishu cloud document from a sanitized estimate.

    Args:
        token: Live material-quotation capability token.
        request: FastAPI request exposing the bound Feishu platform client.

    Returns:
        The idempotent cloud-document URL and current quotation version.

    Raises:
        HTTPException: If no result exists or Feishu document creation fails.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="报价链接已失效，请重新打开")
    if draft.task_type != "quotation":
        raise HTTPException(status_code=400, detail="该链接不是物料报价系统")
    if not draft.delivery_version or not draft.task_data.get("grand_total"):
        raise HTTPException(status_code=409, detail="请先生成市场部估价结果")
    if draft.feishu_doc_url:
        return {
            "feishu_doc_url": draft.feishu_doc_url,
            "version": draft.delivery_version,
            "created": False,
        }

    context = request.app.state.core_lifecycle.star_context
    platform = context.get_platform_inst(draft.platform_id)
    client = getattr(platform, "lark_api", None)
    if client is None:
        raise HTTPException(status_code=503, detail="飞书云文档通道不可用")
    project_name = str(draft.task_data.get("project_name") or "未命名项目")[:120]
    title = f"{project_name} 市场估价 V{draft.delivery_version}"
    create_body = CreateDocumentRequestBody.builder().title(title).build()
    create_request = CreateDocumentRequest.builder().request_body(create_body).build()
    create_response = await client.docx.v1.document.acreate(create_request)
    document = getattr(getattr(create_response, "data", None), "document", None)
    document_id = str(getattr(document, "document_id", "") or "")
    if not create_response.success() or not document_id:
        raise HTTPException(
            status_code=502,
            detail=f"飞书云文档创建失败：{getattr(create_response, 'msg', '')}",
        )

    client_name = str(draft.task_data.get("client_name") or "未填写")[:80]
    validity_days = str(draft.task_data.get("validity_days") or "15")[:3]
    status = str(draft.task_data.get("quotation_status") or "估价方案")[:20]
    safe_lines = [
        "市场部估价结果",
        f"项目：{project_name}",
        f"客户 / 品牌：{client_name}",
        f"估价金额：¥{draft.task_data['grand_total']}",
        f"有效期：{validity_days} 天",
        f"状态：{status}",
        "本结果用于市场方案估价，正式金额以最终复核为准。",
    ]
    children = []
    for content in safe_lines:
        text_run = TextRun.builder().content(content).build()
        text_element = TextElement.builder().text_run(text_run).build()
        text_body = Text.builder().elements([text_element]).build()
        children.append(Block.builder().block_type(2).text(text_body).build())
    write_body = (
        CreateDocumentBlockChildrenRequestBody.builder()
        .children(children)
        .index(-1)
        .build()
    )
    write_request = (
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(document_id)
        .block_id(document_id)
        .request_body(write_body)
        .build()
    )
    write_response = await client.docx.v1.document_block_children.acreate(write_request)
    if not write_response.success():
        raise HTTPException(
            status_code=502,
            detail=f"飞书云文档写入失败：{getattr(write_response, 'msg', '')}",
        )
    feishu_doc_url = f"https://feishu.cn/docx/{document_id}"
    draft = draft_store.save_quotation_delivery(
        token,
        deliverables=draft.deliverables,
        version=draft.delivery_version,
        feishu_doc_url=feishu_doc_url,
    )
    return {
        "feishu_doc_url": draft.feishu_doc_url,
        "version": draft.delivery_version,
        "created": True,
    }


@router.post("/{token}/prompt-candidates")
async def generate_prompt_candidates(
    token: str,
    payload: PromptCandidatesRequest,
) -> dict[str, Any]:
    """Proxy a creative brief to the NAS professional prompt compiler.

    Args:
        token: Live task-workspace capability token.
        payload: Original prompt and current server-whitelisted creative settings.

    Returns:
        Three professional prompt candidates with compiler provenance.

    Raises:
        HTTPException: If the workspace, payload, service, or response is invalid.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="工作台链接已失效")
    if draft.task_type not in {"copy", "image", "video"}:
        raise HTTPException(status_code=400, detail="专业提词器仅支持文案、图片和视频")

    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="请先填写任务提词")
    if len(prompt) > 4000:
        raise HTTPException(status_code=400, detail="任务提词不能超过 4000 字")
    unknown_fields = set(payload.task_data) - WORKSPACE_ALLOWED_FIELDS[draft.task_type]
    if unknown_fields:
        raise HTTPException(status_code=400, detail="任务设置包含不支持的字段")
    task_data = {
        str(key): str(value or "").strip()[:4000]
        for key, value in payload.task_data.items()
        if str(value or "").strip()
    }
    memory_db_path = Path(get_astrbot_data_path()) / "nas_memory.db"
    company_memory: list[dict[str, str]] = []
    try:
        company_memory = await asyncio.to_thread(
            search_company_creative_memory,
            prompt,
            db_path=memory_db_path,
            limit=3,
        )
        company_memory_status = (
            "matched"
            if company_memory
            else "no_match"
            if memory_db_path.is_file()
            else "unavailable"
        )
        company_memory_message = (
            f"已参考 {len(company_memory)} 条公司 Obsidian 原文案"
            if company_memory
            else (
                "没有匹配到相关公司 Obsidian 原文案"
                if memory_db_path.is_file()
                else "公司 Obsidian 文案索引尚未挂载"
            )
        )
    except Exception:  # noqa: BLE001
        company_memory_status = "unavailable"
        company_memory_message = "公司 Obsidian 文案索引暂不可用"

    if draft.task_type == "copy":
        service_payload: dict[str, Any] = {
            "task_type": "copy",
            "prompt": prompt,
            "copy_type": task_data.get("copy_type", "通用文案"),
            "audience": task_data.get("audience", ""),
            "source_material": task_data.get("source_material", ""),
            "output_requirement": task_data.get("output_requirement", ""),
        }
    elif draft.task_type == "image":
        service_payload = {
            "task_type": "image",
            "prompt": prompt,
            "request_mode": "codex",
            "api_model": "gpt-image-2",
            "use_case": task_data.get("image_template", "brand_visual"),
            "aspect_ratio": task_data.get("aspect_ratio", "1:1"),
            "quality": task_data.get("quality", "medium"),
            "has_reference": bool(
                draft.attachments or task_data.get("reference_source")
            ),
        }
    else:
        duration = task_data.get("duration", "5")
        service_payload = {
            "task_type": "video",
            "prompt": prompt,
            "request_mode": "dreamina_mcp",
            "api_model": "dreamina-mcp-video",
            "use_case": "品牌短片",
            "seconds": duration,
            "aspect_ratio": task_data.get("aspect_ratio", "16:9"),
            "resolution": task_data.get("video_quality", "720p"),
            "shot_density": (
                "single" if task_data.get("camera_motion") == "fixed" else "sequence"
            ),
            "style": "commercial",
            "generate_audio": task_data.get("audio_mode", "on") == "on",
            "has_reference": bool(
                draft.attachments
                or task_data.get("reference_source")
                or task_data.get("generation_mode") == "image_to_video"
            ),
        }
    service_payload["company_memory"] = company_memory

    service_base = os.environ.get(CREATIVE_PROMPT_SERVICE_ENV, "").strip().rstrip("/")
    if service_base:
        internal_token = (
            os.environ.get(CREATIVE_PROMPT_SERVICE_TOKEN_ENV, "").strip()
            or os.environ.get("DIANCHI_TOOLBOX_ACCESS_TOKEN", "").strip()
        )
        headers = {"X-Dianchi-Toolbox-Token": internal_token} if internal_token else {}
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    f"{service_base}/api/prompt-candidates",
                    json=service_payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail="NAS 专业提词服务暂不可用，请稍后重试",
            ) from exc
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="NAS 专业提词服务返回了无效结果",
            ) from exc
        if response.status_code >= 400 or response_payload.get("status") != "ok":
            raise HTTPException(
                status_code=502,
                detail=str(response_payload.get("message") or "专业提词生成失败")[:300],
            )
        result = response_payload.get("data")
    else:
        global _creative_prompt_backend, _creative_prompt_backend_error
        if _creative_prompt_backend is None and not _creative_prompt_backend_error:
            backend_path = (
                Path(__file__).resolve().parents[3]
                / "drafts"
                / "aihubmix_standalone"
                / "server.py"
            )
            try:
                spec = importlib.util.spec_from_file_location(
                    "dianchi_creative_prompt_backend", backend_path
                )
                if spec is None or spec.loader is None:
                    raise ImportError("standalone prompt backend loader is unavailable")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _creative_prompt_backend = module.AihubmixBackend()
            except Exception as exc:  # noqa: BLE001
                _creative_prompt_backend_error = str(exc)
        if _creative_prompt_backend is None:
            raise HTTPException(
                status_code=503,
                detail="NAS 专业提词编译器未安装或暂不可用",
            )
        try:
            result = await asyncio.to_thread(
                _creative_prompt_backend.prompt_candidates, service_payload
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"专业提词生成失败：{str(exc)[:240]}",
            ) from exc
    if isinstance(result, dict):
        result["company_memory_sources"] = [
            {
                "title": item["title"],
                "source_path": item["source_path"],
                "source_status": item["source_status"],
            }
            for item in company_memory
        ]
        result["company_memory_status"] = company_memory_status
        result["company_memory_message"] = company_memory_message
    candidates = result.get("candidates") if isinstance(result, dict) else None
    if (
        not isinstance(candidates, list)
        or len(candidates) != 3
        or any(
            not isinstance(item, dict)
            or not str(item.get("title") or "").strip()
            or not str(item.get("summary") or "").strip()
            or not str(item.get("prompt") or "").strip()
            for item in candidates
        )
    ):
        raise HTTPException(
            status_code=502,
            detail="NAS 专业提词服务没有返回 3 个完整候选",
        )
    return result


@router.post("/{token}/workspace")
async def save_task_workspace(
    token: str,
    payload: WorkspaceRequest,
    request: Request,
) -> dict[str, Any]:
    """Save a workspace and deliver its task-specific result.

    Args:
        token: Live task-workspace capability token.
        payload: Prompt, material notes, and server-whitelisted settings.
        request: FastAPI request exposing the Feishu card client.

    Returns:
        Saved task type, attachment count, queue state, card-update status, and
        the result surface when the task is a deterministic quotation.

    Raises:
        HTTPException: If the draft, task type, required prompt, model choice,
            or target card binding is invalid.
    """
    draft = draft_store.get(token)
    if draft is None:
        raise HTTPException(status_code=404, detail="upload link expired or invalid")
    if not draft.message_id:
        raise HTTPException(status_code=409, detail="card message is not bound yet")
    if draft.task_type not in WORKSPACE_ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail="该链接不是任务工作台")
    unknown_fields = set(payload.task_data) - WORKSPACE_ALLOWED_FIELDS[draft.task_type]
    if unknown_fields:
        raise HTTPException(status_code=400, detail="任务设置包含不支持的字段")
    if draft.task_type == "quotation":
        try:
            task_data = normalize_material_quotation(payload.task_data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if task_data.get("pending_inquiry_count") != "0":
            raise HTTPException(
                status_code=400,
                detail="仍有待询价物料，补齐单价后才能发送市场部估价结果",
            )
    else:
        task_data = {
            str(key): str(value or "").strip()[
                : 6000
                if str(key)
                in {
                    "glossary",
                    "task_request",
                    "translation_memory",
                    "video_prompt",
                    "visual_prompt",
                }
                else 800
            ]
            for key, value in payload.task_data.items()
            if str(value or "").strip()
        }
    required_field = TASK_REQUIRED_FIELDS[draft.task_type]
    if not task_data.get(required_field):
        detail = (
            "请先填写项目名称" if draft.task_type == "quotation" else "请先填写任务提词"
        )
        raise HTTPException(status_code=400, detail=detail)
    if draft.task_type == "research":
        task_data.setdefault("analysis_mode", "evidence_review")
        task_data.setdefault("research_depth", "standard")
        task_data.setdefault("source_policy", "mixed")
        task_data.setdefault("output_format", "short_answer")
        task_data.setdefault("delivery_format", "result_card")
        if task_data["analysis_mode"] not in RESEARCH_ANALYSIS_MODES:
            raise HTTPException(status_code=400, detail="AI 分析方法不受支持")
        if task_data["research_depth"] not in RESEARCH_DEPTHS:
            raise HTTPException(status_code=400, detail="研究深度不受支持")
        if task_data["source_policy"] not in RESEARCH_SOURCE_POLICIES:
            raise HTTPException(status_code=400, detail="资料使用范围不受支持")
        if task_data["source_policy"] == "uploaded_only" and not draft.attachments:
            source_urls = re.findall(
                r"https?://[^\s，,；;]+", task_data.get("source_material", "")
            )
            if not source_urls:
                raise HTTPException(
                    status_code=400,
                    detail="选择“仅使用已上传资料”时，请先上传文件、选择云文档或填写来源链接",
                )
        if task_data["output_format"] not in RESEARCH_CONTENT_FORMATS:
            raise HTTPException(status_code=400, detail="内容结构不受支持")
        if task_data["delivery_format"] not in RESEARCH_DELIVERY_FORMATS:
            raise HTTPException(status_code=400, detail="交付文件不受支持")
    if draft.task_type == "file" and not draft.attachments:
        file_source = urlsplit(task_data.get("file_source", ""))
        if file_source.scheme not in {"http", "https"} or not file_source.netloc:
            raise HTTPException(
                status_code=400,
                detail="请先上传文件、选择飞书云文档，或填写有效文件链接",
            )
    if draft.task_type == "file":
        task_data.setdefault("operation", "summarize")
        task_data.setdefault(
            "preserve_layout",
            "preserve" if task_data["operation"] == "translate" else "restructure",
        )
        task_data.setdefault("output_format", "docx")
        if task_data["operation"] not in FILE_OPERATIONS:
            raise HTTPException(status_code=400, detail="文件处理方式不受支持")
        if task_data["preserve_layout"] not in FILE_LAYOUT_MODES:
            raise HTTPException(status_code=400, detail="原结构处理方式不受支持")
        if task_data["output_format"] not in FILE_DELIVERY_FORMATS:
            raise HTTPException(status_code=400, detail="交付文件不受支持")
        source_urls = re.findall(
            r"https?://[^\s，,；;]+", task_data.get("file_source", "")
        )
        source_count = len(draft.attachments) + len(source_urls)
        if task_data["operation"] == "compare" and source_count < 2:
            raise HTTPException(
                status_code=400,
                detail="文件对比至少需要两份上传文件或两个有效链接",
            )
        if task_data["operation"] == "translate":
            if source_count != 1:
                raise HTTPException(
                    status_code=400,
                    detail="第一期全文件翻译每次处理一份原文件；请只保留一个文件或链接",
                )
            if source_urls and urlsplit(source_urls[0]).scheme != "https":
                raise HTTPException(
                    status_code=400,
                    detail="翻译文件链接必须使用 HTTPS；也可以改为本地上传",
                )
            task_data.setdefault("source_language", "auto")
            target_language = task_data.get("target_language", "")
            if target_language not in TRANSLATION_LANGUAGES:
                raise HTTPException(status_code=400, detail="请选择目标语言")
            if task_data["source_language"] not in TRANSLATION_LANGUAGES | {"auto"}:
                raise HTTPException(status_code=400, detail="源语言不受支持")
            if task_data["source_language"] == target_language:
                raise HTTPException(status_code=400, detail="源语言和目标语言不能相同")
            task_data.setdefault("direction_mode", "one_way")
            if task_data["direction_mode"] not in {"one_way", "bidirectional"}:
                raise HTTPException(status_code=400, detail="翻译方向不受支持")
            if (
                task_data["direction_mode"] == "bidirectional"
                and task_data["source_language"] == "auto"
            ):
                raise HTTPException(
                    status_code=400, detail="双向互译需要明确选择两种语言"
                )
            task_data.setdefault("translation_style", "faithful")
            if task_data["translation_style"] not in {"faithful", "professional"}:
                raise HTTPException(status_code=400, detail="翻译方式不受支持")
            task_data.setdefault("professional_domain", "general")
            if task_data["professional_domain"] not in {
                "general",
                "legal",
                "finance",
                "technology",
                "medical",
                "marketing",
                "manufacturing",
            }:
                raise HTTPException(status_code=400, detail="专业领域不受支持")
            task_data.setdefault("output_mode", "target_only")
            if task_data["output_mode"] not in {"target_only", "bilingual"}:
                raise HTTPException(status_code=400, detail="译文呈现方式不受支持")
            if task_data["direction_mode"] == "bidirectional":
                task_data["output_mode"] = "bilingual"
            if task_data["preserve_layout"] not in {"preserve", "business_clean"}:
                raise HTTPException(
                    status_code=400, detail="翻译排版请选择保留原版面或商务简洁排版"
                )

            source_suffix = ""
            if draft.attachments:
                source_attachment = draft.attachments[0]
                if source_attachment.get("type") == "cloud_doc":
                    cloud_file_type = str(
                        source_attachment.get("cloud_file_type") or "docx"
                    ).lower()
                    if cloud_file_type not in {"doc", "docx"}:
                        raise HTTPException(
                            status_code=400,
                            detail="第一期飞书云材料只支持飞书文档；云表格请导出为 Excel 后上传",
                        )
                    source_suffix = ".docx"
                else:
                    source_suffix = Path(
                        str(source_attachment.get("filename") or "")
                    ).suffix.lower()
            elif source_urls:
                parsed_source = urlsplit(source_urls[0])
                if (
                    "feishu.cn" in parsed_source.netloc
                    or "larksuite.com" in parsed_source.netloc
                ):
                    source_suffix = ".docx"
                else:
                    source_suffix = Path(parsed_source.path).suffix.lower()
            delivery_by_source = {
                ".docx": {"docx", "pdf", "txt", "feishu_doc"},
                ".xlsx": {"xlsx"},
                ".txt": {"docx", "pdf", "txt", "feishu_doc"},
                ".md": {"docx", "pdf", "txt", "feishu_doc"},
                ".markdown": {"docx", "pdf", "txt", "feishu_doc"},
                ".pdf": {"docx", "pdf", "txt", "feishu_doc"},
            }
            if source_suffix not in delivery_by_source:
                raise HTTPException(
                    status_code=400,
                    detail="第一期翻译支持 Word、Excel、文本、Markdown 和可搜索文字的 PDF",
                )
            if task_data["output_format"] not in delivery_by_source[source_suffix]:
                detail = (
                    "Excel 翻译只能交付 Excel，以保留工作表、公式、数值和基本格式"
                    if source_suffix == ".xlsx"
                    else "当前源文件不支持所选交付格式"
                )
                raise HTTPException(status_code=400, detail=detail)
    allowed_model_choices = (
        {"auto", "image2", "dreamina"}
        if draft.task_type == "image"
        else {"auto", "fast", "balanced", "quality_first"}
    )
    if task_data.get("model_choice", "auto") not in allowed_model_choices:
        raise HTTPException(status_code=400, detail="模型选择不受支持")
    task_data.setdefault("model_choice", "auto")
    use_linked_attachments = not (
        draft.task_type == "research"
        and task_data.get("source_policy") == "public_only"
    )
    if draft.attachments and use_linked_attachments:
        material_field = str(WORKSPACE_SCHEMAS[draft.task_type]["material_field"])
        material_rows = []
        for attachment in draft.attachments:
            filename = str(attachment.get("filename") or "未命名资料")[:120]
            url = str(attachment.get("url") or "")
            material_rows.append(f"{filename}（{url}）" if url else filename)
        attachment_note = f"已关联 {len(material_rows)} 项资料：" + "、".join(
            material_rows
        )
        existing_note = task_data.get(material_field, "")
        task_data[material_field] = (
            f"{attachment_note}；{existing_note}" if existing_note else attachment_note
        )[:800]
    draft = draft_store.save_task_data(token, task_data)
    if draft.task_type == "quotation":
        version = draft.delivery_version + 1
        try:
            nas_config = load_nas_config()
            nas_path = str((nas_config.get("nas") or {}).get("mount_point") or "")
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail="NAS knowledge 配置不可用，报价文件未生成"
            ) from exc
        nas_root = Path(nas_path)
        if not nas_path or not nas_root.is_dir():
            raise HTTPException(
                status_code=503, detail="NAS knowledge 当前未挂载，报价文件未生成"
            )
        archive_relative_root = Path("projects") / "筹备组物料报价" / draft.token[:16]
        image_dir = nas_root / archive_relative_root / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        quotation_items = json.loads(task_data["quotation_items"])
        image_paths: list[str] = []
        for item in quotation_items:
            raw_item_image_paths = item.get("image_paths")
            item_image_paths = (
                [str(path or "").strip() for path in raw_item_image_paths]
                if isinstance(raw_item_image_paths, list)
                else []
            )
            legacy_image_path = str(item.get("image_path") or "").strip()
            if legacy_image_path and legacy_image_path not in item_image_paths:
                item_image_paths.insert(0, legacy_image_path)
            item_image_paths = list(
                dict.fromkeys(path for path in item_image_paths if path)
            )
            if len(item_image_paths) > MAX_QUOTATION_IMAGES_PER_ITEM:
                raise HTTPException(
                    status_code=400,
                    detail=(f"每项物料最多上传 {MAX_QUOTATION_IMAGES_PER_ITEM} 张图片"),
                )
            for image_path in item_image_paths:
                relative_image = Path(image_path)
                resolved_image = (nas_root / relative_image).resolve()
                if (
                    relative_image.is_absolute()
                    or resolved_image.parent != image_dir.resolve()
                    or resolved_image.suffix.lower() != ".png"
                    or not resolved_image.is_file()
                ):
                    raise HTTPException(
                        status_code=400, detail="物料图片路径无效，请重新上传"
                    )
                normalized_relative_image = relative_image.as_posix()
                if normalized_relative_image not in image_paths:
                    image_paths.append(normalized_relative_image)
        output_dir = (
            Path(get_astrbot_data_path())
            / "output"
            / "material_quotations"
            / draft.token
            / f"v{version}"
        )
        try:
            artifacts = await asyncio.to_thread(
                generate_material_quotation_deliverables,
                draft.task_data,
                output_dir=output_dir,
                version=version,
                image_root=nas_root,
            )
            manifest_path = output_dir / "manifest.json"
            manifest = {
                "project_name": task_data["project_name"],
                "version": version,
                "files": {key: path.name for key, path in artifacts.items()},
                "images": image_paths,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            archive_version_dir = nas_root / archive_relative_root / f"v{version}"
            archive_version_dir.mkdir(parents=True, exist_ok=True)
            for artifact_path in artifacts.values():
                shutil.copy2(artifact_path, archive_version_dir / artifact_path.name)
            (archive_version_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            deliverables = {
                key: (
                    f"{draft.upload_url}/quotation-deliverables/{key}?version={version}"
                )
                for key in artifacts
            }
            draft = draft_store.save_quotation_delivery(
                token,
                deliverables=deliverables,
                version=version,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=500,
                detail="估价已复算，但交付文件生成失败，请重试",
            ) from exc
        await _patch_attachment_card(request, draft)
        return {
            "task_type": draft.task_type,
            "total_attachments": len(draft.attachments),
            "task_queued": False,
            "card_updated": True,
            "result_surface": "market_estimate",
            "version": draft.delivery_version,
            "deliverables": draft.deliverables,
            "feishu_doc_url": draft.feishu_doc_url,
            "nas_archive_path": (
                archive_relative_root / f"v{draft.delivery_version}"
            ).as_posix(),
        }
    await _enqueue_workspace_task(request, draft)
    await _patch_attachment_card(request, draft)
    return {
        "task_type": draft.task_type,
        "total_attachments": len(draft.attachments),
        "task_queued": True,
        "card_updated": True,
    }


__all__ = [
    "AttachmentDraft",
    "AttachmentDraftStore",
    "CloudDocsRequest",
    "PromptCandidatesRequest",
    "build_jsapi_signature",
    "build_attachment_upload_page",
    "build_media_player_page",
    "build_material_quotation_page",
    "build_task_workspace_page",
    "confirm_attachments",
    "download_quotation_deliverable",
    "draft_store",
    "finish_cloud_docs_oauth",
    "get_media_player_page",
    "generate_prompt_candidates",
    "link_cloud_documents",
    "list_cloud_documents",
    "router",
    "save_task_workspace",
    "search_quotation_prices",
    "start_cloud_docs_oauth",
    "sync_quotation_to_feishu_doc",
    "upload_quotation_item_image",
]
