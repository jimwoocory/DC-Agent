import json
from pathlib import Path

CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "harness"
    / "contracts"
    / "lark_multimodal_message_buffer.json"
)


def test_lark_multimodal_message_buffer_contract_is_valid() -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert payload["contract_id"] == "lark_multimodal_message_buffer"
    assert payload["acceptance_criteria"]
    assert (
        "uv run pytest tests/unit/test_lark_polling_fallback.py -q"
        in payload["verification"]
    )
