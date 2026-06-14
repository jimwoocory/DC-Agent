from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_onboarding_guide_module():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "plugins"
        / "onboarding_guide"
        / "onboarding_guide.py"
    )
    spec = importlib.util.spec_from_file_location(
        "dc_onboarding_guide_plugin_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStar:
    def __init__(self, activated: bool) -> None:
        self.activated = activated


class _FakeContext:
    def __init__(self, employee_onboarding_active: bool) -> None:
        self.employee_onboarding_active = employee_onboarding_active

    def get_registered_star(self, name: str):
        if name == "employee_onboarding":
            return _FakeStar(self.employee_onboarding_active)
        return None


class _FakeEvent:
    unified_msg_origin = "lark:FriendMessage:ou_user"
    message_str = "你好"

    def __init__(self) -> None:
        self.result = None
        self.llm_calls: list[bool] = []

    def set_result(self, result) -> None:
        self.result = result

    def should_call_llm(self, value: bool) -> None:
        self.llm_calls.append(value)


async def test_onboarding_guide_yields_to_employee_onboarding() -> None:
    module = _load_onboarding_guide_module()
    plugin = module.OnboardingGuidePlugin(_FakeContext(employee_onboarding_active=True))
    event = _FakeEvent()

    await plugin.on_message(event)

    assert event.result is None
    assert event.llm_calls == []
