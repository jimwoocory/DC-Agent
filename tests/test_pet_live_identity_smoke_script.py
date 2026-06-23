from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts-tools" / "pet_live_identity_smoke.py"
SPEC = importlib.util.spec_from_file_location("pet_live_identity_smoke", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
pet_live_identity_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pet_live_identity_smoke
SPEC.loader.exec_module(pet_live_identity_smoke)


def _json_body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_mutating_bind_stability_uses_employee_identity(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 4.0,
    ) -> tuple[int, str]:
        calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "payload": payload or {},
                "timeout": timeout,
            }
        )
        if url.endswith("/api/pet/bind-desktop"):
            assert headers is not None
            assert headers["Cookie"] == "dc_feishu_session=sess_1"
            assert headers["X-Pet-Live-Bind-Token"] == "bind-secret"
            assert payload is not None
            assert payload["employee_id"] == "emp_001"
            return 200, _json_body(
                {
                    "status": "ok",
                    "data": {
                        "identity": {
                            "pet_id": "pet_stable",
                            "employee_id": "emp_001",
                            "desktop_session_id": "sess_1",
                        }
                    },
                }
            )
        assert url.endswith("/api/pet/me")
        return 200, _json_body(
            {
                "status": "ok",
                "data": {
                    "identity": {
                        "pet_id": "pet_stable",
                        "employee_id": "emp_001",
                        "desktop_session_id": "sess_1",
                    }
                },
            }
        )

    monkeypatch.setenv("PET_LIVE_BINDING_TOKEN", "bind-secret")
    monkeypatch.setattr(pet_live_identity_smoke, "_request", fake_request)

    result = pet_live_identity_smoke.check_mutating_bind_stability(
        "http://127.0.0.1:6185",
        employee_id="emp_001",
        open_id="ou_old",
        rotated_open_id="ou_new",
        session_id="sess_1",
    )

    assert result.ok
    assert result.name == "bind:employee-stable-pet-id"
    assert [call["payload"].get("feishu_open_id") for call in calls[:2]] == [
        "ou_old",
        "ou_new",
    ]


def test_workspace_message_requires_provider_id_when_expected(monkeypatch) -> None:
    def fake_request(
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 4.0,
    ) -> tuple[int, str]:
        assert headers == {"Cookie": "dc_feishu_session=sess_1"}
        assert timeout == 12.0
        if method == "GET":
            assert url.endswith("/api/workspace/messages?conversation_id=ou_user")
            return 200, _json_body(
                {
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "hello",
                            }
                        ]
                    },
                }
            )
        assert method == "POST"
        assert url.endswith("/api/workspace/messages")
        assert payload == {"conversation_id": "ou_user", "content": "hello"}
        return 200, _json_body(
            {
                "status": "ok",
                "data": {
                    "event_id": 9,
                    "sent": False,
                },
            }
        )

    monkeypatch.setattr(pet_live_identity_smoke, "_request", fake_request)

    result = pet_live_identity_smoke.check_workspace_message(
        "http://127.0.0.1:6185",
        conversation_id="ou_user",
        session_id="sess_1",
        text="hello",
        expect_feishu_send=True,
        allow_real_feishu_send=True,
    )

    assert not result.ok
    assert "sent=false" in result.detail
    assert "provider_message_id=false" in result.detail
    assert "history_contains_content=true" in result.detail


def test_workspace_message_skips_when_real_send_enabled_without_allow(
    monkeypatch,
) -> None:
    def fail_request(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("workspace send should not be called")

    monkeypatch.setenv("WORKSPACE_FEISHU_SEND_ENABLED", "1")
    monkeypatch.setattr(pet_live_identity_smoke, "_request", fail_request)

    result = pet_live_identity_smoke.check_workspace_message(
        "http://127.0.0.1:6185",
        conversation_id="ou_user",
        session_id="sess_1",
        text="hello",
        expect_feishu_send=True,
        allow_real_feishu_send=False,
    )

    assert not result.ok
    assert "--allow-real-feishu-send" in result.detail
