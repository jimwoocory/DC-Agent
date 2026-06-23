from __future__ import annotations

from dc_engines.dreamina_cli import (
    dreamina_command_not_found_message,
    resolve_dreamina_executable,
)


def test_resolve_dreamina_executable_uses_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable({"PATH": str(bin_dir)}, home=tmp_path) == str(
        executable
    )


def test_resolve_dreamina_executable_falls_back_to_local_bin(tmp_path):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    executable = local_bin / "dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable({"PATH": ""}, home=tmp_path) == str(executable)


def test_resolve_dreamina_executable_honors_override(tmp_path):
    executable = tmp_path / "custom-dreamina"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert resolve_dreamina_executable(
        {"PATH": "", "DREAMINA_CLI_PATH": str(executable)}, home=tmp_path
    ) == str(executable)


def test_dreamina_command_not_found_message_names_override():
    assert "DREAMINA_CLI_PATH" in dreamina_command_not_found_message()
