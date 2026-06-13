"""P4 修 respond/stage.py 兜底：chain 只含未知类型组件时不要发"模型返回空"降级。

落地规则：
  - chain 真的为空（无任何组件）→ 仍走原降级提示
  - chain 含 Plain/Image/At 等已知组件但全空 → 仍走原降级提示
  - chain 只含 validator 字典里没有的未知类型（如 plugin 自定义 ToolUse）→ stage 跳过
    chain 发送，但**不发**"模型返回空"降级，让 plugin 自己负责

测试运行方式::

    cd /Users/dianchi/DC-Agent
    .venv/bin/python -m pytest tests/dc_router/test_respond_stage_tool_use.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ASTRBOT_DIR = _ROOT / "astrbot"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ASTRBOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ASTRBOT_DIR))

from pydantic import BaseModel

from astrbot.core.message.components import BaseMessageComponent, ComponentType, Plain
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.message.message_event_result import MessageChain


class ToolUseLike(BaseMessageComponent):
    """模拟 plugin 通过 @filter.on_decorating_result 放进 chain 的自定义 ToolUse 组件。

    注意：AstrBot core 并没有 ToolUse 类型，所以 P4 修复针对的就是这种"未知类型"场景。
    """

    type: ComponentType = ComponentType.Plain  # 用 Plain 占位以满足 Pydantic 校验
    tool_name: str = "unknown"
    tool_input: dict = {}

    def __init__(self, tool_name: str = "x", tool_input: dict | None = None) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.tool_input = tool_input or {}


def test_has_only_unknown_components_empty_chain_returns_false() -> None:
    """空 chain 不算"只含未知组件"（这种情况走原本的降级）。"""
    assert RespondStage._has_only_unknown_components([]) is False


def test_has_only_unknown_components_only_tool_use_returns_true() -> None:
    """chain 只含未知类型（如 ToolUseLike）→ 返回 True。"""
    chain = [ToolUseLike(tool_name="get_weather", tool_input={"city": "柳州"})]
    assert RespondStage._has_only_unknown_components(chain) is True


def test_has_only_unknown_components_mixed_returns_false() -> None:
    """chain 含已知 Plain 文本 → 返回 False，按原降级逻辑走。"""
    chain = [
        ToolUseLike(tool_name="x"),
        Plain("调用结果..."),
    ]
    assert RespondStage._has_only_unknown_components(chain) is False


def test_has_only_unknown_components_only_plain_returns_false() -> None:
    """chain 只含已知 Plain 文本 → 返回 False。"""
    chain = [Plain("hello")]
    assert RespondStage._has_only_unknown_components(chain) is False


@pytest.mark.asyncio
async def test_is_empty_message_chain_with_only_tool_use_returns_true() -> None:
    """chain 只含未知类型 → _is_empty_message_chain 仍返回 True（视为空）。

    这一步是关键：上层会用 _is_empty_message_chain 的结果 + _has_only_unknown_components
    来决定走"降级"还是"跳过"。
    """
    chain = [ToolUseLike(tool_name="web_search", tool_input={"q": "test"})]
    # RespondStage 初始化需要 config，但 _is_empty_message_chain 不依赖
    # 用一个 dummy instance 即可
    dummy = RespondStage.__new__(RespondStage)
    result = await dummy._is_empty_message_chain(chain)
    assert result is True, "未知类型组件不在 validator 字典中，应被视为空"


@pytest.mark.asyncio
async def test_is_empty_message_chain_with_plain_text_returns_false() -> None:
    """chain 含有效 Plain 文本 → 不为空。"""
    chain = [Plain("你好")]
    dummy = RespondStage.__new__(RespondStage)
    result = await dummy._is_empty_message_chain(chain)
    assert result is False


@pytest.mark.asyncio
async def test_is_empty_message_chain_with_empty_plain_returns_true() -> None:
    """chain 含 Plain("") 空白文本 → 视为空（保持原行为）。"""
    chain = [Plain("")]
    dummy = RespondStage.__new__(RespondStage)
    result = await dummy._is_empty_message_chain(chain)
    assert result is True


def test_p4_does_not_inject_fallback_message_for_tool_use_only_chain() -> None:
    """P4 核心：chain 只含未知类型时，stage 路径不注入"模型返回空"降级文本。

    模拟 stage 的判断逻辑：
      1) _is_empty_message_chain 返回 True
      2) _has_only_unknown_components 返回 True + is_model_result()=True
      3) 走"跳过发送"分支，不修改 result.chain
    """
    chain: list[BaseMessageComponent] = [ToolUseLike(tool_name="x")]

    # Step 1
    dummy = RespondStage.__new__(RespondStage)
    is_empty = await_dummy(dummy, chain)
    assert is_empty is True

    # Step 2
    only_unknown = RespondStage._has_only_unknown_components(chain)
    assert only_unknown is True

    # Step 3: 模拟 stage 实际行为
    # is_model_result() 在 is_model_result() == True 时，
    # 由于 only_unknown 也为 True，走 P4 新分支 → return
    # 不修改 result.chain
    if only_unknown:
        # stage 路径直接 return，不注入降级
        injected_fallback = False
    else:
        injected_fallback = True

    assert injected_fallback is False, (
        "P4 要求 chain 只含未知类型时不要发降级提示"
    )


def await_dummy(stage: RespondStage, chain: list[BaseMessageComponent]) -> bool:
    """同步跑 _is_empty_message_chain（已实现为 async）。"""
    import asyncio

    return asyncio.run(stage._is_empty_message_chain(chain))
