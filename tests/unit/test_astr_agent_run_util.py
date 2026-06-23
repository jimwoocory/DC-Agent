from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot.core.agent.response import AgentResponse, AgentResponseData
from astrbot.core.astr_agent_run_util import (
    _EmptyLLMResultPolicy,
    _is_empty_llm_chain,
    run_agent,
)
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
)


def test_empty_llm_chain_with_no_components_is_empty() -> None:
    assert _is_empty_llm_chain(MessageChain()) is True


def test_empty_llm_chain_with_blank_plain_is_empty() -> None:
    chain = MessageChain(chain=[Plain(" "), Plain("\n\t")])

    assert _is_empty_llm_chain(chain) is True


def test_empty_llm_chain_with_text_is_sendable() -> None:
    chain = MessageChain(chain=[Plain("收到")])

    assert _is_empty_llm_chain(chain) is False


def test_empty_llm_chain_with_non_plain_component_is_sendable() -> None:
    chain = MessageChain(chain=[Image.fromURL("https://example.com/a.png")])

    assert _is_empty_llm_chain(chain) is False


def test_empty_llm_policy_skips_only_intermediate_empty_results() -> None:
    policy = _EmptyLLMResultPolicy()

    assert policy.should_skip_intermediate(MessageChain(), runner_done=False) is True
    assert policy.should_skip_intermediate(MessageChain(), runner_done=True) is False


def test_empty_llm_policy_requests_convergence_after_threshold() -> None:
    policy = _EmptyLLMResultPolicy(max_consecutive_empty_results=2)

    assert policy.record_empty_intermediate() is False
    assert policy.record_empty_intermediate() is True

    policy.record_sendable_result()

    assert policy.consecutive_empty_results == 0


class _FakeTrace:
    def record(self, *_args, **_kwargs) -> None:
        return None


class _FakeEvent:
    def __init__(self) -> None:
        self.trace = _FakeTrace()
        self.result: MessageEventResult | None = None
        self.extras: dict[str, object] = {}

    def is_stopped(self) -> bool:
        return False

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def set_result(self, result: MessageEventResult) -> None:
        self.result = result

    def clear_result(self) -> None:
        self.result = None

    def get_platform_name(self) -> str:
        return "lark"

    def get_platform_id(self) -> str:
        return "巅池-Agent小助手"


class _FakeRunner:
    streaming = False
    req = SimpleNamespace(func_tool=object())
    stats = SimpleNamespace(to_dict=lambda: {})

    def __init__(self, *, done_before_llm_result: bool) -> None:
        self._done = False
        self.done_before_llm_result = done_before_llm_result
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=_FakeEvent()))
        self.stop_requested = False

    def done(self) -> bool:
        return self._done

    def request_stop(self) -> None:
        self.stop_requested = True

    async def step(self):
        if self.done_before_llm_result:
            self._done = True
        yield AgentResponse(
            type="llm_result",
            data=AgentResponseData(chain=MessageChain()),
        )
        self._done = True


@pytest.mark.asyncio
async def test_run_agent_skips_empty_intermediate_llm_result() -> None:
    runner = _FakeRunner(done_before_llm_result=False)

    yielded = [
        chain async for chain in run_agent(runner, max_step=1, show_tool_use=False)
    ]

    assert yielded == []


@pytest.mark.asyncio
async def test_run_agent_yields_empty_final_llm_result_for_respond_fallback() -> None:
    runner = _FakeRunner(done_before_llm_result=True)

    yielded = [
        chain async for chain in run_agent(runner, max_step=1, show_tool_use=False)
    ]

    assert len(yielded) == 1
    assert isinstance(yielded[0], MessageChain)
    assert yielded[0].chain == []
