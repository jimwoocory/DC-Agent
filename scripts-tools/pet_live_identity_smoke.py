#!/usr/bin/env python3
"""Smoke checks for the Pet Live desktop identity link.

The default mode is read-only except for negative bind requests that should be
rejected before mutating state. Use --mutating-bind only when the operator wants
to create/update a desktop identity link in the live Pet Live store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

DEFAULT_DC_AGENT_BASE_URL = "http://127.0.0.1:6185"
DEFAULT_WEBUI_BASE_URL = "http://127.0.0.1:8032"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _env_present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _workspace_sender_enabled() -> bool:
    return os.environ.get("WORKSPACE_FEISHU_SEND_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 4.0,
) -> tuple[int | None, str]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except OSError as exc:
        return None, str(exc)


def _json_status(body: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    status = data.get("status")
    message = data.get("message")
    return (
        status if isinstance(status, str) else None,
        message if isinstance(message, str) else None,
    )


def _json_payload(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def check_required_env() -> list[CheckResult]:
    required = (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "PET_LIVE_BINDING_TOKEN",
    )
    results = []
    for name in required:
        results.append(
            CheckResult(
                f"env:{name}",
                _env_present(name),
                "present" if _env_present(name) else "missing",
            )
        )
    return results


def check_pet_assets(dc_agent_base_url: str) -> CheckResult:
    url = urllib.parse.urljoin(
        dc_agent_base_url.rstrip("/") + "/",
        "api/pet/assets/sanmaomao",
    )
    code, body = _request("GET", url)
    status, message = _json_status(body)
    ok = code == HTTPStatus.OK and status == "ok"
    detail = f"http={code} status={status or '-'} message={message or '-'}"
    return CheckResult("dc-agent:pet-assets", ok, detail)


def check_bind_negative(dc_agent_base_url: str) -> list[CheckResult]:
    url = urllib.parse.urljoin(
        dc_agent_base_url.rstrip("/") + "/",
        "api/pet/bind-desktop",
    )
    payload = {
        "feishu_open_id": "ou_pet_live_smoke",
        "employee_id": "pet_live_smoke",
    }
    results: list[CheckResult] = []

    code, body = _request("POST", url, payload=payload)
    status, message = _json_status(body)
    results.append(
        CheckResult(
            "bind:missing-cookie",
            code == HTTPStatus.OK
            and status == "error"
            and message == "dc_feishu_session cookie is required",
            f"http={code} status={status or '-'} message={message or '-'}",
        )
    )

    code, body = _request(
        "POST",
        url,
        headers={
            "Cookie": "dc_feishu_session=pet_live_smoke_session",
            "X-Pet-Live-Bind-Token": "wrong-token",
        },
        payload=payload,
    )
    status, message = _json_status(body)
    results.append(
        CheckResult(
            "bind:wrong-token",
            code == HTTPStatus.OK
            and status == "error"
            and message == "pet live binding is not authorized",
            f"http={code} status={status or '-'} message={message or '-'}",
        )
    )
    return results


def _bind_desktop(
    dc_agent_base_url: str,
    *,
    employee_id: str,
    open_id: str,
    session_id: str,
) -> tuple[int | None, dict[str, Any]]:
    token = os.environ.get("PET_LIVE_BINDING_TOKEN", "").strip()
    if not token:
        return None, {"status": "error", "message": "skipped: token missing"}
    url = urllib.parse.urljoin(
        dc_agent_base_url.rstrip("/") + "/",
        "api/pet/bind-desktop",
    )
    code, body = _request(
        "POST",
        url,
        headers={
            "Cookie": f"dc_feishu_session={session_id}",
            "X-Pet-Live-Bind-Token": token,
        },
        payload={
            "feishu_open_id": open_id,
            "employee_id": employee_id,
        },
    )
    return code, _json_payload(body)


def check_mutating_bind(
    dc_agent_base_url: str,
    open_id: str,
    session_id: str,
    *,
    employee_id: str,
) -> CheckResult:
    code, payload = _bind_desktop(
        dc_agent_base_url,
        employee_id=employee_id,
        open_id=open_id,
        session_id=session_id,
    )
    status = payload.get("status")
    message = payload.get("message")
    return CheckResult(
        "bind:correct-token",
        code == HTTPStatus.OK and status == "ok",
        f"http={code} status={status or '-'} message={message or '-'}",
    )


def check_mutating_bind_stability(
    dc_agent_base_url: str,
    *,
    employee_id: str,
    open_id: str,
    rotated_open_id: str,
    session_id: str,
) -> CheckResult:
    first_code, first_payload = _bind_desktop(
        dc_agent_base_url,
        employee_id=employee_id,
        open_id=open_id,
        session_id=session_id,
    )
    second_code, second_payload = _bind_desktop(
        dc_agent_base_url,
        employee_id=employee_id,
        open_id=rotated_open_id,
        session_id=session_id,
    )
    url = urllib.parse.urljoin(dc_agent_base_url.rstrip("/") + "/", "api/pet/me")
    me_code, me_body = _request(
        "GET",
        url,
        headers={"Cookie": f"dc_feishu_session={session_id}"},
    )
    me_payload = _json_payload(me_body)

    first_identity = _response_identity(first_payload)
    second_identity = _response_identity(second_payload)
    me_identity = _response_identity(me_payload)
    pet_id = first_identity.get("pet_id")
    ok = (
        first_code == HTTPStatus.OK
        and second_code == HTTPStatus.OK
        and me_code == HTTPStatus.OK
        and first_payload.get("status") == "ok"
        and second_payload.get("status") == "ok"
        and me_payload.get("status") == "ok"
        and bool(pet_id)
        and pet_id == second_identity.get("pet_id") == me_identity.get("pet_id")
        and me_identity.get("employee_id") == employee_id
        and me_identity.get("desktop_session_id") == session_id
    )
    detail = (
        f"first_http={first_code} second_http={second_code} me_http={me_code} "
        f"same_pet_id={str(bool(pet_id) and pet_id == second_identity.get('pet_id') == me_identity.get('pet_id')).lower()} "
        f"employee_id={me_identity.get('employee_id') or '-'} "
        f"session_bound={str(me_identity.get('desktop_session_id') == session_id).lower()}"
    )
    return CheckResult("bind:employee-stable-pet-id", ok, detail)


def check_workspace_message(
    dc_agent_base_url: str,
    *,
    conversation_id: str,
    session_id: str,
    text: str,
    expect_feishu_send: bool,
    allow_real_feishu_send: bool,
) -> CheckResult:
    if _workspace_sender_enabled() and not allow_real_feishu_send:
        return CheckResult(
            "workspace:send-message",
            False,
            "skipped: WORKSPACE_FEISHU_SEND_ENABLED=1 requires --allow-real-feishu-send",
        )
    url = urllib.parse.urljoin(
        dc_agent_base_url.rstrip("/") + "/",
        "api/workspace/messages",
    )
    code, body = _request(
        "POST",
        url,
        headers={"Cookie": f"dc_feishu_session={session_id}"},
        payload={"conversation_id": conversation_id, "content": text},
        timeout=12.0,
    )
    payload = _json_payload(body)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    sent = bool(data.get("sent"))
    has_provider_message_id = bool(data.get("provider_message_id"))
    ok = code == HTTPStatus.OK and payload.get("status") == "ok"
    if expect_feishu_send:
        ok = ok and sent and has_provider_message_id
    history_code, history_body = _request(
        "GET",
        f"{url}?{urllib.parse.urlencode({'conversation_id': conversation_id})}",
        headers={"Cookie": f"dc_feishu_session={session_id}"},
        timeout=12.0,
    )
    history_payload = _json_payload(history_body)
    history_data = (
        history_payload.get("data")
        if isinstance(history_payload.get("data"), dict)
        else {}
    )
    messages = (
        history_data.get("messages")
        if isinstance(history_data.get("messages"), list)
        else []
    )
    history_contains_content = any(
        isinstance(item, dict)
        and item.get("role") == "user"
        and item.get("content") == text
        for item in messages
    )
    ok = (
        ok
        and history_code == HTTPStatus.OK
        and history_payload.get("status") == "ok"
        and history_contains_content
    )
    detail = (
        f"http={code} status={payload.get('status') or '-'} "
        f"sent={str(sent).lower()} "
        f"provider_message_id={str(has_provider_message_id).lower()} "
        f"event_id={data.get('event_id') or '-'} "
        f"history_http={history_code} "
        f"history_contains_content={str(history_contains_content).lower()} "
        f"error={data.get('error') or '-'}"
    )
    return CheckResult("workspace:send-message", ok, detail)


def check_local_store_desktop_session(
    dc_agent_base_url: str,
    open_id: str,
    session_id: str,
    *,
    employee_id: str,
) -> CheckResult:
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(PROJECT_ROOT / "dc_engines"))
    from dc_engines.pet_live.identity import get_or_create_identity
    from dc_engines.pet_live.store import PetLiveStore

    store = PetLiveStore(PROJECT_ROOT / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id=open_id,
        employee_id=employee_id,
        desktop_session_id=session_id,
    )
    if identity.desktop_session_id != session_id:
        return CheckResult(
            "desktop-session:/api/pet/me",
            False,
            "local smoke identity is already bound to another desktop session",
        )

    url = urllib.parse.urljoin(dc_agent_base_url.rstrip("/") + "/", "api/pet/me")
    code, body = _request(
        "GET",
        url,
        headers={"Cookie": f"dc_feishu_session={session_id}"},
    )
    payload = _json_payload(body)
    status = payload.get("status")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    response_identity = (
        data.get("identity") if isinstance(data.get("identity"), dict) else {}
    )
    pet = data.get("pet") if isinstance(data.get("pet"), dict) else {}
    codex_pets = (
        data.get("codex_pets") if isinstance(data.get("codex_pets"), dict) else {}
    )
    ok = (
        code == HTTPStatus.OK
        and status == "ok"
        and response_identity.get("pet_id") == identity.pet_id
        and pet.get("pet_id") == identity.pet_id
        and codex_pets.get("pet_id") == identity.pet_id
    )
    detail = (
        f"http={code} status={status or '-'} "
        f"pet_id_match={str(ok).lower()} codex_state={codex_pets.get('codex_state') or '-'}"
    )
    return CheckResult("desktop-session:/api/pet/me", ok, detail)


def _response_identity(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    return identity


def check_webui_desktop_entry(webui_base_url: str) -> CheckResult:
    url = urllib.parse.urljoin(
        webui_base_url.rstrip("/") + "/",
        "auth/feishu/start?next=desktop",
    )
    code, body = _request("GET", url)
    if code is None:
        return CheckResult("webui:desktop-entry", False, f"unreachable: {body}")
    if code == HTTPStatus.FOUND:
        return CheckResult("webui:desktop-entry", True, "redirects to Feishu OAuth")
    if code == HTTPStatus.SERVICE_UNAVAILABLE:
        detail = "http=503 missing FEISHU_APP_ID/FEISHU_APP_SECRET"
        return CheckResult("webui:desktop-entry", False, detail)
    return CheckResult("webui:desktop-entry", False, f"http={code}")


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        marker = "OK" if result.ok else "BLOCKED"
        print(f"[{marker}] {result.name}: {result.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dc-agent-base-url",
        default=os.environ.get("DC_AGENT_BASE_URL", DEFAULT_DC_AGENT_BASE_URL),
    )
    parser.add_argument(
        "--webui-base-url",
        default=os.environ.get("PET_LIVE_WEBUI_BASE_URL", DEFAULT_WEBUI_BASE_URL),
    )
    parser.add_argument("--mutating-bind", action="store_true")
    parser.add_argument(
        "--local-store-session",
        action="store_true",
        help=(
            "Create/update a clearly marked local PetLiveStore smoke identity, "
            "then verify /api/pet/me with the desktop session cookie."
        ),
    )
    parser.add_argument("--open-id", default="ou_pet_live_smoke")
    parser.add_argument("--employee-id", default="pet_live_smoke")
    parser.add_argument("--rotated-open-id", default="")
    parser.add_argument("--desktop-session-id", default="desktop_pet_live_smoke")
    parser.add_argument(
        "--workspace-send-message",
        action="store_true",
        help=(
            "POST /api/workspace/messages with the desktop session. This writes "
            "a Pet Live outbox event and may send a real Feishu message when "
            "WORKSPACE_FEISHU_SEND_ENABLED=1."
        ),
    )
    parser.add_argument("--workspace-conversation-id", default="")
    parser.add_argument(
        "--workspace-message-text",
        default="Pet Live identity smoke: workspace message",
    )
    parser.add_argument(
        "--expect-feishu-send",
        action="store_true",
        help="Require the workspace message call to return sent=true and a provider message id.",
    )
    parser.add_argument(
        "--allow-real-feishu-send",
        action="store_true",
        help=(
            "Allow this smoke script to send a real Feishu message when "
            "WORKSPACE_FEISHU_SEND_ENABLED=1. Without this flag, workspace "
            "send checks are skipped instead of posting to Feishu."
        ),
    )
    parser.add_argument(
        "--skip-webui",
        action="store_true",
        help="Skip the WebUI OAuth desktop entry reachability check.",
    )
    args = parser.parse_args(argv)

    results: list[CheckResult] = []
    results.extend(check_required_env())
    results.append(check_pet_assets(args.dc_agent_base_url))
    results.extend(check_bind_negative(args.dc_agent_base_url))
    if args.mutating_bind:
        results.append(
            check_mutating_bind(
                args.dc_agent_base_url,
                args.open_id,
                args.desktop_session_id,
                employee_id=args.employee_id,
            )
        )
        results.append(
            check_mutating_bind_stability(
                args.dc_agent_base_url,
                employee_id=args.employee_id,
                open_id=args.open_id,
                rotated_open_id=args.rotated_open_id or f"{args.open_id}_rotated",
                session_id=args.desktop_session_id,
            )
        )
    else:
        results.append(
            CheckResult(
                "bind:correct-token",
                False,
                "skipped: pass --mutating-bind after env is configured",
            )
        )
    if args.local_store_session:
        results.append(
            check_local_store_desktop_session(
                args.dc_agent_base_url,
                args.open_id,
                args.desktop_session_id,
                employee_id=args.employee_id,
            )
        )
    if args.workspace_send_message:
        conversation_id = args.workspace_conversation_id or args.open_id
        results.append(
            check_workspace_message(
                args.dc_agent_base_url,
                conversation_id=conversation_id,
                session_id=args.desktop_session_id,
                text=args.workspace_message_text,
                expect_feishu_send=args.expect_feishu_send,
                allow_real_feishu_send=args.allow_real_feishu_send,
            )
        )
    if args.skip_webui:
        results.append(CheckResult("webui:desktop-entry", True, "skipped"))
    else:
        results.append(check_webui_desktop_entry(args.webui_base_url))

    print_results(results)
    return 0 if all(result.ok for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
