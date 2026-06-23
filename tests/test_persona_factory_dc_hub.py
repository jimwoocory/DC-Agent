from __future__ import annotations

from types import SimpleNamespace

import pytest
from dc_engines.persona_factory import (
    build_hermes_task_payload,
    build_research_plan,
    create_persona_request,
)


@pytest.mark.asyncio
async def test_dc_hub_persona_factory_dispatches_extra_payload_to_hermes() -> None:
    from data.plugins.dc_hub.main import DCHubPlugin

    calls: list[dict] = []

    async def dispatch(
        task_id,
        workflow_kind,
        brief,
        umo,
        cognitive_context,
        *,
        extra_payload=None,
    ):
        calls.append(
            {
                "task_id": task_id,
                "workflow_kind": workflow_kind,
                "brief": brief,
                "umo": umo,
                "cognitive_context": cognitive_context,
                "extra_payload": extra_payload,
            }
        )
        return True

    plugin = object.__new__(DCHubPlugin)
    plugin.context = SimpleNamespace(dispatch_task_to_hermes=dispatch)
    request = create_persona_request(target="Grace Hopper", requester_id="ou_admin")
    plan = build_research_plan(request)
    payload = build_hermes_task_payload(request, plan)
    event = SimpleNamespace(unified_msg_origin="lark:tenant:user")

    ok = await plugin._dispatch_persona_factory_to_hermes(event, request, payload)

    assert ok is True
    assert calls == [
        {
            "task_id": request.request_id,
            "workflow_kind": "persona_factory",
            "brief": "Grace Hopper",
            "umo": "lark:tenant:user",
            "cognitive_context": {
                "persona_factory_request_id": request.request_id,
            },
            "extra_payload": payload,
        }
    ]
