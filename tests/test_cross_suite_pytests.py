from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_cross_suite_pytests.py"
)
SPEC = importlib.util.spec_from_file_location("run_cross_suite_pytests", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_cross_suite_runner_uses_isolated_pytest_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path, bool]] = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = MODULE.main(
        [
            "--dc-engine",
            "dc_engines/tests/test_one.py::test_one",
            "--root",
            "tests/test_two.py::test_two",
        ]
    )

    assert result == 0
    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "pytest",
                "dc_engines/tests/test_one.py::test_one",
                "-q",
            ],
            Path(__file__).resolve().parents[1],
            False,
        ),
        (
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_two.py::test_two",
                "-q",
            ],
            Path(__file__).resolve().parents[1],
            False,
        ),
    ]


def test_cross_suite_runner_stops_after_dc_engine_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_run(_command, *, cwd, check):
        nonlocal calls
        calls += 1
        assert cwd == Path(__file__).resolve().parents[1]
        assert check is False
        return SimpleNamespace(returncode=3)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = MODULE.main(
        [
            "--dc-engine",
            "dc_engines/tests/test_one.py",
            "--root",
            "tests/test_two.py",
        ]
    )

    assert result == 3
    assert calls == 1


def test_cross_root_contract_verifiers_use_isolated_runner() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative_path in (
        "harness/contracts/organization_permission_model.json",
        "harness/contracts/employee_insight_loop.json",
        "harness/contracts/executor_settlement.json",
        "harness/contracts/codex_advanced_executor.json",
    ):
        contract = json.loads((root / relative_path).read_text(encoding="utf-8"))
        for criterion in contract["acceptance_criteria"]:
            command = criterion["verification"]
            if "dc_engines/tests/" in command and " tests/" in command:
                assert command.startswith(
                    "uv run python scripts/run_cross_suite_pytests.py "
                )
