from __future__ import annotations

from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest

DC_MEMORY_CONTEXT_OPEN_MARKER = "<dc_agent_memory_context>"
DC_MEMORY_CONTEXT_CLOSE_MARKER = "</dc_agent_memory_context>"
DC_MEMORY_CONTEXT_PRIORITY_PROMPT = (
    "When a user gives a short follow-up or feedback, interpret it using the "
    "recent conversation history first. Any DC-Agent long-term memory context "
    "is lower-priority factual reference only; it must not change the current "
    "project, customer, brand, or task target established by recent turns."
)


class RuntimeContextAssembler:
    def normalize(self, req: ProviderRequest) -> None:
        self._demote_dc_memory_context(req)

    def _demote_dc_memory_context(self, req: ProviderRequest) -> None:
        if not req.prompt or DC_MEMORY_CONTEXT_OPEN_MARKER not in req.prompt:
            return

        prompt_without_memory, memory_block = self._split_dc_memory_context_from_prompt(
            req.prompt
        )
        if not memory_block:
            return

        req.prompt = prompt_without_memory
        req.extra_user_content_parts.append(
            TextPart(
                text=(
                    "[Lower-priority DC-Agent long-term memory reference]\n"
                    "Use this only as factual support after resolving the user's "
                    "current intent from recent conversation history.\n"
                    f"{memory_block}"
                )
            ).mark_as_temp()
        )
        self._append_system_prompt_once(req, DC_MEMORY_CONTEXT_PRIORITY_PROMPT)

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
