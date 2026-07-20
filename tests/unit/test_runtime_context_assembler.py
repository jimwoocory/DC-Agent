from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.runtime_context.assembler import (
    RUNTIME_CONTEXT_SECTION_MAX_CHARS,
    RuntimeContextAssembler,
)
from astrbot.core.runtime_context.models import (
    RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY,
    RuntimeContextPriority,
    RuntimeContextSection,
    RuntimeContextSource,
)


class _Event:
    def __init__(self, sections: list[RuntimeContextSection]) -> None:
        self.sections = sections

    def get_extra(self, key: str):
        if key == RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY:
            return self.sections
        return None


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


def test_assembler_orders_deduplicates_and_bounds_event_sections() -> None:
    req = ProviderRequest(prompt="不满意")
    memory = RuntimeContextSection.memory_reference(
        text="长期记忆" * 5_000,
        source_id="dc_memory_context",
    )
    background = RuntimeContextSection(
        text="后台状态",
        source=RuntimeContextSource.SYSTEM,
        priority=RuntimeContextPriority.BACKGROUND,
        no_save=True,
        source_id="runtime_status",
    )
    event = _Event([background, memory, memory])
    assembler = RuntimeContextAssembler()

    assembler.normalize(req, event=event)
    assembler.normalize(req, event=event)

    assert req.prompt == "不满意"
    assert len(req.extra_user_content_parts) == 2
    assert "priority=reference" in req.extra_user_content_parts[0].text
    assert "priority=background" in req.extra_user_content_parts[1].text
    assert req.extra_user_content_parts[0].text.count("长期记忆") < 5_000
    assert len(req.extra_user_content_parts[0].text) < (
        RUNTIME_CONTEXT_SECTION_MAX_CHARS + 500
    )
    assert all(
        getattr(part, "_no_save") is True for part in req.extra_user_content_parts
    )
