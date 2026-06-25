from __future__ import annotations

import json
from pathlib import Path

from dc_engines.department_workflows.memory_profiles import (
    load_department_memory_profiles,
    matching_department_memory_profiles,
)


def test_default_profiles_cover_content_planning_and_client() -> None:
    profiles = load_department_memory_profiles()

    assert {profile.profile_id for profile in profiles} >= {
        "content_director_script_workflow",
        "planning_content_sop_workflow",
        "execution_delivery_workflow",
        "design_delivery_workflow",
        "film_production_delivery_workflow",
        "ai_application_workflow",
        "brand_publicity_ops_workflow",
        "client_touchpoint_workflow",
    }


def test_matching_profiles_for_planning_and_client_requests() -> None:
    planning = matching_department_memory_profiles(
        "中台策划帮我做一版活动传播方案和视频分镜",
    )
    client = matching_department_memory_profiles(
        "客户那边要一套老客户邀约话术和私域文案",
    )

    assert planning[0].department_id == "planning"
    assert "分镜" in planning[0].tone_template
    assert client[0].department_id == "client_dept"
    assert "不得编造优惠" in client[0].tone_template


def test_matching_profiles_for_expanded_planning_memory_scenarios() -> None:
    account = matching_department_memory_profiles(
        "中台账号运营要怎么设计栏目和直播节奏",
    )
    gift = matching_department_memory_profiles("策划给缤果S做一套礼品物料创意")

    assert account[0].department_id == "planning"
    assert gift[0].department_id == "planning"


def test_matching_profile_for_execution_delivery_workflow() -> None:
    execution = matching_department_memory_profiles(
        "活动统筹部需要整理场地物料安装点检和验收材料",
    )

    assert execution[0].department_id == "execution_ops"
    assert "验收材料" in execution[0].tone_template


def test_matching_profiles_for_split_execution_departments() -> None:
    design = matching_department_memory_profiles("设计部检查设计稿VI和尺寸比例")
    film = matching_department_memory_profiles("影视制作部整理拍摄通告和成片交付")
    ai_app = matching_department_memory_profiles("AI应用部配置部门小助手和知识库接入")

    assert design[0].department_id == "design_dept"
    assert film[0].department_id == "film_production"
    assert ai_app[0].department_id == "ai_application"


def test_matching_profile_for_brand_publicity_ops_without_stealing_planning() -> None:
    brand = matching_department_memory_profiles(
        "品宣部整理媒介KOC任务下发和社群舆情复盘",
    )
    planning = matching_department_memory_profiles(
        "中台账号运营要怎么设计栏目和直播节奏",
    )

    assert brand[0].department_id == "brand_publicity"
    assert "柳汽是外派独立分支" in brand[0].tone_template
    assert planning[0].department_id == "planning"


def test_brand_or_vehicle_words_do_not_select_content_director_profile() -> None:
    matches = matching_department_memory_profiles(
        "五菱和柳汽新能源用户观察，顺便看看风行相关讨论",
    )

    assert all(
        profile.department_id != "execution_content_director" for profile in matches
    )


def test_external_profile_config_extends_default_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "department_memory_profiles.json"
    config_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "profile_id": "finance_budget_workflow",
                        "department_id": "finance",
                        "display_name": "财务部",
                        "aliases": ["财务部", "财务"],
                        "trigger_keywords": ["预算", "报销"],
                        "tone_template": "涉及预算和报销时，先核对金额、票据和审批口径。",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    matches = matching_department_memory_profiles(
        "财务帮我看一下报销预算怎么写",
        config_path=config_path,
    )

    assert matches[0].profile_id == "finance_budget_workflow"
    assert matches[0].department_id == "finance"
