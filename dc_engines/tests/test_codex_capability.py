from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from dc_engines.codex_capability import (
    CODEX_DEVELOPMENT_DEFAULT_REASONING,
    CODEX_DEVELOPMENT_MIN_REASONING,
    CODEX_DEVELOPMENT_MODEL,
    authorize_codex,
    list_codex_capabilities,
    validate_codex_development_runtime,
)


def test_capability_catalog_is_truthful_and_never_owns_schedule() -> None:
    catalog = list_codex_capabilities()
    by_id = {item.capability: item for item in catalog}

    assert set(by_id) == {
        "deep_reasoning",
        "image_generation",
        "incident_diagnosis",
        "project_engineering",
    }
    assert all(item.owns_schedule is False for item in catalog)
    assert by_id["deep_reasoning"].authorized_by == ("user",)
    assert "飞书消息：Codex 高级工具" in by_id["deep_reasoning"].entrypoints
    assert by_id["incident_diagnosis"].authorized_by == ("deterministic_controller",)
    assert by_id["project_engineering"].authorized_by == ("local_operator",)
    assert by_id["project_engineering"].execution_mode == "local_workspace_write"


@pytest.mark.parametrize(
    ("capability", "authorized_by"),
    [
        ("deep_reasoning", "user"),
        ("image_generation", "user"),
        ("incident_diagnosis", "deterministic_controller"),
        ("project_engineering", "local_operator"),
    ],
)
def test_known_capability_authority_pairs_are_allowed(
    capability: str,
    authorized_by: str,
) -> None:
    decision = authorize_codex(capability, authorized_by=authorized_by)

    assert decision.allowed is True
    assert decision.role == "advanced_executor"
    assert decision.owns_schedule is False
    assert decision.reason == "authorized"


@pytest.mark.parametrize(
    ("capability", "authorized_by"),
    [
        ("deep_reasoning", "deterministic_controller"),
        ("image_generation", "deterministic_controller"),
        ("incident_diagnosis", "user"),
        ("project_engineering", "user"),
        ("deep_reasoning", "codex"),
        ("scheduled_automation", "user"),
    ],
)
def test_unknown_or_mismatched_authority_is_denied(
    capability: str,
    authorized_by: str,
) -> None:
    decision = authorize_codex(capability, authorized_by=authorized_by)

    assert decision.allowed is False
    assert decision.role == "advanced_executor"
    assert decision.reason == "authority_not_allowed"


def test_scheduler_ownership_is_always_denied() -> None:
    decision = authorize_codex(
        "deep_reasoning",
        authorized_by="user",
        owns_schedule=True,
    )

    assert decision.allowed is False
    assert decision.owns_schedule is True
    assert decision.reason == "scheduler_ownership_denied"


@pytest.mark.parametrize("reasoning_effort", ["high", "xhigh", "max"])
def test_development_runtime_accepts_exact_model_at_or_above_high(
    reasoning_effort: str,
) -> None:
    decision = validate_codex_development_runtime(
        model="gpt-5.6-sol",
        reasoning_effort=reasoning_effort,
    )

    assert decision.allowed is True
    assert decision.reason == "authorized"
    assert decision.required_model == "gpt-5.6-sol"
    assert decision.minimum_reasoning_effort == "high"
    assert decision.default_reasoning_effort == "max"


@pytest.mark.parametrize("reasoning_effort", ["none", "minimal", "low", "medium"])
def test_development_runtime_rejects_reasoning_below_high(
    reasoning_effort: str,
) -> None:
    decision = validate_codex_development_runtime(
        model="gpt-5.6-sol",
        reasoning_effort=reasoning_effort,
    )

    assert decision.allowed is False
    assert decision.reason == "development_reasoning_too_low"


def test_development_runtime_rejects_wrong_model_and_unknown_effort() -> None:
    wrong_model = validate_codex_development_runtime(
        model="gpt-5.4",
        reasoning_effort="max",
    )
    unknown_effort = validate_codex_development_runtime(
        model="gpt-5.6-sol",
        reasoning_effort="ultra",
    )

    assert wrong_model.allowed is False
    assert wrong_model.reason == "development_model_required"
    assert unknown_effort.allowed is False
    assert unknown_effort.reason == "unknown_reasoning_effort"


def test_development_runtime_constants_match_mandatory_policy() -> None:
    assert CODEX_DEVELOPMENT_MODEL == "gpt-5.6-sol"
    assert CODEX_DEVELOPMENT_MIN_REASONING == "high"
    assert CODEX_DEVELOPMENT_DEFAULT_REASONING == "max"


def test_module_cli_emits_machine_readable_authorization() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dc_engines.codex_capability",
            "authorize",
            "--capability",
            "incident_diagnosis",
            "--authorized-by",
            "deterministic_controller",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "allowed": True,
        "authorized_by": "deterministic_controller",
        "capability": "incident_diagnosis",
        "owns_schedule": False,
        "reason": "authorized",
        "role": "advanced_executor",
    }


def test_module_cli_emits_machine_readable_catalog() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dc_engines.codex_capability", "catalog"],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    catalog = json.loads(result.stdout)
    assert {item["capability"] for item in catalog} == {
        "deep_reasoning",
        "image_generation",
        "incident_diagnosis",
        "project_engineering",
    }
    assert all(item["owns_schedule"] is False for item in catalog)


def test_module_cli_validates_development_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dc_engines.codex_capability",
            "validate-development",
            "--model",
            "gpt-5.6-sol",
            "--reasoning-effort",
            "max",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "allowed": True,
        "default_reasoning_effort": "max",
        "minimum_reasoning_effort": "high",
        "model": "gpt-5.6-sol",
        "reason": "authorized",
        "reasoning_effort": "max",
        "required_model": "gpt-5.6-sol",
    }
