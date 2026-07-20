from __future__ import annotations

import hashlib
from typing import Any

from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.runtime_context.models import (
    RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY,
    RuntimeContextPriority,
    RuntimeContextSection,
    RuntimeContextSource,
)

DC_MEMORY_CONTEXT_OPEN_MARKER = "<dc_agent_memory_context>"
DC_MEMORY_CONTEXT_CLOSE_MARKER = "</dc_agent_memory_context>"
DC_MEMORY_CONTEXT_PRIORITY_PROMPT = (
    "When a user gives a short follow-up or feedback, interpret it using the "
    "recent conversation history first. Any DC-Agent long-term memory context "
    "is lower-priority factual reference only; it must not change the current "
    "project, customer, brand, or task target established by recent turns."
)
RUNTIME_CONTEXT_SECTION_MAX_CHARS = 8_000
RUNTIME_CONTEXT_TOTAL_MAX_CHARS = 12_000
_PRIORITY_ORDER = {
    RuntimeContextPriority.CURRENT: 0,
    RuntimeContextPriority.RECENT_HISTORY: 1,
    RuntimeContextPriority.REFERENCE: 2,
    RuntimeContextPriority.BACKGROUND: 3,
}


class RuntimeContextAssembler:
    """Assemble transient provider context behind one runtime Interface."""

    def normalize(self, req: ProviderRequest, *, event: Any | None = None) -> None:
        """Normalize legacy and structured context into a provider request.

        Args:
            req: Provider request receiving transient context sections.
            event: Optional message event carrying typed runtime context sections.
        """
        sections: list[RuntimeContextSection] = []
        legacy_memory = self._extract_dc_memory_context(req)
        if legacy_memory is not None:
            sections.append(legacy_memory)
        sections.extend(self._event_sections(event))
        self._append_sections(req, sections)

    def _extract_dc_memory_context(
        self,
        req: ProviderRequest,
    ) -> RuntimeContextSection | None:
        if not req.prompt or DC_MEMORY_CONTEXT_OPEN_MARKER not in req.prompt:
            return None

        prompt_without_memory, memory_block = self._split_dc_memory_context_from_prompt(
            req.prompt
        )
        if not memory_block:
            return None

        req.prompt = prompt_without_memory
        return RuntimeContextSection.memory_reference(
            text=memory_block,
            source_id="legacy_prompt_marker",
            metadata={"compatibility_path": "prompt_marker"},
        )

    @staticmethod
    def _event_sections(event: Any | None) -> list[RuntimeContextSection]:
        if event is None:
            return []
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return []
        try:
            raw_sections = getter(RUNTIME_CONTEXT_SECTIONS_EXTRA_KEY)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(raw_sections, (list, tuple)):
            return []
        return [
            section
            for section in raw_sections
            if isinstance(section, RuntimeContextSection) and section.text.strip()
        ]

    def _append_sections(
        self,
        req: ProviderRequest,
        sections: list[RuntimeContextSection],
    ) -> None:
        if not sections:
            return

        seen = getattr(req, "_runtime_context_section_keys", set())
        if not isinstance(seen, set):
            seen = set()
        remaining = RUNTIME_CONTEXT_TOTAL_MAX_CHARS
        for section in sorted(
            sections,
            key=lambda item: _PRIORITY_ORDER.get(item.priority, 99),
        ):
            text = section.text.strip()
            key = (
                str(section.source),
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            if key in seen or remaining <= 0:
                continue
            limit = min(RUNTIME_CONTEXT_SECTION_MAX_CHARS, remaining)
            bounded_text = text[:limit]
            if len(text) > limit:
                bounded_text += "\n[Runtime context truncated to fit budget]"
            source_id = section.source_id.replace("\n", " ")[:160]
            label = (
                "Lower-priority DC-Agent long-term memory reference"
                if section.source == RuntimeContextSource.LONG_TERM_MEMORY
                else "Runtime context section"
            )
            part = TextPart(
                text=(
                    f"[{label} | "
                    f"source={section.source} | priority={section.priority} | "
                    f"source_id={source_id}]\n"
                    f"{bounded_text}"
                )
            ).mark_as_temp()
            req.extra_user_content_parts.append(part)
            seen.add(key)
            remaining -= len(bounded_text)
            if section.source == RuntimeContextSource.LONG_TERM_MEMORY:
                self._append_system_prompt_once(
                    req,
                    DC_MEMORY_CONTEXT_PRIORITY_PROMPT,
                )
        setattr(req, "_runtime_context_section_keys", seen)

    @staticmethod
    def _split_dc_memory_context_from_prompt(prompt: str) -> tuple[str, str]:
        start = prompt.find(DC_MEMORY_CONTEXT_OPEN_MARKER)
        if start < 0:
            return prompt, ""

        end = prompt.find(DC_MEMORY_CONTEXT_CLOSE_MARKER, start)
        if end < 0:
            return prompt, ""

        end += len(DC_MEMORY_CONTEXT_CLOSE_MARKER)
        memory_block = prompt[start:end].strip()
        prompt_without_memory = (prompt[:start] + prompt[end:]).strip()
        return prompt_without_memory, memory_block

    @staticmethod
    def _append_system_prompt_once(req: ProviderRequest, prompt: str) -> None:
        existing = (req.system_prompt or "").strip()
        if prompt in existing:
            return
        req.system_prompt = f"{existing}\n\n{prompt}" if existing else prompt
