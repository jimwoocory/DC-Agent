"""Engineering-grade tests for the event → envelope boundary.

`data/plugins/dc_router/routing/event_envelope.py::build_envelope` is the
*only* place that converts an AstrBot event into a dc_router_core
``MessageEnvelope``. It is pure (no ``set_provider`` / ``set_extra`` calls)
and the contract here is the boundary between AstrBot and the pure routing
core.

Tests are organised by the property under test:

- :class:`TestBuildEnvelopeFromText`  — text-only happy path
- :class:`TestBuildEnvelopeAttachments` — attachment kind mapping
- :class:`TestBuildEnvelopePlatformMetadata` — platform_id + router_mode wiring
- :class:`TestBuildEnvelopeFeishuChannelMetadata` — feishu_channel_* extras
- :class:`TestBuildEnvelopeResilience` — malformed event shapes
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dc_router_core.taxonomy import AttachmentKind

# ─────────────────────────────────────────────────────────────────────────
# Module loading — build_envelope imports astrbot at the top of the file,
# so we must import the plugin module as a whole. We also stub a minimal
# astrbot surface so the module imports cleanly in CI / non-AstrBot envs.
# ─────────────────────────────────────────────────────────────────────────


def _ensure_components_module():
    """Return ``astrbot.api.message_components``, building a fake module
    tree only when AstrBot is absent (CI / non-AstrBot envs)."""
    try:
        return importlib.import_module("astrbot.api.message_components")
    except Exception:  # noqa: BLE001
        import types

        fake_pkg = types.ModuleType("astrbot")
        api_pkg = types.ModuleType("astrbot.api")
        components_module = types.ModuleType("astrbot.api.message_components")
        fake_pkg.api = api_pkg  # type: ignore[attr-defined]
        api_pkg.message_components = components_module  # type: ignore[attr-defined]
        sys.modules.setdefault("astrbot", fake_pkg)
        sys.modules.setdefault("astrbot.api", api_pkg)
        sys.modules.setdefault(
            "astrbot.api.message_components", components_module
        )
        return sys.modules["astrbot.api.message_components"]


class _StubComponent:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self._init_args = args
        self._init_kwargs = kwargs


_COMPONENT_NAMES = ("Plain", "Image", "Record", "Video", "File", "At", "AtAll")


@pytest.fixture(autouse=True)
def _stub_message_components(monkeypatch: pytest.MonkeyPatch):
    """Patch component classes to parameterless stubs, per-test reversible.

    The build_envelope module does ``from astrbot.api.message_components
    import File, Image, Record, Video`` inside the function body. The
    real AstrBot classes have required positional arguments (e.g. Image
    requires a ``file`` arg) which makes them awkward to instantiate
    from tests. We patch the public symbols to point at parameterless
    subclasses so the isinstance checks inside build_envelope still pass.

    必须用 monkeypatch 保证可逆——直接 setattr 真实 astrbot 模块会把 stub
    泄漏给同进程内随后运行的其他测试套件（如 tests/harness）。
    """
    components_module = _ensure_components_module()
    for name in _COMPONENT_NAMES:
        monkeypatch.setattr(
            components_module,
            name,
            type(name, (_StubComponent,), {}),
            raising=False,
        )


_ensure_components_module()


# Import after stubbing so the module-level imports succeed. We import
# the routing.event_envelope module via importlib.util so we do not
# trigger ``dc_router.__init__`` (which pulls in plugin.py / health.py and
# depends on AstrBot runtime).
_EVENT_ENVELOPE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "plugins"
    / "dc_router"
    / "routing"
    / "event_envelope.py"
)
_DC_AGENT_ROOT = Path(__file__).resolve().parents[2]
if str(_DC_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_DC_AGENT_ROOT))
_PLUGINS_PARENT = _DC_AGENT_ROOT / "data" / "plugins"
if str(_PLUGINS_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_PARENT))

# Pre-register the parent packages so ``from ..config import ...`` resolves.
import types  # noqa: E402

_dc_router_pkg = types.ModuleType("dc_router")
_dc_router_pkg.__path__ = [str(_PLUGINS_PARENT / "dc_router")]  # type: ignore[attr-defined]
sys.modules.setdefault("dc_router", _dc_router_pkg)
_routing_pkg = types.ModuleType("dc_router.routing")
_routing_pkg.__path__ = [str(_PLUGINS_PARENT / "dc_router" / "routing")]  # type: ignore[attr-defined]
sys.modules.setdefault("dc_router.routing", _routing_pkg)

_spec = importlib.util.spec_from_file_location(
    "dc_router.routing.event_envelope", _EVENT_ENVELOPE_PATH
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
build_envelope = _module.build_envelope


# ─────────────────────────────────────────────────────────────────────────
# Helpers — minimal event mocks that match the build_envelope contract
# ─────────────────────────────────────────────────────────────────────────


def _make_event(
    *,
    text: str = "",
    platform_id: str | None = None,
    umo: str = "ai:chat:user-1",
    sender_id: str = "user-1",
    components: list | None = None,
    extras: dict | None = None,
) -> MagicMock:
    """Build a MagicMock that quacks like an AstrMessageEvent."""
    event = MagicMock(name="event")
    event.message_str = text
    event.unified_msg_origin = umo
    message_obj = MagicMock(name="message_obj")
    message_obj.message = components or []
    event.message_obj = message_obj
    event.get_platform_id = MagicMock(return_value=platform_id or "")
    event.get_sender_id = MagicMock(return_value=sender_id)

    # Mirror set_extra / get_extra
    extras_state: dict = dict(extras or {})

    def _set_extra(key: str, value: object) -> None:
        extras_state[key] = value

    def _get_extra(key: str, default: object = None) -> object:
        return extras_state.get(key, default)

    event.set_extra = MagicMock(side_effect=_set_extra)
    event.get_extra = MagicMock(side_effect=_get_extra)
    event._extras = extras_state
    return event


# ─────────────────────────────────────────────────────────────────────────
# 1. Text-only happy path
# ─────────────────────────────────────────────────────────────────────────


class TestBuildEnvelopeFromText:
    """Locks down the text-only envelope construction."""

    def test_text_only_envelope_has_no_attachments(self) -> None:
        event = _make_event(text="帮我写客户邀约文案")
        envelope = build_envelope(event)
        assert envelope.text == "帮我写客户邀约文案"
        assert envelope.attachment_kinds == ()
        assert envelope.has_attachments is False

    def test_strips_whitespace_from_text(self) -> None:
        event = _make_event(text="   你好  ")
        envelope = build_envelope(event)
        assert envelope.text == "你好"

    def test_empty_text_yields_empty_string(self) -> None:
        event = _make_event(text="")
        envelope = build_envelope(event)
        assert envelope.text == ""

    def test_text_only_envelope_keeps_umo_as_session_id(self) -> None:
        event = _make_event(text="hi", umo="ai:lark:oc_group_1")
        envelope = build_envelope(event)
        assert envelope.session_id == "ai:lark:oc_group_1"

    def test_text_only_envelope_keeps_sender_id(self) -> None:
        event = _make_event(text="hi", sender_id="ou_user_42")
        envelope = build_envelope(event)
        assert envelope.user_id == "ou_user_42"

    def test_sender_id_blank_string_becomes_none(self) -> None:
        event = _make_event(text="hi", sender_id="")
        envelope = build_envelope(event)
        assert envelope.user_id is None


# ─────────────────────────────────────────────────────────────────────────
# 2. Attachment kind mapping
# ─────────────────────────────────────────────────────────────────────────


class TestBuildEnvelopeAttachments:
    """Locks down the attachment kind mapping (Image/Record/Video/File)."""

    def test_image_attachment_marked_as_image_kind(self) -> None:
        # build_envelope uses ``isinstance`` against the real components; the
        # stub class is type(name, (_StubComponent,), {}), which is unique
        # per name, so the isinstance check inside build_envelope passes.
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="看这张图",
            components=[components_module.Image()],
        )
        envelope = build_envelope(event)
        assert envelope.has_attachments is True
        assert AttachmentKind.IMAGE in envelope.attachment_kinds

    def test_record_attachment_marked_as_voice_kind(self) -> None:
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="听一下",
            components=[components_module.Record()],
        )
        envelope = build_envelope(event)
        assert AttachmentKind.VOICE in envelope.attachment_kinds

    def test_video_attachment_marked_as_video_kind(self) -> None:
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="看视频",
            components=[components_module.Video()],
        )
        envelope = build_envelope(event)
        assert AttachmentKind.VIDEO in envelope.attachment_kinds

    def test_file_attachment_marked_as_file_kind(self) -> None:
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="看附件",
            components=[components_module.File()],
        )
        envelope = build_envelope(event)
        assert AttachmentKind.FILE in envelope.attachment_kinds

    def test_unknown_component_types_are_ignored(self) -> None:
        """Components that are not Image/Record/Video/File are silently dropped."""
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="ping",
            components=[components_module.Plain("hello")],
        )
        envelope = build_envelope(event)
        assert envelope.attachment_kinds == ()
        assert envelope.has_attachments is False

    def test_multiple_attachments_all_captured(self) -> None:
        components_module = sys.modules["astrbot.api.message_components"]
        event = _make_event(
            text="复合附件",
            components=[
                components_module.Image(),
                components_module.File(),
                components_module.Plain("context"),
            ],
        )
        envelope = build_envelope(event)
        assert AttachmentKind.IMAGE in envelope.attachment_kinds
        assert AttachmentKind.FILE in envelope.attachment_kinds
        assert len(envelope.attachment_kinds) == 2

    def test_non_list_message_components_returns_empty(self) -> None:
        event = _make_event(text="hi")
        event.message_obj.message = "not a list"  # type: ignore[assignment]
        envelope = build_envelope(event)
        assert envelope.attachment_kinds == ()


# ─────────────────────────────────────────────────────────────────────────
# 3. Platform metadata + router_mode
# ─────────────────────────────────────────────────────────────────────────


class TestBuildEnvelopePlatformMetadata:
    """Locks down the platform_id and router_mode wiring."""

    def test_business_platform_sets_business_router_mode(self) -> None:
        event = _make_event(
            text="hi", platform_id="巅池-Agent小助手", umo="ai:lark:oc_1"
        )
        envelope = build_envelope(event)
        assert envelope.metadata["platform_id"] == "巅池-Agent小助手"
        assert envelope.metadata["router_mode"] == "business"
        assert envelope.metadata["umo"] == "ai:lark:oc_1"

    def test_ops_platform_sets_ops_router_mode(self) -> None:
        event = _make_event(
            text="Hermes 状态如何",
            platform_id="巅池-技术（DevOps）",
            umo="ai:lark:oc_2",
        )
        envelope = build_envelope(event)
        assert envelope.metadata["platform_id"] == "巅池-技术（DevOps）"
        assert envelope.metadata["router_mode"] == "ops"

    def test_alt_ops_platform_also_sets_ops_router_mode(self) -> None:
        event = _make_event(text="队列状态", platform_id="巅池-技术")
        envelope = build_envelope(event)
        assert envelope.metadata["router_mode"] == "ops"

    def test_unknown_platform_does_not_set_router_mode(self) -> None:
        event = _make_event(text="hi", platform_id="巅池-推广 01")
        envelope = build_envelope(event)
        assert "router_mode" not in envelope.metadata

    def test_platform_getter_exception_does_not_crash(self) -> None:
        event = _make_event(text="hi")
        event.get_platform_id = MagicMock(side_effect=RuntimeError("nope"))
        envelope = build_envelope(event)
        assert envelope.metadata["platform_id"] == ""


# ─────────────────────────────────────────────────────────────────────────
# 4. feishu_channel_* metadata passthrough
# ─────────────────────────────────────────────────────────────────────────


class TestBuildEnvelopeFeishuChannelMetadata:
    """Locks down the feishu_channel_* extras → envelope.metadata passthrough."""

    def test_feishu_channel_agent_id_propagates(self) -> None:
        event = _make_event(
            text="帮我写活动文案",
            platform_id="巅池-Agent小助手",
            extras={"feishu_channel_agent_id": "planning-agent"},
        )
        envelope = build_envelope(event)
        assert envelope.metadata["feishu_channel_agent_id"] == "planning-agent"

    def test_reasoning_tier_propagates(self) -> None:
        event = _make_event(
            text="#深度 分析",
            platform_id="巅池-Agent小助手",
            extras={"reasoning_tier": "xhigh"},
        )
        envelope = build_envelope(event)
        assert envelope.metadata["reasoning_tier"] == "xhigh"

    def test_extra_missing_means_key_absent_from_metadata(self) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        envelope = build_envelope(event)
        assert "feishu_channel_agent_id" not in envelope.metadata
        assert "reasoning_tier" not in envelope.metadata

    def test_extra_with_empty_string_means_key_absent(self) -> None:
        event = _make_event(
            text="hi",
            platform_id="巅池-Agent小助手",
            extras={"feishu_channel_agent_id": ""},
        )
        envelope = build_envelope(event)
        # empty string is treated as missing — preserves dc_router_core's
        # "absent key" semantics downstream.
        assert "feishu_channel_agent_id" not in envelope.metadata


# ─────────────────────────────────────────────────────────────────────────
# 5. Resilience — malformed event shapes
# ─────────────────────────────────────────────────────────────────────────


class TestBuildEnvelopeResilience:
    """Locks down the no-throw contract for malformed events."""

    def test_event_without_get_platform_id_returns_empty_platform(self) -> None:
        event = _make_event(text="hi")
        del event.get_platform_id  # remove the callable
        envelope = build_envelope(event)
        assert envelope.metadata["platform_id"] == ""

    def test_event_without_get_sender_id_returns_none_user(self) -> None:
        event = _make_event(text="hi")
        del event.get_sender_id
        envelope = build_envelope(event)
        assert envelope.user_id is None

    def test_event_without_get_extra_still_builds(self) -> None:
        event = _make_event(text="hi", platform_id="巅池-Agent小助手")
        del event.get_extra
        envelope = build_envelope(event)
        assert envelope.metadata["platform_id"] == "巅池-Agent小助手"

    def test_event_with_no_umo_uses_empty_string(self) -> None:
        event = _make_event(text="hi")
        event.unified_msg_origin = ""
        envelope = build_envelope(event)
        assert envelope.session_id is None  # empty umo → None per build_envelope
        assert envelope.metadata["umo"] == ""

    def test_event_with_none_text_yields_empty(self) -> None:
        event = _make_event(text="hi")
        event.message_str = None
        envelope = build_envelope(event)
        assert envelope.text == ""
