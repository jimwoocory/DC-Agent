"""Department-level memory trigger profiles for runtime assistant guidance."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DC_ROOT = Path("/Users/dianchi/DC-Agent")
DEFAULT_PROFILE_CONFIG_PATH = (
    DEFAULT_DC_ROOT / "data" / "config" / "department_memory_profiles.json"
)


@dataclass(frozen=True, slots=True)
class DepartmentMemoryProfile:
    profile_id: str
    department_id: str
    display_name: str
    aliases: tuple[str, ...]
    trigger_keywords: tuple[str, ...]
    tone_template: str


DEFAULT_DEPARTMENT_MEMORY_PROFILES: tuple[DepartmentMemoryProfile, ...] = (
    DepartmentMemoryProfile(
        profile_id="content_director_script_workflow",
        department_id="execution_content_director",
        display_name="执行部影视编导",
        aliases=("执行部影视编导", "编导", "影视编导", "执行部"),
        trigger_keywords=(
            "视频",
            "脚本",
            "分镜",
            "拍摄",
            "混剪",
            "剪辑",
            "口播",
            "镜头",
            "成片",
            "拍摄脚本",
        ),
        tone_template=(
            "处理汽车短视频任务时，优先给可落地方案：明确竖屏/横屏、时长、"
            "镜号、画面、台词、道具和发布文案；减少纯口播，强调轻量化拍摄；"
            "涉及官方账号或汽车质量表述时，主动规避误导消费者和安全风险。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="planning_content_sop_workflow",
        department_id="planning",
        display_name="中台-策略部",
        aliases=(
            "中台-策略部",
            "中台策略部",
            "中台-策略",
            "中台策略",
            "中台策划",
            "策划",
            "策划部",
            "策略部",
            "策略",
        ),
        trigger_keywords=(
            "方案",
            "策划",
            "传播",
            "创意",
            "方案框架",
            "框架搭建",
            "竞品",
            "行业趋势",
            "海报思路",
            "视觉 brief",
            "brief",
            "活动",
            "短视频",
            "分镜",
            "脚本",
            "提案",
            "选题",
            "内容中心",
            "用户运营",
            "企微内容库",
            "账号运营",
            "栏目",
            "直播",
            "礼品",
            "物料",
            "客户活动",
            "答谢会",
            "家宴",
            "研讨会",
        ),
        tone_template=(
            "处理中台-策略部任务时，优先围绕新项目给创意、方案框架、文案方向、"
            "海报思路、视觉 brief、短视频分镜和脚本；竞品、行业趋势、平台数据等"
            "需要标注来源和可信度；历史方案只在案例参考、复盘、集锦或风格校准时调用，"
            "不默认做深度全库检索。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="execution_delivery_workflow",
        department_id="execution_ops",
        display_name="执行部门",
        aliases=(
            "执行部门",
            "执行部",
            "执行运营",
            "项目执行",
            "活动执行",
            "活动统筹部",
            "活动统筹",
        ),
        trigger_keywords=(
            "执行分工表",
            "项目启动会",
            "落地沟通会",
            "场地",
            "勘场",
            "物料",
            "安装",
            "点检",
            "验收材料",
            "礼品打样",
            "生产",
            "入库",
            "出库",
            "库存",
        ),
        tone_template=(
            "处理执行部门任务时，优先输出任务拆解、负责人矩阵、时间排期、"
            "点检清单、风险预案、验收材料和待确认项；不替客户部承诺预算、"
            "合同、价格或回款，不替策略部做创意定调。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="design_delivery_workflow",
        department_id="design_dept",
        display_name="设计部",
        aliases=("设计部", "主设", "平面设计", "视觉设计"),
        trigger_keywords=(
            "设计稿",
            "VI",
            "色调",
            "字体",
            "尺寸比例",
            "制作文件",
            "源文件",
            "排版",
            "视觉检查",
        ),
        tone_template=(
            "处理设计部任务时，优先检查创意是否偏离、色调/VI/字体/背景/尺寸比例"
            "是否符合品牌和制作要求，并输出修改清单、制作风险和交付文件清单；"
            "不替甲方或项目负责人作最终通过结论。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="film_production_delivery_workflow",
        department_id="film_production",
        display_name="影视制作部",
        aliases=("影视制作部", "影视", "影视制作", "编导", "拍摄", "后期", "剪辑"),
        trigger_keywords=(
            "拍摄通告",
            "机位",
            "道化服",
            "现场拍摄",
            "跟拍",
            "后期剪辑",
            "成片",
            "成片交付",
            "验收材料",
        ),
        tone_template=(
            "处理影视制作部任务时，优先落到拍摄通告、机位安排、现场拍摄清单、"
            "后期交付计划和验收材料；拍摄时间、地点、人员、机位数量和交付标准"
            "必须来自确认材料。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="ai_application_workflow",
        department_id="ai_application",
        display_name="AI应用部",
        aliases=("AI应用部", "AI应用", "ai应用部", "数字化应用部", "数字化应用"),
        trigger_keywords=(
            "AI工具",
            "自动化",
            "工作流",
            "知识库接入",
            "机器人配置",
            "小助手配置",
            "部门小助手",
            "使用培训",
            "权限",
        ),
        tone_template=(
            "处理 AI 应用部任务时，先梳理部门场景、现有资料、权限和数据边界，"
            "再输出 AI 工具配置、自动化工作流、知识库接入方案、上线检查清单和培训要点；"
            "不得承诺未验证的接口能力或数据访问权限。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="brand_publicity_ops_workflow",
        department_id="brand_publicity",
        display_name="品宣部",
        aliases=(
            "品宣部",
            "品宣",
            "品牌宣传",
            "宣发",
            "运营部",
            "媒介",
            "KOC",
            "用户故事",
            "企微",
            "社群",
            "舆情",
        ),
        trigger_keywords=(
            "直播运营",
            "账号运营",
            "内容运营",
            "起号",
            "爆品",
            "引流品",
            "周复盘",
            "月复盘",
            "任务下发",
            "需求管理表",
            "日报",
            "反馈闭环",
            "沟通脚本",
            "甲方审核",
            "成片审核",
            "风险预警",
            "负面稀释",
            "用户证言",
        ),
        tone_template=(
            "处理品宣部任务时，默认公司内品宣团队统一承接直播/账号内容运营、"
            "媒介/KOC、用户故事、用户活动、企微、社群和舆情相关工作；先输出"
            "任务表、节奏、KPI、反馈闭环和风险项，不把柳汽拆成公司内自动路由。"
            "柳汽是外派独立分支，只在用户明确提到并提供材料时作为项目/客户上下文。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="client_touchpoint_workflow",
        department_id="client_dept",
        display_name="客户部",
        aliases=(
            "客户部",
            "中台客户部",
            "中台-客户",
            "中台客户",
            "客户",
            "客户那边",
            "客户侧",
            "业务部",
            "市场部",
        ),
        trigger_keywords=(
            "客户",
            "邀约",
            "触达",
            "回访",
            "复联",
            "私域",
            "话术",
            "老客户",
            "高意向",
            "活动报名",
            "续约",
        ),
        tone_template=(
            "处理客户侧任务时，优先输出可直接发给客户或渠道的短话术，并区分"
            "微信/私域/飞书等触达场景；不得编造优惠、价格、权益、客户身份、"
            "到店承诺或合作结果；涉及活动信息时先确认时间、地点、权益和适用范围。"
        ),
    ),
)


def load_department_memory_profiles(
    config_path: Path | str | None = None,
) -> list[DepartmentMemoryProfile]:
    """Load built-in profiles plus optional hot config extensions."""

    profiles = list(DEFAULT_DEPARTMENT_MEMORY_PROFILES)
    path = Path(config_path) if config_path is not None else DEFAULT_PROFILE_CONFIG_PATH
    profiles.extend(_load_config_profiles(path))
    return _dedupe_profiles(profiles)


def matching_department_memory_profiles(
    text: str,
    *,
    config_path: Path | str | None = None,
    limit: int = 3,
) -> list[DepartmentMemoryProfile]:
    """Return department profiles whose aliases or trigger keywords match text."""

    if limit < 1:
        return []
    normalized = _compact(text)
    if not normalized:
        return []
    scored: list[tuple[int, DepartmentMemoryProfile]] = []
    for profile in load_department_memory_profiles(config_path):
        score = _profile_match_score(profile, normalized)
        if score > 0:
            scored.append((score, profile))
    scored.sort(key=lambda item: (-item[0], item[1].profile_id))
    return [profile for _, profile in scored[:limit]]


def _load_config_profiles(path: Path) -> list[DepartmentMemoryProfile]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(raw_profiles, list):
        return []
    profiles: list[DepartmentMemoryProfile] = []
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        profile = _profile_from_dict(item)
        if profile is not None:
            profiles.append(profile)
    return profiles


def _profile_from_dict(item: dict[str, Any]) -> DepartmentMemoryProfile | None:
    profile_id = str(item.get("profile_id") or "").strip()
    department_id = str(item.get("department_id") or "").strip()
    display_name = str(item.get("display_name") or department_id).strip()
    tone_template = str(item.get("tone_template") or "").strip()
    if not profile_id or not department_id or not display_name or not tone_template:
        return None
    return DepartmentMemoryProfile(
        profile_id=profile_id,
        department_id=department_id,
        display_name=display_name,
        aliases=_string_tuple(item.get("aliases")),
        trigger_keywords=_string_tuple(item.get("trigger_keywords")),
        tone_template=tone_template,
    )


def _profile_match_score(profile: DepartmentMemoryProfile, text: str) -> int:
    score = 0
    for alias in profile.aliases:
        if alias and alias in text:
            score += 4
    for keyword in profile.trigger_keywords:
        if keyword and re.search(re.escape(keyword), text, flags=re.IGNORECASE):
            score += 1
    return score


def _dedupe_profiles(
    profiles: list[DepartmentMemoryProfile],
) -> list[DepartmentMemoryProfile]:
    deduped: dict[str, DepartmentMemoryProfile] = {}
    for profile in profiles:
        deduped[profile.profile_id] = profile
    return list(deduped.values())


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list | tuple):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")
