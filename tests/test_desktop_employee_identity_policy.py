import json
from pathlib import Path

OVERRIDES = Path("data/config/employee_identity_overrides.json")


def test_company_has_one_boss_and_cai_ting_is_manager_with_separate_admin_permission() -> (
    None
):
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8"))["employees"]
    by_name = {item["display_name"]: item for item in overrides}

    assert [
        item["display_name"] for item in overrides if item["relation_type"] == "boss"
    ] == ["杨国民"]
    assert by_name["蔡挺"]["relation_type"] == "manager"
    assert by_name["蔡挺"]["department"] == "AI应用部"
    assert by_name["蔡挺"]["role"] == "AI应用部门负责人"
    assert "system_role" not in by_name["蔡挺"]
