"""Tests for the NAS media provider OAuth login manager."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute

from astrbot.dashboard.api import media_auth
from astrbot.dashboard.services.media_auth_service import (
    CommandResult,
    MediaAuthService,
)


def _jwt(expiry: datetime) -> str:
    """Build an unsigned JWT-shaped token for expiry parsing tests.

    Args:
        expiry: Token expiration timestamp.

    Returns:
        JWT-shaped string containing only the expiration claim.
    """
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(expiry.timestamp())}).encode())
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def test_contract_covers_dashboard_login_and_deterministic_provider_choice() -> None:
    contract = json.loads(
        Path("harness/contracts/media_provider_login_manager.json").read_text(
            encoding="utf-8"
        )
    )

    assert contract["feature"] == "media_provider_login_manager"
    assert {item["id"] for item in contract["acceptance_criteria"]} == {
        "media-provider-login-001",
        "media-provider-login-002",
        "media-provider-login-003",
        "media-provider-login-004",
    }


def test_all_media_auth_routes_require_provider_scope() -> None:
    routes = [
        route for route in media_auth.router.routes if isinstance(route, APIRoute)
    ]

    assert routes
    assert all(
        any(
            dependency.call is media_auth.require_provider_scope
            for dependency in route.dependant.dependencies
        )
        for route in routes
    )


def test_authorization_material_parses_dreamina_json_without_exposing_tokens() -> None:
    output = json.dumps(
        {
            "verification_uri": "https://example.test/device",
            "user_code": "ABCD-EFGH",
            "device_code": "device-secret",
            "access_token": "must-not-leak",
        }
    )

    material = MediaAuthService._authorization_material(output)
    sanitized = MediaAuthService._safe_output(output)

    assert material == {
        "verification_url": "https://example.test/device",
        "user_code": "ABCD-EFGH",
        "device_code": "device-secret",
    }
    assert "must-not-leak" not in sanitized
    assert "[REDACTED]" in sanitized


def test_authorization_material_parses_real_codex_ansi_output() -> None:
    output = (
        "Follow these steps to sign in with ChatGPT using device code authorization:\n"
        "1. Open this link\n"
        "\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n"
        "2. Enter this one-time code (expires in 15 minutes)\n"
        "\x1b[94m9FH1-2V7IJ\x1b[0m\n"
    )

    material = MediaAuthService._authorization_material(output)

    assert material["verification_url"] == "https://auth.openai.com/codex/device"
    assert material["user_code"] == "9FH1-2V7IJ"


def test_authorization_material_parses_real_dreamina_plain_text_output() -> None:
    output = (
        "请使用浏览器完成 OAuth Device Flow 登录。\n"
        "verification_uri: https://jimeng.jianying.com/ai-tool/cli-auth?"
        "verification_uri=https%3A%2F%2Fjimeng.jianying.com%2Fpassport%2Fopen%2F"
        "scan_user_code%2F%3Fuser_code%3D9d1a50b8c14f2584f28386c51beb946e\n"
        "user_code: 9d1a50b8c14f2584f28386c51beb946e\n"
        "device_code: dfe61c699d819f11abfe6b9f1f299aea\n"
        "poll_interval: 1s\n"
        "expires_at: 2026-07-16T18:08:36+08:00\n"
    )

    material = MediaAuthService._authorization_material(output)

    assert material == {
        "verification_url": (
            "https://jimeng.jianying.com/ai-tool/cli-auth?"
            "verification_uri=https%3A%2F%2Fjimeng.jianying.com%2Fpassport%2Fopen%2F"
            "scan_user_code%2F%3Fuser_code%3D9d1a50b8c14f2584f28386c51beb946e"
        ),
        "user_code": "9d1a50b8c14f2584f28386c51beb946e",
        "device_code": "dfe61c699d819f11abfe6b9f1f299aea",
    }


@pytest.mark.asyncio
async def test_codex_status_reads_persisted_auth_and_never_returns_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    token = _jwt(datetime.now(UTC) + timedelta(hours=2))
    (codex_dir / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": token, "refresh_token": "refresh"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    service = MediaAuthService()
    monkeypatch.setattr(service, "_executable", lambda provider: "/usr/bin/codex")
    monkeypatch.setattr(
        service,
        "_run",
        AsyncMock(return_value=CommandResult(0, "Logged in using ChatGPT", "")),
    )

    status = await service._codex_status()
    serialized = json.dumps(status, ensure_ascii=False)

    assert status["authenticated"] is True
    assert status["credential_present"] is True
    assert status["expires_at"]
    assert token not in serialized
    assert "refresh" not in serialized


@pytest.mark.asyncio
async def test_dreamina_status_uses_inherited_aws_proxy_and_reports_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://aihubmix-aws-tunnel:7898")
    service = MediaAuthService()
    monkeypatch.setattr(
        service, "_executable", lambda provider: "/usr/local/bin/dreamina"
    )
    monkeypatch.setattr(
        service,
        "_run",
        AsyncMock(
            return_value=CommandResult(
                0,
                '{"total_credit":"88","access_token":"never-return"}',
                "",
            )
        ),
    )

    status = await service._dreamina_status()

    assert status["authenticated"] is True
    assert status["credits"] == "88"
    assert status["proxy"] == {
        "configured": True,
        "route": "aihubmix-aws-tunnel:7898",
    }
    assert "never-return" not in json.dumps(status)


@pytest.mark.asyncio
async def test_dreamina_login_start_returns_only_device_flow_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MediaAuthService()
    monkeypatch.setattr(
        service, "_executable", lambda provider: "/usr/local/bin/dreamina"
    )
    monkeypatch.setattr(
        service,
        "_run",
        AsyncMock(
            return_value=CommandResult(
                0,
                json.dumps(
                    {
                        "verification_uri": "https://dreamina.test/device",
                        "user_code": "DREAM-1234",
                        "device_code": "device-code",
                    }
                ),
                "",
            )
        ),
    )

    result = await service.start_login("dreamina")

    assert result == {
        "provider": "dreamina",
        "state": "pending",
        "verification_url": "https://dreamina.test/device",
        "user_code": "DREAM-1234",
        "device_code": "device-code",
    }


@pytest.mark.asyncio
async def test_dreamina_empty_checklogin_response_remains_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MediaAuthService()
    monkeypatch.setattr(
        service, "_executable", lambda provider: "/usr/local/bin/dreamina"
    )
    monkeypatch.setattr(
        service,
        "_run",
        AsyncMock(return_value=CommandResult(1, "", "")),
    )

    result = await service.check_login(
        "dreamina",
        device_code="device-code",
        poll_seconds=0,
    )

    assert result == {
        "provider": "dreamina",
        "state": "pending",
        "detail": "等待即梦授权完成",
    }


def test_nas_runtime_persists_both_oauth_stores_and_linux_keyring() -> None:
    compose = Path("deploy/nas-unified/compose.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile.nas").read_text(encoding="utf-8")
    entrypoint = Path("deploy/nas-unified/runtime-bin/start-with-keyring.sh").read_text(
        encoding="utf-8"
    )

    assert "./runtime-secrets/codex:/root/.codex" in compose
    assert "./runtime-secrets/dreamina:/root/.dreamina_cli" in compose
    assert "./runtime-secrets/dreamina-keyring:/root/.local/share/keyrings" in compose
    assert 'HTTPS_PROXY: "http://aihubmix-aws-tunnel:7898"' in compose
    assert "@openai/codex@0.144.1" in dockerfile
    assert "gnome-keyring" in dockerfile
    assert "dbus-run-session" in entrypoint


def test_provider_dashboard_mounts_media_login_panel() -> None:
    provider_page = Path("dashboard/src/views/ProviderPage.vue").read_text(
        encoding="utf-8"
    )
    panel = Path("dashboard/src/components/provider/MediaAuthPanel.vue").read_text(
        encoding="utf-8"
    )

    assert "<MediaAuthPanel" in provider_page
    assert "Image2" in panel
    assert "即梦" in panel
    assert "mediaAuthApi.startLogin" in panel
    assert "mediaAuthApi.checkLogin" in panel
    assert "access_token" not in panel
