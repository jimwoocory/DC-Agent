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
            "文案",
            "混剪",
            "五菱",
            "缤果",
            "星光",
            "柳汽",
            "风行",
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
        display_name="中台-策划",
        aliases=("中台-策划", "中台策划", "策划", "策划部", "策略部", "策略"),
        trigger_keywords=(
            "方案",
            "策划",
            "传播",
            "创意",
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
            "处理中台-策划任务时，先拆目标、受众、平台、素材和约束，再输出"
            "创意 brief、传播主线、内容结构、分镜/脚本和执行清单；涉及客户、"
            "品牌规范、车型卖点或历史项目结论时必须标注来源，不把未提供信息当事实。"
        ),
    ),
    DepartmentMemoryProfile(
        profile_id="client_touchpoint_workflow",
        department_id="client_dept",
        display_name="客户部",
        aliases=("客户部", "客户", "客户那边", "客户侧", "业务部", "市场部"),
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
