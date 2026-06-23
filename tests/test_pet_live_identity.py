from pathlib import Path

from dc_engines.pet_live.identity import get_or_create_identity
from dc_engines.pet_live.store import PetLiveStore


def test_get_or_create_identity_reuses_same_pet_for_feishu_user(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    first = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
        desktop_session_id="desktop_a",
    )
    second = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="desktop_a",
    )

    assert second.pet_id == first.pet_id
    assert second.feishu_open_id == "ou_user"
    assert second.employee_id == "emp_001"
    assert second.desktop_session_id == "desktop_a"


def test_get_or_create_identity_updates_missing_desktop_session(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    original = get_or_create_identity(store, feishu_open_id="ou_user")
    updated = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        desktop_session_id="desktop_later",
    )

    assert updated.pet_id == original.pet_id
    assert updated.desktop_session_id == "desktop_later"


def test_get_or_create_identity_prefers_employee_id_across_oauth_changes(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    first = get_or_create_identity(
        store,
        feishu_open_id="ou_old",
        employee_id="emp_001",
        desktop_session_id="desktop_a",
    )
    second = get_or_create_identity(
        store,
        feishu_open_id="ou_new",
        employee_id="emp_001",
        desktop_session_id="desktop_b",
    )

    assert second.pet_id == first.pet_id
    assert second.employee_id == "emp_001"
    assert second.feishu_open_id == "ou_new"
    assert second.desktop_session_id == "desktop_b"


def test_get_or_create_identity_backfills_employee_id_for_feishu_fallback(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    fallback = get_or_create_identity(store, feishu_open_id="ou_user")
    promoted = get_or_create_identity(
        store,
        feishu_open_id="ou_user",
        employee_id="emp_001",
    )

    assert promoted.pet_id == fallback.pet_id
    assert promoted.employee_id == "emp_001"


def test_get_or_create_identity_merges_feishu_only_duplicate_into_employee_pet(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    employee_pet = get_or_create_identity(
        store,
        feishu_open_id="ou_primary",
        employee_id="emp_001",
    )
    duplicate = get_or_create_identity(store, feishu_open_id="ou_later")
    merged = get_or_create_identity(
        store,
        feishu_open_id="ou_later",
        employee_id="emp_001",
    )

    assert merged.pet_id == employee_pet.pet_id
    assert store.get_identity_by_pet_id(duplicate.pet_id) is None
    assert store.get_identity_by_feishu_open_id("ou_later").pet_id == employee_pet.pet_id


def test_get_or_create_identity_supports_employee_only_creation(
    tmp_path: Path,
) -> None:
    store = PetLiveStore(tmp_path / "pet_live.db")

    first = get_or_create_identity(store, employee_id="emp_001")
    second = get_or_create_identity(store, employee_id="emp_002")

    assert first.pet_id != second.pet_id
    assert first.feishu_open_id == ""
    assert second.feishu_open_id == ""
