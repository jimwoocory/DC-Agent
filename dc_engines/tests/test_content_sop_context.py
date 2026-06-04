from __future__ import annotations

from dc_engines.department_workflows import (
    assemble_content_sop_source_context,
    strip_internal_memory_context,
)


def test_assemble_content_sop_source_context_from_memory_hits() -> None:
    context = assemble_content_sop_source_context(
        {
            "project_items": [
                {
                    "project_name": "之光EV预热",
                    "owner": "张三",
                    "owner_department": "策划",
                    "source_rel_path": "projects/之光EV/项目总表.md",
                }
            ],
            "documents": [
                {
                    "title": "之光EV传播策略",
                    "rel_path": "projects/之光EV/传播策略.md",
                    "project_name": "之光EV预热",
                    "owner": "李四",
                    "departments_json": '["客户部", "策划"]',
                    "review_status": "confirmed",
                    "summary": "目标人群是年轻家庭，重点突出城市通勤。",
                }
            ],
        }
    )

    assert "项目关系上下文" in context.knowledge_context
    assert "之光EV传播策略" in context.knowledge_context
    assert {citation["source_path"] for citation in context.source_citations} == {
        "projects/之光EV/项目总表.md",
        "projects/之光EV/传播策略.md",
    }


def test_strip_internal_memory_context_keeps_user_request() -> None:
    text = (
        "帮我写客户邀约文案\n\n"
        "<dc_agent_memory_context>内部资料</dc_agent_memory_context>"
    )

    assert strip_internal_memory_context(text) == "帮我写客户邀约文案"
