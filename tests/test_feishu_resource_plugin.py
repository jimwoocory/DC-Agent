from data.plugins.feishu_resource_plugin.main import (
    FeishuResourcePlugin,
    _extract_user_query_text,
    _should_handle_resource_query,
)


def test_attachment_summary_does_not_trigger_resource_query() -> None:
    text = (
        "那为什么我在培训手册里看到的 /dr 跟你说的又不一样呢？\n\n"
        "<attachment_summary>\n"
        "图片里包含资料、跨源调研、输入 /dr 触发等文字。\n"
        "</attachment_summary>"
    )

    assert _extract_user_query_text(text) == (
        "那为什么我在培训手册里看到的 /dr 跟你说的又不一样呢？"
    )
    assert _should_handle_resource_query(text) is False


def test_explicit_resource_query_still_triggers() -> None:
    assert _should_handle_resource_query("查 员工手册") is True
    assert _should_handle_resource_query("帮我查一下客户资料") is True


def test_feishu_analysis_request_reaches_llm_workflow() -> None:
    text = "请你解读这个飞书链接里面所有方案文档，总结案例内容 https://dianchi.feishu.cn/docx/abc"

    assert _should_handle_resource_query(text) is False


def test_resource_discussion_question_does_not_trigger_query() -> None:
    assert _should_handle_resource_query("为什么资料里看到的和你说的不一样？") is False


def test_keyword_extraction_uses_visible_query_text_only() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)
    text = _extract_user_query_text(
        "帮我查一下客户资料\n\n<attachment_summary>\n资料 /dr\n</attachment_summary>"
    )

    assert plugin._extract_keyword(text) == "客户资料"


def test_keyword_extraction_prefers_longer_query_verbs() -> None:
    plugin = FeishuResourcePlugin.__new__(FeishuResourcePlugin)

    assert plugin._extract_keyword("查询员工手册") == "员工手册"
    assert plugin._extract_keyword("搜索 客户资料") == "客户资料"
