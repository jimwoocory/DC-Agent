"""Standalone AIHUBMIX preview server.

This serves the local AIHUBMIX-style HTML page and proxies generation requests
without registering anything in AstrBot.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = os.environ.get("AIHUBMIX_PREVIEW_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PORT = 8785
VERSION = "0.1.0"
AUTH_COOKIE_NAME = "dianchi_toolbox_session"
AUTH_SESSION_SECONDS = 12 * 60 * 60

ROOT = Path(__file__).resolve().parent
DC_AGENT_ROOT = ROOT.parent.parent
DC_ENGINES_ROOT = DC_AGENT_ROOT / "dc_engines"
INDEX_HTML = ROOT / "pages" / "dashboard" / "index.html"
GALLERY_HTML = ROOT / "pages" / "gallery" / "index.html"
WRITING_HTML = ROOT / "pages" / "writing" / "index.html"
VIDEO_HTML = ROOT / "pages" / "video" / "index.html"
PLUGIN_DRAFT = ROOT.parent / "aihubmix_video_plugin" / "main.py"
DREAMINA_OUTPUT_DIR = ROOT / "data" / "dreamina_outputs"
DREAMINA_INPUT_DIR = ROOT / "data" / "dreamina_inputs"

DEFAULT_API_BASE = "https://aihubmix.com/v1"
DEFAULT_GEMINI_BASE = "https://aihubmix.com/gemini/v1beta"
DEFAULT_MODELS_URL = "https://video.aihubmix.com/api/models/video"
DEFAULT_IMAGE_MODELS_URL = "https://video.aihubmix.com/api/models/image"
DEFAULT_POLL_INTERVAL_SECONDS = 15
MODELS_CACHE_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 45

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_CHAT_MODEL = "gpt-5.4"
CODEX_PROMPT_MODEL = (
    os.environ.get("DIANCHI_PROMPT_MODEL", "gpt-5.5").strip() or "gpt-5.5"
)
CODEX_WRITING_MODEL = (
    os.environ.get("DIANCHI_WRITING_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
)
CODEX_IMAGE_MODEL = "gpt-image-2"
CODEX_IMAGE_INSTRUCTIONS = (
    "You are an assistant that must fulfill image generation requests by "
    "using the image_generation tool when provided. If the user's image brief "
    "is short or casual, first interpret it as a production brief and preserve "
    "the compiled visual requirements when calling the image tool."
)
CODEX_IMAGE_SIZES = {
    "landscape": "1536x1024",
    "16:9": "1536x1024",
    "3:2": "1536x1024",
    "square": "1024x1024",
    "1:1": "1024x1024",
    "portrait": "1024x1536",
    "9:16": "1024x1536",
    "2:3": "1024x1536",
}
WRITING_MODELS: dict[str, dict[str, str]] = {
    "gpt-5.6-sol": {
        "label": "GPT-5.6 Sol",
        "provider": "codex-oauth",
        "api_model": CODEX_WRITING_MODEL,
    },
    "qwen3.7-max": {"label": "Qwen 3.7 Max", "provider": "aihubmix"},
    "doubao-seed-2-1-pro": {
        "label": "豆包 Seed 2.1 Pro",
        "provider": "aihubmix",
    },
    "claude-fable-5": {"label": "Claude Fable 5", "provider": "aihubmix"},
    "gemini-3.5-flash": {"label": "Gemini 3.5 Flash", "provider": "aihubmix"},
    "grok-4.5": {"label": "Grok 4.5", "provider": "aihubmix"},
}
MAX_WRITING_BRIEF_CHARS = 4000


def auth_token() -> str:
    """Return the optional browser access token from the environment.

    Returns:
        The stripped token value, or an empty string when browser auth is disabled.
    """
    return os.environ.get("DIANCHI_TOOLBOX_ACCESS_TOKEN", "").strip()


def auth_enabled() -> bool:
    """Report whether browser login is required.

    Returns:
        True when `DIANCHI_TOOLBOX_ACCESS_TOKEN` is configured.
    """
    return bool(auth_token())


def sign_auth_session(now: int | None = None) -> str:
    """Create a signed session cookie value.

    Args:
        now: Optional Unix timestamp used by tests.

    Returns:
        A compact timestamp and HMAC signature cookie value.
    """
    issued_at = int(now if now is not None else time.time())
    secret = auth_token()
    digest = hmac.new(
        secret.encode("utf-8"), str(issued_at).encode("utf-8"), hashlib.sha256
    )
    return f"{issued_at}.{digest.hexdigest()}"


def verify_auth_session(value: str, now: int | None = None) -> bool:
    """Verify a signed browser session cookie.

    Args:
        value: Cookie value in `<issued_at>.<signature>` form.
        now: Optional Unix timestamp used by tests.

    Returns:
        True when the cookie is signed by the configured token and still valid.
    """
    if not auth_enabled():
        return True
    issued_text, dot, signature = str(value or "").partition(".")
    if not dot or not issued_text.isdigit() or not signature:
        return False
    issued_at = int(issued_text)
    current = int(now if now is not None else time.time())
    if issued_at > current + 60 or current - issued_at > AUTH_SESSION_SECONDS:
        return False
    expected = sign_auth_session(issued_at).split(".", 1)[1]
    return secrets.compare_digest(signature, expected)


LOCAL_CODEX_IMAGE_MODEL: dict[str, Any] = {
    "key": "local-codex-gpt-image-2",
    "label": "GPT Image 2 · Codex OAuth",
    "apiModel": CODEX_IMAGE_MODEL,
    "requestMode": "codex",
    "vendorGroup": "Local Codex OAuth",
    "params": {
        "size": ["1536x1024", "1024x1024", "1024x1536"],
        "quality": ["medium", "high", "low"],
        "supportsInputReference": True,
        "supportsMultipleInputReferences": True,
    },
    "extraBodyParams": [],
}

LOCAL_DREAMINA_IMAGE_MODEL: dict[str, Any] = {
    "key": "local-dreamina-mcp-image",
    "label": "Dreamina 即梦 · MCP 生图",
    "apiModel": "dreamina-mcp-image",
    "requestMode": "dreamina_mcp",
    "vendorGroup": "Local Dreamina MCP",
    "params": {
        "size": ["2k", "1k"],
        "aspectRatio": ["1:1", "3:4", "4:3", "16:9", "9:16", "3:2", "2:3", "21:9"],
        "supportsInputReference": True,
        "supportsMultipleInputReferences": True,
    },
    "extraBodyParams": [],
}

LOCAL_DREAMINA_VIDEO_MODEL: dict[str, Any] = {
    "key": "local-dreamina-mcp-video",
    "label": "Dreamina 即梦 · MCP 视频",
    "apiModel": "dreamina-mcp-video",
    "requestMode": "dreamina_mcp",
    "vendorGroup": "Local Dreamina MCP",
    "params": {
        "seconds": {"min": 4, "max": 15, "default": 5},
        "size": ["720p", "1080p"],
        "aspectRatio": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "supportsInputReference": True,
    },
    "extraBodyParams": [],
}

AWESOME_GPT_IMAGE_2_SOURCE = "https://github.com/freestylefly/awesome-gpt-image-2"
YOUMIND_GPT_IMAGE_2_SOURCE = "https://github.com/YouMind-OpenLab/awesome-gpt-image-2"
PROMPT_TAXONOMY_SOURCES = (AWESOME_GPT_IMAGE_2_SOURCE, YOUMIND_GPT_IMAGE_2_SOURCE)
YOUMIND_LOCAL_CORPUS = ROOT / "data" / "youmind_gpt_image2_prompts.json"
YOUMIND_EXAMPLE_LIMIT = 3
YOUMIND_EXAMPLE_EXCERPT_CHARS = 700
MAX_VIDEO_BRIEF_CHARS = 4000
MAX_VIDEO_PROMPT_CHARS = 5200
VIDEO_PROMPT_PACK_SOURCES = (
    "https://github.com/dexhunter/seedance2-skill",
    "https://github.com/ZeroLu/awesome-seedance",
    "https://github.com/YouMind-OpenLab/awesome-seedance-2-prompts",
    "https://github.com/aiskillstore/marketplace/tree/main/skills/op7418/seedance-prompt",
)
VIDEO_PROMPT_PACK: tuple[dict[str, Any], ...] = (
    {
        "id": "product-commercial",
        "title": "产品英雄叙事",
        "summary": "围绕产品识别、材质卖点和使用价值组织镜头，形成清楚的广告记忆点。",
        "keywords": (
            "产品",
            "品牌",
            "汽车",
            "车辆",
            "新能源",
            "包装",
            "功能",
            "广告",
            "发布",
        ),
        "use_cases": ("品牌短片", "产品展示"),
        "styles": ("commercial",),
        "camera": "以材质特写建立视觉钩子，再用低角度跟拍或受控环绕完成主体揭示。",
        "opening": "从最具辨识度的产品细节或使用动作进入，第一秒就让主体和卖点可读。",
        "middle": "围绕核心功能展开一个完整动作，镜头运动服务于结构、材质和使用价值。",
        "ending": "动作落定为干净的产品主视觉，品牌识别清楚但不额外生成广告文案。",
    },
    {
        "id": "story-continuity",
        "title": "叙事推进",
        "summary": "用事件因果、动作承接和情绪变化推动画面，建立明确的开场、发展与落点。",
        "keywords": (
            "故事",
            "剧情",
            "最后",
            "然后",
            "人物",
            "家庭",
            "短剧",
            "拜年",
            "相遇",
        ),
        "use_cases": ("故事镜头", "品牌短片"),
        "styles": ("cinematic",),
        "camera": "先交代主体与环境关系，再跟随关键动作推进，收束时给出清楚情绪落点。",
        "opening": "建立人物、主体与环境关系，让触发事件在开场立即发生。",
        "middle": "沿同一运动方向跟随核心动作，利用前后景变化交代事件因果。",
        "ending": "完成用户指定的最终动作并稳定停留，让情绪和叙事都有明确落点。",
    },
    {
        "id": "character-continuity",
        "title": "角色连续表演",
        "summary": "优先保护人物身份、表情、视线与肢体连续性，用微动作而不是频繁切镜表达情绪。",
        "keywords": ("人物", "角色", "人像", "表情", "对话", "口播", "服装", "舞蹈"),
        "use_cases": ("动态人像", "故事镜头"),
        "styles": ("portrait",),
        "camera": "以中近景和面部特写捕捉微表情，运镜缓慢，动作必须有起点与落点。",
        "opening": "先锁定人物身份、服装、姿态和视线，建立可持续的表演状态。",
        "middle": "通过一个连续动作和细微表情变化推进情绪，避免肢体遮挡和身份漂移。",
        "ending": "让动作自然完成并保留短暂停顿，以眼神或姿态形成情绪余韵。",
    },
    {
        "id": "single-take-space",
        "title": "空间一镜到底",
        "summary": "用遮挡、前后景和路径变化完成连续空间叙事，全程保持运动方向与物理关系。",
        "keywords": ("一镜到底", "连续镜头", "跟拍", "穿过", "走进", "空间", "转场"),
        "use_cases": ("故事镜头", "社媒视频"),
        "styles": ("cinematic", "documentary"),
        "camera": "采用连续跟拍，利用自然遮挡和景别变化过渡，不切镜、不瞬移、不改变轴线。",
        "opening": "镜头从可读的空间位置出发，立即锁定主体与前进方向。",
        "middle": "跟随主体穿过空间，通过遮挡、转身或前后景完成自然视角变化。",
        "ending": "在目标位置减速并稳定构图，让主体动作完整结束。",
    },
    {
        "id": "social-ugc",
        "title": "社媒真实感",
        "summary": "使用直接、可信和轻量的拍摄语言，在短时间内建立钩子、过程和可分享的结束点。",
        "keywords": (
            "社媒",
            "小红书",
            "抖音",
            "UGC",
            "ugc",
            "手机",
            "自拍",
            "日常",
            "真实",
        ),
        "use_cases": ("社媒视频",),
        "styles": ("documentary",),
        "camera": "使用自然机位和轻微手持呼吸感，避免过度电影化，动作与环境声保持真实。",
        "opening": "从真实使用瞬间或反应切入，快速建立观看理由。",
        "middle": "保留自然动作和环境细节，让信息通过过程被看懂而不是依赖字幕说明。",
        "ending": "用一个自然反应、结果展示或回看动作结束，保持生活化可信度。",
    },
    {
        "id": "atmosphere-concept",
        "title": "氛围视觉母题",
        "summary": "提炼一个贯穿全片的光影、色彩或空间母题，用感官变化建立独立记忆点。",
        "keywords": (
            "氛围",
            "光影",
            "梦境",
            "超现实",
            "概念",
            "视觉",
            "实验",
            "意境",
            "节日",
        ),
        "use_cases": ("品牌短片", "动态人像"),
        "styles": ("surreal", "cinematic"),
        "camera": "让同一视觉母题驱动镜头和空间变化，表达大胆但不得改变主体事实。",
        "opening": "从光影、倒影、风、雾或色彩中的一个母题进入，再逐步显露主体。",
        "middle": "让母题随主体动作改变空间层次，保持运动逻辑和视觉因果。",
        "ending": "让母题与主体在最终构图中汇合，以余韵形成可识别的视觉记忆点。",
    },
)

YOUMIND_USE_CASE_TAXONOMY: tuple[dict[str, Any], ...] = (
    {
        "id": "profile-avatar",
        "name_zh": "个人资料 / 头像",
        "template_id": "character-design-sheet",
        "keywords": ("头像", "个人资料", "profile", "avatar", "selfie avatar"),
    },
    {
        "id": "social-media-post",
        "name_zh": "社交媒体帖子",
        "template_id": "poster-layout-system",
        "keywords": (
            "社交媒体",
            "社媒",
            "小红书",
            "朋友圈",
            "微博",
            "instagram",
            "social post",
        ),
    },
    {
        "id": "infographic-edu-visual",
        "name_zh": "信息图 / 教育视觉图",
        "template_id": "infographic-engine",
        "keywords": ("信息图", "教育视觉", "科普", "知识卡片", "图解", "edu visual"),
    },
    {
        "id": "youtube-thumbnail",
        "name_zh": "YouTube 缩略图",
        "template_id": "poster-layout-system",
        "keywords": ("youtube", "缩略图", "封面图", "频道封面", "thumbnail"),
    },
    {
        "id": "comic-storyboard",
        "name_zh": "漫画 / 故事板",
        "template_id": "scene-storytelling",
        "keywords": ("漫画", "故事板", "分镜", "storyboard", "comic strip"),
    },
    {
        "id": "product-marketing",
        "name_zh": "产品营销",
        "template_id": "product-commerce-visual",
        "keywords": ("产品营销", "产品广告", "卖点图", "product marketing", "campaign"),
    },
    {
        "id": "ecommerce-main-image",
        "name_zh": "电商主图",
        "template_id": "product-commerce-visual",
        "keywords": ("电商", "主图", "详情页", "商品图", "ecommerce"),
    },
    {
        "id": "game-asset",
        "name_zh": "游戏素材",
        "template_id": "concept-product-breakdown",
        "keywords": (
            "游戏素材",
            "游戏道具",
            "技能图标",
            "装备图标",
            "game asset",
            "sprite",
        ),
    },
    {
        "id": "poster-flyer",
        "name_zh": "海报 / 传单",
        "template_id": "poster-layout-system",
        "keywords": (
            "海报",
            "传单",
            "拜年图",
            "宣传图",
            "活动图",
            "主视觉",
            "banner",
            "poster",
            "flyer",
            "kv",
            "KV",
        ),
    },
    {
        "id": "app-web-design",
        "name_zh": "App / 网页设计",
        "template_id": "ui-screenshot-system",
        "keywords": (
            "app",
            "APP",
            "网页设计",
            "网站设计",
            "UI",
            "ui",
            "界面",
            "landing page",
        ),
    },
)

YOUMIND_STYLE_TAXONOMY: tuple[dict[str, Any], ...] = (
    {
        "id": "photography",
        "name_zh": "摄影",
        "keywords": ("摄影", "写实", "photo", "photography", "realistic"),
    },
    {
        "id": "cinematic-film-still",
        "name_zh": "电影 / 电影剧照",
        "keywords": ("电影感", "电影剧照", "cinematic", "film still"),
    },
    {
        "id": "anime-manga",
        "name_zh": "动漫 / 漫画",
        "keywords": ("动漫", "日漫", "二次元", "anime", "manga"),
    },
    {
        "id": "illustration",
        "name_zh": "插画",
        "keywords": ("插画", "illustration", "绘本"),
    },
    {
        "id": "sketch-line-art",
        "name_zh": "草图 / 线稿",
        "keywords": ("草图", "线稿", "素描", "sketch", "line art"),
    },
    {
        "id": "comic-graphic-novel",
        "name_zh": "漫画 / 图画小说",
        "keywords": ("漫画风", "图画小说", "graphic novel", "comic style"),
    },
    {
        "id": "3d-render",
        "name_zh": "3D 渲染",
        "keywords": ("3D", "3d", "三维", "渲染", "render"),
    },
    {
        "id": "chibi-q-style",
        "name_zh": "Q 版 / Q 萌风",
        "keywords": ("Q版", "q版", "Q萌", "q萌", "chibi"),
    },
    {
        "id": "isometric",
        "name_zh": "等距",
        "keywords": ("等距", "isometric"),
    },
    {
        "id": "pixel-art",
        "name_zh": "像素艺术",
        "keywords": ("像素", "pixel art", "pixel"),
    },
    {
        "id": "oil-painting",
        "name_zh": "油画",
        "keywords": ("油画", "oil painting"),
    },
    {
        "id": "watercolor",
        "name_zh": "水彩画",
        "keywords": ("水彩", "watercolor"),
    },
    {
        "id": "ink-chinese-style",
        "name_zh": "水墨 / 中国风",
        "keywords": ("水墨", "中国风", "国风", "ink painting", "chinese style"),
    },
    {
        "id": "retro-vintage",
        "name_zh": "复古 / 怀旧",
        "keywords": ("复古", "怀旧", "retro", "vintage"),
    },
    {
        "id": "cyberpunk-sci-fi",
        "name_zh": "赛博朋克 / 科幻",
        "keywords": ("赛博朋克", "科幻", "cyberpunk", "sci-fi", "scifi"),
    },
    {
        "id": "minimalism",
        "name_zh": "极简主义",
        "keywords": ("极简", "minimalism", "minimalist"),
    },
)

YOUMIND_SUBJECT_TAXONOMY: tuple[dict[str, Any], ...] = (
    {
        "id": "portrait-selfie",
        "name_zh": "人像 / 自拍",
        "keywords": ("人像", "自拍", "portrait", "selfie"),
    },
    {
        "id": "influencer-model",
        "name_zh": "网红 / 模特",
        "keywords": ("网红", "模特", "influencer", "model"),
    },
    {
        "id": "character",
        "name_zh": "角色",
        "keywords": ("角色", "人物", "IP形象", "ip形象", "character"),
    },
    {
        "id": "group-couple",
        "name_zh": "团体 / 情侣",
        "keywords": ("团体", "情侣", "合照", "group", "couple"),
    },
    {
        "id": "product",
        "name_zh": "产品",
        "keywords": ("产品", "商品", "product"),
    },
    {
        "id": "food-drink",
        "name_zh": "食品 / 饮料",
        "keywords": ("食品", "饮料", "餐饮", "咖啡", "奶茶", "food", "drink"),
    },
    {
        "id": "fashion-item",
        "name_zh": "时尚单品",
        "keywords": ("服装", "鞋", "包", "首饰", "fashion", "wearable"),
    },
    {
        "id": "animal-creature",
        "name_zh": "动物 / 生物",
        "keywords": ("动物", "宠物", "animal", "creature"),
    },
    {
        "id": "vehicle",
        "name_zh": "车辆",
        "keywords": (
            "车辆",
            "汽车",
            "新能源",
            "新能源车",
            "车型",
            "轿车",
            "SUV",
            "MPV",
            "vehicle",
            "car",
        ),
    },
    {
        "id": "architecture-interior",
        "name_zh": "建筑 / 室内设计",
        "keywords": ("建筑", "室内", "空间设计", "architecture", "interior"),
    },
    {
        "id": "landscape-nature",
        "name_zh": "风景 / 自然",
        "keywords": ("风景", "自然", "山水", "landscape", "nature"),
    },
    {
        "id": "cityscape-street",
        "name_zh": "城市风光 / 街道",
        "keywords": ("城市", "街道", "cityscape", "street"),
    },
    {
        "id": "diagram-chart",
        "name_zh": "图表",
        "keywords": ("图表", "diagram", "chart"),
    },
    {
        "id": "text-typography",
        "name_zh": "文本 / 排版",
        "keywords": ("文本", "排版", "字体", "typography", "type design"),
    },
    {
        "id": "abstract-background",
        "name_zh": "摘要 / 背景",
        "keywords": ("抽象", "背景", "abstract", "background"),
    },
)

GPT_IMAGE2_TEMPLATE_LIBRARY: dict[str, dict[str, Any]] = {
    "ui-screenshot-system": {
        "name_zh": "UI 截图系统",
        "category": "UI & Interfaces",
        "tags": ["UI", "Dashboard", "Screenshot"],
        "example_cases": [],
        "intent": "App、网页、仪表盘、社媒截图和产品界面",
        "rules": [
            "明确平台、比例、布局和界面层级",
            "文字必须可读，平台特征不能混搭",
            "组件状态、按钮、导航和内容区要像真实截图",
        ],
    },
    "poster-layout-system": {
        "name_zh": "海报排版系统",
        "category": "Posters & Typography",
        "tags": ["Poster", "Typography", "Campaign"],
        "example_cases": [345, 5, 10],
        "intent": "成品海报、封面、社媒 Campaign 主视觉",
        "rules": [
            "锁定主体、标题、版式、配色和比例",
            "输出单张完成图，不输出 moodboard、过程稿或多方案展示板",
            "标题和主视觉层级必须清楚",
        ],
    },
    "product-commerce-visual": {
        "name_zh": "商品商业视觉",
        "category": "Products & E-commerce",
        "tags": ["Product", "Commerce", "Packaging"],
        "example_cases": [373, 358],
        "intent": "商品主图、产品广告、卖点图和详情页主视觉",
        "rules": [
            "区分主商品、卖点标签、辅助道具和背景场景",
            "补齐材质、光影、镜头角度和商业摄影质感",
            "避免无关道具削弱商品识别",
        ],
    },
    "brand-touchpoint-board": {
        "name_zh": "品牌触点视觉板",
        "category": "Brand & Logos",
        "tags": ["Brand", "Identity", "Campaign"],
        "example_cases": [362],
        "intent": "品牌 Campaign、品牌触点和应用系统",
        "rules": [
            "让配色、字体、图形语言和触点样机统一",
            "品牌元素服务主体，不随机发明官方物料或法律标识",
            "减少触点数量，保持可读性和落地感",
        ],
    },
    "brand-identity-package": {
        "name_zh": "品牌身份包",
        "category": "Brand & Logos",
        "tags": ["Brand", "Logo", "Identity"],
        "example_cases": [354],
        "intent": "Logo、VI、品牌识别系统",
        "rules": [
            "先定义品牌关键词、目标受众、行业和情绪目标",
            "保持品牌文字准确，避免无关 Logo 变体",
            "展示应用场景以检查缩小后的可读性",
        ],
    },
    "character-design-sheet": {
        "name_zh": "角色设定表",
        "category": "Characters & People",
        "tags": ["Character", "Pose", "Style"],
        "example_cases": [347],
        "intent": "人物、角色设定、动作一致性",
        "rules": [
            "明确身份锚点、服装、发型、比例和动作",
            "角色作为辅助时不能抢走产品主体",
            "避免不同姿态里服装和面部特征漂移",
        ],
    },
    "scene-storytelling": {
        "name_zh": "场景叙事",
        "category": "Scenes & Storytelling",
        "tags": ["Scene", "Story", "Storyboard"],
        "example_cases": [330],
        "intent": "故事场景、分镜、情绪叙事",
        "rules": [
            "写清人物、地点、时间、事件、冲突和机位",
            "场景细节服务叙事，不做通用背景装饰",
            "画面里必须能看出正在发生的动作",
        ],
    },
    "history-classical-themes": {
        "name_zh": "历史与古风题材",
        "category": "History & Classical Themes",
        "tags": ["History", "Classical", "Scroll"],
        "example_cases": [375, 338],
        "intent": "古风、传统文化、朝代服饰、长卷或节庆题材",
        "rules": [
            "明确传统元素、服饰、器物、建筑和文化气质",
            "古风元素要服务主题，不混入随机现代物件",
            "需要准确时避免朝代和服饰系统混搭",
        ],
    },
    "infographic-engine": {
        "name_zh": "信息图引擎",
        "category": "Charts & Infographics",
        "tags": ["Infographic", "Chart", "Education"],
        "example_cases": [],
        "intent": "信息图、知识图谱、结构化图解",
        "rules": [
            "控制模块数量和信息流方向",
            "每个模块只放短标题和短说明",
            "避免长段正文和无结构装饰",
        ],
    },
    "concept-product-breakdown": {
        "name_zh": "概念拆解与素材设计",
        "category": "Assets & Technical Visuals",
        "tags": ["Asset", "Concept", "Breakdown"],
        "example_cases": [],
        "intent": "游戏素材、图标、道具、技术拆解和可复用视觉资产",
        "rules": [
            "明确素材用途、视角、轮廓、材质和交付形态",
            "需要拆解或标注时控制模块数量，文字必须短且可读",
            "让主体边界干净，背景不能抢走资产识别度",
        ],
    },
}

MINIMAL_VIDEO_FALLBACK: list[dict[str, Any]] = [
    {
        "key": "happyhorse-1.1-t2v",
        "label": "Happyhorse 1.1",
        "enabled": True,
        "apiModel": "happyhorse-1.1-t2v",
        "apiModelI2V": "happyhorse-1.1-i2v",
        "vendorGroup": "Alibaba",
        "params": {
            "seconds": {"min": 3, "max": 12, "default": 5},
            "supportsInputReference": True,
        },
        "extraBodyParams": [],
    },
    {
        "key": "sora-2",
        "label": "Sora 2",
        "enabled": True,
        "apiModel": "sora-2",
        "apiModelI2V": None,
        "vendorGroup": "OpenAI",
        "params": {
            "seconds": [4, 8, 12],
            "size": ["720x1280", "1280x720", "1024x1792", "1792x1024"],
            "supportsInputReference": True,
            "supportedFrameImages": ["first_frame"],
        },
        "extraBodyParams": [],
    },
]

MINIMAL_IMAGE_FALLBACK: list[dict[str, Any]] = [
    {
        "key": "gpt-image-2",
        "label": "GPT Image 2",
        "apiModel": "gpt-image-2",
        "apiModelI2V": "openai/gpt-image-2",
        "requestMode": "images",
        "vendorGroup": "OpenAI",
        "params": {
            "n": [1, 2, 3, 4],
            "size": ["auto", "1024x1024", "1536x1024", "1024x1536"],
            "quality": ["auto", "low", "medium", "high"],
            "supportsInputReference": True,
        },
        "extraBodyParams": [],
    },
    {
        "key": "nano-banana-pro",
        "label": "Nano Banana Pro",
        "apiModel": "gemini-3-pro-image-preview",
        "requestMode": "gemini",
        "vendorGroup": "Google Gemini",
        "params": {
            "size": ["1K", "2K", "4K"],
            "aspectRatio": ["1:1", "2:3", "3:2", "9:16", "16:9"],
            "googleSearch": True,
            "supportsInputReference": True,
            "supportsMultipleInputReferences": True,
        },
        "extraBodyParams": [],
    },
]


def load_literal_from_plugin(
    name: str, fallback: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Load fallback model literals from the AstrBot draft without importing it."""
    try:
        tree = ast.parse(PLUGIN_DRAFT.read_text(encoding="utf-8"))
    except OSError:
        return fallback
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                value = ast.literal_eval(node.value)
                return value if isinstance(value, list) else fallback
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = ast.literal_eval(node.value)
                    return value if isinstance(value, list) else fallback
    return fallback


FALLBACK_MODELS = load_literal_from_plugin("FALLBACK_MODELS", MINIMAL_VIDEO_FALLBACK)
FALLBACK_IMAGE_MODELS = load_literal_from_plugin(
    "FALLBACK_IMAGE_MODELS",
    MINIMAL_IMAGE_FALLBACK,
)


def with_local_video_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place local video-capable models before remote AIHUBMIX video models."""
    local_keys = {LOCAL_DREAMINA_VIDEO_MODEL["key"]}
    result = [LOCAL_DREAMINA_VIDEO_MODEL]
    result.extend(model for model in models if model.get("key") not in local_keys)
    return result


def with_local_image_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Place local image-capable models before remote AIHUBMIX image models."""
    local_keys = {LOCAL_CODEX_IMAGE_MODEL["key"], LOCAL_DREAMINA_IMAGE_MODEL["key"]}
    result = [LOCAL_CODEX_IMAGE_MODEL, LOCAL_DREAMINA_IMAGE_MODEL]
    result.extend(model for model in models if model.get("key") not in local_keys)
    return result


def clean_prompt_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


class AihubmixBackend:
    def __init__(self) -> None:
        self.api_key = os.environ.get("AIHUBMIX_API_KEY", "").strip()
        self.api_base = (
            os.environ.get("AIHUBMIX_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")
        )
        self.gemini_base = (
            os.environ.get("AIHUBMIX_GEMINI_BASE", DEFAULT_GEMINI_BASE)
            .strip()
            .rstrip("/")
        )
        self.models_url = os.environ.get(
            "AIHUBMIX_MODELS_URL", DEFAULT_MODELS_URL
        ).strip()
        self.image_models_url = os.environ.get(
            "AIHUBMIX_IMAGE_MODELS_URL",
            DEFAULT_IMAGE_MODELS_URL,
        ).strip()
        self.poll_interval_seconds = int(
            os.environ.get(
                "AIHUBMIX_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
            )
        )
        self._models_cache: dict[str, Any] | None = None
        self._models_cache_at = 0.0
        self._image_models_cache: dict[str, Any] | None = None
        self._image_models_cache_at = 0.0
        self._youmind_corpus_cache: dict[str, Any] | None = None
        self._task_keys: dict[str, str] = {}

    def health(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "has_configured_key": bool(self.api_key),
            "has_codex_oauth": bool(self._read_codex_access_token()),
            "has_dreamina_mcp": self._has_dreamina_mcp(),
            "features": [
                "video",
                "image",
                "writing",
                "multi-model-writing",
                "codex-oauth-image",
                "dreamina-mcp",
            ],
            "mode": "standalone",
            "youmind_prompt_corpus_count": self._youmind_corpus_count(),
        }

    def models(self) -> dict[str, Any]:
        now = time.time()
        if self._models_cache and now - self._models_cache_at < MODELS_CACHE_SECONDS:
            return self._models_cache
        try:
            payload = self._get_json(self.models_url)
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                raise ValueError("models response missing models list")
            data = {
                "models": with_local_video_models(models),
                "source": "remote",
                "poll_interval_seconds": self.poll_interval_seconds,
                "has_configured_key": bool(self.api_key),
                "has_dreamina_mcp": self._has_dreamina_mcp(),
            }
        except Exception as exc:  # noqa: BLE001
            data = {
                "models": with_local_video_models(FALLBACK_MODELS),
                "source": "fallback",
                "message": str(exc),
                "poll_interval_seconds": self.poll_interval_seconds,
                "has_configured_key": bool(self.api_key),
                "has_dreamina_mcp": self._has_dreamina_mcp(),
            }
        self._models_cache = data
        self._models_cache_at = now
        return data

    def image_models(self) -> dict[str, Any]:
        now = time.time()
        if (
            self._image_models_cache
            and now - self._image_models_cache_at < MODELS_CACHE_SECONDS
        ):
            return self._image_models_cache
        try:
            payload = self._get_json(self.image_models_url)
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                raise ValueError("image models response missing models list")
            data = {
                "models": with_local_image_models(models),
                "source": "remote",
                "has_configured_key": bool(self.api_key),
                "has_codex_oauth": bool(self._read_codex_access_token()),
                "has_dreamina_mcp": self._has_dreamina_mcp(),
            }
        except Exception as exc:  # noqa: BLE001
            data = {
                "models": with_local_image_models(FALLBACK_IMAGE_MODELS),
                "source": "fallback",
                "message": str(exc),
                "has_configured_key": bool(self.api_key),
                "has_codex_oauth": bool(self._read_codex_access_token()),
                "has_dreamina_mcp": self._has_dreamina_mcp(),
            }
        self._image_models_cache = data
        self._image_models_cache_at = now
        return data

    def generate_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_mode = str(payload.get("request_mode") or "").strip().lower()
        if request_mode == "dreamina_mcp":
            payload = self._with_optimized_dreamina_video_prompt(payload)
            return self._generate_dreamina_video(payload)

        api_key = self._resolve_api_key(payload.get("api_key"))
        if not api_key:
            raise UserFacingError("缺少 AIHUBMIX API Key", {"needs_api_key": True})

        body = self._build_video_create_body(payload)
        if not body.get("model"):
            raise UserFacingError("缺少 model")
        if not body.get("prompt"):
            raise UserFacingError("缺少 prompt")

        result = self._post_json(f"{self.api_base}/videos", body, api_key)
        video_id = self._extract_video_id(result)
        if video_id:
            self._task_keys[video_id] = api_key
        return {
            "video_id": video_id,
            "request": self._redact_request(body),
            "response": result,
        }

    def generate_image(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_mode = str(payload.get("request_mode") or "images").strip().lower()
        api_model = str(payload.get("api_model") or payload.get("model") or "").strip()
        if not api_model:
            raise UserFacingError("缺少 model")
        if not str(payload.get("prompt") or "").strip():
            raise UserFacingError("缺少 prompt")

        payload = self._with_optimized_image_prompt(payload, request_mode, api_model)

        if request_mode == "codex":
            return self._generate_codex_image(payload, api_model)
        if request_mode == "dreamina_mcp":
            return self._generate_dreamina_image(payload, api_model)

        api_key = self._resolve_api_key(payload.get("api_key"))
        if not api_key:
            raise UserFacingError("缺少 AIHUBMIX API Key", {"needs_api_key": True})

        if request_mode == "gemini" or "gemini" in api_model:
            body = self._build_gemini_image_body(payload)
            url = f"{self.gemini_base}/models/{url_quote(api_model)}:generateContent"
            result = self._post_json(url, body, api_key, "x-goog-api-key")
        else:
            body = self._build_openai_image_body(payload, api_model)
            url = str(payload.get("base_url") or "").strip()
            if not url:
                url = f"{self.api_base}/images/generations"
            result = self._post_json(url, body, api_key)

        return {
            "request_mode": request_mode,
            "prompt_original": payload.get("_prompt_original") or payload.get("prompt"),
            "prompt_optimized": payload.get("_prompt_optimized")
            or payload.get("prompt"),
            "template": payload.get("_prompt_template"),
            "template_name": payload.get("_prompt_template_name"),
            "supporting_templates": payload.get("_prompt_supporting_templates") or [],
            "template_source": payload.get("_prompt_template_source"),
            "taxonomy_sources": payload.get("_prompt_taxonomy_sources") or [],
            "taxonomy_tags": payload.get("_prompt_taxonomy_tags") or [],
            "local_examples": payload.get("_prompt_local_examples") or [],
            "request": self._redact_request(body),
            "response": result,
            "images": self._extract_image_outputs(result),
        }

    def optimize_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_mode = str(payload.get("request_mode") or "codex").strip().lower()
        api_model = str(
            payload.get("api_model") or payload.get("model") or CODEX_IMAGE_MODEL
        ).strip()
        if not str(payload.get("prompt") or "").strip():
            raise UserFacingError("缺少 prompt")
        if request_mode == "dreamina_mcp" and self._is_video_prompt_payload(
            payload, api_model
        ):
            optimized = self._with_optimized_dreamina_video_prompt(payload)
        else:
            optimized = self._with_optimized_image_prompt(
                payload, request_mode, api_model
            )
        return {
            "prompt_original": optimized.get("_prompt_original")
            or payload.get("prompt"),
            "prompt_optimized": optimized.get("prompt"),
            "optimized": bool(optimized.get("_prompt_optimized")),
            "template": optimized.get("_prompt_template"),
            "template_name": optimized.get("_prompt_template_name"),
            "supporting_templates": optimized.get("_prompt_supporting_templates") or [],
            "template_source": optimized.get("_prompt_template_source"),
            "taxonomy_sources": optimized.get("_prompt_taxonomy_sources") or [],
            "taxonomy_tags": optimized.get("_prompt_taxonomy_tags") or [],
            "local_examples": optimized.get("_prompt_local_examples") or [],
            "request_mode": request_mode,
            "api_model": api_model,
        }

    def generate_writing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Generate comparable drafts with the selected writing models.

        Args:
            payload: Writing brief, document controls, selected model IDs, and an
                optional temporary AIHUBMIX API key.

        Returns:
            Ordered per-model results with independent success or error states.

        Raises:
            UserFacingError: If the brief or selected model list is invalid.
        """
        brief = str(payload.get("brief") or "").strip()
        if not brief:
            raise UserFacingError("请先输入写作主题")
        if len(brief) > MAX_WRITING_BRIEF_CHARS:
            raise UserFacingError(f"写作主题不能超过 {MAX_WRITING_BRIEF_CHARS} 字")

        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raw_models = []
        selected_models = list(
            dict.fromkeys(
                str(model_id).strip()
                for model_id in raw_models
                if str(model_id).strip() in WRITING_MODELS
            )
        )
        if not selected_models:
            raise UserFacingError("请至少选择一个写作模型")
        if len(selected_models) > len(WRITING_MODELS):
            raise UserFacingError("选择的写作模型过多")

        target_length = self._safe_int(
            payload.get("target_length"), default=1500, minimum=200, maximum=5000
        )
        base_context = {
            "brief": brief,
            "article_type": self._compact_text(
                str(payload.get("article_type") or "自由写作"), 40
            ),
            "tone": self._compact_text(str(payload.get("tone") or "专业清晰"), 40),
            "target_length": target_length,
        }
        api_key = self._resolve_api_key(payload.get("api_key"))
        results_by_id: dict[str, dict[str, Any]] = {}
        runnable: list[tuple[str, dict[str, Any]]] = []
        for model_id in selected_models:
            model = WRITING_MODELS[model_id]
            if model["provider"] == "aihubmix" and not api_key:
                results_by_id[model_id] = {
                    "model_id": model_id,
                    "label": model["label"],
                    "provider": model["provider"],
                    "status": "error",
                    "elapsed_ms": 0,
                    "error": "需要临时 AIHUBMIX API Key",
                }
                continue
            runnable.append(
                (
                    model_id,
                    {
                        **base_context,
                        "model_id": model_id,
                        "model_label": model["label"],
                    },
                )
            )

        if runnable:
            with ThreadPoolExecutor(max_workers=len(runnable)) as executor:
                futures = {}
                for model_id, context in runnable:
                    started = time.monotonic()
                    if WRITING_MODELS[model_id]["provider"] == "codex-oauth":
                        future = executor.submit(self._generate_codex_writing, context)
                    else:
                        future = executor.submit(
                            self._generate_aihubmix_writing, context, str(api_key)
                        )
                    futures[future] = (model_id, started)

                for future in as_completed(futures):
                    model_id, started = futures[future]
                    model = WRITING_MODELS[model_id]
                    elapsed_ms = round((time.monotonic() - started) * 1000)
                    try:
                        draft = future.result()
                        results_by_id[model_id] = {
                            "model_id": model_id,
                            "label": model["label"],
                            "provider": model["provider"],
                            "status": "success",
                            "elapsed_ms": elapsed_ms,
                            "title": draft["title"],
                            "content": draft["content"],
                        }
                    except Exception as exc:  # noqa: BLE001
                        results_by_id[model_id] = {
                            "model_id": model_id,
                            "label": model["label"],
                            "provider": model["provider"],
                            "status": "error",
                            "elapsed_ms": elapsed_ms,
                            "error": self._compact_text(str(exc), 300),
                        }

        results = [results_by_id[model_id] for model_id in selected_models]
        success_count = sum(item["status"] == "success" for item in results)
        return {
            "results": results,
            "success_count": success_count,
            "failure_count": len(results) - success_count,
        }

    def _generate_codex_writing(self, context: dict[str, Any]) -> dict[str, str]:
        """Generate one draft through the local Codex OAuth session.

        Args:
            context: Normalized writing controls and the Codex model ID.

        Returns:
            A normalized title and Markdown body.

        Raises:
            RuntimeError: If Codex OAuth, the SDK, or the model response fails.
        """
        token = self._read_codex_access_token()
        if not token:
            raise RuntimeError("未找到 Codex OAuth 登录")
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError("当前 Python 环境未安装 openai SDK") from exc

        instructions = self._writing_instructions(context)
        try:
            client = openai.OpenAI(
                api_key=token,
                base_url=CODEX_BASE_URL,
                default_headers=self._codex_cloudflare_headers(token),
                timeout=180.0,
            )
            chunks: list[str] = []
            with client.responses.stream(
                model=WRITING_MODELS[context["model_id"]]["api_model"],
                store=False,
                instructions=instructions,
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(context, ensure_ascii=False),
                            }
                        ],
                    }
                ],
            ) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if isinstance(delta, str):
                            chunks.append(delta)
                final_response = stream.get_final_response()
                raw_output = ("".join(chunks) or final_response.output_text).strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"GPT-5.6 Sol 写作失败：{exc}") from exc
        return self._parse_writing_response(raw_output, context["brief"])

    def _generate_aihubmix_writing(
        self, context: dict[str, Any], api_key: str
    ) -> dict[str, str]:
        """Generate one draft through AIHUBMIX's OpenAI-compatible chat API.

        Args:
            context: Normalized writing controls and requested AIHUBMIX model ID.
            api_key: Temporary or environment-provided AIHUBMIX credential.

        Returns:
            A normalized title and Markdown body.

        Raises:
            RuntimeError: If AIHUBMIX returns an empty or malformed response.
        """
        output_limit = min(12000, max(2048, int(context["target_length"]) * 2))
        body = {
            "model": context["model_id"],
            "messages": [
                {"role": "system", "content": self._writing_instructions(context)},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False),
                },
            ],
        }
        if str(context["model_id"]).startswith("gemini-"):
            body["max_output_tokens"] = output_limit
        else:
            body["max_tokens"] = output_limit
        response = self._post_json(f"{self.api_base}/chat/completions", body, api_key)
        choices = response.get("choices") if isinstance(response, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("AIHUBMIX 没有返回写作内容")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("AIHUBMIX 返回的写作内容为空")
        return self._parse_writing_response(content, context["brief"])

    def _writing_instructions(self, context: dict[str, Any]) -> str:
        """Build the shared fair-comparison writing instruction.

        Args:
            context: Normalized writing controls.

        Returns:
            A provider-neutral Chinese writing instruction.
        """
        return (
            "你是巅池 Agent 工具箱的中文写作助手。严格依据用户主题写出可直接使用的完整成稿，"
            f"类型为{context['article_type']}，语气为{context['tone']}，目标约"
            f"{context['target_length']}个汉字。不要输出提纲、写作说明、自我评价或模型名称。"
            "事实不足时保持克制，不编造数据、引语和来源。正文使用 Markdown，二级标题以 ## 开头。"
            "只返回 JSON，不要代码围栏。格式必须是"
            '{"title":"文章标题","content":"完整 Markdown 正文"}。'
        )

    def _parse_writing_response(self, raw: str, brief: str) -> dict[str, str]:
        """Normalize JSON or plain-text model output into a writing draft.

        Args:
            raw: Raw provider output.
            brief: Original brief used to derive a fallback title.

        Returns:
            A title and Markdown body.

        Raises:
            RuntimeError: If no usable body can be recovered.
        """
        text = str(raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
        candidate = fenced.group(1) if fenced else text
        if not fenced and "{" in text and "}" in text:
            candidate = text[text.find("{") : text.rfind("}") + 1]
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            title = str(decoded.get("title") or "").strip()
            content = str(decoded.get("content") or decoded.get("body") or "").strip()
        else:
            lines = [line.rstrip() for line in text.splitlines()]
            first_index = next(
                (index for index, line in enumerate(lines) if line.strip()), 0
            )
            first_line = lines[first_index].strip().lstrip("#").strip()
            if first_line and len(first_line) <= 80:
                title = first_line
                content = "\n".join(lines[first_index + 1 :]).strip()
            else:
                title = ""
                content = text
        if not title:
            title = re.split(r"[。！？\n]", brief, maxsplit=1)[0].strip()[:36]
        if not content:
            raise RuntimeError("模型没有返回可用正文")
        return {"title": title or "未命名文稿", "content": content}

    def prompt_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Route one creative brief to the matching professional compiler.

        Args:
            payload: Creative task type, original brief, materials, and output
                controls supplied by the NAS workbench.

        Returns:
            Three production-ready prompt candidates and compiler provenance.

        Raises:
            UserFacingError: If the creative task type is unsupported.
        """
        task_type = str(payload.get("task_type") or "").strip().lower()
        if task_type == "copy":
            result = self.writing_prompt_candidates(payload)
        elif task_type == "image":
            result = self.image_prompt_candidates(payload)
        elif task_type == "video":
            result = self.video_prompt_candidates(payload)
        else:
            raise UserFacingError("通用提词器仅支持文案、图片和视频")
        raw_memory = payload.get("company_memory")
        result["company_memory_sources"] = [
            {
                "title": str(item.get("title") or "公司历史资料")[:120],
                "source_path": str(item.get("source_path") or "")[:400],
                "source_status": str(item.get("source_status") or "待复核")[:40],
            }
            for item in (raw_memory if isinstance(raw_memory, list) else [])[:3]
            if isinstance(item, dict)
        ]
        return result

    def _company_memory_prompt_block(self, payload: dict[str, Any]) -> str:
        """Format bounded Obsidian evidence with explicit trust boundaries.

        Args:
            payload: Creative payload containing optional company-memory rows.

        Returns:
            A source-attributed prompt block, or an empty string when unmatched.
        """
        raw_memory = payload.get("company_memory")
        if not isinstance(raw_memory, list):
            return ""
        lines = [
            "公司 Obsidian 原文案参考：",
            (
                "已复核资料可用于相关公司事实、术语和历史表达；待复核资料只能用于风格、"
                "结构和创意启发，不能写成已经确认的公司事实。不得复制无关段落，也不要把内部来源路径写进最终成稿。"
            ),
        ]
        for index, item in enumerate(raw_memory[:3], start=1):
            if not isinstance(item, dict):
                continue
            title = self._compact_text(str(item.get("title") or "公司历史资料"), 120)
            source_path = self._compact_text(str(item.get("source_path") or ""), 300)
            doc_type = self._compact_text(str(item.get("doc_type") or "资料文档"), 40)
            excerpt = self._compact_text(str(item.get("excerpt") or ""), 800)
            source_status = self._compact_text(
                str(item.get("source_status") or "待复核"), 40
            )
            usage = (
                "可用于相关事实与表达"
                if str(item.get("usage_policy") or "") == "facts_and_style"
                else "仅作风格与结构参考"
            )
            if excerpt:
                lines.append(
                    f"{index}. 标题={title}；类型={doc_type}；状态={source_status}；"
                    f"使用边界={usage}；来源={source_path or '未标注'}；原文摘录={excerpt}"
                )
        return "\n".join(lines) if len(lines) > 2 else ""

    def writing_prompt_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compile a copy brief into three distinct professional instructions.

        Args:
            payload: Original brief, copy type, audience, source material, and
                delivery requirements.

        Returns:
            Three structured prompts that can be adopted before generation.

        Raises:
            UserFacingError: If the brief is empty or too long.
        """
        source = str(payload.get("prompt") or payload.get("brief") or "").strip()
        if not source:
            raise UserFacingError("请先输入文案主题或原始要求")
        if len(source) > MAX_WRITING_BRIEF_CHARS:
            raise UserFacingError(
                f"原始要求超过 {MAX_WRITING_BRIEF_CHARS} 字，请精简后再生成候选"
            )

        copy_type = self._compact_text(
            str(payload.get("copy_type") or payload.get("article_type") or "通用文案"),
            80,
        )
        audience = self._compact_text(
            str(payload.get("audience") or "按主题识别主要受众与使用场景"), 160
        )
        material = str(payload.get("source_material") or "").strip()
        requirement = self._compact_text(
            str(payload.get("output_requirement") or "结构完整，可直接发布或交付"),
            240,
        )
        material_rule = (
            "只把下列资料作为事实依据；不得补造其中没有的数据、引语、案例、"
            f"承诺或来源：\n{material}"
            if material
            else "未提供事实资料；不得编造数据、引语、客户案例、政策、奖项或来源。"
        )
        company_memory_block = self._company_memory_prompt_block(payload)
        directions = (
            {
                "id": "copy-direct-delivery",
                "title": "直接成稿",
                "summary": "以清晰结论和完整结构优先，生成可直接发布的稳定版本。",
                "strategy": (
                    "先提炼一句核心观点，再按读者理解顺序组织标题、开场、主体和收束。"
                    "每一段只承担一个信息任务，删除套话、空泛口号和重复表达。"
                ),
                "output": "直接输出最终标题与完整正文，不输出思考过程、提纲说明或自我评价。",
            },
            {
                "id": "copy-audience-resonance",
                "title": "受众共鸣",
                "summary": "从目标读者的处境、疑问与行动阻力切入，增强可读性和说服力。",
                "strategy": (
                    "先判断受众最关心的问题、已有认知和可能反对点，用具体场景建立开场钩子；"
                    "再用事实、解释和行动建议完成说服，语气自然，不制造焦虑。"
                ),
                "output": "输出一个主标题、必要的小标题和完整正文；结尾给出自然且不过度营销的行动落点。",
            },
            {
                "id": "copy-structure-strategy",
                "title": "结构策划",
                "summary": "先校验事实边界和内容逻辑，再以最适合该渠道的结构完成成稿。",
                "strategy": (
                    "在内部先完成事实清单、信息优先级和结构选择；区分已知事实、合理表达和不可写内容。"
                    "采用总分总、问题—分析—方案或故事—洞察—行动中最匹配的一种，避免模板痕迹。"
                ),
                "output": "只交付经过结构化处理的最终成稿，并确保标题、正文层级和篇幅符合要求。",
            },
        )
        candidates = []
        for direction in directions:
            prompt = "\n".join(
                (
                    f"创作方向：{direction['title']}",
                    f"原始要求（最高优先级，完整保留）：{source}",
                    f"任务类型：{copy_type}",
                    f"目标受众 / 渠道：{audience}",
                    f"交付要求：{requirement}",
                    f"事实与素材边界：{material_rule}",
                    company_memory_block,
                    f"写作策略：{direction['strategy']}",
                    (
                        "质量标准：观点明确，信息密度高，语言像真实编辑完成的中文；"
                        "不编造、不抄袭、不堆砌形容词，不出现模型身份或提示词说明。"
                    ),
                    f"输出契约：{direction['output']}",
                )
            )
            candidates.append(
                {
                    "id": direction["id"],
                    "title": direction["title"],
                    "summary": direction["summary"],
                    "prompt": prompt,
                }
            )
        return {
            "source": source,
            "provider": "dianchi-writing-prompt-pack",
            "model_target": "通用中文写作模型结构化指令",
            "framework": "writing-editorial-compiler-v1",
            "prompt_pack": [item["id"] for item in directions],
            "prompt_pack_sources": [],
            "candidates": candidates,
        }

    def image_prompt_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compile an image brief through the existing GPT Image prompt stack.

        Args:
            payload: Original image brief, target model, reference state, and
                output controls.

        Returns:
            Three distinct model-ready prompts with taxonomy provenance.

        Raises:
            UserFacingError: If the brief is empty or too long.
        """
        source = str(payload.get("prompt") or payload.get("brief") or "").strip()
        if not source:
            raise UserFacingError("请先输入画面想法或原始要求")
        if len(source) > MAX_WRITING_BRIEF_CHARS:
            raise UserFacingError(
                f"原始要求超过 {MAX_WRITING_BRIEF_CHARS} 字，请精简后再生成候选"
            )

        request_mode = str(payload.get("request_mode") or "codex").strip().lower()
        api_model = str(
            payload.get("api_model") or payload.get("model") or CODEX_IMAGE_MODEL
        ).strip()
        company_memory_block = self._company_memory_prompt_block(payload)
        directions = (
            {
                "id": "image-faithful-commercial",
                "title": "忠实商业成图",
                "summary": "优先锁定主体、事实与品牌识别，追求可直接交付的商业完成度。",
                "instruction": (
                    "以原始要求中的主体和硬约束为最高优先级；建立单一视觉中心，"
                    "材质、光线、透视和比例真实可信，画面像最终成品而不是草图或灵感板。"
                ),
            },
            {
                "id": "image-narrative-atmosphere",
                "title": "叙事氛围强化",
                "summary": "通过场景关系、光影和瞬间动作增加故事感，同时不改变原始事实。",
                "instruction": (
                    "保留全部主体事实，以一个明确的事件瞬间组织前景、中景和背景；"
                    "用有方向的光线、空气感和色彩节奏建立情绪，但不得牺牲产品或人物识别。"
                ),
            },
            {
                "id": "image-layout-adaptation",
                "title": "版式交付优化",
                "summary": "围绕目标画幅和实际投放场景安排层级、留白与安全区域。",
                "instruction": (
                    "把目标画幅当作真实交付版面设计；主体、辅助元素和负空间层级清楚，"
                    "边缘保留安全区。如未明确要求文字，不生成文字、标语、价格或虚构 logo。"
                ),
            },
        )
        candidates = []
        template_ids: list[str] = []
        sources: list[str] = []
        for direction in directions:
            candidate_payload = dict(payload)
            candidate_payload["prompt"] = (
                f"{source}\n\n{company_memory_block}\n\n"
                f"候选创作策略（不得覆盖原始要求）：{direction['instruction']}"
            )
            compiled = self._with_optimized_image_prompt(
                candidate_payload, request_mode, api_model
            )
            optimized = str(compiled.get("prompt") or "").strip()
            prompt = (
                f"创作方向：{direction['title']}\n"
                f"原始要求（最高优先级，完整保留）：{source}\n\n"
                f"{company_memory_block}\n\n"
                f"专业模型指令：\n{optimized}"
            )
            template_id = str(compiled.get("_prompt_template") or "")
            if template_id and template_id not in template_ids:
                template_ids.append(template_id)
            for item in compiled.get("_prompt_taxonomy_sources") or []:
                if str(item) not in sources:
                    sources.append(str(item))
            candidates.append(
                {
                    "id": direction["id"],
                    "title": direction["title"],
                    "summary": direction["summary"],
                    "prompt": prompt,
                }
            )
        return {
            "source": source,
            "provider": "local-image-prompt-pack",
            "model_target": f"{api_model} 结构化生图指令",
            "framework": "gpt-image-taxonomy-compiler-v2",
            "prompt_pack": template_ids,
            "prompt_pack_sources": sources or list(PROMPT_TAXONOMY_SOURCES),
            "candidates": candidates,
        }

    def video_prompt_candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Compile a video brief with semantic analysis and a local Prompt Pack.

        Args:
            payload: Source brief, model settings, output settings, and creative controls.

        Returns:
            Three production-ready candidates and compiler provenance.

        Raises:
            UserFacingError: If the source brief is empty or exceeds the supported limit.
        """
        raw_source = str(
            payload.get("prompt") or payload.get("source") or payload.get("brief") or ""
        ).replace("\r\n", "\n")
        source = "\n".join(
            re.sub(r"[ \t]+", " ", line).strip() for line in raw_source.splitlines()
        ).strip()
        source = re.sub(r"\n{3,}", "\n\n", source)
        if not source:
            raise UserFacingError("请先输入视频想法或原始指令")
        if len(source) > MAX_VIDEO_BRIEF_CHARS:
            raise UserFacingError(
                f"原始指令超过 {MAX_VIDEO_BRIEF_CHARS} 字，请精简重复说明后再生成候选"
            )
        company_memory_block = self._company_memory_prompt_block(payload)

        seconds = self._safe_int(
            payload.get("seconds") or payload.get("duration"),
            default=5,
            minimum=4,
            maximum=15,
        )
        ratio = str(
            payload.get("aspect_ratio") or payload.get("ratio") or "16:9"
        ).strip()
        resolution = str(
            payload.get("resolution") or payload.get("size") or "720p"
        ).strip()
        use_case = self._compact_text(str(payload.get("use_case") or "品牌短片"), 30)
        density = str(payload.get("shot_density") or "single").strip().lower()
        if density not in {"single", "sequence", "fast"}:
            density = "single"
        style_id = str(payload.get("style") or "cinematic").strip().lower()
        request_mode = str(payload.get("request_mode") or "").strip().lower()
        api_model = str(payload.get("api_model") or payload.get("model") or "").strip()
        reference_value = payload.get("has_reference")
        has_reference = (
            reference_value is True
            or str(reference_value or "").strip().lower() in {"1", "true", "yes"}
            or bool(self._normalize_references(payload.get("input_reference")))
        )
        audio_value = payload.get("generate_audio")
        generate_audio = audio_value is True or str(
            audio_value or ""
        ).strip().lower() in {
            "1",
            "true",
            "yes",
        }
        facts = self._infer_prompt_facts(source)
        hard_constraints = [
            clause.strip()
            for clause in re.split(r"[。；;\n]", source)
            if clause.strip()
            and any(
                marker in clause
                for marker in ("必须", "不要", "禁止", "保留", "绝对", "不得")
            )
        ]

        haystack = f"{source}\n{use_case}\n{style_id}".lower()
        ranked_packs: list[tuple[int, int, dict[str, Any]]] = []
        for order, pack in enumerate(VIDEO_PROMPT_PACK):
            score = sum(
                3 for keyword in pack["keywords"] if str(keyword).lower() in haystack
            )
            if use_case in pack["use_cases"]:
                score += 4
            if style_id in pack["styles"]:
                score += 3
            if facts.get("is_automotive") and pack["id"] == "product-commercial":
                score += 5
            if facts.get("is_character") and pack["id"] == "character-continuity":
                score += 5
            if density == "single" and pack["id"] == "single-take-space":
                score += 7
            ranked_packs.append((score, -order, pack))
        ranked_packs.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected_packs = [item[2] for item in ranked_packs[:3]]

        is_seedance = (
            request_mode == "dreamina_mcp"
            or "dreamina" in api_model.lower()
            or "seedance" in api_model.lower()
        )
        if is_seedance:
            version = (
                "Seedance 2.0 VIP"
                if resolution.lower() == "1080p"
                else "Seedance 2.0 Fast"
            )
            model_target = f"即梦 {version} 中文结构化提示词"
        else:
            model_target = f"{api_model or '通用视频模型'} 结构化提示词"

        reference_role = "主体外观、品牌标志、车身结构与主色"
        if facts.get("is_automotive"):
            reference_role = "车辆外观、品牌标志、车身结构与主色"
        elif facts.get("is_character"):
            reference_role = "人物身份、五官、服装与发型"
        elif facts.get("is_product"):
            reference_role = "产品外观、材质、包装与品牌识别"
        else:
            reference_role = "首帧构图、主体身份与整体风格"
        reference_slots = (
            [{"token": "@图片1", "role": reference_role, "mode": "first-frame"}]
            if has_reference
            else []
        )
        context = {
            "source": source,
            "seconds": seconds,
            "ratio": ratio,
            "resolution": resolution,
            "use_case": use_case,
            "shot_density": density,
            "style": style_id,
            "request_mode": request_mode,
            "api_model": api_model,
            "model_target": model_target,
            "has_reference": has_reference,
            "reference_slots": reference_slots,
            "generate_audio": generate_audio,
            "facts": facts,
            "hard_constraints": hard_constraints,
            "company_memory_block": company_memory_block,
            "prompt_pack": selected_packs,
        }

        prompt_engine = str(payload.get("prompt_engine") or "auto").strip().lower()
        warning = ""
        if prompt_engine != "local" and self._read_codex_access_token():
            try:
                candidates = self._generate_codex_video_prompt_candidates(context)
                if company_memory_block:
                    for candidate in candidates:
                        prompt = str(candidate.get("prompt") or "").strip()
                        if company_memory_block not in prompt:
                            prompt_limit = max(
                                1,
                                MAX_VIDEO_PROMPT_CHARS - len(company_memory_block) - 2,
                            )
                            candidate["prompt"] = (
                                self._limit_prompt_text(prompt, prompt_limit)
                                + "\n\n"
                                + company_memory_block
                            )
                return {
                    "source": source,
                    "provider": "codex-oauth",
                    "model_target": model_target,
                    "framework": "seedance-semantic-compiler-v2",
                    "prompt_pack": [pack["id"] for pack in selected_packs],
                    "prompt_pack_sources": list(VIDEO_PROMPT_PACK_SOURCES),
                    "reference_slots": reference_slots,
                    "candidates": candidates,
                }
            except Exception as exc:  # noqa: BLE001
                warning = (
                    "Codex 智能分析暂不可用，已使用本地 Prompt Pack："
                    + self._compact_text(str(exc), 160)
                )
        elif prompt_engine == "codex":
            warning = "未找到 Codex OAuth 登录，已使用本地 Prompt Pack"

        candidates = self._compile_local_video_prompt_candidates(context)
        result = {
            "source": source,
            "provider": "local-prompt-pack",
            "model_target": model_target,
            "framework": "seedance-prompt-pack-v2",
            "prompt_pack": [pack["id"] for pack in selected_packs],
            "prompt_pack_sources": list(VIDEO_PROMPT_PACK_SOURCES),
            "reference_slots": reference_slots,
            "candidates": candidates,
        }
        if warning:
            result["warning"] = warning
        return result

    def _compile_local_video_prompt_candidates(
        self, context: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Compile candidates from the selected local Prompt Pack.

        Args:
            context: Normalized brief, controls, inferred facts, and selected packs.

        Returns:
            Three structurally distinct candidate dictionaries.
        """
        source = str(context["source"])
        seconds = int(context["seconds"])
        density = str(context["shot_density"])
        facts = context["facts"]
        style_map = {
            "cinematic": "电影级光影，景深和空间层次自然，运镜克制",
            "commercial": "广告级布光，材质反射受控，卖点和主体轮廓清楚",
            "portrait": "人物表情自然，皮肤、发丝、服装和肢体稳定",
            "documentary": "自然光与轻微手持呼吸感，环境细节真实可信",
            "surreal": "视觉变化大胆但运动逻辑连续，主体事实保持不变",
        }
        style = style_map.get(str(context["style"]), style_map["cinematic"])
        if context["has_reference"]:
            role = context["reference_slots"][0]["role"]
            reference_rule = (
                f"素材引用：@图片1 作为首帧，并专门用于锁定{role}；"
                "后续镜头不得改变身份、结构、配色或关键细节。"
            )
        else:
            reference_rule = (
                "素材引用：没有参考素材，只依据文字设定；不得臆造官方标志、"
                "价格、认证或未提供的产品结构。"
            )

        subject_rule = "主体运动自然，空间方向、物理关系和光源连续。"
        if facts.get("is_automotive"):
            subject_rule = (
                "车辆比例、轮毂转动、车灯、车漆反射和行驶方向稳定，"
                "品牌与车身关键轮廓始终可辨。"
            )
        elif facts.get("is_character"):
            subject_rule = (
                "角色身份、服装、五官、肢体和视线连续，动作有明确起点与落点。"
            )
        elif facts.get("is_product"):
            subject_rule = (
                "产品外形、材质、包装细节和品牌识别保持一致，卖点通过真实动作呈现。"
            )

        audio_rule = (
            "声音设计：Music 按动作节奏递进；Sound Design 精确对应环境、"
            "接触和转场；仅在原始要求明确需要时编写 Voiceover。"
            if context["generate_audio"]
            else "声音设计：当前生成设置关闭声音，不添加 Music、Sound Design 或 Voiceover。"
        )
        common_negative = (
            "禁止：额外字幕、乱码、额外 logo、水印、UI、闪烁、跳帧、穿模、"
            "主体复制、形体突变和镜头轴线混乱；原始要求明确指定的文字或标志除外。"
        )

        candidates: list[dict[str, str]] = []
        for pack in context["prompt_pack"]:
            if density == "single":
                shot_plan = (
                    f"一镜到底，全程无剪辑。{pack['camera']}"
                    "使用连续路径和自然遮挡完成景别变化，不得瞬移或跳轴。"
                )
                phases = (
                    f"起始阶段：{pack['opening']}",
                    f"连续发展：{pack['middle']}",
                    f"收束阶段：{pack['ending']}",
                )
            else:
                density_rule = (
                    "使用起势、发展、收束的连续分镜，动作匹配剪辑，转场方向一致。"
                    if density == "sequence"
                    else "使用紧凑镜头组织，每段只承担一个信息点，切换干净且主体连续。"
                )
                shot_plan = f"{pack['camera']}{density_rule}"
                if seconds >= 10:
                    first_end = max(3, seconds // 3)
                    second_end = min(
                        seconds - 2, max(first_end + 2, (seconds * 2) // 3)
                    )
                    phases = (
                        f"[00-{first_end:02d}s] {pack['opening']}",
                        f"[{first_end:02d}-{second_end:02d}s] {pack['middle']}",
                        f"[{second_end:02d}-{seconds:02d}s] {pack['ending']}",
                    )
                else:
                    split = max(2, min(seconds - 2, seconds // 2))
                    phases = (
                        f"[00-{split:02d}s] {pack['opening']}",
                        f"[{split:02d}-{seconds:02d}s] {pack['middle']} {pack['ending']}",
                    )

            prompt = self._limit_prompt_text(
                "\n".join(
                    (
                        f"创作方向：{pack['title']}",
                        f"创意策略：{pack['summary']}",
                        f"原始要求（最高优先级，完整保留）：{source}",
                        str(context.get("company_memory_block") or ""),
                        (
                            f"模型目标：{context['model_target']}；用途：{context['use_case']}；"
                            f"输出：{context['ratio']}，{seconds} 秒，{context['resolution']}。"
                        ),
                        reference_rule,
                        f"镜头方案：{shot_plan}",
                        *phases,
                        f"视觉：{style}。",
                        f"连续性：{subject_rule}",
                        audio_rule,
                        common_negative,
                    )
                ),
                MAX_VIDEO_PROMPT_CHARS,
            )
            candidates.append(
                {
                    "id": str(pack["id"]),
                    "title": str(pack["title"]),
                    "summary": str(pack["summary"]),
                    "prompt": prompt,
                }
            )
        return candidates

    def _generate_codex_video_prompt_candidates(
        self, context: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Generate context-specific candidates through the local Codex OAuth session.

        Args:
            context: Normalized brief, output controls, references, and Prompt Pack rules.

        Returns:
            Three validated candidate dictionaries.

        Raises:
            RuntimeError: If Codex is unavailable or returns invalid candidate JSON.
        """
        token = self._read_codex_access_token()
        if not token:
            raise RuntimeError("未找到 Codex OAuth 登录")
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError("当前 Python 环境未安装 openai SDK") from exc

        instructions = (
            "你是巅池 Agent 工具箱的视频提示词编译器。"
            "根据用户原始要求和三个 Prompt Pack 方向生成三个真正不同的候选。"
            "每个候选都必须把原始场景里的具体主体、动作、环境和结尾写进镜头，"
            "禁止使用“完成原始任务”“展示主体”等空泛占位句。"
            "保留用户所有明确事实、否定词、品牌要求、台词和结尾约束，不擅自添加声明。"
            "如果上下文含公司 Obsidian 原文案，必须遵守其中的复核状态和使用边界："
            "待复核资料只能用于风格与结构启发，不能当作已确认事实。"
            "Seedance 素材必须使用 @图片1 语法并说明具体用途。"
            "单镜头模式必须一镜到底且不得使用分段时间码；10 秒以下最多两个镜头；"
            "10 秒及以上才可使用三段时间轴。三个候选的叙事目标、镜头路径、"
            "开场钩子和收束方式必须实质不同。只返回 JSON，不要 Markdown。"
            'JSON 格式为 {"candidates":[{"id":str,"title":str,'
            '"summary":str,"prompt":str}]}。'
        )
        request_context = {
            "source": context["source"],
            "output": {
                "seconds": context["seconds"],
                "ratio": context["ratio"],
                "resolution": context["resolution"],
                "model_target": context["model_target"],
            },
            "controls": {
                "use_case": context["use_case"],
                "shot_density": context["shot_density"],
                "style": context["style"],
                "generate_audio": context["generate_audio"],
            },
            "references": context["reference_slots"],
            "company_memory": context.get("company_memory_block") or "",
            "prompt_pack": [
                {
                    key: pack[key]
                    for key in (
                        "id",
                        "title",
                        "summary",
                        "camera",
                        "opening",
                        "middle",
                        "ending",
                    )
                }
                for pack in context["prompt_pack"]
            ],
        }
        try:
            client = openai.OpenAI(
                api_key=token,
                base_url=CODEX_BASE_URL,
                default_headers=self._codex_cloudflare_headers(token),
                timeout=120.0,
            )
            output_chunks: list[str] = []
            with client.responses.stream(
                model=CODEX_PROMPT_MODEL,
                store=False,
                instructions=instructions,
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(request_context, ensure_ascii=False),
                            }
                        ],
                    }
                ],
            ) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if isinstance(delta, str):
                            output_chunks.append(delta)
                final_response = stream.get_final_response()
                raw_output = (
                    "".join(output_chunks) or final_response.output_text
                ).strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Codex 提示词分析失败: {exc}") from exc

        fenced = re.search(r"\`\`\`(?:json)?\s*(\{.*\})\s*\`\`\`", raw_output, re.S)
        if fenced:
            raw_output = fenced.group(1)
        elif "{" in raw_output and "}" in raw_output:
            raw_output = raw_output[raw_output.find("{") : raw_output.rfind("}") + 1]
        try:
            decoded = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            excerpt = self._compact_text(raw_output, 240)
            raise RuntimeError(f"Codex 没有返回有效候选 JSON：{excerpt}") from exc
        raw_candidates = (
            decoded.get("candidates") if isinstance(decoded, dict) else None
        )
        if not isinstance(raw_candidates, list) or len(raw_candidates) != 3:
            raise RuntimeError("Codex 没有返回三个完整候选")

        candidates: list[dict[str, str]] = []
        for index, item in enumerate(raw_candidates):
            if not isinstance(item, dict):
                raise RuntimeError("Codex 候选结构无效")
            candidate_id = self._compact_text(
                str(item.get("id") or context["prompt_pack"][index]["id"]), 60
            )
            title = self._compact_text(
                str(item.get("title") or context["prompt_pack"][index]["title"]), 40
            )
            summary = self._compact_text(str(item.get("summary") or ""), 160)
            prompt = (
                str(item.get("prompt") or "").strip().replace("镜头方案:", "镜头方案：")
            )
            if not prompt:
                raise RuntimeError("Codex 候选缺少提示词内容")
            if "镜头方案：" not in prompt:
                direction = "" if "创作方向：" in prompt else f"创作方向：{title}\n"
                prompt = f"{direction}镜头方案：\n{prompt}"
            missing_constraints = [
                constraint
                for constraint in context["hard_constraints"]
                if constraint not in prompt
            ]
            if missing_constraints:
                constraint_suffix = "原始硬约束（必须遵守）：" + "；".join(
                    missing_constraints
                )
                prompt = (
                    self._limit_prompt_text(
                        prompt,
                        max(500, MAX_VIDEO_PROMPT_CHARS - len(constraint_suffix) - 1),
                    )
                    + "\n"
                    + constraint_suffix
                )
            else:
                prompt = self._limit_prompt_text(prompt, MAX_VIDEO_PROMPT_CHARS)
            if context["has_reference"] and "@图片1" not in prompt:
                raise RuntimeError("Codex 候选没有标明参考图用途")
            if context["shot_density"] == "single" and "[00-" in prompt:
                raise RuntimeError("Codex 单镜头候选错误使用了分段时间码")
            candidates.append(
                {
                    "id": candidate_id,
                    "title": title,
                    "summary": summary,
                    "prompt": prompt,
                }
            )
        if len({item["prompt"] for item in candidates}) != 3:
            raise RuntimeError("Codex 返回了重复候选")
        return candidates

    def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        video_id = str(payload.get("video_id") or payload.get("id") or "").strip()
        if not video_id:
            raise UserFacingError("缺少 video_id")
        api_key = self._resolve_api_key(payload.get("api_key"), video_id=video_id)
        if not api_key:
            raise UserFacingError("缺少 AIHUBMIX API Key", {"needs_api_key": True})
        return self._get_json(f"{self.api_base}/videos/{url_quote(video_id)}", api_key)

    def download(self, video_id: str) -> tuple[bytes, str | None]:
        api_key = self._resolve_api_key(video_id=video_id)
        if not api_key:
            raise UserFacingError("缺少 AIHUBMIX API Key", {"needs_api_key": True})
        return self._get_bytes(
            f"{self.api_base}/videos/{url_quote(video_id)}/content", api_key
        )

    def local_media(self, raw_path: str) -> tuple[bytes, str]:
        if not raw_path:
            raise UserFacingError("缺少 path")
        path = Path(raw_path).expanduser().resolve()
        allowed_roots = [
            DREAMINA_OUTPUT_DIR.resolve(),
            DREAMINA_INPUT_DIR.resolve(),
        ]
        if not any(is_relative_to(path, root) for root in allowed_roots):
            raise UserFacingError("不允许访问该本地媒体路径")
        if not path.is_file():
            raise UserFacingError("本地媒体文件不存在")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), content_type

    def _resolve_api_key(
        self, supplied: Any = None, *, video_id: str | None = None
    ) -> str:
        key = str(supplied or "").strip()
        if key:
            return key
        if video_id and self._task_keys.get(video_id):
            return self._task_keys[video_id]
        return self.api_key

    def _build_video_create_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        for source, target in (
            ("model", "model"),
            ("prompt", "prompt"),
            ("seconds", "seconds"),
            ("size", "size"),
            ("resolution", "resolution"),
            ("aspect_ratio", "aspect_ratio"),
            ("ratio", "ratio"),
            ("seed", "seed"),
            ("watermark", "watermark"),
            ("camera_fixed", "camera_fixed"),
            ("generate_audio", "generate_audio"),
        ):
            if source in payload and payload[source] not in (None, ""):
                body[target] = payload[source]

        for source in (
            "input_reference",
            "image_url",
            "first_frame",
            "last_frame",
            "reference_image",
        ):
            if payload.get(source):
                body[source] = payload[source]

        extra = payload.get("extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                if isinstance(key, str) and key and value not in (None, ""):
                    body[key] = value
        return body

    def _with_optimized_image_prompt(
        self,
        payload: dict[str, Any],
        request_mode: str,
        api_model: str,
    ) -> dict[str, Any]:
        if payload.get("prompt_optimize") is False:
            return payload
        original = clean_prompt_text(payload.get("prompt"))
        if not original or self._looks_like_compiled_prompt(original):
            return payload
        facts = self._infer_prompt_facts(original)
        plan = self._select_gpt_image2_template_plan(original, facts)
        aspect = self._display_aspect(payload)
        reference_value = payload.get("has_reference")
        has_reference = (
            bool(self._normalize_references(payload.get("input_reference")))
            or reference_value is True
            or str(reference_value or "").strip().lower()
            in {
                "1",
                "true",
                "yes",
            }
        )
        if request_mode == "dreamina_mcp":
            plan["local_examples"] = []
            optimized = self._compile_dreamina_image_prompt(
                original,
                aspect=aspect,
                has_reference=has_reference,
                facts=facts,
                plan=plan,
            )
        elif self._uses_gpt_image2_prompt(request_mode, api_model):
            plan["local_examples"] = self._match_youmind_prompt_examples(
                original, facts, plan
            )
            optimized = self._compile_codex_image2_prompt(
                original,
                request_mode=request_mode,
                api_model=api_model,
                aspect=aspect,
                has_reference=has_reference,
                facts=facts,
                plan=plan,
            )
        else:
            plan["local_examples"] = self._match_youmind_prompt_examples(
                original, facts, plan
            )
            optimized = self._compile_image_prompt(
                original,
                request_mode=request_mode,
                api_model=api_model,
                aspect=aspect,
                has_reference=has_reference,
                facts=facts,
                plan=plan,
            )
        next_payload = dict(payload)
        next_payload["prompt"] = optimized
        next_payload["_prompt_original"] = original
        next_payload["_prompt_optimized"] = optimized
        next_payload["_prompt_template"] = plan["primary"]["id"]
        next_payload["_prompt_template_name"] = plan["primary"]["name_zh"]
        next_payload["_prompt_supporting_templates"] = [
            item["id"] for item in plan["supporting"]
        ]
        next_payload["_prompt_template_source"] = AWESOME_GPT_IMAGE_2_SOURCE
        next_payload["_prompt_taxonomy_sources"] = plan.get("sources") or list(
            PROMPT_TAXONOMY_SOURCES
        )
        next_payload["_prompt_taxonomy_tags"] = plan.get("taxonomy_tags") or []
        next_payload["_prompt_local_examples"] = [
            self._public_youmind_example(item)
            for item in plan.get("local_examples", [])
        ]
        return next_payload

    def _looks_like_compiled_prompt(self, prompt: str) -> bool:
        markers = (
            "业务说明:",
            "Source brief",
            "原始需求:",
            "模板选择:",
            "Model Prompt:",
            "Visual direction:",
            "Negative constraints:",
            "画面主体:",
            "视频主体:",
            "镜头设计:",
            "创作方向：",
            "镜头方案：",
        )
        return any(marker in prompt for marker in markers)

    def _is_video_prompt_payload(self, payload: dict[str, Any], api_model: str) -> bool:
        model = str(api_model or payload.get("model") or "").lower()
        return "video" in model or "seconds" in payload or "duration" in payload

    def _with_optimized_dreamina_video_prompt(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("prompt_optimize") is False:
            return payload
        original = clean_prompt_text(payload.get("prompt"))
        if not original or self._looks_like_compiled_prompt(original):
            return payload
        facts = self._infer_prompt_facts(original)
        aspect = self._display_aspect(payload)
        has_reference = bool(self._normalize_references(payload.get("input_reference")))
        optimized = self._compile_dreamina_video_prompt(
            original,
            aspect=aspect,
            has_reference=has_reference,
            facts=facts,
        )
        next_payload = dict(payload)
        next_payload["prompt"] = optimized
        next_payload["_prompt_original"] = original
        next_payload["_prompt_optimized"] = optimized
        next_payload["_prompt_template"] = "dreamina-video-chinese"
        next_payload["_prompt_template_name"] = "即梦中文视频工程提示词"
        next_payload["_prompt_supporting_templates"] = []
        next_payload["_prompt_template_source"] = "local"
        next_payload["_prompt_taxonomy_sources"] = []
        next_payload["_prompt_taxonomy_tags"] = facts.get("taxonomy_tags") or []
        next_payload["_prompt_local_examples"] = []
        return next_payload

    def _compile_dreamina_video_prompt(
        self,
        source: str,
        *,
        aspect: str,
        has_reference: bool,
        facts: dict[str, Any],
    ) -> str:
        visual_brief = self._dreamina_visual_brief(source, facts)
        reference_note = (
            "参考图作为首帧和主体风格依据，保持主体身份、构图和配色连续。"
            if has_reference
            else "无参考图，只根据文本生成，不要加入无关 logo、字幕或水印。"
        )
        motion_notes: list[str] = []
        if facts.get("is_automotive"):
            motion_notes.append("车辆运动自然，镜头跟随产品，不遮挡车身关键轮廓。")
        if facts.get("is_character"):
            motion_notes.append("角色动作连贯，肢体结构稳定，表情和视线自然。")
        if facts.get("is_history"):
            motion_notes.append("东方意境、山水、云雾、水墨或节庆元素随镜头轻微流动。")
        if not motion_notes:
            motion_notes.append("主体动作明确，环境有轻微自然运动，镜头稳定。")
        return self._limit_prompt_text(
            "\n".join(
                (
                    f"视频主体: {visual_brief}",
                    f"画幅：{aspect}，生成一段完整短视频。",
                    reference_note,
                    "镜头设计：开场立即展示主体，镜头运动平稳，节奏清楚，不频繁切换，不出现跳帧。",
                    "动作设计：" + " ".join(motion_notes),
                    "风格：高质量、光影统一、细节可信、电影感或广告级完成度。",
                    "禁止：字幕、乱码、logo、水印、UI界面、低质拼贴、主体变形、肢体错乱、闪烁、画面撕裂。",
                )
            ),
            900,
        )

    def _display_aspect(self, payload: dict[str, Any]) -> str:
        explicit = str(
            payload.get("aspect_ratio")
            or payload.get("ratio")
            or payload.get("aspectRatio")
            or ""
        ).strip()
        if explicit:
            return explicit
        size = str(payload.get("size") or "").strip()
        if size in {"1536x1024", "1792x1024", "2048x1152", "3840x2160"}:
            return "16:9 landscape"
        if size in {"1024x1536", "1024x1792", "1152x2048", "2160x3840"}:
            return "9:16 portrait"
        if size in {"1024x1024", "2048x2048"}:
            return "1:1 square"
        return "16:9 landscape"

    def _compile_image_prompt(
        self,
        source: str,
        *,
        request_mode: str,
        api_model: str,
        aspect: str,
        has_reference: bool,
        facts: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> str:
        facts = facts or self._infer_prompt_facts(source)
        plan = plan or self._select_gpt_image2_template_plan(source, facts)
        if (
            facts["is_automotive"]
            or facts["is_marketing"]
            or plan["primary"]["id"]
            in {
                "poster-layout-system",
                "product-commerce-visual",
                "brand-touchpoint-board",
                "brand-identity-package",
            }
        ):
            return self._compile_marketing_image_prompt(
                source,
                facts=facts,
                plan=plan,
                request_mode=request_mode,
                api_model=api_model,
                aspect=aspect,
                has_reference=has_reference,
            )
        return self._compile_generic_image_prompt(
            source,
            plan=plan,
            request_mode=request_mode,
            api_model=api_model,
            aspect=aspect,
            has_reference=has_reference,
        )

    def _uses_gpt_image2_prompt(self, request_mode: str, api_model: str) -> bool:
        model = str(api_model or "").lower()
        return request_mode == "codex" or "gpt-image-2" in model

    def _compile_codex_image2_prompt(
        self,
        source: str,
        *,
        request_mode: str,
        api_model: str,
        aspect: str,
        has_reference: bool,
        facts: dict[str, Any],
        plan: dict[str, Any],
    ) -> str:
        source_brief = self._compact_text(source, 1200)
        directions: list[str] = []
        if facts.get("is_automotive"):
            directions.append(
                "Make the named vehicle or product line the dominant subject; keep body shape, lights, wheels, reflections, and perspective believable."
            )
        if facts.get("is_marketing"):
            directions.append(
                "Resolve the request as a finished commercial key visual or poster, not a moodboard, mockup board, or multi-option collage."
            )
        if facts.get("is_history"):
            directions.append(
                "Use refined Chinese cultural aesthetics, traditional architecture, ink, mountains, mythic or festival details only where they support the brief."
            )
        if facts.get("is_character"):
            directions.append(
                "Characters must be original, readable, and secondary unless the brief makes them the main subject."
            )
        if facts.get("is_ui"):
            directions.append(
                "If the brief asks for UI, render a coherent real product screen with consistent components and readable hierarchy."
            )
        if not directions:
            directions.append(
                "Preserve the named subjects, environment, style, color palette, and narrative relationships from the source brief."
            )

        style_notes: list[str] = []
        style_ids = set(facts.get("style_ids") or [])
        if "cinematic-film-still" in style_ids:
            style_notes.append(
                "cinematic lighting, atmospheric depth, controlled contrast"
            )
        if "ink-chinese-style" in style_ids:
            style_notes.append(
                "Chinese ink aesthetics blended with modern visual design"
            )
        if "photography" in style_ids:
            style_notes.append(
                "realistic material response and believable natural light"
            )
        if "illustration" in style_ids:
            style_notes.append("polished editorial illustration finish")
        if not style_notes:
            style_notes.append(
                "high-quality finish, coherent lighting, credible details"
            )

        headline = self._headline_for_facts(source, facts)
        text_rule = (
            f"Only include this exact title if text is needed: {headline!r}. Keep it sparse, legible, and placed in clean negative space."
            if headline
            else "Avoid readable text unless the source brief explicitly asks for it; do not invent slogans, prices, legal copy, or random letters."
        )
        reference_rule = (
            "Use the supplied reference image as the highest-priority guide for subject identity, structure, palette, and composition."
            if has_reference
            else "No reference image is supplied; do not invent official logos, prices, endorsements, legal claims, or exact product specifications."
        )
        return self._limit_prompt_text(
            "\n".join(
                (
                    "Source brief:",
                    source_brief,
                    "",
                    f"Target model: {api_model or CODEX_IMAGE_MODEL} via {request_mode}.",
                    "Create one finished image that can be used directly, with no process frames or explanatory layout.",
                    reference_rule,
                    "",
                    "Visual direction:",
                    " ".join(directions),
                    "",
                    "Composition:",
                    f"Aspect ratio: {aspect}. Use a stable, readable composition with clear subject hierarchy, balanced negative space, and coherent perspective.",
                    "",
                    "Style, lighting, and color:",
                    "; ".join(style_notes) + ".",
                    "",
                    "Text handling:",
                    text_rule,
                    "Preserve non-English proper nouns exactly as written when they are part of the requested content.",
                    "",
                    "Negative constraints:",
                    "No random text, watermark, fake logo, false legal or promotional claim, malformed anatomy, distorted product geometry, broken perspective, low-quality collage look, or cluttered composition.",
                )
            ),
            2200,
        )

    def _compile_dreamina_image_prompt(
        self,
        source: str,
        *,
        aspect: str,
        has_reference: bool,
        facts: dict[str, Any],
        plan: dict[str, Any],
    ) -> str:
        visual_brief = self._dreamina_visual_brief(source, facts)
        subject_notes: list[str] = []
        if facts.get("is_automotive"):
            subject_notes.append(
                "车辆或产品系列必须是最强主体，结构、车灯、轮毂和透视要可信。"
            )
        if facts.get("is_marketing"):
            subject_notes.append(
                "成图应像一张完成度高的商业海报或主视觉，不要变成过程稿。"
            )
        if facts.get("is_history"):
            subject_notes.append(
                "强化东方审美、传统建筑、山水、书法、神话或节庆元素的整体统一感。"
            )
        if facts.get("is_character"):
            subject_notes.append(
                "人物或角色要原创，动作清楚，不能遮挡用户要求的核心主体。"
            )
        if facts.get("is_ui"):
            subject_notes.append(
                "只有当用户明确要求界面时才生成 UI；否则不要生成网页、App、按钮或截图。"
            )
        if not subject_notes:
            subject_notes.append("保留用户点名的主体、环境、风格、色彩和叙事关系。")

        style_notes: list[str] = []
        style_ids = set(facts.get("style_ids") or [])
        if "cinematic-film-still" in style_ids:
            style_notes.append("电影感光影，层次丰富，景深自然。")
        if "ink-chinese-style" in style_ids:
            style_notes.append("水墨与现代视觉融合，笔触有张力但细节清晰。")
        if "photography" in style_ids:
            style_notes.append("写实质感，材质、反射和环境光可信。")
        if not style_notes:
            style_notes.append("高质量、精致、细节可信，光影和材质统一。")

        reference_note = (
            "参考图用于锁定主体、风格、结构、配色和关键形态。" if has_reference else ""
        )
        lines = [
            f"画面主体: {visual_brief}",
            f"画幅：{aspect}，单张完整成品图。",
            reference_note,
            "主体：" + " ".join(subject_notes),
            "风格：" + " ".join(style_notes),
            "构图：主体清晰，层级分明，透视稳定，留白自然，所有元素围绕主视觉流线组织。",
            "禁止：文字、乱码、logo、水印、UI截图、网页界面、边框、低质拼贴、错误透视、变形物体。",
        ]
        return self._limit_prompt_text("\n".join(item for item in lines if item), 900)

    def _dreamina_visual_brief(self, source: str, facts: dict[str, Any]) -> str:
        source_compact = self._compact_text(source, 700)
        if self._looks_like_guangzhou_ink_brief(source):
            return (
                "纯黑背景，中式水墨书法粗笔触呈 S 形横贯画面，形成中央主视觉流线；"
                "半透明画眉鸟悬浮在曲线上方，体内隐约映出岭南传统建筑、层叠阴影和柔和蓝绿色光；"
                "曲线两侧错落出现广州地标与古典建筑，形成节奏和纵深；"
                "前景有优雅白鹤与安静湖面，远处是雾气层叠的山峦；"
                "非线性透视，蓝青冷色调为主，少量暖色点缀，东方诗意与现代电影感融合，高细节、强氛围。"
            )
        if self._mostly_ascii(source):
            return (
                "根据英文画面描述生成高质量成品图，只保留画面元素和视觉风格，不生成文字："
                f"{source_compact}"
            )
        return source_compact

    def _looks_like_guangzhou_ink_brief(self, source: str) -> bool:
        source_lower = source.lower()
        required_groups = (
            ("s-shaped", "s shaped", "calligraphy", "书法", "水墨"),
            ("guangzhou", "广州"),
            ("crane", "cranes", "白鹤", "鹤"),
            ("mountain", "mountains", "山峦", "山水"),
        )
        return all(
            any(item in source_lower for item in group) for group in required_groups
        )

    def _mostly_ascii(self, source: str) -> bool:
        text = str(source or "").strip()
        if not text:
            return False
        ascii_count = sum(1 for char in text if ord(char) < 128 and char.isalpha())
        chinese_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        return ascii_count > 0 and ascii_count > chinese_count * 3

    def _infer_prompt_facts(self, source: str) -> dict[str, Any]:
        taxonomy = self._match_youmind_taxonomy(source)
        use_case_ids = self._taxonomy_ids(taxonomy, "use_cases")
        style_ids = self._taxonomy_ids(taxonomy, "styles")
        subject_ids = self._taxonomy_ids(taxonomy, "subjects")
        brand = self._first_keyword(
            source,
            (
                "五菱新能源",
                "五菱",
                "比亚迪",
                "小鹏",
                "蔚来",
                "理想",
                "问界",
                "吉利",
                "极氪",
                "长安",
                "小米汽车",
                "特斯拉",
                "宝马",
                "奔驰",
                "奥迪",
            ),
        )
        characters = [
            name
            for name in ("哪吒", "敖丙", "孙悟空", "嫦娥", "龙王", "财神", "吉祥物")
            if name in source
        ]
        festival = self._first_keyword(
            source,
            (
                "马年",
                "龙年",
                "蛇年",
                "春节",
                "新春",
                "拜年",
                "元宵",
                "元旦",
                "中秋",
                "国庆",
                "七夕",
            ),
        )
        vehicle_terms = (
            "汽车",
            "新能源",
            "新能源车",
            "车辆",
            "车型",
            "轿车",
            "SUV",
            "MPV",
            "小车",
        )
        marketing_terms = (
            "海报",
            "广告",
            "主视觉",
            "KV",
            "封面",
            "拜年图",
            "宣传图",
            "活动图",
            "系列",
            "产品",
            "Campaign",
            "campaign",
            "社媒",
            "小红书",
            "缩略图",
        )
        ui_terms = (
            "UI",
            "ui",
            "界面",
            "截图",
            "App",
            "APP",
            "网页",
            "仪表盘",
            "dashboard",
        )
        infographic_terms = (
            "信息图",
            "图解",
            "流程图",
            "结构图",
            "知识图谱",
            "架构图",
            "对比图",
        )
        product_terms = ("商品", "电商", "主图", "详情页", "包装", "卖点", "产品图")
        brand_terms = (
            "品牌",
            "VI",
            "logo",
            "Logo",
            "标志",
            "视觉识别",
            "触点",
            "品牌系统",
        )
        character_terms = (
            "角色",
            "人物设定",
            "设定表",
            "动作",
            "姿势",
            "pose",
            "IP形象",
            "ip形象",
        )
        history_terms = (
            "古风",
            "国潮",
            "水墨",
            "中国风",
            "国风",
            "朝代",
            "唐",
            "宋",
            "明",
            "山水",
            "神话",
        )
        scene_terms = ("场景", "故事", "分镜", "叙事", "世界观", "镜头", "故事板")
        source_lower = source.lower()
        has_vehicle = (
            any(term in source for term in vehicle_terms) or "vehicle" in subject_ids
        )
        has_product = (
            any(term in source for term in product_terms)
            or bool({"product", "food-drink", "fashion-item", "vehicle"} & subject_ids)
            or bool({"product-marketing", "ecommerce-main-image"} & use_case_ids)
        )
        has_brand_signal = any(term in source for term in brand_terms)
        has_marketing = any(term in source for term in marketing_terms) or bool(
            {
                "social-media-post",
                "youtube-thumbnail",
                "product-marketing",
                "ecommerce-main-image",
                "poster-flyer",
            }
            & use_case_ids
        )
        has_character = (
            bool(characters)
            or any(term in source for term in character_terms)
            or bool(
                {
                    "profile-avatar",
                    "comic-storyboard",
                    "game-asset",
                }
                & use_case_ids
            )
            or bool({"portrait-selfie", "influencer-model", "character"} & subject_ids)
        )
        return {
            "brand": brand,
            "characters": characters,
            "festival": festival,
            "taxonomy": taxonomy,
            "taxonomy_tags": self._format_youmind_taxonomy_tags(taxonomy),
            "is_automotive": has_vehicle,
            "is_marketing": has_marketing,
            "is_ui": any(
                self._keyword_in_source(source_lower, term) for term in ui_terms
            )
            or "app-web-design" in use_case_ids,
            "is_infographic": any(term in source for term in infographic_terms)
            or (
                "infographic-edu-visual" in use_case_ids
                or "diagram-chart" in subject_ids
            ),
            "is_product": has_product or bool(brand and has_vehicle),
            "is_brand": bool(brand or has_brand_signal),
            "is_brand_system": has_brand_signal,
            "is_character": has_character,
            "is_history": bool(
                festival
                or any(term in source for term in history_terms)
                or "ink-chinese-style" in style_ids
                or "landscape-nature" in subject_ids
            ),
            "is_scene": any(term in source for term in scene_terms)
            or "comic-storyboard" in use_case_ids,
            "is_thumbnail": "youtube-thumbnail" in use_case_ids,
            "is_social": "social-media-post" in use_case_ids,
            "is_game_asset": "game-asset" in use_case_ids,
            "is_comic_storyboard": "comic-storyboard" in use_case_ids,
            "style_ids": style_ids,
            "use_case_ids": use_case_ids,
            "subject_ids": subject_ids,
        }

    def _match_youmind_taxonomy(self, source: str) -> dict[str, list[dict[str, str]]]:
        source_lower = source.lower()
        groups = (
            ("use_cases", "使用场景", YOUMIND_USE_CASE_TAXONOMY),
            ("styles", "风格", YOUMIND_STYLE_TAXONOMY),
            ("subjects", "主体", YOUMIND_SUBJECT_TAXONOMY),
        )
        result: dict[str, list[dict[str, str]]] = {}
        for group_key, group_name, items in groups:
            matches: list[dict[str, str]] = []
            for item in items:
                keywords = item.get("keywords") or ()
                if any(
                    self._keyword_in_source(source_lower, keyword)
                    for keyword in keywords
                ):
                    match = {
                        "id": str(item["id"]),
                        "name_zh": str(item["name_zh"]),
                        "kind": group_key,
                        "kind_name": group_name,
                    }
                    if item.get("template_id"):
                        match["template_id"] = str(item["template_id"])
                    matches.append(match)
            result[group_key] = matches
        return result

    def _keyword_in_source(self, source_lower: str, keyword: Any) -> bool:
        keyword_lower = str(keyword or "").strip().lower()
        if not keyword_lower:
            return False
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,3}", keyword_lower):
            pattern = rf"(?<![a-z0-9_-]){re.escape(keyword_lower)}(?![a-z0-9_-])"
            return re.search(pattern, source_lower) is not None
        return keyword_lower in source_lower

    def _taxonomy_ids(
        self, taxonomy: dict[str, list[dict[str, str]]], key: str
    ) -> set[str]:
        return {str(item.get("id") or "") for item in taxonomy.get(key, [])}

    def _format_youmind_taxonomy_tags(
        self,
        taxonomy: dict[str, list[dict[str, str]]],
    ) -> list[str]:
        tags: list[str] = []
        for key in ("use_cases", "styles", "subjects"):
            for item in taxonomy.get(key, []):
                text = (
                    f"{item.get('kind_name')}:{item.get('name_zh')}({item.get('id')})"
                )
                if text not in tags:
                    tags.append(text)
        return tags

    def _load_youmind_corpus(self) -> dict[str, Any]:
        if self._youmind_corpus_cache is not None:
            return self._youmind_corpus_cache
        try:
            data = json.loads(YOUMIND_LOCAL_CORPUS.read_text(encoding="utf-8"))
        except Exception:
            data = {
                "source": YOUMIND_GPT_IMAGE_2_SOURCE,
                "license": "CC BY 4.0",
                "records": [],
            }
        if not isinstance(data, dict):
            data = {"source": YOUMIND_GPT_IMAGE_2_SOURCE, "records": []}
        if not isinstance(data.get("records"), list):
            data["records"] = []
        self._youmind_corpus_cache = data
        return data

    def _youmind_corpus_count(self) -> int:
        return len(self._load_youmind_corpus().get("records") or [])

    def _match_youmind_prompt_examples(
        self,
        source: str,
        facts: dict[str, Any],
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records = self._load_youmind_corpus().get("records") or []
        if not records:
            return []

        query_terms = self._youmind_query_terms(source, facts, plan)
        scored: list[tuple[int, dict[str, Any]]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            score = self._score_youmind_record(record, query_terms, facts)
            if score > 0:
                scored.append((score, record))

        scored.sort(key=lambda item: (-item[0], int(item[1].get("number") or 0)))
        examples: list[dict[str, Any]] = []
        for score, record in scored[:YOUMIND_EXAMPLE_LIMIT]:
            prompt = str(record.get("prompt") or "").strip()
            examples.append(
                {
                    "id": record.get("id"),
                    "number": record.get("number"),
                    "category": record.get("category"),
                    "title": record.get("title"),
                    "description": record.get("description"),
                    "author": record.get("author"),
                    "source_url": record.get("source_url"),
                    "try_url": record.get("try_url"),
                    "score": score,
                    "prompt_excerpt": self._compact_text(
                        prompt,
                        YOUMIND_EXAMPLE_EXCERPT_CHARS,
                    ),
                }
            )
        return examples

    def _youmind_query_terms(
        self,
        source: str,
        facts: dict[str, Any],
        plan: dict[str, Any],
    ) -> list[str]:
        terms: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text.lower() not in {item.lower() for item in terms}:
                terms.append(text)

        for token in re.findall(
            r"[A-Za-z0-9][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", source
        ):
            add(token)
        for template in [plan.get("primary") or {}, *(plan.get("supporting") or [])]:
            add(template.get("name_zh"))
            add(template.get("category"))
            add(template.get("intent"))
            for tag in template.get("tags") or []:
                add(tag)
        taxonomy = facts.get("taxonomy") or {}
        for group in ("use_cases", "styles", "subjects"):
            for item in taxonomy.get(group, []):
                add(item.get("id"))
                add(item.get("name_zh"))
        for key in ("brand", "festival"):
            add(facts.get(key))
        for character in facts.get("characters") or []:
            add(character)
        if facts.get("is_automotive"):
            for term in ("汽车", "车辆", "新能源", "新能源车", "车型", "跑车", "机车"):
                add(term)
        if facts.get("festival"):
            for term in ("新年", "春节", "拜年", "节日", "红金"):
                add(term)
        return terms

    def _score_youmind_record(
        self,
        record: dict[str, Any],
        terms: list[str],
        facts: dict[str, Any],
    ) -> int:
        category = str(record.get("category") or "")
        title = str(record.get("title") or "")
        description = str(record.get("description") or "")
        prompt = str(record.get("prompt") or "")
        category_lower = category.lower()
        title_lower = title.lower()
        haystack = f"{category}\n{title}\n{description}\n{prompt}".lower()
        score = 0

        if facts.get("is_game_asset") and "游戏素材" in category:
            score += 36
        if facts.get("is_thumbnail") and "YouTube" in category:
            score += 36
        if facts.get("is_infographic") and "信息图" in category:
            score += 34
        if facts.get("is_ui") and any(
            term in haystack for term in ("ui", "界面", "网页", "app")
        ):
            score += 30
        if facts.get("is_product") and any(
            term in category for term in ("电商主图", "产品营销")
        ):
            score += 28
        if facts.get("is_character") and any(
            term in category
            for term in ("个人资料", "头像", "漫画", "故事板", "游戏素材")
        ):
            score += 18
        if facts.get("is_history") and any(
            term in haystack for term in ("水墨", "中国风", "古风", "国风", "神话")
        ):
            score += 16
        if facts.get("is_scene") and "故事板" in category:
            score += 14
        if facts.get("is_marketing") and any(
            term in category
            for term in ("产品营销", "电商主图", "YouTube", "社交媒体", "海报")
        ):
            score += 12
        if facts.get("is_automotive") and any(
            term in haystack
            for term in ("汽车", "车辆", "新能源", "新能源车", "车型", "跑车", "机车")
        ):
            score += 20
        if facts.get("is_automotive") and "个人资料" in category:
            score -= 40
        if facts.get("is_automotive") and "头像" in category:
            score -= 30
        if facts.get("is_marketing") and "个人资料" in category:
            score -= 16
        if not facts.get("is_game_asset") and "游戏素材" in category:
            score -= 30

        taxonomy = facts.get("taxonomy") or {}
        for group in ("use_cases", "styles", "subjects"):
            for item in taxonomy.get(group, []):
                name = str(item.get("name_zh") or "")
                slug = str(item.get("id") or "")
                if name and name in category:
                    score += 24
                elif name and name.lower() in haystack:
                    score += 8
                if slug and slug.lower() in haystack:
                    score += 6

        for term in terms:
            term_lower = term.lower()
            if not term_lower:
                continue
            if term_lower == category_lower:
                score += 12
            elif term_lower in category_lower:
                score += 8
            elif term_lower in title_lower:
                score += 6
            elif term_lower in haystack:
                score += 2

        return score

    def _public_youmind_example(self, example: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": example.get("id"),
            "number": example.get("number"),
            "category": example.get("category"),
            "title": example.get("title"),
            "author": example.get("author"),
            "source_url": example.get("source_url"),
            "try_url": example.get("try_url"),
            "score": example.get("score"),
        }

    def _format_youmind_examples(self, plan: dict[str, Any]) -> str:
        examples = plan.get("local_examples") or []
        if not examples:
            return "本地案例参考: 未匹配到可用 YouMind 本地案例。"
        lines = [
            "本地案例参考: 以下案例来自本机 YouMind GPT Image 2 语料，用于学习结构、镜头、版式、约束密度和参数化写法；不要照搬其中的主体、品牌、人物或具体文字。",
            f"案例来源: {YOUMIND_GPT_IMAGE_2_SOURCE} / CC BY 4.0。",
        ]
        for index, example in enumerate(examples, 1):
            meta = " / ".join(
                item
                for item in (
                    str(example.get("id") or ""),
                    str(example.get("category") or ""),
                    str(example.get("title") or ""),
                )
                if item
            )
            lines.append(f"{index}. {meta}")
            if example.get("description"):
                lines.append(
                    f"   摘要: {self._compact_text(example['description'], 180)}"
                )
            lines.append(f"   prompt片段: {example.get('prompt_excerpt') or ''}")
        return "\n".join(lines)

    def _compact_text(self, text: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 1)].rstrip() + "…"

    def _limit_prompt_text(self, text: str, limit: int) -> str:
        lines = [
            re.sub(r"[ \t]+", " ", line).strip()
            for line in str(text or "").splitlines()
        ]
        clean = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if len(clean) <= limit:
            return clean
        return clean[: max(0, limit - 1)].rstrip() + "…"

    def _first_keyword(self, source: str, candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            if candidate in source:
                return candidate
        return ""

    def _select_gpt_image2_template_plan(
        self,
        source: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        primary_id = "poster-layout-system"
        if facts.get("is_ui"):
            primary_id = "ui-screenshot-system"
        elif facts.get("is_infographic"):
            primary_id = "infographic-engine"
        elif facts.get("is_game_asset"):
            primary_id = "concept-product-breakdown"
        elif facts.get("is_brand_system") and not facts.get("is_marketing"):
            primary_id = "brand-touchpoint-board"
        elif bool(
            {"product-marketing", "ecommerce-main-image"}
            & set(facts.get("use_case_ids") or [])
        ):
            primary_id = "product-commerce-visual"
        elif facts.get("is_thumbnail") or facts.get("is_social"):
            primary_id = "poster-layout-system"
        elif facts.get("is_comic_storyboard") and not facts.get("is_marketing"):
            primary_id = "scene-storytelling"
        elif facts.get("is_product") and not facts.get("is_marketing"):
            primary_id = "product-commerce-visual"
        elif facts.get("is_brand") and not facts.get("is_marketing"):
            primary_id = "brand-touchpoint-board"
        elif facts.get("is_character") and not (
            facts.get("is_product") or facts.get("is_marketing")
        ):
            primary_id = "character-design-sheet"
        elif facts.get("is_history") and not facts.get("is_marketing"):
            primary_id = "history-classical-themes"
        elif facts.get("is_scene") and not facts.get("is_marketing"):
            primary_id = "scene-storytelling"

        supporting_ids: list[str] = []
        if primary_id == "poster-layout-system":
            if facts.get("is_product"):
                supporting_ids.append("product-commerce-visual")
            if facts.get("is_brand"):
                supporting_ids.append("brand-touchpoint-board")
            if facts.get("is_character"):
                supporting_ids.append("character-design-sheet")
            if facts.get("is_history"):
                supporting_ids.append("history-classical-themes")
            if facts.get("is_scene"):
                supporting_ids.append("scene-storytelling")
            if facts.get("is_game_asset"):
                supporting_ids.append("concept-product-breakdown")
        elif primary_id == "product-commerce-visual":
            if facts.get("is_brand"):
                supporting_ids.append("brand-touchpoint-board")
            if facts.get("is_character"):
                supporting_ids.append("character-design-sheet")
            if facts.get("is_history"):
                supporting_ids.append("history-classical-themes")
        elif primary_id == "brand-touchpoint-board":
            if facts.get("is_product"):
                supporting_ids.append("product-commerce-visual")
        elif primary_id == "concept-product-breakdown":
            if facts.get("is_character"):
                supporting_ids.append("character-design-sheet")
            if facts.get("is_product"):
                supporting_ids.append("product-commerce-visual")
            if facts.get("is_scene"):
                supporting_ids.append("scene-storytelling")
        elif primary_id == "character-design-sheet":
            if facts.get("is_history"):
                supporting_ids.append("history-classical-themes")
            if facts.get("is_scene"):
                supporting_ids.append("scene-storytelling")
        elif primary_id == "scene-storytelling":
            if facts.get("is_character"):
                supporting_ids.append("character-design-sheet")
            if facts.get("is_history"):
                supporting_ids.append("history-classical-themes")

        primary = self._template_by_id(primary_id)
        supporting = [
            self._template_by_id(template_id)
            for template_id in supporting_ids
            if template_id != primary_id
        ]
        tags: list[str] = []
        for template in [primary, *supporting]:
            for tag in template.get("tags", []):
                if tag not in tags:
                    tags.append(tag)
        taxonomy_tags = facts.get("taxonomy_tags") or []
        return {
            "source": AWESOME_GPT_IMAGE_2_SOURCE,
            "sources": list(PROMPT_TAXONOMY_SOURCES),
            "primary": primary,
            "supporting": supporting,
            "tags": tags,
            "taxonomy_tags": taxonomy_tags,
            "matched_reason": self._template_match_reason(
                source, facts, primary, supporting
            ),
        }

    def _template_by_id(self, template_id: str) -> dict[str, Any]:
        template = GPT_IMAGE2_TEMPLATE_LIBRARY.get(template_id)
        if template is None:
            template = {
                "name_zh": template_id,
                "category": "Other Use Cases",
                "tags": [],
                "example_cases": [],
                "intent": "特殊图像任务",
                "rules": [],
            }
        return {"id": template_id, **template}

    def _template_match_reason(
        self,
        source: str,
        facts: dict[str, Any],
        primary: dict[str, Any],
        supporting: list[dict[str, Any]],
    ) -> str:
        signals: list[str] = []
        if facts.get("is_marketing"):
            signals.append("营销/海报")
        if facts.get("is_thumbnail"):
            signals.append("缩略图/封面")
        if facts.get("is_social"):
            signals.append("社媒帖子")
        if facts.get("is_game_asset"):
            signals.append("游戏素材")
        if facts.get("is_comic_storyboard"):
            signals.append("漫画/分镜")
        if facts.get("is_automotive"):
            signals.append("汽车/新能源产品")
        elif facts.get("is_product"):
            signals.append("商品/产品")
        if facts.get("is_brand"):
            signals.append("品牌")
        if facts.get("is_character"):
            signals.append("角色")
        if facts.get("is_history"):
            signals.append("传统文化/节日")
        if facts.get("is_infographic"):
            signals.append("信息图")
        if facts.get("is_ui"):
            signals.append("界面")
        support_names = "、".join(item["name_zh"] for item in supporting) or "无"
        return (
            f"从需求中识别到：{'、'.join(signals) or '通用视觉'}；"
            f"主模板 {primary['id']} / {primary['name_zh']}；"
            f"辅助模板：{support_names}。"
        )

    def _format_template_plan(self, plan: dict[str, Any]) -> str:
        primary = plan["primary"]
        supporting = plan.get("supporting") or []
        case_ids: list[int] = []
        for template in [primary, *supporting]:
            for case_id in template.get("example_cases", []):
                if case_id not in case_ids:
                    case_ids.append(case_id)
        supporting_text = (
            "、".join(f"{item['id']}（{item['name_zh']}）" for item in supporting)
            if supporting
            else "无"
        )
        case_text = "、".join(str(item) for item in case_ids[:6]) or "无"
        sources_text = "；".join(plan.get("sources") or [plan.get("source", "")])
        taxonomy_text = "、".join(plan.get("taxonomy_tags") or []) or "无"
        return "\n".join(
            (
                f"模板来源: {sources_text}",
                f"主模板: {primary['id']}（{primary['name_zh']} / {primary['category']}）",
                f"辅助模板: {supporting_text}",
                f"模板标签: {', '.join(plan.get('tags') or []) or 'General'}",
                f"YouMind分类标签: {taxonomy_text}",
                f"参考案例ID: {case_text}",
                f"匹配原因: {plan['matched_reason']}",
            )
        )

    def _format_template_rules(self, plan: dict[str, Any]) -> str:
        rules: list[str] = []
        for template in [plan["primary"], *(plan.get("supporting") or [])]:
            for rule in template.get("rules", []):
                if rule not in rules:
                    rules.append(rule)
        return (
            "；".join(rules)
            if rules
            else "明确主体、构图、风格、文字、比例和负面约束。"
        )

    def _headline_for_facts(self, source: str, facts: dict[str, Any]) -> str:
        brand = facts.get("brand") or ""
        festival = facts.get("festival") or ""
        if brand and festival == "马年":
            return f"{brand} 马到成功"
        if brand and festival in {"春节", "新春", "拜年"}:
            return f"{brand} 新春大吉"
        if brand and festival:
            return f"{brand} {festival}快乐"
        if brand:
            return brand
        return ""

    def _compile_marketing_image_prompt(
        self,
        source: str,
        *,
        facts: dict[str, Any],
        plan: dict[str, Any],
        request_mode: str,
        api_model: str,
        aspect: str,
        has_reference: bool,
    ) -> str:
        brand = facts.get("brand") or "用户指定的品牌/产品"
        headline = self._headline_for_facts(source, facts)
        character_text = self._character_direction(facts.get("characters") or [])
        festival_text = self._festival_direction(facts.get("festival") or "")
        reference_text = (
            "已上传参考图：参考图优先级最高，用于锁定产品外形、品牌识别、颜色、构图和材质细节。"
            if has_reference
            else "未上传官方参考图：只推断宽泛品类和氛围，不伪造精确 logo、徽章、法务标识、具体车型配置、价格和官方承诺。"
        )
        secondary_focus = (
            "节日 Campaign 氛围"
            if facts.get("festival")
            else "使用场景、卖点表达和品牌氛围"
        )
        product_focus = (
            f"视觉主体优先级：1）{brand} 新能源车辆/产品系列；2）{secondary_focus}；3）角色、神话、装饰和背景元素。"
            if facts.get("is_automotive")
            else f"视觉主体优先级：1）{brand} 产品或 Campaign 主体；2）{secondary_focus}；3）角色、道具和装饰元素。"
        )
        title_rule = (
            f"如需生成文字，只允许出现这个标题：『{headline}』。标题必须少、准、可读，放在干净安全标题区。"
            if headline
            else "如需生成文字，必须极少且可读；不要发明额外 slogan、价格、法务小字或营销承诺。"
        )
        vehicle_direction = (
            "汽车类任务：车辆/产品系列必须是最强主视觉。前景使用可信的前 3/4 角度车辆，背景可放 2-3 台辅助车辆形成系列感。角色只能站在车辆两侧或作为框景，不能遮挡格栅、车轮、车灯、车身轮廓和产品主体。"
            if facts.get("is_automotive")
            else "产品/品牌任务：命名产品或 Campaign 主体必须清晰。人物、吉祥物和装饰只能框住主体，不能替代主体。"
        )
        return "\n".join(
            item
            for item in (
                f"原始需求: {source}",
                f"模型目标: {api_model} via {request_mode}",
                self._format_template_plan(plan),
                self._format_youmind_examples(plan),
                "",
                "任务: 将用户的自然语言描述编译成一张可直接生成的成品商业视觉，不生成过程稿、moodboard、样机展示板或多方案拼贴。保留用户点名的品牌、产品、节日、角色和意图。",
                f"模板规则: {self._format_template_rules(plan)}",
                "",
                "1. 主体与任务",
                product_focus,
                reference_text,
                vehicle_direction,
                character_text,
                festival_text,
                "",
                "2. 构图与版式",
                "使用单张完成图构图。主视觉占据画面核心，标题区、产品区、角色区、节日氛围区层级清楚。构图要适合社媒传播和移动端封面阅读，留出足够负空间，不要把画面塞满。",
                "优先使用稳定的商业海报构图：居中或轻微对角动势，产品位于视觉中心或下三分之一，辅助角色左右分布，背景元素向主体聚拢。",
                "",
                "3. 风格与材质",
                "高端中国商业 Campaign 主视觉，精致广告级完成度，明亮但克制，材质可信，车漆/玻璃/金属/布料/火焰/水纹/灯笼等元素要有一致光源和真实反射。",
                "整体色彩围绕主题组织，节庆图可使用高级红金与少量冷色对比，避免廉价贴纸感和杂乱渐变。",
                "",
                "4. 文字与标签",
                title_rule,
                "除标题外不要添加其他大段可读文字。不要生成随机英文、乱码、价格、获奖、官方认证、促销政策或法务小字。",
                "",
                "5. 画幅与输出",
                f"画幅: {aspect}。输出必须是一张完成度高、可直接预览的成品图。",
                "",
                "6. 约束与负面细节",
                "不要随机额外 logo；不要不可读或变形中文；不要虚假价格、虚假奖项、官方背书和法律声明；不要车身变形、破轮子、错误透视、低端素材拼贴、过度拥挤、廉价库存海报风；不要让人物压过产品主体。",
            )
            if item
        )

    def _character_direction(self, characters: list[str]) -> str:
        if not characters:
            return ""
        directions: list[str] = []
        if "哪吒" in characters:
            directions.append(
                "哪吒使用原创中国神话少年形象：红色火焰飘带、风火轮、火尖枪、热烈动势；不要复制任何特定电影造型。"
            )
        if "敖丙" in characters:
            directions.append(
                "敖丙使用原创蓝白水系/冰系神话少年形象：水龙、冰蓝水纹、优雅衣袍、冷色动势；不要复制任何特定电影造型。"
            )
        for name in characters:
            if name not in {"哪吒", "敖丙"}:
                directions.append(
                    f"{name} 只能作为辅助 Campaign 角色，不能成为第一主体。"
                )
        return "角色设定: " + " ".join(directions)

    def _festival_direction(self, festival: str) -> str:
        if not festival:
            return ""
        if festival == "马年":
            return "节日方向: 中国新年马年拜年视觉。使用高级红金、灯笼、祥云、烟花、奔马剪影或马形动势，整体是精致商业新春 Campaign，而不是廉价年画拼贴。"
        if festival in {"春节", "新春", "拜年"}:
            return "节日方向: 中国新春拜年视觉。使用高级红金、灯笼、祥云、烟花和温暖喜庆氛围，保持商业海报质感。"
        return f"节日方向: {festival} Campaign 视觉。使用符合文化语境的季节符号，并保持高级商业完成度。"

    def _compile_generic_image_prompt(
        self,
        source: str,
        *,
        plan: dict[str, Any],
        request_mode: str,
        api_model: str,
        aspect: str,
        has_reference: bool,
    ) -> str:
        reference_text = (
            "已上传参考图：参考图优先级最高，用于锁定主体身份、构图、色彩和风格。"
            if has_reference
            else "未上传参考图：只从原始需求推断宽泛视觉方向。"
        )
        return "\n".join(
            (
                f"原始需求: {source}",
                f"模型目标: {api_model} via {request_mode}",
                self._format_template_plan(plan),
                self._format_youmind_examples(plan),
                reference_text,
                "任务: 按模板把用户自然语言补齐为可生成的成品图。保留用户意图，明确主体、辅助元素、环境、构图、光影、风格和完成度。",
                f"模板规则: {self._format_template_rules(plan)}",
                f"画幅: {aspect}。",
                "构图: 主体清晰，层级可读，透视稳定，负空间平衡，不拥挤。",
                "风格: 高质量、视觉精致、细节可信、光影和材质一致。",
                "负面约束: 不要随机文字，不要伪造 logo 或承诺，不要肢体/物体变形，不要低质素材拼贴感。",
            )
        )

    def _build_openai_image_body(
        self,
        payload: dict[str, Any],
        api_model: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": api_model,
            "prompt": str(payload.get("prompt") or "").strip(),
        }
        for source, target in (
            ("n", "n"),
            ("size", "size"),
            ("quality", "quality"),
            ("background", "background"),
            ("moderation", "moderation"),
            ("output_format", "output_format"),
            ("input_fidelity", "input_fidelity"),
            ("response_format", "response_format"),
            ("seed", "seed"),
            ("watermark", "watermark"),
            ("prompt_extend", "prompt_extend"),
            ("negative_prompt", "negative_prompt"),
        ):
            if source in payload and payload[source] not in (None, ""):
                body[target] = payload[source]

        refs = self._normalize_references(payload.get("input_reference"))
        if refs:
            body["input_reference"] = refs if len(refs) > 1 else refs[0]

        extra = payload.get("extra")
        if isinstance(extra, dict):
            for key, value in extra.items():
                if isinstance(key, str) and key and value not in (None, ""):
                    body[key] = value
        return body

    def _build_gemini_image_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {"text": str(payload.get("prompt") or "").strip()}
        ]
        for ref in self._normalize_references(payload.get("input_reference")):
            if ref.startswith("data:"):
                mime_type, data = split_data_url(ref)
                if data:
                    parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
            else:
                parts.append({"fileData": {"mimeType": "image/*", "fileUri": ref}})

        image_config: dict[str, Any] = {}
        if payload.get("aspect_ratio"):
            image_config["aspectRatio"] = payload["aspect_ratio"]
        if payload.get("size"):
            image_config["imageSize"] = payload["size"]

        generation_config: dict[str, Any] = {
            "responseModalities": ["TEXT", "IMAGE"],
        }
        if image_config:
            generation_config["imageConfig"] = image_config

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }
        if payload.get("google_search"):
            body["tools"] = [{"googleSearch": {}}]
        return body

    def _generate_dreamina_image(
        self,
        payload: dict[str, Any],
        api_model: str,
    ) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        refs = self._dreamina_reference_files(payload.get("input_reference"))
        ratio = self._dreamina_ratio(payload, default="1:1")
        resolution_type = self._dreamina_image_resolution(payload)
        poll_seconds = self._dreamina_poll_seconds(
            "DREAMINA_MCP_IMAGE_POLL_SECONDS", 600
        )
        model_version = str(payload.get("model_version") or "").strip()

        if refs:
            args = ["image2image", "--prompt", prompt, "--images", ",".join(refs)]
            args.extend(["--ratio", ratio, "--resolution_type", resolution_type])
        else:
            args = ["text2image", "--prompt", prompt, "--ratio", ratio]
            args.extend(["--resolution_type", resolution_type])
        if model_version:
            args.extend(["--model_version", model_version])
        args.extend(["--poll", str(max(0, poll_seconds))])

        result = self._run_dreamina_mcp(
            args,
            timeout=max(120, poll_seconds + 60),
            media_kind="image",
            prompt=prompt,
        )
        if not result.get("ok"):
            error_data = self._dreamina_error_data(result)
            raise UserFacingError(
                error_data["message"] or "Dreamina 即梦 MCP 生图失败",
                error_data,
            )

        return {
            "request_mode": "dreamina_mcp",
            "prompt_original": payload.get("_prompt_original") or prompt,
            "prompt_optimized": payload.get("_prompt_optimized") or prompt,
            "template": payload.get("_prompt_template"),
            "template_name": payload.get("_prompt_template_name"),
            "supporting_templates": payload.get("_prompt_supporting_templates") or [],
            "template_source": payload.get("_prompt_template_source"),
            "taxonomy_sources": payload.get("_prompt_taxonomy_sources") or [],
            "taxonomy_tags": payload.get("_prompt_taxonomy_tags") or [],
            "local_examples": payload.get("_prompt_local_examples") or [],
            "request": {
                "provider": "Dreamina MCP",
                "model": api_model,
                "ratio": ratio,
                "resolution_type": resolution_type,
                "input_reference_count": len(refs),
            },
            "response": result,
            "images": self._dreamina_image_outputs(result),
        }

    def _generate_dreamina_video(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise UserFacingError("缺少 prompt")

        refs = self._dreamina_reference_files(payload.get("input_reference"))
        duration = self._safe_int(
            payload.get("seconds"), default=5, minimum=4, maximum=15
        )
        ratio = self._dreamina_ratio(payload, default="16:9")
        video_resolution = self._dreamina_video_resolution(payload)
        poll_seconds = self._dreamina_poll_seconds(
            "DREAMINA_MCP_VIDEO_POLL_SECONDS", 900
        )
        model_version = str(payload.get("model_version") or "").strip()
        if video_resolution == "1080p":
            if model_version and model_version != "seedance2.0_vip":
                raise UserFacingError("即梦 1080p 仅支持 Seedance 2.0 VIP 模型")
            model_version = "seedance2.0_vip"
        elif not model_version:
            model_version = "seedance2.0fast"

        if refs:
            args = ["image2video", "--image", refs[0], "--prompt", prompt]
            args.extend(
                ["--duration", str(duration), "--video_resolution", video_resolution]
            )
        else:
            args = [
                "text2video",
                "--prompt",
                prompt,
                "--duration",
                str(duration),
                "--ratio",
                ratio,
            ]
            args.extend(["--video_resolution", video_resolution])
        args.extend(["--model_version", model_version])
        args.extend(["--poll", str(max(0, poll_seconds))])

        result = self._run_dreamina_mcp(
            args,
            timeout=max(180, poll_seconds + 90),
            media_kind="image2video" if refs else "video",
            prompt=prompt,
        )
        if not result.get("ok"):
            error_data = self._dreamina_error_data(result)
            raise UserFacingError(
                error_data["message"] or "Dreamina 即梦 MCP 视频生成失败",
                error_data,
            )

        video_url = self._dreamina_video_url(result)
        submit_id = str(result.get("submit_id") or "").strip()
        return {
            "request_mode": "dreamina_mcp",
            "video_id": submit_id or f"dreamina-{int(time.time())}",
            "video_url": video_url,
            "videos": [{"type": "url", "url": video_url}] if video_url else [],
            "request": {
                "provider": "Dreamina MCP",
                "model": str(payload.get("model") or "dreamina-mcp-video"),
                "ratio": ratio,
                "video_resolution": video_resolution,
                "model_version": model_version,
                "seconds": duration,
                "input_reference_count": len(refs),
            },
            "response": result,
        }

    def _run_dreamina_mcp(
        self,
        args: list[str],
        *,
        timeout: int,
        media_kind: str,
        prompt: str,
    ) -> dict[str, Any]:
        for path in (DC_ENGINES_ROOT, DC_AGENT_ROOT):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        try:
            from dc_engines.dreamina_mcp.runner import run_dreamina
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Dreamina MCP runner 导入失败: {exc}") from exc

        result = asyncio.run(
            run_dreamina(
                args,
                timeout=timeout,
                output_dir=str(DREAMINA_OUTPUT_DIR),
                download=True,
                media_kind=media_kind,  # type: ignore[arg-type]
                prompt=prompt,
            )
        )
        return result.to_dict()

    def _dreamina_error_data(self, result: dict[str, Any]) -> dict[str, Any]:
        parsed = result.get("parsed_json")
        first = parsed[0] if isinstance(parsed, list) and parsed else {}
        queue_info = first.get("queue_info") if isinstance(first, dict) else None
        message = str(
            result.get("error_hint") or result.get("fail_reason") or ""
        ).strip()
        logid = str(first.get("logid") or "").strip() if isinstance(first, dict) else ""
        submit_id = (
            str(result.get("submit_id") or first.get("submit_id") or "").strip()
            if isinstance(first, dict)
            else str(result.get("submit_id") or "").strip()
        )
        queue_name = ""
        if isinstance(queue_info, dict):
            debug_info = str(queue_info.get("debug_info") or "")
            match = re.search(r'"queue_name"\s*:\s*"([^"]+)"', debug_info)
            queue_name = match.group(1) if match else ""
        details = []
        if message:
            details.append(message)
        if logid:
            details.append(f"logid={logid}")
        if submit_id:
            details.append(f"submit_id={submit_id}")
        return {
            "message": "Dreamina 即梦生成失败：" + "；".join(details)
            if details
            else "Dreamina 即梦生成失败",
            "provider": "Dreamina MCP",
            "fail_reason": str(result.get("fail_reason") or ""),
            "error_hint": str(result.get("error_hint") or ""),
            "submit_id": submit_id,
            "logid": logid,
            "queue_name": queue_name,
            "gen_status": str(result.get("gen_status") or ""),
            "returncode": result.get("returncode"),
        }

    def _has_dreamina_mcp(self) -> bool:
        for path in (DC_ENGINES_ROOT, DC_AGENT_ROOT):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        try:
            from dc_engines.dreamina_cli import resolve_dreamina_executable

            return resolve_dreamina_executable() is not None
        except Exception:
            return False

    def _dreamina_ratio(self, payload: dict[str, Any], *, default: str) -> str:
        value = str(
            payload.get("aspect_ratio")
            or payload.get("ratio")
            or payload.get("aspectRatio")
            or ""
        ).strip()
        if not value:
            size = str(payload.get("size") or payload.get("resolution") or "").strip()
            if size in {"1536x1024", "1280x720", "720p", "1080p"}:
                value = "16:9"
            elif size in {"1024x1536", "720x1280"}:
                value = "9:16"
            elif size in {"1024x1024"}:
                value = "1:1"
        mapping = {
            "landscape": "16:9",
            "wide": "16:9",
            "square": "1:1",
            "portrait": "9:16",
            "vertical": "9:16",
        }
        return mapping.get(value, value or default)

    def _dreamina_image_resolution(self, payload: dict[str, Any]) -> str:
        value = (
            str(payload.get("resolution_type") or payload.get("size") or "2k")
            .strip()
            .lower()
        )
        if value == "1k":
            return "1k"
        return "2k"

    def _dreamina_video_resolution(self, payload: dict[str, Any]) -> str:
        value = (
            str(
                payload.get("video_resolution")
                or payload.get("resolution")
                or payload.get("size")
                or "720p"
            )
            .strip()
            .lower()
        )
        return value if value in {"720p", "1080p"} else "720p"

    def _dreamina_poll_seconds(self, env_name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(env_name, default)))
        except ValueError:
            return default

    def _dreamina_reference_files(self, raw: Any) -> list[str]:
        refs = self._normalize_references(raw)
        files: list[str] = []
        if not refs:
            return files
        DREAMINA_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        for index, ref in enumerate(refs, start=1):
            if ref.startswith(("http://", "https://")):
                raise UserFacingError(
                    "Dreamina MCP 参考图暂只支持本地文件或页面上传图片"
                )
            if ref.startswith("data:"):
                mime_type, data = split_data_url(ref)
                suffix = mimetypes.guess_extension(mime_type) or ".png"
                target = (
                    DREAMINA_INPUT_DIR
                    / f"dreamina_ref_{int(time.time())}_{index}{suffix}"
                )
                target.write_bytes(base64.b64decode(data))
                files.append(str(target.resolve()))
                continue
            path = Path(ref).expanduser().resolve()
            if not path.is_file():
                raise UserFacingError(f"Dreamina MCP 参考图文件不存在：{path}")
            files.append(str(path))
        return files

    def _dreamina_image_outputs(self, result: dict[str, Any]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        for path in result.get("downloaded_files") or []:
            mime_type = mimetypes.guess_type(str(path))[0] or ""
            if mime_type.startswith("image/"):
                data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                images.append({"type": "base64", "mime_type": mime_type, "data": data})
        for url in result.get("media_urls") or []:
            if re.search(r"\.(?:png|jpe?g|webp)(?:\?|$)", str(url), re.I):
                images.append({"type": "url", "url": str(url)})
        return images

    def _dreamina_video_url(self, result: dict[str, Any]) -> str:
        for path in result.get("downloaded_files") or []:
            mime_type = mimetypes.guess_type(str(path))[0] or ""
            if mime_type.startswith("video/"):
                return self._local_media_url(str(path))
        for url in result.get("media_urls") or []:
            if re.search(r"\.(?:mp4|mov|m4v)(?:\?|$)", str(url), re.I):
                return str(url)
        return ""

    def _local_media_url(self, path: str) -> str:
        query = urllib.parse.urlencode({"path": str(Path(path).expanduser().resolve())})
        return f"/api/local-media?{query}"

    def _safe_int(
        self,
        value: Any,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    def _generate_codex_image(
        self,
        payload: dict[str, Any],
        api_model: str,
    ) -> dict[str, Any]:
        token = self._read_codex_access_token()
        if not token:
            raise UserFacingError(
                "未找到 Codex OAuth token，请先在本机完成 codex login",
                {"needs_codex_login": True},
            )
        try:
            import openai
        except ImportError as exc:
            raise UserFacingError(
                "当前 Python 环境未安装 openai SDK，无法走 Codex OAuth 生图"
            ) from exc

        prompt = str(payload.get("prompt") or "").strip()
        size = self._codex_image_size(payload)
        quality = self._codex_image_quality(payload)
        content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
        for ref in self._normalize_references(payload.get("input_reference")):
            content.append(self._codex_input_image(ref))

        image_b64: str | None = None
        request_summary = {
            "provider": "Codex OAuth",
            "host_model": CODEX_CHAT_MODEL,
            "model": api_model or CODEX_IMAGE_MODEL,
            "prompt": prompt,
            "prompt_original": payload.get("_prompt_original") or prompt,
            "prompt_optimized": payload.get("_prompt_optimized") or prompt,
            "template": payload.get("_prompt_template"),
            "template_name": payload.get("_prompt_template_name"),
            "supporting_templates": payload.get("_prompt_supporting_templates") or [],
            "template_source": payload.get("_prompt_template_source"),
            "taxonomy_sources": payload.get("_prompt_taxonomy_sources") or [],
            "taxonomy_tags": payload.get("_prompt_taxonomy_tags") or [],
            "local_examples": payload.get("_prompt_local_examples") or [],
            "size": size,
            "quality": quality,
            "input_reference_count": max(0, len(content) - 1),
        }
        try:
            client = openai.OpenAI(
                api_key=token,
                base_url=CODEX_BASE_URL,
                default_headers=self._codex_cloudflare_headers(token),
                timeout=300.0,
            )
            with client.responses.stream(
                model=CODEX_CHAT_MODEL,
                store=False,
                instructions=CODEX_IMAGE_INSTRUCTIONS,
                input=[
                    {
                        "type": "message",
                        "role": "user",
                        "content": content,
                    }
                ],
                tools=[
                    {
                        "type": "image_generation",
                        "model": api_model or CODEX_IMAGE_MODEL,
                        "size": size,
                        "quality": quality,
                        "output_format": "png",
                        "background": "auto" if len(content) > 1 else "opaque",
                    }
                ],
                tool_choice={
                    "type": "allowed_tools",
                    "mode": "required",
                    "tools": [{"type": "image_generation"}],
                },
            ) as stream:
                for event in stream:
                    if getattr(event, "type", "") != "response.output_item.done":
                        continue
                    item = getattr(event, "item", None)
                    item_type = getattr(item, "type", None)
                    if item_type != "image_generation_call":
                        continue
                    result = getattr(item, "result", None)
                    if isinstance(result, str) and result:
                        image_b64 = result
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"GPT Image 2 (Codex OAuth) 调用失败: {exc}") from exc

        if not image_b64:
            raise RuntimeError("GPT Image 2 (Codex OAuth) 没返回图片数据")

        return {
            "request_mode": "codex",
            "request": request_summary,
            "response": {
                "provider": "codex-oauth",
                "model": api_model or CODEX_IMAGE_MODEL,
                "size": size,
                "quality": quality,
            },
            "images": [
                {
                    "type": "base64",
                    "mime_type": "image/png",
                    "data": image_b64,
                }
            ],
        }

    def _codex_image_size(self, payload: dict[str, Any]) -> str:
        explicit = str(payload.get("size") or "").strip()
        if explicit in {"1024x1024", "1536x1024", "1024x1536"}:
            return explicit
        ratio = str(
            payload.get("aspect_ratio")
            or payload.get("ratio")
            or payload.get("aspectRatio")
            or ""
        ).strip()
        return CODEX_IMAGE_SIZES.get(ratio, CODEX_IMAGE_SIZES["landscape"])

    def _codex_image_quality(self, payload: dict[str, Any]) -> str:
        quality = str(payload.get("quality") or "medium").strip().lower()
        return quality if quality in {"low", "medium", "high"} else "medium"

    def _codex_input_image(self, ref: str) -> dict[str, str]:
        if ref.startswith(("http://", "https://", "data:")):
            return {"type": "input_image", "image_url": ref}
        image_path = Path(ref).expanduser()
        image_bytes = image_path.read_bytes()
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
        }

    def _read_codex_access_token(self) -> str | None:
        try:
            if not CODEX_AUTH_PATH.exists():
                return None
            data = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
            tokens = data.get("tokens") if isinstance(data, dict) else None
            access_token = (
                tokens.get("access_token") if isinstance(tokens, dict) else None
            )
            if isinstance(access_token, str) and access_token.strip():
                return access_token.strip()
        except Exception:
            return None
        return None

    def _codex_cloudflare_headers(self, access_token: str) -> dict[str, str]:
        headers = {
            "User-Agent": "codex_cli_rs/0.0.0 (DC-Agent AIHUBMIX Standalone)",
            "originator": "codex_cli_rs",
        }
        try:
            parts = access_token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                acct_id = claims.get("https://api.openai.com/auth", {}).get(
                    "chatgpt_account_id"
                )
                if isinstance(acct_id, str) and acct_id:
                    headers["ChatGPT-Account-ID"] = acct_id
        except Exception:
            pass
        return headers

    def _get_json(self, url: str, api_key: str | None = None) -> dict[str, Any]:
        content = self._request_bytes("GET", url, api_key=api_key)
        try:
            return json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("AIHUBMIX 返回了非 JSON 响应") from exc

    def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        api_key: str,
        auth_header: str = "Authorization",
    ) -> dict[str, Any]:
        content = self._request_bytes(
            "POST",
            url,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            api_key=api_key,
            auth_header=auth_header,
        )
        try:
            return json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("AIHUBMIX 返回了非 JSON 响应") from exc

    def _get_bytes(self, url: str, api_key: str) -> tuple[bytes, str | None]:
        content, headers = self._request_bytes_with_headers("GET", url, api_key=api_key)
        return content, headers.get("content-type")

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        api_key: str | None = None,
        auth_header: str = "Authorization",
    ) -> bytes:
        content, _ = self._request_bytes_with_headers(
            method,
            url,
            body=body,
            api_key=api_key,
            auth_header=auth_header,
        )
        return content

    def _request_bytes_with_headers(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        api_key: str | None = None,
        auth_header: str = "Authorization",
    ) -> tuple[bytes, dict[str, str]]:
        headers = {
            "User-Agent": "DC-Agent AIHUBMIX Standalone/0.1",
            "Accept": "application/json, video/mp4, */*",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if api_key and auth_header == "Authorization":
            headers["Authorization"] = f"Bearer {api_key}"
        elif api_key:
            headers[auth_header] = api_key
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"AIHUBMIX HTTP {exc.code}: {detail[:500] or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"AIHUBMIX 请求失败：{exc.reason}") from exc

    def _extract_video_id(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        candidates = [
            payload.get("id"),
            payload.get("video_id"),
            payload.get("task_id"),
            data.get("id"),
            data.get("video_id"),
        ]
        for item in candidates:
            text = str(item or "").strip()
            if text:
                return text
        return None

    def _normalize_references(self, raw: Any) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("data:"):
            return [text]
        return [item.strip() for item in text.split(",") if item.strip()]

    def _extract_image_outputs(self, payload: Any) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []

        def add_url(value: Any) -> None:
            text = str(value or "").strip()
            if text:
                images.append({"type": "url", "url": text})

        def add_b64(value: Any, mime_type: str = "image/png") -> None:
            text = str(value or "").strip()
            if text:
                images.append({"type": "base64", "mime_type": mime_type, "data": text})

        if not isinstance(payload, dict):
            return images

        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            add_url(item.get("url"))
            add_b64(item.get("b64_json"), item.get("mime_type") or "image/png")

        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                parts = (
                    candidate.get("content", {}).get("parts", [])
                    if isinstance(candidate, dict)
                    else []
                )
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict):
                        add_b64(
                            inline.get("data"),
                            inline.get("mimeType")
                            or inline.get("mime_type")
                            or "image/png",
                        )
                    file_data = part.get("fileData") or part.get("file_data")
                    if isinstance(file_data, dict):
                        add_url(file_data.get("fileUri") or file_data.get("file_uri"))

        for key in ("url", "image_url", "output_url"):
            add_url(payload.get(key))
        return images

    def _redact_request(self, body: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in body.items() if key != "api_key"}


class UserFacingError(RuntimeError):
    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data or {}


def url_quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def safe_filename(video_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", video_id).strip("._") or "video"
    return f"aihubmix-{safe_id}.mp4"


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def split_data_url(value: str) -> tuple[str, str]:
    match = re.match(r"^data:([^;,]+)?(?:;base64)?,(.*)$", value, re.DOTALL)
    if not match:
        return "image/png", ""
    return match.group(1) or "image/png", match.group(2) or ""


class PreviewHandler(BaseHTTPRequestHandler):
    backend = AihubmixBackend()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if not self._is_public_path(path) and not self._has_valid_auth():
                self._send_auth_required()
                return
            if path in {"/", "/index.html", "/gallery", "/pages/gallery/index.html"}:
                self._send_file(GALLERY_HTML, "text/html; charset=utf-8")
                return
            if path in {"/dashboard", "/pages/dashboard/index.html"}:
                self._send_file(INDEX_HTML, "text/html; charset=utf-8")
                return
            if path in {"/writing", "/pages/writing/index.html"}:
                self._send_file(WRITING_HTML, "text/html; charset=utf-8")
                return
            if path in {"/video", "/pages/video/index.html"}:
                self._send_file(VIDEO_HTML, "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT)
                return
            if path == "/api/auth/status":
                self._send_ok(
                    {
                        "enabled": auth_enabled(),
                        "authenticated": self._has_valid_auth(),
                        "session_seconds": AUTH_SESSION_SECONDS,
                    }
                )
                return
            if path == "/api/health":
                self._send_ok(self.backend.health())
                return
            if path == "/api/models":
                self._send_ok(self.backend.models())
                return
            if path == "/api/image-models":
                self._send_ok(self.backend.image_models())
                return
            if path == "/api/download":
                query = urllib.parse.parse_qs(parsed.query)
                video_id = str((query.get("video_id") or [""])[0]).strip()
                if not video_id:
                    raise UserFacingError("缺少 video_id")
                content, content_type = self.backend.download(video_id)
                self._send_bytes(
                    content,
                    content_type or "video/mp4",
                    extra_headers={
                        "Content-Disposition": f'attachment; filename="{safe_filename(video_id)}"'
                    },
                )
                return
            if path == "/api/local-media":
                query = urllib.parse.parse_qs(parsed.query)
                media_path = str((query.get("path") or [""])[0]).strip()
                content, content_type = self.backend.local_media(media_path)
                self._send_bytes(content, content_type)
                return
            self._send_json(
                {"status": "error", "message": "Not found", "data": {}},
                HTTPStatus.NOT_FOUND,
            )
        except UserFacingError as exc:
            self._send_error_json(str(exc), exc.data, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(str(exc), {}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            payload = self._read_json()
            if path == "/api/auth/login":
                if not auth_enabled():
                    self._send_ok({"enabled": False, "authenticated": True})
                    return
                supplied = str(payload.get("token") or "")
                if not secrets.compare_digest(supplied, auth_token()):
                    self._send_error_json("访问口令不正确", {}, HTTPStatus.UNAUTHORIZED)
                    return
                cookie = self._session_cookie(sign_auth_session())
                self._send_json(
                    {
                        "status": "ok",
                        "message": None,
                        "data": {"enabled": True, "authenticated": True},
                    },
                    extra_headers={"Set-Cookie": cookie},
                )
                return
            if path == "/api/auth/logout":
                self._send_json(
                    {
                        "status": "ok",
                        "message": None,
                        "data": {"authenticated": False},
                    },
                    extra_headers={
                        "Set-Cookie": (
                            f"{AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                        )
                    },
                )
                return
            if not self._is_public_path(path) and not self._has_valid_auth():
                self._send_auth_required()
                return
            if path == "/api/generate":
                self._send_ok(self.backend.generate_video(payload))
                return
            if path == "/api/image-generate":
                self._send_ok(self.backend.generate_image(payload))
                return
            if path == "/api/prompt-optimize":
                self._send_ok(self.backend.optimize_prompt(payload))
                return
            if path == "/api/prompt-candidates":
                self._send_ok(self.backend.prompt_candidates(payload))
                return
            if path == "/api/video-prompt-candidates":
                self._send_ok(self.backend.video_prompt_candidates(payload))
                return
            if path == "/api/writing-generate":
                self._send_ok(self.backend.generate_writing(payload))
                return
            if path == "/api/status":
                self._send_ok(self.backend.status(payload))
                return
            self._send_json(
                {"status": "error", "message": "Not found", "data": {}},
                HTTPStatus.NOT_FOUND,
            )
        except UserFacingError as exc:
            self._send_error_json(str(exc), exc.data, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(str(exc), {}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise UserFacingError("请求体不是有效 JSON") from exc
        return value if isinstance(value, dict) else {}

    def _send_ok(self, data: Any) -> None:
        self._send_json({"status": "ok", "message": None, "data": data})

    def _is_public_path(self, path: str) -> bool:
        """Return whether a route is readable before login.

        Args:
            path: Normalized request path.

        Returns:
            True for shell, health, and auth bootstrap routes.
        """
        return path in {
            "/",
            "/index.html",
            "/gallery",
            "/pages/gallery/index.html",
            "/dashboard",
            "/pages/dashboard/index.html",
            "/writing",
            "/pages/writing/index.html",
            "/video",
            "/pages/video/index.html",
            "/favicon.ico",
            "/api/health",
            "/api/auth/status",
            "/api/auth/login",
            "/api/auth/logout",
        }

    def _has_valid_auth(self) -> bool:
        """Validate browser cookies or the NAS-internal service token.

        Returns:
            True when auth is disabled or either credential verifies.
        """
        if not auth_enabled():
            return True
        internal_token = self.headers.get("X-Dianchi-Toolbox-Token") or ""
        if internal_token and secrets.compare_digest(internal_token, auth_token()):
            return True
        cookie_header = self.headers.get("Cookie") or ""
        for part in cookie_header.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == AUTH_COOKIE_NAME:
                return verify_auth_session(value)
        return False

    def _session_cookie(self, value: str) -> str:
        """Build the HttpOnly session cookie header.

        Args:
            value: Signed session value.

        Returns:
            A Set-Cookie header value.
        """
        cookie = (
            f"{AUTH_COOKIE_NAME}={value}; Path=/; Max-Age={AUTH_SESSION_SECONDS}; "
            "HttpOnly; SameSite=Lax"
        )
        if os.environ.get("DIANCHI_TOOLBOX_AUTH_COOKIE_SECURE", "").strip():
            cookie += "; Secure"
        return cookie

    def _send_auth_required(self) -> None:
        """Return a JSON 401 response for protected routes."""
        self._send_json(
            {
                "status": "error",
                "message": "需要登录巅池 Agent 工具箱",
                "data": {"auth_required": True},
            },
            HTTPStatus.UNAUTHORIZED,
        )

    def _send_error_json(
        self,
        message: str,
        data: dict[str, Any],
        status: HTTPStatus,
    ) -> None:
        self._send_json({"status": "error", "message": message, "data": data}, status)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(
            content,
            "application/json; charset=utf-8",
            status=status,
            extra_headers=extra_headers,
        )

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json(
                {"status": "error", "message": "File not found", "data": {}},
                HTTPStatus.NOT_FOUND,
            )
            return
        self._send_bytes(path.read_bytes(), content_type)

    def _send_bytes(
        self,
        content: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        print(
            f"[{self.log_date_time_string()}] {self.address_string()} {format % args}"
        )


def find_port(start: int, end: int) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available local port in {start}-{end}")


def main() -> None:
    preferred_port = int(os.environ.get("AIHUBMIX_PREVIEW_PORT", DEFAULT_PORT))
    port = find_port(preferred_port, max(preferred_port, MAX_PORT))
    server = ThreadingHTTPServer((HOST, port), PreviewHandler)
    server.daemon_threads = True
    print(f"AIHUBMIX standalone preview: http://{HOST}:{port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AIHUBMIX standalone preview.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
