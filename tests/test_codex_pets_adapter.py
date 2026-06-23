from dc_engines.pet_live.codex_pets_adapter import (
    CODEX_PETS_VENDOR_COMMIT,
    codex_pets_payload,
    codex_pets_state_for,
)
from dc_engines.pet_live.contracts import PetState


def test_codex_pets_state_mapping_preserves_business_state_boundary() -> None:
    assert codex_pets_state_for("idle") == "idle"
    assert codex_pets_state_for("waiting") == "waiting"
    assert codex_pets_state_for("thinking") == "review"
    assert codex_pets_state_for("working") == "running"
    assert codex_pets_state_for("success") == "waving"
    assert codex_pets_state_for("failed") == "failed"
    assert codex_pets_state_for("review") == "review"
    assert codex_pets_state_for("happy") == "waving"
    assert codex_pets_state_for("focused") == "idle"
    assert codex_pets_state_for("sleeping") == "waiting"


def test_codex_pets_payload_is_json_safe_scene_state() -> None:
    pet = PetState(
        pet_id="pet_1",
        user_id="ou_user",
        name="散猫猫",
        asset_id="sanmaomao",
        state="working",
        emotion="focused",
        scene="desk",
        level=3,
        xp=120,
        energy=81,
        coins=9,
    )

    payload = codex_pets_payload(pet)

    assert payload["pet_id"] == "pet_1"
    assert payload["asset_id"] == "sanmaomao"
    assert payload["live_state"] == "working"
    assert payload["codex_state"] == "running"
    assert payload["display_name"] == "散猫猫"
    assert payload["level"] == 3
    assert payload["atlas"] == {
        "cell": {"width": 192, "height": 208},
        "columns": 8,
        "rows": 9,
    }
    assert payload["vendor_commit"] == CODEX_PETS_VENDOR_COMMIT
