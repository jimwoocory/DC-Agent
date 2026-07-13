import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from dc_engines.pet_live.event_bus import publish_pet_event
from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore
from quart import Quart

from astrbot.dashboard.routes.pet_live import PetLiveRoute
from astrbot.dashboard.routes.route import RouteContext


def _signed_session(
    user: dict[str, str], *, secret: str, expires_in: int = 3600
) -> str:
    payload = {
        "user": user,
        "iat": int(time.time()),
        "exp": int(time.time()) + expires_in,
    }
    body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    sig = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    encoded_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{body}.{encoded_sig}"


def _desktop_assertion(
    *,
    secret: str,
    employee_id: str = "emp_asserted",
    feishu_open_id: str = "ou_asserted",
    desktop_session_id: str = "desktop_asserted",
    expires_in: int = 60,
) -> str:
    now = int(time.time())
    payload = {
        "employee_id": employee_id,
        "feishu_open_id": feishu_open_id,
        "desktop_session_id": desktop_session_id,
        "nonce": "nonce_asserted",
        "iat": now,
        "exp": now + expires_in,
    }
    body = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{body}.{encoded_signature}"


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_accepts_signed_desktop_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DESKTOP_BINDING_SECRET", "desktop-secret")
    monkeypatch.delenv("PET_LIVE_BINDING_TOKEN", raising=False)
    assertion = _desktop_assertion(secret="desktop-secret")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=desktop_asserted",
                    "X-Dianchi-Desktop-Assertion": assertion,
                },
                json={"employee_id": "emp_spoofed", "feishu_open_id": "ou_spoofed"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["identity"]["employee_id"] == "emp_asserted"
    assert payload["data"]["identity"]["feishu_open_id"] == "ou_asserted"
    assert payload["data"]["identity"]["desktop_session_id"] == "desktop_asserted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "tampered"])
async def test_pet_live_bind_desktop_rejects_invalid_desktop_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("DESKTOP_BINDING_SECRET", "desktop-secret")
    monkeypatch.delenv("PET_LIVE_BINDING_TOKEN", raising=False)
    assertion = _desktop_assertion(
        secret="desktop-secret",
        expires_in=-1 if mode == "expired" else 60,
    )
    if mode == "tampered":
        body, signature = assertion.split(".", 1)
        decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        decoded["employee_id"] = "emp_tampered"
        body = (
            base64.urlsafe_b64encode(
                json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode(
                    "utf-8"
                )
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        assertion = f"{body}.{signature}"
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=desktop_asserted",
                    "X-Dianchi-Desktop-Assertion": assertion,
                },
                json={},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert "assertion" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_assertion_cannot_rebind_desktop_session_to_another_employee(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DESKTOP_BINDING_SECRET", "desktop-secret")
    monkeypatch.delenv("PET_LIVE_BINDING_TOKEN", raising=False)
    first_assertion = _desktop_assertion(secret="desktop-secret")
    second_assertion = _desktop_assertion(
        secret="desktop-secret",
        employee_id="emp_other",
        feishu_open_id="ou_other",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        first = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=desktop_asserted",
                    "X-Dianchi-Desktop-Assertion": first_assertion,
                },
                json={},
            )
        ).get_json()
        second = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=desktop_asserted",
                    "X-Dianchi-Desktop-Assertion": second_assertion,
                },
                json={},
            )
        ).get_json()

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "already bound" in second["message"]


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_connects_session_to_feishu_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_BINDING_TOKEN", "bind-secret")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        bind_payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=sess_1",
                    "X-Pet-Live-Bind-Token": "bind-secret",
                },
                json={"feishu_open_id": "ou_user", "employee_id": "emp_1"},
            )
        ).get_json()
        me_payload = await (
            await client.get(
                "/api/pet/me", headers={"Cookie": "dc_feishu_session=sess_1"}
            )
        ).get_json()

    assert bind_payload["status"] == "ok"
    assert bind_payload["data"]["identity"]["feishu_open_id"] == "ou_user"
    assert bind_payload["data"]["identity"]["employee_id"] == "emp_1"
    assert bind_payload["data"]["identity"]["desktop_session_id"] == "sess_1"
    assert me_payload["status"] == "ok"
    assert (
        me_payload["data"]["identity"]["pet_id"]
        == bind_payload["data"]["identity"]["pet_id"]
    )
    assert me_payload["data"]["light_feedback"]["mood"] == "calm"


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_can_resolve_signed_session_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PET_LIVE_BINDING_TOKEN", raising=False)
    monkeypatch.setenv("SITE_SESSION_SECRET", "site-secret")
    session = _signed_session(
        {
            "open_id": "ou_signed",
            "user_id": "emp_signed",
            "union_id": "on_signed",
            "email": "signed@example.test",
        },
        secret="site-secret",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        bind_payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={"Cookie": f"dc_feishu_session={session}"},
                json={},
            )
        ).get_json()
        me_payload = await (
            await client.get(
                "/api/pet/me",
                headers={"Cookie": f"dc_feishu_session={session}"},
            )
        ).get_json()

    assert bind_payload["status"] == "ok"
    assert bind_payload["data"]["identity"]["employee_id"] == "emp_signed"
    assert bind_payload["data"]["identity"]["feishu_open_id"] == "ou_signed"
    assert me_payload["status"] == "ok"
    assert (
        me_payload["data"]["identity"]["pet_id"]
        == bind_payload["data"]["identity"]["pet_id"]
    )


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_rejects_expired_signed_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PET_LIVE_BINDING_TOKEN", raising=False)
    monkeypatch.setenv("SITE_SESSION_SECRET", "site-secret")
    session = _signed_session(
        {"open_id": "ou_expired", "user_id": "emp_expired"},
        secret="site-secret",
        expires_in=-60,
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={"Cookie": f"dc_feishu_session={session}"},
                json={},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert "expired or invalid" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_reuses_employee_pet_for_changed_oauth_and_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_BINDING_TOKEN", "bind-secret")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        first = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=sess_1",
                    "X-Pet-Live-Bind-Token": "bind-secret",
                },
                json={"feishu_open_id": "ou_old", "employee_id": "emp_1"},
            )
        ).get_json()
        second = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=sess_2",
                    "X-Pet-Live-Bind-Token": "bind-secret",
                },
                json={"feishu_open_id": "ou_new", "employee_id": "emp_1"},
            )
        ).get_json()

    assert second["status"] == "ok"
    assert second["data"]["identity"]["pet_id"] == first["data"]["identity"]["pet_id"]
    assert second["data"]["identity"]["feishu_open_id"] == "ou_new"
    assert second["data"]["identity"]["desktop_session_id"] == "sess_2"


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_requires_binding_token(tmp_path: Path) -> None:
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={"Cookie": "dc_feishu_session=sess_1"},
                json={"feishu_open_id": "ou_user"},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert "authorized" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_bind_desktop_rejects_session_takeover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_BINDING_TOKEN", "bind-secret")
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    get_or_create_identity(
        store,
        feishu_open_id="ou_first",
        desktop_session_id="sess_1",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.post(
                "/api/pet/bind-desktop",
                headers={
                    "Cookie": "dc_feishu_session=sess_1",
                    "Authorization": "Bearer bind-secret",
                },
                json={"feishu_open_id": "ou_second"},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert "already bound" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_route_resolves_pet_from_desktop_session_cookie(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="sess_1",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="router_decision_made",
        source_ref={"platform": "lark", "router_trace_id": "trace_1"},
        payload={"intent": "hermes_escalation"},
    )

    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        headers = {"Cookie": "dc_feishu_session=sess_1"}
        me = await (await client.get("/api/pet/me", headers=headers)).get_json()
        events = await (
            await client.get("/api/pet/events?after_id=0", headers=headers)
        ).get_json()

    assert me["status"] == "ok"
    assert me["data"]["pet"]["pet_id"] == identity.pet_id
    assert me["data"]["pet"]["state"] == "thinking"
    assert me["data"]["codex_pets"]["pet_id"] == identity.pet_id
    assert me["data"]["codex_pets"]["live_state"] == "thinking"
    assert me["data"]["codex_pets"]["codex_state"] == "review"
    assert events["status"] == "ok"
    assert events["data"]["events"][0]["source_ref"]["router_trace_id"] == "trace_1"


@pytest.mark.asyncio
async def test_pet_live_me_returns_recent_light_feedback_signal(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="sess_1",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="emp_001",
        source="assistant",
        event_type="work_habit_updated",
        source_ref={"employee_id": "emp_001"},
        payload={
            "focus_level": 88,
            "work_rhythm": "deep_work",
            "summary": "quiet focus",
        },
    )

    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/pet/me",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["identity"]["employee_id"] == "emp_001"
    assert payload["data"]["light_feedback"]["focus_level"] == 88
    assert payload["data"]["light_feedback"]["last_signal"]["summary"] == "quiet focus"


@pytest.mark.asyncio
async def test_pet_live_route_filters_events_by_time_window(tmp_path: Path) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="sess_1",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="feishu_message_received",
        source_ref={"platform": "lark", "message_id": "old"},
        created_at="2026-06-15T15:59:59+00:00",
    )
    included = publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="router_decision_made",
        source_ref={"platform": "lark", "router_trace_id": "trace_today"},
        created_at="2026-06-15T16:00:00+00:00",
    )
    publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="memory_context_injected",
        source_ref={"platform": "lark", "message_id": "tomorrow"},
        created_at="2026-06-16T16:00:00+00:00",
    )

    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/pet/events"
                "?after_id=0"
                "&from=2026-06-15T16:00:00%2B00:00"
                "&to=2026-06-16T16:00:00%2B00:00",
                headers={"Cookie": "dc_feishu_session=sess_1"},
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert [event["id"] for event in payload["data"]["events"]] == [included.id]
    assert payload["data"]["last_event_id"] == included.id
    assert payload["data"]["window"] == {
        "from": "2026-06-15T16:00:00+00:00",
        "to": "2026-06-16T16:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_pet_live_route_returns_unbound_dashboard_without_session(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
    )
    event = publish_pet_event(
        store,
        pet_id=identity.pet_id,
        user_id="ou_user",
        source="router",
        event_type="router_decision_made",
        source_ref={"platform": "lark", "router_trace_id": "trace_global"},
        created_at="2026-06-16T01:00:00+00:00",
    )

    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (await client.get("/api/pet/me")).get_json()
        events = await (
            await client.get(
                "/api/pet/events"
                "?after_id=0"
                "&from=2026-06-16T00:00:00%2B00:00"
                "&to=2026-06-17T00:00:00%2B00:00"
            )
        ).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["identity"] is None
    assert payload["data"]["pet"]["pet_id"] == identity.pet_id
    assert events["status"] == "ok"
    assert [item["id"] for item in events["data"]["events"]] == [event.id]
    assert events["data"]["last_event_id"] == event.id


@pytest.mark.asyncio
async def test_pet_live_route_rejects_unbound_desktop_session(
    tmp_path: Path,
) -> None:
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (
            await client.get(
                "/api/pet/me",
                headers={"Cookie": "dc_feishu_session=unbound_session"},
            )
        ).get_json()

    assert payload["status"] == "error"
    assert payload["message"] == "desktop session is not bound to a pet"


@pytest.mark.asyncio
async def test_pet_live_route_heartbeat_records_desktop_session_ref(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="sess_1",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.post(
            "/api/pet/heartbeat",
            headers={"Cookie": "dc_feishu_session=sess_1"},
            json={"last_event_id": 12, "app_version": "0.1.0"},
        )
        payload = await response.get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)

    assert payload["status"] == "ok"
    assert events[0].event_type == "desktop_heartbeat"
    assert events[0].source_ref.desktop_session_id == "sess_1"
    assert events[0].payload["last_event_id"] == 12


@pytest.mark.asyncio
async def test_pet_live_route_feed_action_updates_pet_state(tmp_path: Path) -> None:
    store = PetLiveStore(tmp_path / "data" / "feishu_pet.db")
    identity = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="sess_1",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.post(
            "/api/pet/actions",
            headers={"Cookie": "dc_feishu_session=sess_1"},
            json={"action": "feed", "payload": {"item_id": "coffee"}},
        )
        payload = await response.get_json()

    events = store.list_events_after(identity.pet_id, after_id=0)
    state = store.get_pet_state(identity.pet_id)

    assert payload["status"] == "ok"
    assert events[0].event_type == "pet_fed"
    assert state is not None
    assert state.state == "happy"
    assert state.energy == 70


@pytest.mark.asyncio
async def test_pet_live_route_can_be_disabled_by_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_API_ENABLED", "false")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (await client.get("/api/pet/me")).get_json()

    assert payload["status"] == "error"
    assert "disabled" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_route_respects_global_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PET_LIVE_ENABLED", "false")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (await client.get("/api/pet/assets/orange-cat-v1")).get_json()

    assert payload["status"] == "error"
    assert "disabled" in payload["message"]


@pytest.mark.asyncio
async def test_pet_live_asset_route_returns_manifest(tmp_path: Path) -> None:
    asset_dir = tmp_path / "data" / "pet_assets" / "orange-cat-v1"
    asset_dir.mkdir(parents=True)
    (asset_dir / "pet.json").write_text(
        '{"id":"orange-cat-v1","name":"小橘","states":{"working":{"label":"工作中"}}}',
        encoding="utf-8",
    )
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        payload = await (await client.get("/api/pet/assets/orange-cat-v1")).get_json()

    assert payload["status"] == "ok"
    assert payload["data"]["manifest"]["id"] == "orange-cat-v1"
    assert payload["data"]["manifest"]["states"]["working"]["label"] == "工作中"


@pytest.mark.asyncio
async def test_pet_live_asset_file_route_returns_sprite_bytes(tmp_path: Path) -> None:
    asset_dir = tmp_path / "data" / "pet_assets" / "orange-cat-v1"
    asset_dir.mkdir(parents=True)
    (asset_dir / "spritesheet.webp").write_bytes(b"fake-webp")
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=tmp_path)  # type: ignore[arg-type]

    async with app.test_client() as client:
        response = await client.get("/api/pet/assets/orange-cat-v1/spritesheet.webp")

    assert response.status_code == 200
    assert await response.get_data() == b"fake-webp"


@pytest.mark.asyncio
async def test_pet_live_builtin_orange_cat_asset_is_served() -> None:
    dc_root = Path(__file__).resolve().parents[1]
    app = Quart(__name__)
    PetLiveRoute(RouteContext(config={}, app=app), dc_root=dc_root)  # type: ignore[arg-type]

    async with app.test_client() as client:
        manifest_payload = await (
            await client.get("/api/pet/assets/orange-cat-v1")
        ).get_json()
        sprite_response = await client.get(
            "/api/pet/assets/orange-cat-v1/spritesheet.webp"
        )

    assert manifest_payload["status"] == "ok"
    assert manifest_payload["data"]["manifest"]["id"] == "orange-cat-v1"
    assert manifest_payload["data"]["manifest"]["spritesheet"] == "spritesheet.webp"
    assert sprite_response.status_code == 200
    assert len(await sprite_response.get_data()) > 10_000
