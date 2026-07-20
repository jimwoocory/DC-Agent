from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from dc_engines.assistant_workbench_cards import (
    TASK_LABELS,
    TASK_REQUIRED_FIELDS,
    build_assistant_attachment_demo_card,
    build_assistant_session_choice_card,
    build_assistant_task_confirmation_card,
    build_assistant_task_intake_card,
    build_assistant_task_list_card,
    build_assistant_task_submitted_card,
    build_assistant_task_workspace_card,
    build_codex_toolbox_card,
)
from dc_engines.card_system import CARD_REGISTRY

from astrbot.api.event import MessageEventResult

ROOT = Path(__file__).resolve().parents[2]
PLUGINS_PARENT = ROOT / "data" / "plugins"
if str(PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_PARENT))
dc_router_package = types.ModuleType("dc_router")
dc_router_package.__path__ = [str(PLUGINS_PARENT / "dc_router")]
dc_router_package.__file__ = str(PLUGINS_PARENT / "dc_router" / "__init__.py")
sys.modules["dc_router"] = dc_router_package

assistant_workbench = importlib.import_module(
    "dc_router.preprocessing.assistant_workbench"
)
card_action = importlib.import_module("dc_router.preprocessing.card_action")
session_choice = importlib.import_module("dc_router.preprocessing.session_choice")


def _event(text: str = "") -> MagicMock:
    event = MagicMock()
    event.message_str = text
    event.unified_msg_origin = "巅池-Agent小助手:FriendMessage:ou_test"
    event.message_obj = SimpleNamespace(
        is_card_action=False,
        card_action_payload=None,
        message_id="om_source",
    )
    event.get_platform_name.return_value = "lark"
    event.get_platform_id.return_value = "巅池-Agent小助手"
    event.get_group_id.return_value = ""
    event.get_sender_id.return_value = ""
    extras: dict[str, object] = {}
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value: extras.__setitem__(key, value)
    event.should_call_llm = MagicMock()
    event.stop_event = MagicMock()
    event.set_result = MagicMock()
    return event


def _buttons(card: dict) -> list[dict]:
    buttons: list[dict] = []

    def visit(element: dict) -> None:
        if element.get("tag") == "button":
            buttons.append(element)
        for child in element.get("elements", []):
            visit(child)
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                visit(child)

    for element in card["body"]["elements"]:
        visit(element)
    return buttons


def _card_text(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)


def test_workspace_base_url_prefers_public_https_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DC_ASSISTANT_H5_ORIGIN",
        "https://workbench.example.com/",
    )

    assert assistant_workbench._workspace_base_url() == "https://workbench.example.com"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("auto", "生图模型：自动（Image2 优先，即梦兜底）"),
        ("image2", "生图模型：Image2（仅使用）"),
        ("dreamina", "生图模型：即梦（仅使用）"),
    ],
)
def test_image_workspace_provider_choice_survives_router_resume_text(
    provider: str,
    expected: str,
) -> None:
    text = assistant_workbench._task_resume_text(
        "image",
        {"visual_prompt": "新车发布海报", "model_choice": provider},
    )

    assert expected in text


def test_contract_matches_runtime_aliases_and_card_registry() -> None:
    contract = json.loads(
        Path("harness/contracts/feishu_assistant_workbench.json").read_text(
            encoding="utf-8"
        )
    )

    assert contract["published_menu"] == {
        "创作": ["写文案/方案", "生成图片", "生成视频"],
        "办公": ["查资料/分析", "处理文件", "物料报价", "AI转CDR"],
        "任务": ["继续最近", "进行中", "待补充", "已完成"],
    }
    assert contract["network_access"] == {
        "shared_origin": "http://192.168.1.35:6185 during LAN phase; HTTPS origin configured by DC_ASSISTANT_H5_ORIGIN after domain cutover",
        "origin_environment": "DC_ASSISTANT_H5_ORIGIN",
        "runtime_host": "NAS",
        "ingress": "NAS LAN forward during phase 1; Tencent Cloud domestic reverse tunnel after domain cutover",
        "employee_entry": "http://192.168.1.35:6185",
        "container_binding": "127.0.0.1:6285 -> dc-agent:6185",
        "future_origin": "https://workbench.gx-dianchi.cn",
        "public_nas_port_required": False,
        "entrypoints": [
            "Feishu web",
            "Feishu desktop client",
            "company desktop client",
        ],
        "covered_h5_tasks": [
            "copy",
            "image",
            "video",
            "research",
            "file",
            "quotation",
            "ai_cdr",
            "codex",
        ],
        "must_not_depend_on": [
            "employee Mac IP",
            "employee Mac process",
            "manual local download",
            "direct inbound NAS port",
        ],
    }
    assert set(contract["menu_aliases"]["legacy_workbench"]) == set(
        assistant_workbench.WORKBENCH_MENU_ALIASES
    )
    assert set(contract["menu_aliases"]["assistant_task_list"]) == set(
        assistant_workbench.TASK_LIST_MENU_ALIASES
    )
    assert set(contract["menu_aliases"]["codex_tools"]) == set(
        assistant_workbench.CODEX_TOOL_MENU_ALIASES
    )
    assert set(contract["card_types"]).issubset(CARD_REGISTRY)


def test_all_workbench_cards_are_card_json_v2_and_have_callbacks() -> None:
    cards = [
        build_assistant_task_intake_card(),
        build_assistant_task_intake_card(task_type="image"),
        build_assistant_task_intake_card(task_type="video"),
        build_assistant_task_intake_card(task_type="codex"),
        build_assistant_task_confirmation_card(),
        build_assistant_task_list_card(),
        build_assistant_session_choice_card("decision-1"),
        build_codex_toolbox_card(),
    ]

    assert all(card["schema"] == "2.0" for card in cards)
    assert all(card["body"]["elements"] for card in cards)
    for card in cards:
        assert _buttons(card)
        assert all(
            button.get("form_action_type") == "submit"
            or button.get("behaviors", [{}])[0].get("value", {}).get("source")
            == "assistant_workbench"
            for button in _buttons(card)
        )


def test_session_choice_card_has_two_closed_loop_actions_and_terminal_states() -> None:
    pending = build_assistant_session_choice_card("decision-1")
    buttons = _buttons(pending)

    assert pending["schema"] == "2.0"
    assert [button["text"]["content"] for button in buttons] == [
        "开启新对话",
        "继续当前对话",
    ]
    assert [button["behaviors"][0]["value"]["action"] for button in buttons] == [
        "open_new_session",
        "continue_current_session",
    ]
    assert all(
        button["behaviors"][0]["value"]["decision_id"] == "decision-1"
        for button in buttons
    )

    opened = build_assistant_session_choice_card("decision-1", "new_conversation")
    continued = build_assistant_session_choice_card("decision-1", "continue_current")
    assert opened["header"]["title"]["content"] == "新对话已开启"
    assert continued["header"]["title"]["content"] == "已继续当前对话"
    assert _buttons(opened) == []
    assert _buttons(continued) == []


@pytest.mark.parametrize(
    ("copy_type", "label"),
    [
        ("xiaohongshu_note", "小红书图文笔记"),
        ("douyin_script", "抖音短视频脚本"),
        ("xigua_script", "西瓜视频长视频脚本"),
    ],
)
def test_copy_form_and_confirmation_keep_platform_specific_types(
    copy_type: str,
    label: str,
) -> None:
    intake = build_assistant_task_intake_card(task_type="copy")
    confirmation = build_assistant_task_confirmation_card(
        task_type="copy",
        task_data={
            "copy_type": copy_type,
            "task_request": "围绕新品发布制作平台内容",
        },
    )

    assert label in _card_text(intake)
    assert label in _card_text(confirmation)


def test_attachment_demo_card_has_one_real_material_action() -> None:
    card = build_assistant_attachment_demo_card(
        app_id="cli_test",
        upload_url="http://127.0.0.1:6185/api/v1/assistant-attachments/token-1",
    )
    buttons = _buttons(card)

    assert len(buttons) == 1
    assert buttons[0]["text"]["content"] == "添加资料"
    assert buttons[0].get("disabled") is not True
    assert buttons[0]["behaviors"][0]["type"] == "open_url"
    assert buttons[0]["behaviors"][0]["default_url"] == (
        "http://127.0.0.1:6185/api/v1/assistant-attachments/token-1"
    )
    assert (
        buttons[0]["behaviors"][0]["pc_url"]
        == buttons[0]["behaviors"][0]["android_url"]
    )
    assert (
        "applink.feishu.cn/client/web_app/open"
        in buttons[0]["behaviors"][0]["android_url"]
    )
    assert "appId=cli_test" in buttons[0]["behaviors"][0]["android_url"]
    assert "mode=sidebar-semi" in buttons[0]["behaviors"][0]["ios_url"]
    assert "min_width=" not in buttons[0]["behaviors"][0]["android_url"]
    assert "max_width=" not in buttons[0]["behaviors"][0]["android_url"]
    assert "lk_target_url=" in buttons[0]["behaviors"][0]["android_url"]
    assert "source%3Ddocs" not in buttons[0]["behaviors"][0]["android_url"]


def test_task_workspace_card_has_recoverable_fixed_sidebar_action() -> None:
    card = build_assistant_task_workspace_card(
        task_type="image",
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/api/v1/assistant-attachments/task-1",
    )
    buttons = _buttons(card)

    assert len(buttons) == 2
    assert buttons[0]["text"]["content"] == "打开工作台"
    assert buttons[0]["behaviors"][0]["type"] == "open_url"
    assert buttons[0]["behaviors"][0]["default_url"] == (
        "http://127.0.0.1:6185/api/v1/assistant-attachments/task-1"
    )
    assert (
        buttons[0]["behaviors"][0]["pc_url"]
        == buttons[0]["behaviors"][0]["android_url"]
    )
    assert "mode=sidebar-semi" in buttons[0]["behaviors"][0]["android_url"]
    assert "window-semi" not in buttons[0]["behaviors"][0]["android_url"]
    assert buttons[1]["text"]["content"] == "刷新安全入口"
    assert buttons[1]["behaviors"] == [
        {
            "type": "callback",
            "value": {
                "source": "assistant_workbench",
                "action": "select_task_type",
                "task_type": "image",
            },
        }
    ]
    assert card["header"]["title"]["content"] == "生成图片"


@pytest.mark.parametrize("task_type", TASK_LABELS)
def test_every_published_h5_workspace_keeps_its_card_recovery_action(
    task_type: str,
) -> None:
    card = build_assistant_task_workspace_card(
        task_type=task_type,
        app_id="cli_test",
        workspace_url=f"http://127.0.0.1:6185/{task_type}",
    )
    buttons = _buttons(card)

    assert buttons[0]["behaviors"][0]["type"] == "open_url"
    assert buttons[0]["behaviors"][0]["default_url"] == (
        f"http://127.0.0.1:6185/{task_type}"
    )
    assert (
        buttons[0]["behaviors"][0]["pc_url"]
        == buttons[0]["behaviors"][0]["android_url"]
    )
    assert "mode=sidebar-semi" in buttons[0]["behaviors"][0]["pc_url"]
    assert buttons[-1]["text"]["content"] in {
        "刷新安全入口",
        "刷新报价入口",
    }
    assert buttons[-1]["behaviors"][0]["value"] == {
        "source": "assistant_workbench",
        "action": "select_task_type",
        "task_type": task_type,
    }


@pytest.mark.parametrize("task_type", ["copy", "image", "video"])
def test_creative_submitted_card_reopens_h5_before_regeneration(
    task_type: str,
) -> None:
    card = build_assistant_task_submitted_card(
        task_type=task_type,
        task_data={
            TASK_REQUIRED_FIELDS[task_type]: "保留原始资料，调整生成要求",
        },
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/api/v1/assistant-attachments/task-1",
    )
    text = _card_text(card)
    buttons = _buttons(card)

    assert card["header"]["title"]["content"] == "任务已提交"
    assert "后台生成并直接交付" in text
    assert "人工审核" not in text
    assert "确认定稿" not in text
    assert "退回修改" not in text
    assert [button["text"]["content"] for button in buttons] == [
        "打开 H5 修改资料",
        "刷新安全入口",
    ]
    assert buttons[0]["behaviors"][0]["type"] == "open_url"
    assert parse_qs(urlsplit(buttons[0]["behaviors"][0]["default_url"]).query) == {
        "mode": ["revise"]
    }
    app_link_query = parse_qs(urlsplit(buttons[0]["behaviors"][0]["android_url"]).query)
    assert (
        buttons[0]["behaviors"][0]["pc_url"]
        == buttons[0]["behaviors"][0]["android_url"]
    )
    assert app_link_query["reload"] == ["true"]
    revision_url = app_link_query["lk_target_url"][0]
    assert parse_qs(urlsplit(revision_url).query)["mode"] == ["revise"]
    assert "打开 H5 不会自动执行" in text
    assert "确认修改并再次生成" in text
    assert buttons[1]["behaviors"][0]["value"] == {
        "source": "assistant_workbench",
        "action": "select_task_type",
        "task_type": task_type,
    }


@pytest.mark.parametrize("task_type", ["research", "file", "codex"])
def test_office_and_executor_submitted_cards_do_not_offer_regeneration(
    task_type: str,
) -> None:
    card = build_assistant_task_submitted_card(
        task_type=task_type,
        task_data={TASK_REQUIRED_FIELDS[task_type]: "已提交的任务要求"},
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/api/v1/assistant-attachments/task-1",
    )
    text = _card_text(card)
    buttons = _buttons(card)

    assert "补充要求" not in text
    assert "重新生成" not in text
    assert [button["text"]["content"] for button in buttons] == ["刷新安全入口"]
    assert buttons[0]["behaviors"][0]["value"] == {
        "source": "assistant_workbench",
        "action": "select_task_type",
        "task_type": task_type,
    }


def test_ai_cdr_submitted_card_only_reopens_conversion_progress() -> None:
    card = build_assistant_task_submitted_card(
        task_type="ai_cdr",
        task_data={"source_name": "品牌主视觉.ai"},
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/api/v1/assistant-attachments/task-1",
    )
    text = _card_text(card)
    buttons = _buttons(card)

    assert "重新生成" not in text
    assert [button["text"]["content"] for button in buttons] == [
        "查看转换进度",
        "刷新安全入口",
    ]
    behavior = buttons[0]["behaviors"][0]
    assert parse_qs(urlsplit(behavior["default_url"]).query) == {"mode": ["progress"]}
    app_link_query = parse_qs(urlsplit(behavior["android_url"]).query)
    assert behavior["pc_url"] == behavior["android_url"]
    assert app_link_query["reload"] == ["true"]
    progress_url = app_link_query["lk_target_url"][0]
    assert parse_qs(urlsplit(progress_url).query)["mode"] == ["progress"]


def test_office_workspace_entry_cards_explain_distinct_capabilities() -> None:
    research = _card_text(
        build_assistant_task_workspace_card(
            task_type="research",
            app_id="cli_test",
            workspace_url="http://127.0.0.1:6185/research",
        )
    )
    file_processing = _card_text(
        build_assistant_task_workspace_card(
            task_type="file",
            app_id="cli_test",
            workspace_url="http://127.0.0.1:6185/file",
        )
    )

    assert "检索、查证与分析" in research
    assert "研究问题 → 参考资料 → 分析交付" in research
    assert "总结、提取、改写、转换与对比" in file_processing
    assert "处理目标 → 选择文件 → 输出规则" in file_processing
    assert research != file_processing


def test_material_quotation_entry_opens_a_dedicated_system() -> None:
    card = build_assistant_task_workspace_card(
        task_type="quotation",
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/quotation",
    )
    text = _card_text(card)
    button = _buttons(card)[0]

    assert card["header"]["title"]["content"] == "筹备组物料报价"
    assert "报价工具首页" in text
    assert "项目估价测算" in text
    assert "新增合作公司价格" in text
    assert button["text"]["content"] == "打开报价工具"
    assert button["behaviors"][0]["default_url"] == ("http://127.0.0.1:6185/quotation")
    assert button["behaviors"][0]["pc_url"] == button["behaviors"][0]["android_url"]
    assert "mode=sidebar-semi" in button["behaviors"][0]["android_url"]


def test_ai_cdr_entry_opens_the_nas_batch_workspace() -> None:
    card = build_assistant_task_workspace_card(
        task_type="ai_cdr",
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/ai-cdr",
    )
    text = _card_text(card)
    button = _buttons(card)[0]

    assert card["header"]["title"]["content"] == "AI 转 CDR"
    assert "批量上传、颜色校准与转换进度" in text
    assert "CMYK 数值校准" in text
    assert "mini4 按队列顺序转换" in text
    assert button["text"]["content"] == "打开 AI 转 CDR 工具"
    assert button["behaviors"][0]["default_url"] == "http://127.0.0.1:6185/ai-cdr"
    assert button["behaviors"][0]["pc_url"] == button["behaviors"][0]["android_url"]
    assert "mode=sidebar-semi" in button["behaviors"][0]["android_url"]


def test_market_estimate_result_card_hides_internal_quote_details() -> None:
    card = build_assistant_task_submitted_card(
        task_type="quotation",
        task_data={
            "project_name": "柳州用户共创会",
            "client_name": "五菱",
            "grand_total": "106000.00",
            "validity_days": "15",
            "quotation_status": "估价方案",
            "quotation_items": '[{"item_name":"背胶","supplier":"柳州东成广告"}]',
            "profit_fee": "4321.98",
        },
        app_id="cli_test",
        workspace_url="http://127.0.0.1:6185/quotation",
    )
    text = _card_text(card)

    assert card["header"]["title"]["content"] == "市场部估价结果"
    assert "柳州用户共创会" in text
    assert "¥106000.00" in text
    assert "15 天" in text
    assert "估价方案" in text
    assert "背胶" not in text
    assert "柳州东成广告" not in text
    assert "4321.98" not in text
    assert _buttons(card)[-1]["behaviors"][0]["value"] == {
        "source": "assistant_workbench",
        "action": "select_task_type",
        "task_type": "quotation",
    }


def test_market_estimate_card_links_only_sanitized_deliverables() -> None:
    card = build_assistant_task_submitted_card(
        task_type="quotation",
        task_data={
            "project_name": "柳州用户共创会",
            "grand_total": "106000.00",
            "validity_days": "15",
        },
        app_id="cli_test",
        workspace_url="https://example.com/workspace",
        deliverables={
            "internal_xlsx": "https://example.com/private.xlsx",
            "market_docx": "https://example.com/market.docx",
            "market_pdf": "https://example.com/market.pdf",
        },
    )
    serialized = json.dumps(card, ensure_ascii=False)

    assert "下载 Word 估价" in serialized
    assert "下载 PDF 估价" in serialized
    assert "https://example.com/market.docx" in serialized
    assert "https://example.com/market.pdf" in serialized
    assert "private.xlsx" not in serialized


def test_attachment_demo_card_renders_truthful_uploaded_file() -> None:
    card = build_assistant_attachment_demo_card(
        app_id="cli_test",
        upload_url="http://127.0.0.1:6185/api/v1/assistant-attachments/token-1",
        attachments=[
            {
                "attachment_id": "att_123",
                "filename": "夏季新品资料.docx",
                "type": "file",
                "size_bytes": 2048,
            },
            {
                "attachment_id": "cloud_456",
                "filename": "新品发布节奏",
                "type": "cloud_doc",
                "url": "https://example.feishu.cn/docx/doc_123",
                "size_bytes": 0,
            },
        ],
    )
    text = _card_text(card)

    assert "夏季新品资料.docx" in text
    assert "2.0 KB" in text
    assert "新品发布节奏" in text
    assert "共 2 项资料" in text
    assert "已关联到本次任务" in text
    assert "att_123" not in text
    assert "cloud_456" not in text


@pytest.mark.parametrize(
    ("task_type", "required_text", "forbidden_text"),
    [
        (
            "copy",
            ("写作类型", "核心要求", "受众 / 发布渠道", "已有资料", "篇幅、语气、结构"),
            ("视频时长", "画面比例"),
        ),
        (
            "image",
            ("图片模板", "画面描述", "参考图", "画面比例", "输出数量"),
            ("公众号推文", "视频时长"),
        ),
        (
            "video",
            ("生成方式", "镜头描述", "参考素材", "视频时长", "视频比例"),
            ("公众号推文", "输出数量"),
        ),
        (
            "research",
            ("研究问题", "范围 / 时间", "来源要求", "交付形式"),
            ("公众号推文", "视频时长"),
        ),
        (
            "file",
            ("处理目标", "文件来源", "处理方式", "输出格式"),
            ("公众号推文", "画面比例"),
        ),
    ],
)
def test_task_forms_are_specific_to_the_selected_task(
    task_type: str,
    required_text: tuple[str, ...],
    forbidden_text: tuple[str, ...],
) -> None:
    text = _card_text(build_assistant_task_intake_card(task_type=task_type))

    assert all(item in text for item in required_text)
    assert all(item not in text for item in forbidden_text)


def test_task_form_does_not_start_execution() -> None:
    card = build_assistant_task_intake_card(task_type="copy")
    submit = next(
        button for button in _buttons(card) if button.get("name") == "preview_task"
    )

    assert submit["form_action_type"] == "submit"
    assert submit["behaviors"][0]["value"]["action"] == "preview_task"
    assert submit["behaviors"][0]["value"]["action"] != "start_task"


def test_select_components_use_feishu_supported_properties() -> None:
    selects: list[dict] = []

    def visit(element: dict) -> None:
        if element.get("tag") == "select_static":
            selects.append(element)
        for child in element.get("elements", []):
            visit(child)
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                visit(child)

    for task_type in ("copy", "image", "video", "research", "file"):
        for element in build_assistant_task_intake_card(task_type=task_type)["body"][
            "elements"
        ]:
            visit(element)

    assert selects
    assert all("label" not in select for select in selects)
    assert all(select.get("name") for select in selects)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "expected_card_type", "expected_title"),
    [
        ("当前状态", "assistant_task_list", "任务中心"),
        ("工作台", "assistant_task_list", "任务中心"),
        ("打开工作台", "assistant_task_list", "任务中心"),
        ("媒体生成", "assistant_task_intake", "选择创作类型"),
        ("文案资料", "assistant_task_intake", "写文案 / 方案"),
        ("写文案/方案", "assistant_task_intake", "写文案 / 方案"),
        ("生成图片", "assistant_task_intake", "生成图片"),
        ("生成视频", "assistant_task_intake", "生成视频"),
        ("查资料/分析", "assistant_task_intake", "查资料 / 分析"),
        ("处理文件", "assistant_task_intake", "处理文件"),
        ("物料报价", "assistant_task_intake", "筹备组物料报价"),
        ("筹备组物料报价", "assistant_task_intake", "筹备组物料报价"),
        ("报价系统", "assistant_task_intake", "筹备组物料报价"),
        ("AI转CDR", "assistant_task_intake", "AI 转 CDR"),
        ("AI 转 CDR", "assistant_task_intake", "AI 转 CDR"),
        ("AI转CDR工具", "assistant_task_intake", "AI 转 CDR"),
        ("图片/视频", "assistant_task_intake", "选择创作类型"),
        ("发起任务", "assistant_task_intake", "选择任务类型"),
        ("我的任务", "assistant_task_list", "任务中心"),
        ("进行中", "assistant_task_list", "进行中任务"),
        ("待补充", "assistant_task_list", "待补充任务"),
        ("已完成", "assistant_task_list", "已完成任务"),
        ("Codex 高级工具", "assistant_task_intake", "Codex 高级工具"),
        ("打开 Codex", "assistant_task_intake", "Codex 高级工具"),
    ],
)
async def test_menu_aliases_send_cards_without_llm(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    expected_card_type: str,
    expected_title: str,
) -> None:
    event = _event(label)
    send = AsyncMock(return_value=True)
    send_workspace = AsyncMock(return_value=True)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)
    monkeypatch.setattr(
        assistant_workbench,
        "_send_task_workspace",
        send_workspace,
    )

    handled = await assistant_workbench.try_handle_assistant_workbench(
        MagicMock(), event, label
    )

    assert handled is True
    if label in assistant_workbench.LEGACY_TASK_MENU_PRESETS:
        send_workspace.assert_awaited_once_with(
            ANY,
            event,
            task_type=assistant_workbench.LEGACY_TASK_MENU_PRESETS[label],
        )
        send.assert_not_awaited()
    else:
        assert send.await_args.kwargs["card_type"] == expected_card_type
        assert (
            send.await_args.kwargs["card"]["header"]["title"]["content"]
            == expected_title
        )
    event.should_call_llm.assert_called_once_with(False)
    event.stop_event.assert_called_once()
    event.set_result.assert_not_called()


@pytest.mark.asyncio
async def test_card_send_rebuilds_missing_lark_streamer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("生成图片")
    event.unified_msg_origin = "lark:FriendMessage:ou_test"
    event.message_obj.raw_message = SimpleNamespace(chat_id="oc_test")
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(
            platform_insts=[SimpleNamespace(lark_api=object(), config=object())]
        )
    )
    send = AsyncMock(return_value=SimpleNamespace(message_id="om_card"))
    monkeypatch.setattr("dc_engines.card_runtime.send_card_via_runtime", send)

    sent = await assistant_workbench._send_card(
        context,
        event,
        card_type="assistant_task_intake",
        card=build_assistant_task_intake_card(task_type="image"),
    )

    assert sent.message_id == "om_card"
    assert "巅池-Agent小助手" in context.feishu_streamers
    assert send.await_args.kwargs["chat_id"] == "oc_test"


@pytest.mark.asyncio
async def test_callback_navigation_patches_origin_card_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    event.message_obj.card_action_payload = {"open_chat_id": "oc_test"}
    streamer = MagicMock()
    streamer.get_stream.return_value = None
    context = SimpleNamespace(
        feishu_streamers={"巅池-Agent小助手": streamer},
        platform_manager=SimpleNamespace(platform_insts=[]),
    )
    patch = AsyncMock(return_value=True)
    send = AsyncMock()
    monkeypatch.setattr(
        "dc_engines.card_runtime.patch_card_via_runtime",
        patch,
        raising=False,
    )
    monkeypatch.setattr("dc_engines.card_runtime.send_card_via_runtime", send)

    result = await assistant_workbench._send_card(
        context,
        event,
        card_type="assistant_task_intake",
        card=build_assistant_task_intake_card(),
        replace_message_id="om_origin",
    )

    assert result.message_id == "om_origin"
    patch.assert_awaited_once_with(
        streamer,
        card_type="assistant_task_intake",
        message_id="om_origin",
        card=ANY,
        platform_id="巅池-Agent小助手",
        chat_id="oc_test",
        receive_id_type="chat_id",
        event="navigation_patch",
        detail="deterministic assistant workbench navigation",
    )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_workspace_is_bound_to_the_sent_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrbot.dashboard.api import assistant_attachments

    event = _event("生成图片")
    context = SimpleNamespace(
        get_platform_inst=lambda _platform_id: SimpleNamespace(
            config={"app_id": "cli_test"}
        )
    )
    draft = SimpleNamespace(
        token="task-token",
        upload_url="http://127.0.0.1:6185/api/v1/assistant-attachments/task-token",
    )
    store = SimpleNamespace(
        create=MagicMock(return_value=draft),
        bind_message=MagicMock(),
    )
    send = AsyncMock(return_value=SimpleNamespace(message_id="om_workspace"))
    monkeypatch.setattr(assistant_attachments, "draft_store", store)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)
    monkeypatch.setattr(
        assistant_workbench,
        "_workspace_base_url",
        MagicMock(return_value="http://127.0.0.1:6185"),
    )

    sent = await assistant_workbench._send_task_workspace(
        context,
        event,
        task_type="image",
    )

    assert sent is True
    store.create.assert_called_once_with(
        platform_id="巅池-Agent小助手",
        upload_base_url="http://127.0.0.1:6185",
        task_type="image",
        session_id="",
        message_type="FriendMessage",
        sender_id="",
        sender_name="",
        group_id="",
    )
    store.bind_message.assert_called_once_with("task-token", "om_workspace")
    card = send.await_args.kwargs["card"]
    assert card["header"]["title"]["content"] == "生成图片"
    behavior = _buttons(card)[0]["behaviors"][0]
    assert behavior["default_url"] == draft.upload_url
    assert behavior["pc_url"] == behavior["android_url"]
    assert "mode=sidebar-semi" in behavior["android_url"]


@pytest.mark.asyncio
async def test_remote_workspace_is_created_and_bound_on_the_nas_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrbot.dashboard.api import assistant_attachments

    event = _event("处理文件")
    event.session_id = "lark:FriendMessage:ou_test"
    event.get_sender_id.return_value = "ou_test"
    event.get_sender_name.return_value = "测试员工"
    context = SimpleNamespace(
        get_platform_inst=lambda _platform_id: SimpleNamespace(
            config={"app_id": "cli_test"}
        )
    )
    remote_url = (
        "https://workbench.example.com/api/v1/assistant-attachments/remote-token"
    )
    create_response = MagicMock()
    create_response.json.return_value = {
        "token": "remote-token",
        "upload_url": remote_url,
    }
    bind_response = MagicMock()
    client = SimpleNamespace(
        post=AsyncMock(side_effect=[create_response, bind_response]),
        aclose=AsyncMock(),
    )
    client_factory = MagicMock(return_value=client)
    local_store = SimpleNamespace(create=MagicMock(), bind_message=MagicMock())
    send = AsyncMock(return_value=SimpleNamespace(message_id="om_remote_workspace"))
    monkeypatch.setenv("DC_ASSISTANT_H5_ORIGIN", "https://workbench.example.com")
    monkeypatch.setenv("DC_ASSISTANT_H5_ADMIN_TOKEN", "shared-test-token")
    monkeypatch.setattr(assistant_attachments, "draft_store", local_store)
    monkeypatch.setattr(assistant_workbench.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    sent = await assistant_workbench._send_task_workspace(
        context,
        event,
        task_type="file",
    )

    assert sent is True
    local_store.create.assert_not_called()
    local_store.bind_message.assert_not_called()
    assert client.post.await_count == 2
    create_call, bind_call = client.post.await_args_list
    assert create_call.args[0] == (
        "https://workbench.example.com/api/v1/assistant-attachments/drafts"
    )
    assert create_call.kwargs["headers"] == {
        "X-DC-Assistant-Admin-Token": "shared-test-token"
    }
    assert create_call.kwargs["json"] == {
        "platform_id": "巅池-Agent小助手",
        "upload_base_url": "https://workbench.example.com",
        "task_type": "file",
        "session_id": "lark:FriendMessage:ou_test",
        "message_type": "FriendMessage",
        "sender_id": "ou_test",
        "sender_name": "测试员工",
        "group_id": "",
    }
    assert bind_call.args[0] == (
        "https://workbench.example.com/api/v1/assistant-attachments/drafts/remote-token/bind"
    )
    assert bind_call.kwargs["json"] == {"message_id": "om_remote_workspace"}
    create_response.raise_for_status.assert_called_once_with()
    bind_response.raise_for_status.assert_called_once_with()
    client.aclose.assert_awaited_once_with()
    card = send.await_args.kwargs["card"]
    open_behavior = _buttons(card)[0]["behaviors"][0]
    assert open_behavior["default_url"] == remote_url
    assert open_behavior["pc_url"] == open_behavior["android_url"]
    assert (
        remote_url
        in parse_qs(urlsplit(open_behavior["android_url"]).query)["lk_target_url"]
    )


@pytest.mark.asyncio
async def test_remote_workspace_without_admin_token_does_not_publish_a_dead_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrbot.dashboard.api import assistant_attachments

    event = _event("处理文件")
    context = SimpleNamespace(
        get_platform_inst=lambda _platform_id: SimpleNamespace(
            config={"app_id": "cli_test"}
        )
    )
    local_store = SimpleNamespace(create=MagicMock(), bind_message=MagicMock())
    send = AsyncMock()
    monkeypatch.setenv("DC_ASSISTANT_H5_ORIGIN", "https://workbench.example.com")
    monkeypatch.delenv("DC_ASSISTANT_H5_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(assistant_attachments, "draft_store", local_store)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    sent = await assistant_workbench._send_task_workspace(
        context,
        event,
        task_type="file",
    )

    assert sent is False
    local_store.create.assert_not_called()
    local_store.bind_message.assert_not_called()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_form_preview_is_deterministic_and_stops_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    event.get_sender_id.return_value = "ou_test"
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "preview_task",
            "task_type": "copy",
        },
        payload={
            "open_message_id": "om_origin",
            "form_value": {
                "task_request": "写一篇新品发布推文",
                "output_requirement": "500 字以内",
            },
        },
    )

    assert result.handled is True
    assert result.resumed_text == ""
    assert send.await_args.kwargs["card_type"] == "assistant_task_confirmation"
    assert send.await_args.kwargs["replace_message_id"] == "om_origin"
    event.should_call_llm.assert_called_once_with(False)
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_task_type_selection_reuses_origin_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    send_workspace = AsyncMock(return_value=True)
    monkeypatch.setattr(
        assistant_workbench,
        "_send_task_workspace",
        send_workspace,
    )

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "select_task_type",
            "task_type": "image",
        },
        payload={"open_message_id": "om_origin"},
    )

    assert result.handled is True
    send_workspace.assert_awaited_once_with(
        ANY,
        event,
        task_type="image",
        replace_message_id="om_origin",
    )


@pytest.mark.asyncio
async def test_image_preview_preserves_structured_generation_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "preview_task",
            "task_type": "image",
        },
        payload={
            "form_value": {
                "image_template": "social_cover",
                "visual_prompt": "夏季新品的小红书封面",
                "reference_source": "recent_attachment",
                "aspect_ratio": "3:4",
                "image_count": "2",
                "quality": "high",
            }
        },
    )

    assert result.handled is True
    confirmation = send.await_args.kwargs["card"]
    start = next(
        button
        for button in _buttons(confirmation)
        if button["text"]["content"] == "确认并开始执行"
    )
    task_data = start["behaviors"][0]["value"]["task_data"]
    assert task_data["visual_prompt"] == "夏季新品的小红书封面"
    assert task_data["aspect_ratio"] == "3:4"
    assert task_data["image_count"] == "2"
    event.should_call_llm.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_task_center_uses_real_session_tasks_and_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("进行中")
    event.unified_msg_origin = "lark:FriendMessage:ou_test"
    store = SimpleNamespace(
        list_tasks_for_session=AsyncMock(
            return_value=[
                SimpleNamespace(
                    task_id="task-active",
                    title="制作新品发布方案",
                    status="in_progress",
                    updated_at="2026-07-11T17:30:00+08:00",
                ),
                SimpleNamespace(
                    task_id="task-blocked",
                    title="等待客户补充预算",
                    status="blocked",
                    updated_at="2026-07-11T17:20:00+08:00",
                ),
            ]
        )
    )
    context = SimpleNamespace(harness_store=store)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    handled = await assistant_workbench.try_handle_assistant_workbench(
        context,
        event,
        "进行中",
    )

    assert handled is True
    card_text = _card_text(send.await_args.kwargs["card"])
    assert "制作新品发布方案" in card_text
    assert "等待客户补充预算" not in card_text
    assert "进行中任务" in card_text
    store.list_tasks_for_session.assert_awaited_once_with(
        "lark:FriendMessage:ou_test",
        limit=20,
    )
    event.should_call_llm.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_only_explicit_start_resumes_normal_task_text() -> None:
    event = _event("__card_action__:{}")

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "video",
            "task_data": {
                "generation_mode": "image_to_video",
                "video_prompt": "人物向镜头走来，镜头缓慢后退",
                "reference_source": "recent_attachment",
                "duration": "5",
                "aspect_ratio": "9:16",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert "视频任务目标" in result.resumed_text
    assert "人物向镜头走来" in result.resumed_text
    assert "图生视频" in result.resumed_text
    assert "9:16" in result.resumed_text
    event.set_extra.assert_any_call("dc_middle_router_capability", "execute.video")
    event.set_extra.assert_any_call(
        "dc_middle_router_parameters",
        {
            "generation_mode": "image_to_video",
            "video_prompt": "人物向镜头走来，镜头缓慢后退",
            "reference_source": "recent_attachment",
            "duration": "5",
            "aspect_ratio": "9:16",
        },
    )
    event.should_call_llm.assert_not_called()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_video_start_preserves_long_compiled_prompt() -> None:
    event = _event("__card_action__:{}")
    compiled_prompt = "五菱品牌短片分镜；" + "镜头语言与事实边界。" * 400

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "video",
            "task_data": {
                "generation_mode": "text_to_video",
                "video_prompt": compiled_prompt,
                "duration": "8",
            },
        },
        payload={},
    )

    parameters = next(
        call.args[1]
        for call in event.set_extra.call_args_list
        if call.args[0] == "dc_middle_router_parameters"
    )
    assert len(compiled_prompt) > 800
    assert parameters["video_prompt"] == compiled_prompt
    assert compiled_prompt in result.resumed_text


@pytest.mark.asyncio
async def test_copy_start_forces_creative_route_without_duplicate_prompt() -> None:
    event = _event("__card_action__:{}")

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "copy",
            "task_data": {
                "task_request": "为新能源电池服务中心写公众号开场文案",
                "copy_type": "official_account",
                "audience": "公众号读者",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert result.resumed_text.startswith("#创意 为新能源电池服务中心写公众号开场文案")
    assert result.resumed_text.count("为新能源电池服务中心写公众号开场文案") == 1
    assert "任务类型：写文案 / 方案" in result.resumed_text
    assert "写作类型：公众号推文" in result.resumed_text
    assert "不要检索或调用工具" in result.resumed_text
    event.set_extra.assert_any_call("assistant_workbench_task_type", "copy")
    event.set_extra.assert_any_call("disable_llm_tools", True)
    event.should_call_llm.assert_not_called()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_research_start_uses_llm_analysis_instructions_and_real_delivery() -> (
    None
):
    event = _event("__card_action__:{}")

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "research",
            "task_data": {
                "research_question": "比较三家供应商的交付能力",
                "analysis_mode": "comparison",
                "research_depth": "deep",
                "source_policy": "mixed",
                "output_format": "comparison",
                "delivery_format": "xlsx",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert "LLM 研究分析" in result.resumed_text
    assert "必须先理解已附资料再分析" in result.resumed_text
    assert "Markdown 对比表" in result.resumed_text
    assert "公开来源" in result.resumed_text
    event.set_extra.assert_any_call("assistant_workbench_task_type", "research")
    event.set_extra.assert_any_call(
        "assistant_workbench_task_data",
        {
            "research_question": "比较三家供应商的交付能力",
            "analysis_mode": "comparison",
            "research_depth": "deep",
            "source_policy": "mixed",
            "output_format": "comparison",
            "delivery_format": "xlsx",
        },
    )


@pytest.mark.asyncio
async def test_file_start_requires_llm_to_read_and_process_real_files() -> None:
    event = _event("__card_action__:{}")

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "file",
            "task_data": {
                "file_goal": "对比两版合同的风险变化",
                "operation": "compare",
                "processing_scope": "只比较付款和违约条款",
                "preserve_layout": "restructure",
                "output_format": "pdf",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert "LLM 文件处理" in result.resumed_text
    assert "必须先读取本次上传或关联的真实文件" in result.resumed_text
    assert "相同点、差异、冲突" in result.resumed_text
    assert "Word、Excel、PDF" in result.resumed_text
    event.set_extra.assert_any_call("assistant_workbench_task_type", "file")


@pytest.mark.asyncio
async def test_translation_start_bypasses_generic_llm_and_runs_document_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    translated_result = MessageEventResult().message("翻译完成").stop_event()
    execute = AsyncMock(return_value=translated_result)
    monkeypatch.setattr(
        "dc_engines.office_translation_runtime.run_translation_workbench",
        execute,
    )
    context = MagicMock()

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        context,
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "file",
            "task_data": {
                "file_goal": "翻译整个合同",
                "operation": "translate",
                "source_language": "auto",
                "target_language": "English",
                "translation_style": "faithful",
                "output_format": "docx",
            },
        },
        payload={},
    )

    assert result.handled is True
    assert result.resumed_text == ""
    execute.assert_awaited_once()
    assert execute.await_args.args[0] is context
    assert execute.await_args.args[1] is event
    event.should_call_llm.assert_called_with(False)
    event.set_result.assert_called_with(translated_result)
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_material_quotation_legacy_start_exposes_only_estimate_result() -> None:
    event = _event("__card_action__:{}")
    items = json.dumps(
        [
            {
                "item_name": "背胶",
                "specification": "过哑膜",
                "quantity": "10",
                "unit": "㎡",
                "unit_price": "8.00",
                "line_subtotal": "80.00",
                "supplier": "柳州东成广告",
                "source_path": "供应商库/柳州-南宁搭建物料.md",
                "price_status": "历史价待复核",
            },
            {
                "item_name": "现场摄影",
                "quantity": "1",
                "unit": "项",
                "unit_price": "",
                "price_status": "待询价",
            },
        ],
        ensure_ascii=False,
    )

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "quotation",
            "task_data": {
                "project_name": "柳州用户共创会",
                "quotation_items": items,
                "grand_total": "80.00",
                "pending_inquiry_count": "1",
                "quotation_status": "报价草案",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert "市场部估价结果" in result.resumed_text
    assert "柳州用户共创会" in result.resumed_text
    assert "¥80.00" in result.resumed_text
    assert "背胶" not in result.resumed_text
    assert "供应商库/柳州-南宁搭建物料.md" not in result.resumed_text
    assert "现场摄影" not in result.resumed_text
    assert "供应商" not in result.resumed_text
    event.set_extra.assert_any_call("assistant_workbench_task_type", "quotation")


@pytest.mark.asyncio
async def test_codex_tool_requires_explicit_start_and_emits_authorized_prefix() -> None:
    event = _event("__card_action__:{}")

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "codex",
            "task_data": {
                "task_request": "复核新品方案的关键假设与风险",
                "reasoning_depth": "codex_xhigh",
                "source_material": "飞书文档 docx_123",
                "output_requirement": "先给结论，再列证据",
            },
        },
        payload={},
    )

    assert result.handled is False
    assert result.resumed_text.startswith("#codex工具 复核新品方案的关键假设与风险")
    assert "飞书文档 docx_123" in result.resumed_text
    event.set_extra.assert_any_call("codex_tool_entry", "assistant_workbench")
    event.set_extra.assert_any_call("codex_capability", "deep_reasoning")
    event.set_extra.assert_any_call("codex_role", "advanced_executor")
    event.should_call_llm.assert_not_called()
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_codex_tool_denial_stops_before_provider_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    monkeypatch.setattr(
        assistant_workbench,
        "authorize_codex",
        MagicMock(
            return_value=SimpleNamespace(
                allowed=False,
                reason="authority_not_allowed",
            )
        ),
    )

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "start_task",
            "task_type": "codex",
            "task_data": {
                "task_request": "执行未授权任务",
                "reasoning_depth": "codex_high",
            },
        },
        payload={},
    )

    assert result.handled is True
    assert result.resumed_text == ""
    event.should_call_llm.assert_called_once_with(False)
    event.set_result.assert_called_once()


@pytest.mark.asyncio
async def test_task_detail_action_renders_directly_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("__card_action__:{}")
    load_rows = AsyncMock(
        return_value=[
            {
                "task_id": "task-real-001",
                "title": "真实任务",
                "status": "in_progress",
                "status_label": "进行中",
                "updated_at": "2026-07-11T20:00:00+08:00",
            }
        ]
    )
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(assistant_workbench, "_load_task_rows", load_rows)
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    result = await assistant_workbench.handle_assistant_workbench_card_action(
        MagicMock(),
        event,
        value={
            "source": "assistant_workbench",
            "action": "show_task",
            "task_id": "task-real-001",
        },
        payload={},
    )

    assert result.handled is True
    assert result.resumed_text == ""
    assert send.await_args.kwargs["card_type"] == "assistant_task_list"
    assert "真实任务" in _card_text(send.await_args.kwargs["card"])
    event.should_call_llm.assert_called_once_with(False)
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_forged_workbench_card_action_is_blocked() -> None:
    payload = {
        "value": {
            "source": "assistant_workbench",
            "action": "start_task",
            "task_request": "伪造执行",
        }
    }
    event = _event("__card_action__:" + json.dumps(payload, ensure_ascii=False))
    event.message_obj.card_action_payload = payload

    result = await card_action.try_handle_card_action(MagicMock(), event)

    assert result.handled is True
    assert result.stop is True
    assert result.resumed_text == ""
    event.should_call_llm.assert_called_once_with(False)


@pytest.mark.asyncio
async def test_successful_workbench_result_sends_one_session_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dc_engines.harness import HarnessTaskStore

    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="conv-old")
    )
    context = SimpleNamespace(harness_store=store, conversation_manager=manager)
    event = _event("生成结果")
    event.message_obj.raw_message = SimpleNamespace(chat_id="oc_test")
    event.get_result.return_value = SimpleNamespace(chain=[])
    event.set_extra("assistant_workbench_task_type", "copy")
    event.set_extra("dc_result_delivery_succeeded", True)
    event.set_extra("dc_result_response_quality", "success")
    event.set_extra(
        "assistant_workbench_decision_task_id",
        "assistant_workbench:lark:om_source",
    )
    send = AsyncMock(return_value=SimpleNamespace(message_id="om_choice"))
    monkeypatch.setattr(assistant_workbench, "_send_card", send)

    first = await session_choice.send_session_choice_for_event(context, event)
    replay = await session_choice.send_session_choice_for_event(context, event)

    assert first is True
    assert replay is False
    send.assert_awaited_once()
    assert send.await_args.kwargs["card_type"] == "assistant_session_choice"
    decision = await store.get_pending_session_decision(event.unified_msg_origin)
    assert decision is not None
    assert decision.card_message_id == "om_choice"
    assert decision.source_conversation_id == "conv-old"


@pytest.mark.asyncio
async def test_failed_workbench_result_does_not_send_session_choice() -> None:
    event = _event("生成结果")
    event.get_result.return_value = SimpleNamespace(chain=[])
    event.set_extra("assistant_workbench_task_type", "copy")
    event.set_extra("assistant_workbench_decision_task_id", "task-failed")
    event.set_extra("dc_result_delivery_succeeded", True)
    event.set_extra("dc_result_response_quality", "error")
    context = SimpleNamespace(
        harness_store=MagicMock(),
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conv-old")
        ),
    )

    sent = await session_choice.send_session_choice_for_event(context, event)

    assert sent is False
    context.conversation_manager.get_curr_conversation_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_button_creates_one_conversation_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dc_engines.harness import HarnessTaskStore

    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    decision, _, _ = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_test",
        platform_id="巅池-Agent小助手",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )
    await store.bind_session_decision_card(decision.decision_id, "om_choice")
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="conv-old"),
        get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="persona")),
        new_conversation=AsyncMock(return_value="conv-new"),
    )
    context = SimpleNamespace(harness_store=store, conversation_manager=manager)
    event = _event("__card_action__:{}")
    payload = {"open_message_id": "om_choice", "open_chat_id": "oc_test"}
    value = {
        "source": "assistant_workbench",
        "action": "open_new_session",
        "decision_id": decision.decision_id,
    }
    patch = AsyncMock(return_value=True)
    monkeypatch.setattr(session_choice, "_patch_session_choice_card", patch)

    first, replay = await asyncio.gather(
        session_choice.handle_session_choice_action(
            context,
            event,
            value=value,
            payload=payload,
        ),
        session_choice.handle_session_choice_action(
            context,
            event,
            value=value,
            payload=payload,
        ),
    )

    assert first.state == "new_conversation"
    assert replay.state == "new_conversation"
    manager.new_conversation.assert_awaited_once_with(
        event.unified_msg_origin,
        "巅池-Agent小助手",
        persona_id="persona",
    )
    persisted = await store.get_session_decision(decision.decision_id)
    assert persisted is not None
    assert persisted.target_conversation_id == "conv-new"
    assert persisted.card_patch_state == "synced"


@pytest.mark.asyncio
async def test_next_message_implicitly_continues_pending_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dc_engines.harness import HarnessTaskStore

    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    decision, _, _ = await store.create_session_decision(
        task_id="task-1",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_test",
        platform_id="巅池-Agent小助手",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )
    await store.bind_session_decision_card(decision.decision_id, "om_choice")
    context = SimpleNamespace(harness_store=store)
    event = _event("继续补充一个要求")
    event.get_sender_id.return_value = "ou_test"
    patch = AsyncMock(return_value=True)
    monkeypatch.setattr(session_choice, "_patch_session_choice_card", patch)

    resolved = await session_choice.resolve_pending_session_choice_for_message(
        context,
        event,
    )

    assert resolved is True
    persisted = await store.get_session_decision(decision.decision_id)
    assert persisted is not None
    assert persisted.state == "continue_current"
    assert persisted.decision_source == "implicit_message"
    assert persisted.target_conversation_id == "conv-old"
    assert event.message_str == "继续补充一个要求"


@pytest.mark.asyncio
async def test_continue_button_keeps_conversation_when_card_patch_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dc_engines.harness import HarnessTaskStore

    store = HarnessTaskStore(tmp_path / "harness.db")
    await store.initialize()
    decision, _, _ = await store.create_session_decision(
        task_id="task-continue",
        unified_msg_origin="巅池-Agent小助手:FriendMessage:ou_test",
        platform_id="巅池-Agent小助手",
        chat_id="ou_test",
        source_conversation_id="conv-old",
    )
    await store.bind_session_decision_card(decision.decision_id, "om_choice")
    new_conversation = AsyncMock(return_value="conv-unexpected")
    context = SimpleNamespace(
        harness_store=store,
        conversation_manager=SimpleNamespace(new_conversation=new_conversation),
    )
    event = _event("__card_action__:{}")
    event.get_sender_id.return_value = "ou_test"
    patch = AsyncMock(return_value=False)
    monkeypatch.setattr(session_choice, "_patch_session_choice_card", patch)

    result = await session_choice.handle_session_choice_action(
        context,
        event,
        value={
            "source": "assistant_workbench",
            "action": "continue_current_session",
            "decision_id": decision.decision_id,
        },
        payload={"open_message_id": "om_choice", "open_chat_id": "oc_test"},
    )

    assert result.state == "continue_current"
    new_conversation.assert_not_awaited()
    persisted = await store.get_session_decision(decision.decision_id)
    assert persisted is not None
    assert persisted.target_conversation_id == "conv-old"
    assert persisted.card_patch_state == "failed"
