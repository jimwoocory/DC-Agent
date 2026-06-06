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
