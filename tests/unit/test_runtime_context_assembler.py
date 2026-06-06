from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.runtime_context.assembler import RuntimeContextAssembler
from astrbot.core.runtime_context.models import (
    RuntimeContextPriority,
    RuntimeContextSection,
    RuntimeContextSource,
)


def test_runtime_context_section_defaults_to_transient_reference_when_memory() -> None:
    section = RuntimeContextSection.memory_reference(
        text="相关文档：东风柳汽活动方案。",
        source_id="dc_memory_context",
    )

    assert section.priority == RuntimeContextPriority.REFERENCE
    assert section.source == RuntimeContextSource.LONG_TERM_MEMORY
    assert section.no_save is True
    assert section.source_id == "dc_memory_context"


def test_assembler_demotes_dc_memory_marker_to_transient_reference() -> None:
    req = ProviderRequest()
    req.prompt = (
        "不满意\n\n"
        "<dc_agent_memory_context>\n"
        "相关文档：东风柳汽活动方案。\n"
        "</dc_agent_memory_context>"
    )

    RuntimeContextAssembler().normalize(req)

    assert req.prompt == "不满意"
    assert "recent conversation history first" in req.system_prompt
    assert len(req.extra_user_content_parts) == 1
    memory_part = req.extra_user_content_parts[0]
    assert "Lower-priority DC-Agent long-term memory reference" in memory_part.text
    assert "东风柳汽活动方案" in memory_part.text
    assert getattr(memory_part, "_no_save") is True


def test_assembler_is_idempotent_for_existing_priority_prompt() -> None:
    req = ProviderRequest()
    req.system_prompt = "base"
    req.prompt = (
        "不满意\n\n"
        "<dc_agent_memory_context>\n"
        "相关文档：东风柳汽活动方案。\n"
        "</dc_agent_memory_context>"
    )

    assembler = RuntimeContextAssembler()
    assembler.normalize(req)
    assembler.normalize(req)

    assert req.prompt == "不满意"
    assert req.system_prompt.count("recent conversation history first") == 1
    assert len(req.extra_user_content_parts) == 1


def test_assembler_preserves_prompt_when_memory_marker_is_unclosed() -> None:
    req = ProviderRequest()
    req.prompt = (
        "这句话后面用户还在继续说 "
        "<dc_agent_memory_context> 但这个 marker 没有关掉，所以不是可靠记忆块"
    )

    RuntimeContextAssembler().normalize(req)

    assert req.prompt == (
        "这句话后面用户还在继续说 "
        "<dc_agent_memory_context> 但这个 marker 没有关掉，所以不是可靠记忆块"
    )
    assert req.extra_user_content_parts == []
    assert "recent conversation history first" not in req.system_prompt
