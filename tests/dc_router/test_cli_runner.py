"""Policy tests for the controlled Codex CLI subprocess Adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from data.plugins.dc_router.cli_runner import CliResult, CliRunner


@pytest.mark.asyncio
async def test_codex_analysis_remains_read_only() -> None:
    runner = CliRunner()
    runner._run_text_cli = AsyncMock(return_value=CliResult(text="done"))

    result = await runner.run_codex(
        "review this",
        model="gpt-5.4",
        reasoning_effort="high",
        task_kind="analysis",
        authorized_by="user",
    )

    assert result.ok is True
    args = runner._run_text_cli.await_args.args[0]
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--model") + 1] == "gpt-5.4"
    assert 'model_reasoning_effort="high"' in args


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_effort", ["high", "xhigh", "max"])
async def test_codex_development_uses_required_model_and_workspace_write(
    reasoning_effort: str,
) -> None:
    runner = CliRunner()
    runner._run_text_cli = AsyncMock(return_value=CliResult(text="done"))

    result = await runner.run_codex(
        "implement the change",
        model="gpt-5.6-sol",
        reasoning_effort=reasoning_effort,
        task_kind="development",
        authorized_by="local_operator",
    )

    assert result.ok is True
    args = runner._run_text_cli.await_args.args[0]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("--model") + 1] == "gpt-5.6-sol"
    assert f'model_reasoning_effort="{reasoning_effort}"' in args


@pytest.mark.asyncio
async def test_codex_development_defaults_to_max_reasoning() -> None:
    runner = CliRunner()
    runner._run_text_cli = AsyncMock(return_value=CliResult(text="done"))

    result = await runner.run_codex(
        "implement the change",
        model="gpt-5.6-sol",
        task_kind="development",
        authorized_by="local_operator",
    )

    assert result.ok is True
    args = runner._run_text_cli.await_args.args[0]
    assert 'model_reasoning_effort="max"' in args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "reasoning_effort", "authorized_by", "reason"),
    [
        ("gpt-5.4", "high", "local_operator", "development_model_required"),
        ("gpt-5.6-sol", "medium", "local_operator", "development_reasoning_too_low"),
        ("gpt-5.6-sol", "high", "user", "authority_not_allowed"),
        ("gpt-5.6-sol", "ultra", "local_operator", "unknown_reasoning_effort"),
    ],
)
async def test_codex_development_policy_denial_never_starts_subprocess(
    model: str,
    reasoning_effort: str,
    authorized_by: str,
    reason: str,
) -> None:
    runner = CliRunner()
    runner._run_text_cli = AsyncMock(return_value=CliResult(text="unexpected"))

    result = await runner.run_codex(
        "implement the change",
        model=model,
        reasoning_effort=reasoning_effort,
        task_kind="development",
        authorized_by=authorized_by,
    )

    assert result.ok is False
    assert result.error_code == "development_policy_denied"
    assert result.error == reason
    runner._run_text_cli.assert_not_awaited()
