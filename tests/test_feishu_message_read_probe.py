from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts-tools" / "feishu_message_read_probe.py"
SPEC = importlib.util.spec_from_file_location("feishu_message_read_probe", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
feishu_message_read_probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = feishu_message_read_probe
SPEC.loader.exec_module(feishu_message_read_probe)


def test_parse_text_content_from_feishu_json() -> None:
    assert (
        feishu_message_read_probe._parse_text_content('{"text": "hello"}')
        == "hello"
    )


def test_message_summary_redacts_content_by_default() -> None:
    item = SimpleNamespace(
        message_id="om_1",
        chat_id="oc_1",
        msg_type="text",
        create_time=123,
        sender=SimpleNamespace(id="ou_1", sender_type="user"),
        body=SimpleNamespace(content='{"text": "secret message"}'),
    )

    summary = feishu_message_read_probe._message_to_summary(
        item,
        show_text=False,
    )

    assert summary["content"] == "<redacted chars=14>"
    assert summary["message_id"] == "om_1"
    assert summary["chat_id"] == "oc_1"


def test_send_probe_text_requires_explicit_real_send_allowance(capsys) -> None:
    args = SimpleNamespace(
        send_probe_text=True,
        allow_real_feishu_send=False,
        container_id="",
        container_id_type="chat",
        receive_id="ou_1",
        receive_id_type="open_id",
        probe_text="hello",
        lookback_minutes=30,
        page_size=20,
        output_limit=5,
        show_text=False,
    )

    async def run() -> int:
        return await feishu_message_read_probe._amain(args)

    import asyncio

    code = asyncio.run(run())
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["stage"] == "send_probe"
    assert "--allow-real-feishu-send" in payload["error"]
