import asyncio
from types import SimpleNamespace

from dc_engines.feishu_card_streamer import (
    WaitingCardHandle,
    build_antigravity_queue_card,
    build_case_overview_card,
    build_daily_response_card,
    build_deleted_skill_list_card,
    build_devops_status_card,
    build_employee_pending_card,
    build_error_card,
    build_kb_archive_card,
    build_media_generation_card,
    build_multimodal_understanding_card,
    build_onboarding_dept_card,
    build_progress_card,
    build_skill_confirm_card,
    build_skill_detail_card,
    build_skill_list_card,
    build_skill_review_card,
    build_source_trace_card,
    build_task_reminder_card,
    build_truth_intake_received_card,
    build_truth_intake_request_card,
    get_dept_lesson_body,
    get_quiz_for_dept,
    start_waiting_card_for_event,
    waiting_pulse,
    waiting_track,
)


def _body_elements(card):
    return card["body"]["elements"]


def _markdown_contents(card):
    contents = []

    def collect(element):
        if element.get("tag") == "markdown":
            contents.append(element["content"])
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                collect(child)

    for element in _body_elements(card):
        collect(element)
    return contents


def _button_elements(card):
    buttons = []

    def collect(element):
        if element.get("tag") == "button":
            buttons.append(element)
        for column in element.get("columns", []):
            for child in column.get("elements", []):
                collect(child)

    for element in _body_elements(card):
        collect(element)
    return buttons


def test_daily_response_card_uses_stable_heading_sizes():
    card = build_daily_response_card(
        content_md="# 主标题\n\n## 章节\n\n### 小节\n\n正文内容",
    )
    sizes = [
        element.get("text_size")
        for element in _body_elements(card)
        if element.get("tag") == "markdown"
    ]

    assert sizes == ["heading_1", "heading_2", "heading_3", "normal"]


def test_daily_response_card_centers_compact_markdown_tables():
    card = build_daily_response_card(
        content_md=(
            "| 状态 | 展示文案 | 优先级 |\n"
            "|---|---|---|\n"
            "| 已核验 | 已基于真实资料生成 | 高 |\n"
            "| 需要补充 | 需要补一点资料 | 中 |"
        ),
    )

    elements = _body_elements(card)
    table_rows = [element for element in elements if element.get("tag") == "column_set"]
    assert len(table_rows) == 3

    first_cell = table_rows[0]["columns"][0]["elements"][0]
    assert first_cell["content"] == "**状态**"
    assert first_cell["text_align"] == "center"
    assert first_cell["text_size"] == "notation"

    body_cell = table_rows[1]["columns"][1]["elements"][0]
    assert body_cell["content"] == "已基于真实资料生成"
    assert body_cell["text_align"] == "center"
    assert body_cell["text_size"] == "normal"


def test_progress_card_pulse_changes_with_elapsed_time():
    first = build_progress_card(title="测试", brief="等待", elapsed_sec=0)
    second = build_progress_card(title="测试", brief="等待", elapsed_sec=3)

    first_progress = next(
        content for content in _markdown_contents(first) if "任务进度" in content
    )
    second_progress = next(
        content for content in _markdown_contents(second) if "任务进度" in content
    )
    assert first_progress.startswith(waiting_pulse(0))
    assert second_progress.startswith(waiting_pulse(3))
    assert first_progress != second_progress
    assert "已等待 0 秒" in first_progress
    assert "已等待 3 秒" in second_progress
    assert "0%" in first_progress
    assert "1%" in second_progress
    assert "任务进度" in first_progress
    assert "⏱️已等待" in first_progress
    assert waiting_track(0) != waiting_track(3)


def test_antigravity_queue_card_uses_real_queue_fields_and_fallback_button():
    card = build_antigravity_queue_card(
        job_id="agy-job-1",
        queue_position=3,
        eta_text="约 1 分钟",
        elapsed_sec=12,
        original_prompt="你好",
    )

    assert card["header"]["title"]["content"] == "小助手排队中"
    contents = "\n".join(_markdown_contents(card))
    assert "**当前位置**：第 3 位" in contents
    assert "**预计等待**：约 1 分钟" in contents
    assert "**当前通道**：巅巅小助手" in contents
    assert "OAuth" not in contents
    assert "provider" not in contents
    assert "AIHubMix" not in contents
    button = _button_elements(card)[0]
    assert button["text"]["content"] == "不想排队，使用池池小助手"
    assert button["value"]["source"] == "antigravity_queue_card"
    assert button["value"]["action"] == "use_fallback"
    assert button["value"]["job_id"] == "agy-job-1"
    assert button["value"]["fallback_provider_id"] == "aihubmix/gemini-3-flash-preview"


def test_antigravity_queue_card_fallback_running_removes_button():
    card = build_antigravity_queue_card(
        job_id="agy-job-1",
        queue_position=3,
        eta_text="约 1 分钟",
        status="fallback_running",
    )

    assert card["header"]["title"]["content"] == "池池小助手处理中"
    assert card["header"]["template"] == "green"
    contents = "\n".join(_markdown_contents(card))
    assert "**当前通道**：池池小助手" in contents
    assert "好的，我马上请池池小助手先帮您处理" in contents
    assert _button_elements(card) == []


def test_error_card_accepts_legacy_error_message_keyword():
    card = build_error_card(title="失败", error_message="legacy keyword")
    error_blocks = [
        element["content"]
        for element in _body_elements(card)
        if element.get("tag") == "markdown"
    ]

    assert "legacy keyword" in error_blocks


class _FakeStreamer:
    def __init__(self):
        self.started = None
        self.auto_update = None
        self.updated = None

    async def start(self, *, chat_id, receive_id_type, card):
        self.started = {
            "chat_id": chat_id,
            "receive_id_type": receive_id_type,
            "card": card,
        }
        return SimpleNamespace(message_id="mid_waiting")

    def start_auto_update(self, message_id, builder, interval_sec=15.0):
        self.auto_update = {
            "message_id": message_id,
            "builder": builder,
            "interval_sec": interval_sec,
        }

    def get_stream(self, message_id):
        return SimpleNamespace(message_id=message_id, elapsed_sec=12)

    async def update(self, message_id, card):
        self.updated = {"message_id": message_id, "card": card}
        return True


class _FakeEvent:
    message_str = "帮我生成一张新车发布会海报"
    message_obj = SimpleNamespace(raw_message=SimpleNamespace(chat_id="oc_test_chat"))

    def get_platform_id(self):
        return "巅池-Agent小助手"

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "ou_sender"


def test_start_waiting_card_for_event_uses_lark_chat_context():
    streamer = _FakeStreamer()
    context = SimpleNamespace(feishu_streamers={"巅池-Agent小助手": streamer})

    handle = asyncio.run(
        start_waiting_card_for_event(
            context,
            _FakeEvent(),
            title="生图任务",
            current_stage="GPT Image 2 正在生成",
            reasoning_tier="high",
            interval_sec=5,
        )
    )

    assert isinstance(handle, WaitingCardHandle)
    assert handle.message_id == "mid_waiting"
    assert streamer.started["chat_id"] == "oc_test_chat"
    assert streamer.started["receive_id_type"] == "chat_id"
    assert streamer.auto_update["interval_sec"] == 5
    first_content = "\n".join(_markdown_contents(streamer.started["card"]))
    assert "帮我生成一张新车发布会海报" in first_content

    asyncio.run(handle.update_stage("Dreamina 即梦兜底生成中"))
    updated_contents = "\n".join(_markdown_contents(streamer.updated["card"]))
    assert "Dreamina 即梦兜底生成中" in updated_contents


def test_onboarding_department_card_uses_current_seven_function_departments():
    card = build_onboarding_dept_card(welcome_name="新同事")

    buttons = _button_elements(card)
    assert [button["text"]["content"] for button in buttons] == [
        "总经办",
        "客户部",
        "策划",
        "品宣部",
        "执行运营",
        "综合部",
        "财务部",
    ]
    assert [button["value"]["dept"] for button in buttons] == [
        "executive_office",
        "client_dept",
        "planning",
        "brand_publicity",
        "execution_ops",
        "general_affairs",
        "finance",
    ]


def test_department_lessons_and_quiz_alias_legacy_codes_to_current_departments():
    client_name, client_body = get_dept_lesson_body("marketing")
    planning_name, planning_body = get_dept_lesson_body("strategy")
    general_name, general_body = get_dept_lesson_body("general_affairs")

    assert client_name == "客户部"
    assert "客户触达话术" in client_body
    assert "客户邮件起草" not in client_body
    assert "客户问候邮件" not in client_body
    assert "不能硬生成正式稿" in get_quiz_for_dept("client_dept")[2]["explain"]
    assert planning_name == "策划"
    assert "应标 / 客户提案" in planning_body
    assert general_name == "综合部"
    assert "行政通知整理" in general_body

    assert (
        get_quiz_for_dept("marketing")[-1]["question"]
        == get_quiz_for_dept("client_dept")[-1]["question"]
    )
    assert (
        get_quiz_for_dept("strategy")[-1]["question"]
        == get_quiz_for_dept("planning")[-1]["question"]
    )
    assert len(get_quiz_for_dept("general_affairs")) == 6


def test_truth_intake_request_card_asks_for_sources_gently():
    card = build_truth_intake_request_card(
        task_title="草拟端午客户问候话术",
        task_id="abc12345",
        missing_fields=["背景", "目标对象", "原文/附件"],
        task_brief="帮我写一段客户问候话术",
    )

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "需要真实资料"
    assert _body_elements(card)[0]["text_size"] == "heading_2"
    contents = _markdown_contents(card)
    assert "truth_intake / blocked" in "\n".join(contents)
    assert "`#abc12345`" in "\n".join(contents)
    assert "- 原文/附件" in "\n".join(contents)
    buttons = _button_elements(card)
    assert buttons[-1]["value"] == {"action": "noop"}
    assert buttons[-1]["width"] == "fill"


def test_truth_intake_received_card_summarizes_archive_and_next_route():
    card = build_truth_intake_received_card(
        task_id="abc12345",
        sources_summary={"附件": 2, "飞书链接": 1, "文字材料": 1},
        archive_path="data/harness_intake/raw/abc12345/",
        next_route="Hermes",
    )

    assert card["header"]["template"] == "green"
    contents = "\n".join(_markdown_contents(card))
    assert "附件 2 个 · 飞书链接 1 个 · 文字材料 1 段" in contents
    assert "data/harness_intake/raw/abc12345/" in contents
    assert "已交回 **Hermes** 继续执行" in contents


def test_source_trace_card_maps_truth_status_and_lists_sections():
    card = build_source_trace_card(
        truth_status="部分待确认",
        sources=["飞书 wiki: 端午活动", "附件: 端午方案.pdf"],
        kb_names=["品牌规范"],
        unconfirmed=["预算", "发布时间"],
        risk_note="以下内容仍需老板确认",
    )

    assert card["header"]["template"] == "red"
    contents = "\n".join(_markdown_contents(card))
    assert "**真实性状态**：**<font color='red'>部分待确认</font>**" in contents
    assert "- 飞书 wiki: 端午活动" in contents
    assert "品牌规范" in contents
    assert "- 预算" in contents
    assert "以下内容仍需老板确认" in contents


def test_devops_status_card_uses_red_header_and_restart_button():
    card = build_devops_status_card(
        services=[
            {
                "name": "AstrBot",
                "status": "正常",
                "pid": "51997",
                "metric": "lark 3/3 在线",
            },
            {
                "name": "Hermes",
                "status": "异常",
                "pid": "",
                "metric": "queue stalled",
            },
        ],
        queue_summary={"hermes": 2, "claude_cli": 0, "codex": 1},
        recent_errors=["Hermes timeout", "Card patch failed", "Quota unavailable"],
        next_action_hint="建议重启 Hermes 前先保留现场",
    )

    assert card["header"]["template"] == "red"
    contents = "\n".join(_markdown_contents(card))
    assert "- **AstrBot**：<font color='green'>正常</font>" in contents
    assert "- **Hermes**：<font color='red'>异常</font>" in contents
    assert "Hermes 队列：2 · Claude CLI：0 · Codex：1" in contents
    assert "- Hermes timeout" in contents
    buttons = _button_elements(card)
    assert buttons[-1]["value"] == {"action": "devops_restart_prompt"}
    assert buttons[-1]["width"] == "fill"


def test_kb_archive_card_failure_shows_trace_and_error():
    card = build_kb_archive_card(
        archived_items={"附件": 2, "文字材料": 1},
        kb_name="品牌规范",
        status="入库失败",
        archive_path="data/harness_intake/archived/abc12345/",
        doc_id="doc_001",
        error_hint="向量索引写入失败",
    )

    assert card["header"]["template"] == "red"
    contents = "\n".join(_markdown_contents(card))
    assert "**知识库**：品牌规范" in contents
    assert "附件 2 个 · 文字材料 1 段" in contents
    assert "入库失败" in contents
    assert "data/harness_intake/archived/abc12345/" in contents
    assert "doc_id: doc_001" in contents
    assert "向量索引写入失败" in contents


def test_employee_pending_card_lists_fields_and_uses_noop_button():
    card = build_employee_pending_card(
        task_title="端午客户问候话术草稿",
        pending_items=[
            {"field": "发布时间", "hint": "默认 6 月 17 日，需确认"},
            {"field": "客户名单", "hint": "请补充收件范围"},
        ],
        current_draft_summary="已整理节日问候主文案和署名。",
        task_id="abc12345",
    )

    assert card["header"]["template"] == "red"
    assert _body_elements(card)[0]["text_size"] == "heading_2"
    contents = "\n".join(_markdown_contents(card))
    assert (
        "- **发布时间**：<font color='red'>默认 6 月 17 日，需确认</font>" in contents
    )
    assert "`#abc12345`" in contents
    assert "已整理节日问候主文案和署名。" in contents
    buttons = _button_elements(card)
    assert buttons[-1]["value"] == {"action": "noop"}
    assert buttons[-1]["width"] == "fill"


def test_media_generation_card_covers_running_and_result_fields():
    card = build_media_generation_card(
        task_title="端午海报",
        media_type="image",
        status="生成中",
        prompt="绿色科技感端午海报",
        engine="GPT Image 2",
        task_id="img001",
        aspect_ratio="16:9",
        elapsed_sec=42,
    )

    assert card["header"]["title"]["content"] == "生图 · 生成中"
    contents = "\n".join(_markdown_contents(card))
    assert "**类型**：生图" in contents
    assert "**引擎**：GPT Image 2" in contents
    assert "绿色科技感端午海报" in contents
    assert "已等待 42 秒" in contents


def test_multimodal_case_and_task_reminder_cards_have_core_fields():
    multimodal = build_multimodal_understanding_card(
        task_title="看一下客户发来的素材",
        modality="video",
        status="已理解",
        files_summary={"视频": 1, "截图": 2},
        truth_status="部分待确认",
        findings=["出现产品外观", "包含门店场景"],
        limits=["视频没有声音"],
        task_id="mm001",
    )
    multi_contents = "\n".join(_markdown_contents(multimodal))
    assert multimodal["header"]["title"]["content"] == "视频理解 · 已理解"
    assert "视频 1 个 · 截图 2 个" in multi_contents
    assert "<font color='red'>部分待确认</font>" in multi_contents
    assert "<font color='red'>视频没有声音</font>" in multi_contents

    case = build_case_overview_card(
        case_id="case001",
        case_title="端午客户运营项目",
        status="部分待确认",
        owner="市场部",
        progress="60%",
        task_summary={"todo": 2, "in_progress": 1, "done": 4, "blocked": 1},
        risks=["预算待确认"],
        next_actions=["确认客户名单"],
    )
    case_contents = "\n".join(_markdown_contents(case))
    assert case["header"]["title"]["content"] == "Case 概览"
    assert "待办 2 · 进行中 1 · 已完成 4 · 阻塞 1" in case_contents
    assert "<font color='red'>预算待确认</font>" in case_contents

    reminder = build_task_reminder_card(
        task_id="task001",
        task_title="确认端午触达名单",
        assignee="蔡挺",
        due_text="今天 18:00",
        priority="紧急",
        overdue=True,
    )
    reminder_contents = "\n".join(_markdown_contents(reminder))
    assert reminder["header"]["template"] == "red"
    assert "<font color='red'>已逾期</font>" in reminder_contents
    assert [button["value"]["action"] for button in _button_elements(reminder)] == [
        "ack",
        "done",
        "snooze",
    ]


def test_skill_list_card_buttons_bind_concrete_skill_payloads():
    card = build_skill_list_card(
        kind="boss",
        skills=[
            {
                "name": "杨总",
                "slug": "yang-zong",
                "version": "20260521T120000Z",
                "corrections_count": 2,
                "updated_at": "2026-05-21T12:00:00Z",
            }
        ],
    )

    assert card["header"]["title"]["content"] == "老板 Skill 列表"
    assert card["header"]["template"] == "green"
    contents = "\n".join(_markdown_contents(card))
    assert "杨总" in contents
    assert "`yang-zong`" in contents
    buttons = _button_elements(card)
    assert [button["text"]["content"] for button in buttons] == [
        "查看",
        "审阅",
        "纠错",
        "回滚",
        "删除",
    ]
    payload = buttons[0]["value"]
    assert payload == {
        "source": "hermes_skill_card",
        "action": "inspect",
        "kind": "boss",
        "slug": "yang-zong",
        "version": "20260521T120000Z",
    }
    assert buttons[-1]["value"]["action"] == "delete_request"


def test_skill_detail_review_deleted_and_confirm_cards_are_interactive():
    detail = build_skill_detail_card(
        kind="colleague",
        skill={
            "name": "市场同事",
            "slug": "marketing",
            "version": "v2",
            "conversation_type": "group",
            "message_count": 8,
            "speaker_count": 3,
            "corrections_count": 1,
            "updated_at": "2026-05-21T13:00:00Z",
            "knowledge_sources": ["knowledge/corrections.md"],
            "files": ["SKILL.md", "work.md"],
        },
    )
    assert detail["header"]["title"]["content"] == "同事 Skill 详情"
    assert "knowledge/corrections.md" in "\n".join(_markdown_contents(detail))
    assert {button["value"]["action"] for button in _button_elements(detail)} >= {
        "review",
        "correct_request",
        "rollback_request",
        "delete_request",
    }

    review = build_skill_review_card(
        kind="colleague",
        review={
            "name": "市场同事",
            "slug": "marketing",
            "version": "v2",
            "score": 60,
            "passes": ["必要文件完整"],
            "risks": ["存在待确认或证据不足内容"],
            "corrections_count": 1,
        },
    )
    assert review["header"]["template"] == "red"
    assert "<font color='red'>存在待确认或证据不足内容</font>" in "\n".join(
        _markdown_contents(review)
    )

    deleted = build_deleted_skill_list_card(
        kind="boss",
        skills=[
            {
                "name": "杨总",
                "slug": "yang-zong",
                "deleted_at": "2026-05-21T14:00:00Z",
                "path": "/tmp/.deleted/boss_yang-zong",
            }
        ],
    )
    assert deleted["header"]["title"]["content"] == "已删除老板 Skill"
    assert [button["value"]["action"] for button in _button_elements(deleted)] == [
        "restore",
        "inspect_deleted_path",
    ]

    confirm = build_skill_confirm_card(
        operation="rollback",
        kind="boss",
        slug="yang-zong",
        version="v1",
        operator_id="admin-user",
    )
    assert confirm["header"]["template"] == "red"
    buttons = _button_elements(confirm)
    assert buttons[0]["value"]["action"] == "rollback_confirm"
    assert buttons[0]["value"]["source"] == "hermes_skill_card"
    assert buttons[1]["value"]["action"] == "cancel"
