from astrbot.core.astr_main_agent import (
    MainAgentBuildConfig,
    _get_lark_context_budget_override,
)


class _FakeEvent:
    def __init__(self, platform_name: str = "lark") -> None:
        self.platform_name = platform_name
        self.extras: dict[str, object] = {}

    def get_platform_name(self) -> str:
        return self.platform_name

    def get_extra(self, key: str) -> object:
        return self.extras.get(key)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value


def _config(provider_settings: dict | None = None) -> MainAgentBuildConfig:
    return MainAgentBuildConfig(
        tool_call_timeout=30,
        provider_settings=provider_settings or {},
    )


def test_lark_context_budget_defaults_to_casual_limit() -> None:
    event = _FakeEvent()

    limit = _get_lark_context_budget_override(event, _config())

    assert limit == 12_000
    assert event.extras["runtime_context_budget"] == {
        "layer": "short_term",
        "platform": "lark",
        "scenario": "casual",
        "max_context_tokens": 12_000,
        "action": "compress_or_trim_when_exceeded",
    }


def test_lark_context_budget_uses_business_and_complex_scenarios() -> None:
    business = _FakeEvent()
    business.extras["runtime_context_scenario"] = "work_preflight"
    complex_task = _FakeEvent()
    complex_task.extras["runtime_context_scenario"] = "deep_insight"

    assert _get_lark_context_budget_override(business, _config()) == 24_000
    assert _get_lark_context_budget_override(complex_task, _config()) == 32_000


def test_lark_context_budget_can_be_overridden_in_provider_settings() -> None:
    event = _FakeEvent()

    limit = _get_lark_context_budget_override(
        event,
        _config(
            {
                "runtime_context_budgets": {
                    "lark": {
                        "casual_tokens": 10_000,
                        "business_tokens": 20_000,
                        "complex_tokens": 30_000,
                    }
                }
            }
        ),
    )

    assert limit == 10_000


def test_lark_context_budget_clamps_configured_limits_to_system_maximums() -> None:
    event = _FakeEvent()

    limit = _get_lark_context_budget_override(
        event,
        _config(
            {
                "runtime_context_budgets": {
                    "lark": {
                        "enabled": False,
                        "casual_tokens": 128_000,
                    }
                }
            }
        ),
    )

    assert limit == 12_000
    assert event.extras["runtime_context_budget"]["max_context_tokens"] == 12_000


def test_context_budget_does_not_apply_to_non_lark_platforms() -> None:
    event = _FakeEvent(platform_name="webchat")

    assert _get_lark_context_budget_override(event, _config()) is None
