from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from astrbot.dashboard.api.static_files import router as static_files_router
from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter


@pytest.mark.asyncio
async def test_dynamic_get_route_precedes_static_catch_all() -> None:
    app = FastAPI()
    app.include_router(static_files_router)
    adapter = FastAPIAppAdapter(app)

    async def pet_me() -> dict[str, bool]:
        return {"ok": True}

    adapter.add_url_rule("/api/pet/me", pet_me, methods=["GET"])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/pet/me")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
